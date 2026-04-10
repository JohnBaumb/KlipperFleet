import os
import re
import asyncio
import glob
import httpx
import logging
from typing import List, Dict, AsyncGenerator, Optional, Any, Set
from asyncio.subprocess import Process

logger = logging.getLogger('klipperfleet.flash')


class FlashManager:
    def __init__(self, klipper_dir: str, katapult_dir: str) -> None:
        self.klipper_dir: str = klipper_dir
        self.katapult_dir: str = katapult_dir

        # DFU operations can get flaky if multiple dfu-util processes run concurrently
        # (e.g. UI polling dfu-util -l while a flash is in progress).
        self._dfu_lock: asyncio.Lock = asyncio.Lock()
        self._dfu_cache: List[Dict[str, str]] = []
        self._dfu_cache_time: float = 0.0
        self._dfu_cache_ttl_s: float = 1.0

        # CAN operations (discovery, flashing) must be mutexed to prevent bus contention.
        # High-bandwidth flashing can fail if background discovery queries are running.
        self._can_lock: asyncio.Lock = asyncio.Lock()
        self._can_cache: Dict[str, List[Dict[str, str]]] = {}
        self._can_cache_time: Dict[str, float] = {}
        self._can_cache_ttl_s: float = (
            2.0  # Short TTL to keep status feeling "live"
        )

        # Beacon path cache: resolved once at startup, refreshable on demand
        self._beacon_klipper_path: Optional[str] = None

    async def discover_serial_devices(
        self, skip_moonraker: bool = False
    ) -> List[Dict[str, str]]:
        """Lists all serial devices in /dev/serial/by-id/ and common UART ports."""
        devices = []

        # 1. USB Serial devices (by-id is preferred for stability)
        usb_devs: List[str] = [
            d
            for d in glob.glob('/dev/serial/by-id/*')
            if 'Beacon_Beacon_Rev' not in d
        ]

        # 2. Common UART and CDC-ACM devices
        # Keep globs tight to avoid matching /dev/serial/ directories and by-id trees.
        candidates: List[str] = (
            glob.glob('/dev/ttyACM*')
            + glob.glob('/dev/ttyUSB*')
            + glob.glob('/dev/serial[0-9]*')
            + glob.glob('/dev/ttyAMA[0-9]*')
            + glob.glob('/dev/ttyS[0-9]*')
        )

        moonraker_mcus: Dict[str, Dict[str, str]] = {}
        if not skip_moonraker:
            moonraker_mcus = await self._get_moonraker_mcus()

        # Build a lookup keyed by both configured path and resolved real path
        configured_lookup: Dict[str, Dict[str, str]] = {}
        for configured_id, meta in moonraker_mcus.items():
            abs_id = os.path.abspath(configured_id)
            configured_lookup[abs_id] = meta
            if os.path.exists(abs_id):
                configured_lookup[os.path.realpath(abs_id)] = meta

        # Also include serial devices explicitly configured in Moonraker/Klipper.
        # This makes UART aliases like /dev/serial0 discoverable even on systems
        # where only the configured alias is known reliably.
        configured_serial_ids: List[str] = []
        for configured_id in moonraker_mcus.keys():
            if isinstance(configured_id, str) and configured_id.startswith(
                '/dev/'
            ):
                configured_serial_ids.append(os.path.abspath(configured_id))

        # Combine and deduplicate while preserving order
        all_candidates: List[str] = (
            usb_devs
            + [os.path.abspath(d) for d in candidates if os.path.exists(d)]
            + configured_serial_ids
        )
        all_devs: List[str] = []
        seen_ids: Set[str] = set()
        for dev in all_candidates:
            if dev in seen_ids:
                continue
            seen_ids.add(dev)
            all_devs.append(dev)

        seen_real_paths: Set[str] = set()

        for dev in all_devs:
            name: str = os.path.basename(dev)
            real_path: str = os.path.realpath(dev)
            configured_meta: Optional[Dict[str, str]] = configured_lookup.get(
                dev
            ) or configured_lookup.get(real_path)
            is_configured: bool = configured_meta is not None

            # Determine mode from the by-id name
            if dev.startswith('/dev/serial/by-id/'):
                mode = 'ready'
                dev_lower = dev.lower()
                if 'klipper' in dev_lower or 'kalico' in dev_lower:
                    mode = 'service'
                elif 'katapult' in dev_lower or 'canboot' in dev_lower:
                    mode = 'ready'
                elif is_configured:
                    mode = 'service'

                if is_configured:
                    name = f'{configured_meta["name"]} ({name})'
                devices.append(
                    {'id': dev, 'name': name, 'type': 'usb', 'mode': mode}
                )
                seen_real_paths.add(real_path)

            # Skip if already represented by a by-id symlink
            elif dev.startswith('/dev/ttyACM') or dev.startswith('/dev/ttyUSB'):
                already_added = real_path in seen_real_paths

                if not already_added:
                    # For generic tty devices, we rely on is_configured or name hints
                    mode = 'ready'
                    dev_lower = dev.lower()
                    if 'klipper' in dev_lower or 'kalico' in dev_lower:
                        mode = 'service'
                    elif 'katapult' in dev_lower or 'canboot' in dev_lower:
                        mode = 'ready'
                    elif is_configured:
                        mode = 'service'

                    if is_configured:
                        name = f'{configured_meta["name"]} ({name})'
                    devices.append(
                        {'id': dev, 'name': name, 'type': 'usb', 'mode': mode}
                    )
                    seen_real_paths.add(real_path)

            # Raw UART aliases/ports on SBCs (e.g. Raspberry Pi serial0/AMA/S ports)
            # are only shown when explicitly configured in Klipper/Moonraker.
            # This avoids duplicate/noise entries like ttyS0 when serial0 is the real MCU path.
            elif (
                dev.startswith('/dev/serial')
                or dev.startswith('/dev/ttyAMA')
                or dev.startswith('/dev/ttyS')
            ):
                if not is_configured:
                    continue
                if real_path in seen_real_paths:
                    continue
                mode = 'service'
                name = f'{configured_meta["name"]} ({name})'
                devices.append(
                    {'id': dev, 'name': name, 'type': 'uart', 'mode': mode}
                )
                seen_real_paths.add(real_path)

        # Final fallback: always include configured /dev serial endpoints from Moonraker,
        # even if they were not discovered by glob/exists checks above.
        known_ids = {d['id'] for d in devices}
        for configured_id, meta in moonraker_mcus.items():
            if not isinstance(
                configured_id, str
            ) or not configured_id.startswith('/dev/'):
                continue
            cfg_id = os.path.abspath(configured_id)
            if cfg_id in known_ids:
                continue
            cfg_real = (
                os.path.realpath(cfg_id) if os.path.exists(cfg_id) else cfg_id
            )
            if cfg_real in seen_real_paths:
                continue
            cfg_name = f'{meta["name"]} ({os.path.basename(cfg_id)})'
            devices.append(
                {
                    'id': cfg_id,
                    'name': cfg_name,
                    'type': 'uart',
                    'mode': 'service',
                }
            )
            seen_real_paths.add(cfg_real)

        # Remove any device whose underlying physical node belongs to a Beacon probe.
        # The by-id filter (line 52) excludes Beacon symlinks, but the raw /dev/ttyACM*
        # node can still slip through the dedup logic.
        beacon_real_paths: set = set()
        for bp in glob.glob('/dev/serial/by-id/*Beacon_Beacon_Rev*'):
            beacon_real_paths.add(os.path.realpath(bp))
        if beacon_real_paths:
            devices = [
                d
                for d in devices
                if os.path.realpath(d['id']) not in beacon_real_paths
            ]

        return devices

    async def discover_dfu_devices(self) -> List[Dict[str, str]]:
        """Lists all devices in DFU mode using dfu-util -l."""
        async with self._dfu_lock:
            now: float = asyncio.get_event_loop().time()
            if (now - self._dfu_cache_time) < self._dfu_cache_ttl_s:
                return list(self._dfu_cache)

            devices: List[Dict[str, str]] = []
            try:
                process: Process = await asyncio.create_subprocess_exec(
                    'sudo',
                    'dfu-util',
                    '-l',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await process.communicate()
                lines: List[str] = stdout.decode().splitlines()

                # Example line: Found DFU: [0483:df11] ver=0200, devnum=12, cfg=1, intf=0, path="1-1.2", alt=0, name="@Internal Flash  /0x08000000/064*0002Kg", serial="357236543131"
                for line in lines:
                    if 'Found DFU:' in line:
                        # Extract VID:PID
                        vid_pid: str = ''
                        if '[' in line and ']' in line:
                            vid_pid = line.split('[')[1].split(']')[0]

                        serial: str = ''
                        if 'serial="' in line:
                            serial = line.split('serial="')[1].split('"')[0]

                        path: str = ''
                        if 'path="' in line:
                            path = line.split('path="')[1].split('"')[0]

                        name: str = f'DFU Device ({vid_pid})'
                        if serial:
                            name += f' S/N: {serial}'

                        # Use serial or path as ID for disambiguation
                        dev_id: str = (
                            serial if (serial and serial != 'UNKNOWN') else path
                        )

                        # Deduplicate (dfu-util lists multiple alt settings per device)
                        if any(d['id'] == dev_id for d in devices):
                            continue

                        devices.append(
                            {
                                'id': dev_id,
                                'name': name,
                                'type': 'dfu',
                                'vid_pid': vid_pid,
                                'path': path,
                                'serial': serial,
                                'mode': 'ready',
                            }
                        )
            except Exception as e:
                logger.error('Error discovering DFU devices: %s', e)

            self._dfu_cache = list(devices)
            self._dfu_cache_time = now
            return list(devices)

    async def _get_moonraker_mcus(self) -> Dict[str, Dict[str, str]]:
        """Queries Moonraker for configured MCUs and their current status."""
        mcus = {}
        try:
            async with httpx.AsyncClient() as client:
                # 1. Query configfile to get all configured MCUs
                response: httpx.Response = await client.get(
                    'http://127.0.0.1:7125/printer/objects/query?configfile',
                    timeout=2.0,
                )
                config = {}
                if response.status_code == 200:
                    data = response.json()
                    config = (
                        data.get('result', {})
                        .get('status', {})
                        .get('configfile', {})
                        .get('config', {})
                    )

                # 2. Query all mcu and canbus_stats objects
                # We first need to know which ones exist
                list_response: httpx.Response = await client.get(
                    'http://127.0.0.1:7125/printer/objects/list', timeout=2.0
                )
                can_stats = {}
                mcu_statuses = {}
                if list_response.status_code == 200:
                    all_objects = (
                        list_response.json()
                        .get('result', {})
                        .get('objects', [])
                    )
                    stat_objects = [
                        obj
                        for obj in all_objects
                        if obj.startswith('canbus_stats')
                    ]
                    mcu_objects = [
                        obj for obj in all_objects if obj.startswith('mcu')
                    ]

                    query_objects = stat_objects + mcu_objects
                    if query_objects:
                        query_url = f'http://127.0.0.1:7125/printer/objects/query?{"&".join(query_objects)}'
                        stats_response: httpx.Response = await client.get(
                            query_url, timeout=2.0
                        )
                        if stats_response.status_code == 200:
                            raw_data = (
                                stats_response.json()
                                .get('result', {})
                                .get('status', {})
                            )
                            for obj_name, obj_data in raw_data.items():
                                if obj_name.startswith('canbus_stats'):
                                    section = obj_name.replace(
                                        'canbus_stats ', ''
                                    ).strip()
                                    can_stats[section] = obj_data
                                elif obj_name.startswith('mcu'):
                                    section = (
                                        obj_name  # e.g. "mcu" or "mcu toolhead"
                                    )
                                    mcu_statuses[section] = obj_data

                for section_name, section_data in config.items():
                    if not isinstance(section_data, dict):
                        continue

                    identifier = None
                    if 'canbus_uuid' in section_data:
                        identifier = section_data['canbus_uuid'].lower().strip()
                    elif 'serial' in section_data:
                        identifier = section_data['serial'].strip()

                    if identifier:
                        # Check if this MCU is active
                        is_active = False
                        stats = {}

                        # 1. Check canbus_stats (for CAN nodes)
                        stats_key = section_name
                        if section_name.startswith('mcu '):
                            stats_key = section_name[4:].strip()

                        if stats_key in can_stats:
                            stats = can_stats[stats_key]
                            if stats.get('bus_state') == 'Connected':
                                is_active = True
                        elif identifier in can_stats:
                            stats = can_stats[identifier]
                            if stats.get('bus_state') == 'Connected':
                                is_active = True

                        # 2. Check mcu status (for serial/all nodes)
                        # If it has an mcu_version, it's connected and active
                        if not is_active:
                            mcu_key = section_name
                            if mcu_key in mcu_statuses:
                                if mcu_statuses[mcu_key].get('mcu_version'):
                                    is_active = True

                        mcus[identifier] = {
                            'name': section_name,
                            'active': is_active,
                            'stats': stats,
                        }
        except Exception as e:
            logger.error('Error querying Moonraker: %s', e)
        return mcus

    async def get_mcu_versions(self) -> Dict[str, Dict[str, Any]]:
        """Queries Moonraker for MCU version information."""
        versions: Dict[str, Dict[str, Any]] = {}
        try:
            async with httpx.AsyncClient() as client:
                # Get list of all MCU objects
                list_response: httpx.Response = await client.get(
                    'http://127.0.0.1:7125/printer/objects/list', timeout=2.0
                )
                if list_response.status_code != 200:
                    return versions

                all_objects = (
                    list_response.json().get('result', {}).get('objects', [])
                )
                mcu_objects = [
                    obj for obj in all_objects if obj.startswith('mcu')
                ]

                if not mcu_objects:
                    return versions

                # Query all MCU objects for version info
                query_url = f'http://127.0.0.1:7125/printer/objects/query?{"&".join(mcu_objects)}'
                mcu_response: httpx.Response = await client.get(
                    query_url, timeout=2.0
                )
                if mcu_response.status_code != 200:
                    return versions

                mcu_data = (
                    mcu_response.json().get('result', {}).get('status', {})
                )

                # Also get configfile to map MCU names to identifiers
                config_response: httpx.Response = await client.get(
                    'http://127.0.0.1:7125/printer/objects/query?configfile',
                    timeout=2.0,
                )
                config = {}
                if config_response.status_code == 200:
                    config = (
                        config_response.json()
                        .get('result', {})
                        .get('status', {})
                        .get('configfile', {})
                        .get('config', {})
                    )

                for mcu_name, mcu_info in mcu_data.items():
                    version = mcu_info.get('mcu_version', 'unknown')

                    # Find the identifier (canbus_uuid or serial) for this MCU
                    identifier = None
                    config_section = config.get(mcu_name, {})
                    if 'canbus_uuid' in config_section:
                        identifier = (
                            config_section['canbus_uuid'].lower().strip()
                        )
                    elif 'serial' in config_section:
                        identifier = config_section['serial'].strip()

                    if identifier:
                        versions[identifier] = {
                            'name': mcu_name,
                            'version': version,
                            'mcu_constants': mcu_info.get('mcu_constants', {}),
                        }

                    # Also store by name for easy lookup
                    versions[mcu_name] = {
                        'name': mcu_name,
                        'version': version,
                        'identifier': identifier,
                        'mcu_constants': mcu_info.get('mcu_constants', {}),
                    }

        except Exception as e:
            logger.error('Error querying MCU versions: %s', e)
        return versions

    async def check_printer_printing(self) -> Dict[str, Any]:
        """Queries Moonraker to check if a print is in progress."""
        try:
            async with httpx.AsyncClient() as client:
                response: httpx.Response = await client.get(
                    'http://127.0.0.1:7125/printer/objects/query?print_stats',
                    timeout=2.0,
                )
                if response.status_code == 200:
                    data = response.json()
                    stats = (
                        data.get('result', {})
                        .get('status', {})
                        .get('print_stats', {})
                    )
                    state = stats.get('state', 'unknown')
                    filename = stats.get('filename', '')
                    return {
                        'printing': state in ('printing', 'paused'),
                        'state': state,
                        'filename': filename,
                    }
        except Exception:
            pass
        return {'printing': False, 'state': 'unknown', 'filename': ''}

    async def trigger_firmware_restart(self) -> None:
        """Sends a FIRMWARE_RESTART command to Klipper via Moonraker."""
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    'http://localhost:7125/printer/gcode/script?script=FIRMWARE_RESTART',
                    timeout=2.0,
                )
        except Exception as e:
            logger.error('Error sending FIRMWARE_RESTART: %s', e)

    async def ensure_canbus_up(
        self, interface: str = 'can0', bitrate: int = 1000000
    ) -> None:
        """Ensures the CAN interface is up."""
        try:
            # Check if up
            process: Process = await asyncio.create_subprocess_exec(
                'ip',
                'link',
                'show',
                interface,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            if b'state UP' not in stdout:
                logger.info('Bringing up %s...', interface)
                process = await asyncio.create_subprocess_exec(
                    'sudo',
                    'ip',
                    'link',
                    'set',
                    interface,
                    'up',
                    'type',
                    'can',
                    'bitrate',
                    str(bitrate),
                )
                await process.wait()
                await asyncio.sleep(1)
        except Exception as e:
            logger.error('Error ensuring CAN up: %s', e)

    async def list_can_interfaces(self) -> List[str]:
        """Lists all CAN interfaces present in the system."""
        try:
            can_interfaces: List[str] = []
            process: Process = await asyncio.create_subprocess_exec(
                'ip',
                'link',
                'show',
                'type',
                'can',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            for line in stdout.decode().splitlines():
                if ': ' in line:
                    iface: str = line.split(':')[1].strip().split('@')[0]
                    can_interfaces.append(iface)
            return can_interfaces
        except Exception as e:
            logger.error('Error listing CAN interfaces: %s', e)
            return []

    async def discover_can_devices(
        self, skip_moonraker: bool = False, force: bool = False
    ) -> List[Dict[str, str]]:
        """List canbus devices present in the system, and collect all the devices on each bus"""
        try:
            # use the ip tool to get each can interface
            can_interfaces: List[str] = await self.list_can_interfaces()
            devices: List[Dict[str, str]] = []
            for iface in can_interfaces:
                devices_on_iface: List[
                    Dict[str, str]
                ] = await self.discover_can_devices_with_interface(
                    skip_moonraker=skip_moonraker, force=force, interface=iface
                )
                for dev in devices_on_iface:
                    devices.append(dev)
                skip_moonraker = True  # Only query Moonraker once for the first interface if at all
            return devices
        except Exception as e:
            logger.error('Error discovering CAN devices: %s', e)
            return []

    async def discover_can_devices_with_interface(
        self,
        skip_moonraker: bool = False,
        force: bool = False,
        interface: str = 'can0',
    ) -> List[Dict[str, str]]:
        """Discovers CAN devices using Klipper's canbus_query.py, Katapult's flashtool.py, and Moonraker API in parallel."""
        await self.ensure_canbus_up(interface=interface)

        now: float = asyncio.get_event_loop().time()
        if (
            not force
            and (now - self._can_cache_time.get(interface, 0.0))
            < self._can_cache_ttl_s
        ):
            return list(self._can_cache.get(interface, []))

        async with self._can_lock:
            logger.debug('CAN Lock Acquired for discovery')
            seen_uuids = {}  # uuid -> device_dict

            async def run_klipper_query():
                try:
                    klipper_python: str = os.path.abspath(
                        os.path.join(
                            self.klipper_dir,
                            '..',
                            'klippy-env',
                            'bin',
                            'python3',
                        )
                    )
                    if not os.path.exists(klipper_python):
                        klipper_python = 'python3'

                    process: Process = await asyncio.create_subprocess_exec(
                        klipper_python,
                        os.path.join(
                            self.klipper_dir, 'scripts', 'canbus_query.py'
                        ),
                        interface,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await asyncio.wait_for(
                        process.communicate(), timeout=2.0
                    )
                    results = []
                    for line in stdout.decode().splitlines():
                        if 'canbus_uuid=' in line:
                            uuid: str = (
                                line.split('canbus_uuid=')[1]
                                .split(',')[0]
                                .strip()
                            )
                            app = 'Unknown'
                            if 'Application:' in line:
                                app: str = line.split('Application:')[1].strip()
                            results.append((uuid, app))
                    return results
                except Exception:
                    return []

            async def run_katapult_query():
                try:
                    process: Process = await asyncio.create_subprocess_exec(
                        'python3',
                        os.path.join(
                            self.katapult_dir, 'scripts', 'flashtool.py'
                        ),
                        '-i',
                        interface,
                        '-q',
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=5.0
                    )
                    results = []
                    output: str = stdout.decode()
                    for line in output.splitlines():
                        if 'UUID:' in line or 'Detected UUID:' in line:
                            parts: List[str] = line.replace(
                                'Detected UUID:', 'UUID:'
                            ).split(',')
                            uuid: str = parts[0].split('UUID:')[1].strip()
                            app = 'Unknown'
                            if len(parts) > 1 and 'Application:' in parts[1]:
                                app: str = (
                                    parts[1].split('Application:')[1].strip()
                                )
                            results.append((uuid, app))
                    return results
                except Exception as e:
                    logger.error('Katapult query error: %s', e)
                    return []

            # Run discovery methods sequentially to avoid CAN bus contention
            katapult_res = await run_katapult_query()
            klipper_res = await run_klipper_query()

            moonraker_res = {}
            if not skip_moonraker:
                moonraker_res: Dict[
                    str, Dict[str, str]
                ] = await self._get_moonraker_mcus()

            # Merge results (Priority: Katapult > Klipper > Moonraker)
            for uuid, app in katapult_res:
                # If application is Klipper, it's in service. If Katapult/CanBoot, it's ready.
                mode: str = (
                    'ready'
                    if app.lower() in ['katapult', 'canboot']
                    else 'service'
                )
                seen_uuids[uuid] = {
                    'id': uuid,
                    'name': f'CAN Device ({uuid})',
                    'application': app,
                    'mode': mode,
                    'interface': interface,
                }

            for uuid, app in klipper_res:
                if uuid not in seen_uuids:
                    seen_uuids[uuid] = {
                        'id': uuid,
                        'name': f'CAN Device ({uuid})',
                        'application': app,
                        'mode': 'service',
                        'interface': interface,
                    }

            # Moonraker: name enrichment and fallback
            if isinstance(moonraker_res, dict):
                for identifier, info in moonraker_res.items():
                    # Check if identifier looks like a UUID (12 hex chars)
                    if len(identifier) == 12 and all(
                        c in '0123456789abcdef' for c in identifier
                    ):
                        section_name = info['name']
                        if identifier in seen_uuids:
                            if 'CAN Device' in seen_uuids[identifier]['name']:
                                seen_uuids[identifier]['name'] = section_name
                        else:
                            # Fallback: Add it as 'service' if Moonraker knows about it
                            # But only if it's actually active, otherwise mark as offline
                            mode = (
                                'service' if info.get('active') else 'offline'
                            )
                            seen_uuids[identifier] = {
                                'id': identifier,
                                'name': section_name,
                                'application': 'Klipper (Configured)'
                                if info.get('active')
                                else 'Klipper (Offline)',
                                'mode': mode,
                                'interface': interface,
                            }

                        # Add stats if available
                        if info.get('stats'):
                            seen_uuids[identifier]['stats'] = info['stats']

            results = list(seen_uuids.values())
            self._can_cache[interface] = results
            self._can_cache_time[interface] = asyncio.get_event_loop().time()

            logger.debug('CAN Lock Released')
            return results

    def discover_linux_process(self) -> List[Dict[str, str]]:
        """Returns the local Linux process MCU if it exists or as a target."""
        # Klipper's host MCU usually uses /tmp/klipper_host_mcu
        # We'll return it as a discoverable 'device'
        return [{'id': 'linux_process', 'name': 'Linux Process (Host MCU)'}]

    async def is_interface_up(self, interface: str = 'can0') -> bool:
        """Checks if a network interface is UP and has a carrier."""
        try:
            process: Process = await asyncio.create_subprocess_exec(
                'ip',
                'link',
                'show',
                interface,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            output = stdout.decode()
            # Interface must be UP and NOT have NO-CARRIER
            is_up = 'state UP' in output or 'state UNKNOWN' in output
            has_carrier = 'NO-CARRIER' not in output
            return is_up and has_carrier
        except Exception:
            return False

    def _extract_serial_from_id(self, device_id: str) -> Optional[str]:
        """Helper to extract a potential serial number from a device ID or path."""
        if not device_id:
            return None

        # 1. If it's a path, extract from filename
        if device_id.startswith('/dev/serial/by-id/'):
            filename = os.path.basename(device_id)
            # Heuristic: the serial is usually the longest part between underscores or before -if
            parts = filename.replace('-if', '_').split('_')
            # Filter out common prefixes/suffixes
            candidates = [
                p
                for p in parts
                if p not in ['usb', 'Klipper', 'katapult', 'CanBoot', '00']
            ]
            if candidates:
                # Sort by length, longest is likely the serial
                return sorted(candidates, key=len, reverse=True)[0]

        # 2. If it's not a path and looks like a serial number (long enough, no slashes)
        if not '/' in device_id and len(device_id) > 5:
            return device_id

        return None

    async def post_flash_rescan(
        self,
        old_device_id: str,
        initial_serials: List[str],
        fleet_mgr: Any,
    ) -> AsyncGenerator[str, None]:
        """Issue #17: After a successful flash, rescan serial devices and update
        fleet.json if the USB descriptor (and thus /dev/serial/by-id/ path) changed.

        Strategy:
        1. Match by chip serial suffix (e.g. rp2040_E66160F42367B137) — unique and reliable.
        2. Fallback: diff-based detection (one device disappeared, one new appeared).
        3. If ambiguous, log a warning and let the user resolve manually.
        """
        current_serials_raw: List[
            Dict[str, str]
        ] = await self.discover_serial_devices(skip_moonraker=True)
        current_ids: List[str] = [d['id'] for d in current_serials_raw]

        # If the old path still exists, nothing changed
        if old_device_id in current_ids:
            return

        # Strategy 1: Match by chip serial suffix
        old_serial = self._extract_serial_from_id(old_device_id)
        if old_serial:
            for cid in current_ids:
                if old_serial in cid:
                    updated = await fleet_mgr.update_device_id(
                        old_device_id, cid
                    )
                    if updated:
                        yield f'>>> Device path changed: {old_device_id} -> {cid}\n'
                        yield f'>>> Fleet updated automatically (matched by serial: {old_serial}).\n'
                    else:
                        yield f'>>> WARNING: Device re-enumerated as {cid} but fleet entry not found for update.\n'
                    return

        # Strategy 2: Diff-based — find which device disappeared and which appeared
        disappeared = [s for s in initial_serials if s not in current_ids]
        appeared = [s for s in current_ids if s not in initial_serials]

        if old_device_id in disappeared and len(appeared) == 1:
            new_id = appeared[0]
            updated = await fleet_mgr.update_device_id(old_device_id, new_id)
            if updated:
                yield f'>>> Device path changed: {old_device_id} -> {new_id}\n'
                yield f'>>> Fleet updated automatically (matched by diff: 1 disappeared, 1 appeared).\n'
            else:
                yield f'>>> WARNING: Device re-enumerated as {new_id} but fleet entry not found for update.\n'
            return

        # Strategy 3: Ambiguous — warn the user
        yield f'>>> WARNING: Device {old_device_id} did not re-appear after flash.\n'
        if appeared:
            yield f'>>> New devices detected: {", ".join(appeared)}\n'
        yield '>>> Please verify fleet.json and update the device ID manually if needed.\n'

    async def resolve_dfu_id(
        self,
        device_id: str,
        known_dfu_id: Optional[str] = None,
        strict: bool = False,
    ) -> str:
        """Attempts to find a DFU device ID that matches a Serial ID (via serial number).
        If strict=True, does not fall back to single-device assumption."""
        devs: List[Dict[str, str]] = await self.discover_dfu_devices()

        if not devs:
            return device_id

        # 1. If we have a known DFU ID, try to find it exactly
        if known_dfu_id:
            for d in devs:
                if d['id'] == known_dfu_id:
                    return d['id']

            # 2. If known_dfu_id is a generic name (like STM32FxSTM32),
            # and there's only one DFU device, assume it's the one.
            if not strict and len(devs) == 1:
                return devs[0]['id']

        # 3. Try to match by serial number
        target_serial = self._extract_serial_from_id(device_id)
        for d in devs:
            if d['id'] == device_id:
                return d['id']
            # Check if the DFU serial matches
            if (
                d.get('serial')
                and target_serial
                and d['serial'] == target_serial
            ):
                return d['id']

        # 4. Fallback: If there is only ONE DFU device connected, assume it is the target.
        # This handles cases where the DFU serial is generic (e.g. "STM32FxSTM32") or
        # does not match the Klipper serial number.
        # Skipped in strict mode to avoid flashing the wrong device.
        if not strict and len(devs) == 1:
            return devs[0]['id']

        return device_id

    async def resolve_serial_id(
        self, device_id: str, known_serial_id: Optional[str] = None
    ) -> str:
        """Attempts to find a Serial ID that matches a DFU ID or a Klipper ID (via serial number)."""
        if known_serial_id and os.path.exists(known_serial_id):
            return known_serial_id

        # If it's already a serial device that exists, return it
        if os.path.exists(device_id):
            return device_id

        serials: List[Dict[str, str]] = await self.discover_serial_devices(
            skip_moonraker=True
        )

        # 1. Try to extract a serial number
        target_serial = self._extract_serial_from_id(device_id)

        # 2. If we still don't have it, try looking it up as a DFU device
        if not target_serial:
            dfus: List[Dict[str, str]] = await self.discover_dfu_devices()
            for d in dfus:
                if d['id'] == device_id:
                    target_serial = d.get('serial')
                    break

        if target_serial:
            for s in serials:
                # Match if the target serial is in the new ID
                if target_serial in s['id']:
                    return s['id']

        return device_id

    async def refresh_beacon_path(self) -> Optional[str]:
        """Refreshes the beacon_klipper path by querying Moonraker's /server/config API."""
        try:
            async with httpx.AsyncClient() as client:
                response: httpx.Response = await client.get(
                    'http://127.0.0.1:7125/server/config', timeout=5.0
                )
                if response.status_code != 200:
                    return None
                data = response.json()
                config: Dict[str, Any] = data.get('result', {}).get(
                    'config', {}
                )
                for key, section in config.items():
                    if not key.startswith('update_manager '):
                        continue
                    if 'beacon' not in key.lower():
                        continue
                    raw_path: Optional[str] = section.get('path')
                    if raw_path:
                        self._beacon_klipper_path = os.path.expanduser(raw_path)
                        return self._beacon_klipper_path
        except Exception as e:
            logger.warning(
                'Failed to query Moonraker for beacon_klipper path: %s', e
            )
        return None

    async def get_beacon_klipper_path(self) -> Optional[str]:
        """Returns cached beacon_klipper path, or refreshes it if not yet cached."""
        if self._beacon_klipper_path is not None:
            return self._beacon_klipper_path
        return await self.refresh_beacon_path()

    async def discover_beacon_devices(self) -> List[Dict[str, str]]:
        """Discovers Beacon probes by scanning /dev/serial/by-id/ for Beacon device paths."""
        devices: List[Dict[str, str]] = []
        beacon_pattern: str = '/dev/serial/by-id/*Beacon_Beacon_Rev*'
        matches: List[str] = glob.glob(beacon_pattern)
        for path in sorted(matches):
            basename: str = os.path.basename(path)
            # Extract revision (e.g. "RevH") from paths like usb-Beacon_Beacon_RevH_<serial>-if00
            rev_match = re.search(r'Beacon_Rev([A-Za-z0-9]+)', basename)
            revision: str = (
                f'Rev{rev_match.group(1)}' if rev_match else 'Unknown'
            )
            # Extract serial number — typically the segment after the last Rev* part before -if
            serial_match = re.search(
                r'Beacon_Rev[A-Za-z0-9]+_([^-]+)', basename
            )
            serial: str = serial_match.group(1) if serial_match else ''
            devices.append(
                {
                    'id': path,
                    'name': f'Beacon {revision}',
                    'revision': revision,
                    'serial': serial,
                    'mode': 'service',
                }
            )
        return devices

    async def flash_beacon(
        self, device_id: str, beacon_klipper_path: str, force: bool = False
    ) -> AsyncGenerator[str, None]:
        """Flashes a Beacon probe using the beacon_klipper update_firmware.py script."""
        yield f'>>> Beacon flash: using repo at {beacon_klipper_path}\n'

        cmd: list = [
            'python3',
            os.path.join(beacon_klipper_path, 'update_firmware.py'),
            'update',
            device_id,
        ]
        if force:
            cmd.append('--force')

        yield f'>>> Running: {" ".join(cmd)}\n'

        async for line in self._run_flash_command(cmd):
            yield line
        yield '>>> Flashing Beacon successful!\n'

    async def check_device_status(
        self,
        device_id: str,
        method: str,
        dfu_id: Optional[str] = None,
        skip_moonraker: bool = False,
        is_bridge: bool = False,
        interface: str = 'can0',
        serial_id: Optional[str] = None,
    ) -> str:
        """Checks if a device is reachable and its current mode."""
        method = method.lower()

        # Special handling for bridges
        if is_bridge:
            # 1. Check if it's in Katapult mode (Serial)
            serials = await self.discover_serial_devices(skip_moonraker=True)
            serial_ids = [s['id'] for s in serials]

            # Direct match using known serial_id (e.g. CAN bridges with a serial Katapult path)
            if serial_id and serial_id in serial_ids:
                return 'ready'

            target_serial = self._extract_serial_from_id(device_id)
            for s in serials:
                # Match by ID exactly
                if s['id'] == device_id:
                    return 'ready'
                # Or match by serial number if we have one
                if target_serial and target_serial in s['id']:
                    return 'ready'

            # 2. Check if it's in DFU mode
            # Only if we have a reason to look for DFU (e.g. dfu_id provided)
            if dfu_id:
                dfus = await self.discover_dfu_devices()
                resolved_dfu_id = await self.resolve_dfu_id(
                    device_id, known_dfu_id=dfu_id
                )
                if any(d['id'] == resolved_dfu_id for d in dfus):
                    return 'ready'

            # 3. Check if the interface is up (In Service)
            # But only if the hardware is actually present
            if method == 'serial' or device_id.startswith('/dev/'):
                # For serial bridges, the serial device MUST exist
                if os.path.exists(device_id) and await self.is_interface_up(
                    interface
                ):
                    return 'service'
            else:
                # For CAN-based bridges (identified by UUID), we check Moonraker
                # or if the interface is up (if Moonraker is skipped)
                if await self.is_interface_up(interface):
                    if not skip_moonraker:
                        mcus = await self._get_moonraker_mcus()
                        if device_id in mcus and mcus[device_id]['active']:
                            return 'service'
                        # If Moonraker says it's NOT active, then it's not in service
                        # even if the interface is up (might be another device)
                        return 'offline'

                    # If skipping moonraker, we can't be 100% sure, but if the interface is up
                    # and it's a bridge, it's likely "in service" (providing the bus)
                    return 'service'

            return 'offline'

        if method == 'serial':
            if os.path.exists(device_id):
                # Check if it's Klipper or Katapult
                if (
                    'katapult' in device_id.lower()
                    or 'canboot' in device_id.lower()
                ):
                    return 'ready'
                return (
                    'service'  # Assume Klipper if it exists and isn't katapult
                )

            # 1. Check if it's currently in DFU mode
            resolved_dfu_id: str = await self.resolve_dfu_id(
                device_id, known_dfu_id=dfu_id
            )
            if resolved_dfu_id != device_id:
                return 'dfu'

            # 2. If the path doesn't exist, it might have changed ID (e.g. Klipper -> Katapult)
            resolved_id: str = await self.resolve_serial_id(device_id)
            if resolved_id != device_id and os.path.exists(resolved_id):
                if (
                    'katapult' in resolved_id.lower()
                    or 'canboot' in resolved_id.lower()
                ):
                    return 'ready'
                return 'service'

            return 'offline'
        elif method == 'can':
            devs: List[Dict[str, str]] = await self.discover_can_devices(
                skip_moonraker=skip_moonraker
            )
            for d in devs:
                if d['id'] == device_id:
                    return d.get('mode', 'offline')

            # Check if it's currently in DFU mode (if it has a dfu_id)
            if dfu_id:
                resolved_dfu_id: str = await self.resolve_dfu_id(
                    device_id, known_dfu_id=dfu_id
                )
                if resolved_dfu_id != device_id:
                    return 'dfu'

            return 'offline'
        elif method == 'dfu':
            # 1. Check if it's actually in DFU mode
            resolved_dfu_id: str = await self.resolve_dfu_id(
                device_id, known_dfu_id=dfu_id
            )
            devs: List[Dict[str, str]] = await self.discover_dfu_devices()
            if any(d['id'] == resolved_dfu_id for d in devs):
                return 'dfu'

            # 2. Check if it's in Serial mode (In Service)
            serial_id: str = await self.resolve_serial_id(device_id)
            if os.path.exists(serial_id):
                return 'service'

            return 'offline'
        elif method == 'linux':
            return (
                'service'
                if os.path.exists('/tmp/klipper_host_mcu')
                else 'ready'
            )
        elif method == 'beacon':
            return 'service' if os.path.exists(device_id) else 'offline'
        return 'unknown'

    async def reboot_device(
        self,
        device_id: str,
        mode: str = 'katapult',
        method: str = 'can',
        interface: str = 'can0',
        is_bridge: bool = False,
        serial_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """Reboots a device, either to Katapult, DFU, or a regular reboot."""
        if mode == 'katapult':
            async for line in self.reboot_to_katapult(
                device_id,
                method=method,
                interface=interface,
                is_bridge=is_bridge,
            ):
                yield line
        elif mode == 'dfu':
            async for line in self.reboot_to_dfu(device_id):
                yield line
        else:
            # Regular reboot (Return to Service)

            # If it's a serial device, check if it's actually in DFU mode right now
            if method == 'serial':
                dfus = await self.discover_dfu_devices()
                resolved_dfu_id = await self.resolve_dfu_id(device_id)
                if any(d['id'] == resolved_dfu_id for d in dfus):
                    method = 'dfu'
                    device_id = resolved_dfu_id
                    yield f'>>> Detected {device_id} in DFU mode. Using DFU reboot.\n'

            if method == 'can':
                # CAN bridges in Katapult mode ARE the CAN interface, so can0 won't exist.
                # Auto-switch to serial if the bridge has a serial_id.
                if (
                    is_bridge
                    and serial_id
                    and not os.path.exists(f'/sys/class/net/{interface}')
                ):
                    yield f'>>> CAN interface {interface} is down (bridge is in bootloader). Falling back to serial.\n'
                    try:
                        from backend.katapult_protocol import (
                            restart_firmware_serial,
                        )

                        result = restart_firmware_serial(serial_id)
                        yield result + '\n'
                        yield '>>> Restart command sent via serial. Device should return to firmware.\n'
                    except Exception as e:
                        yield f'>>> Serial fallback failed: {e}\n'
                else:
                    yield f'>>> Requesting regular reboot for {device_id}...\n'
                    try:
                        from backend.katapult_protocol import (
                            restart_firmware_can,
                        )

                        result = restart_firmware_can(interface, device_id)
                        yield result + '\n'
                        yield '>>> Regular reboot command sent.\n'
                    except Exception as e:
                        yield f'>>> CAN reboot failed: {e}\n'
            elif method == 'dfu':
                yield f'>>> Requesting reboot for DFU device {device_id}...\n'
                # For STM32 DFU, the most reliable way to "leave" DFU mode without flashing
                # is to perform a dummy upload with the ':leave' flag.
                # We use 0x08000000 as the default start address for STM32.
                cmd: List[str] = [
                    'dfu-util',
                    '-d',
                    '0483:df11',
                    '-a',
                    '0',
                    '-s',
                    '0x08000000:leave',
                    '-U',
                    '/tmp/reboot_dummy',
                    '-Z',
                    '1',
                ]

                # Try to disambiguate if possible
                if ':' not in device_id and '/' in device_id:  # Path
                    cmd.extend(['-p', device_id])
                elif ':' not in device_id:  # Serial
                    cmd.extend(['-S', device_id])

                # Ensure the dummy file doesn't block the upload
                if os.path.exists('/tmp/reboot_dummy'):
                    os.remove('/tmp/reboot_dummy')

                process: Process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await process.communicate()
                yield stdout.decode()
                if process.returncode == 0:
                    yield '>>> Leave request sent. Device should reboot into firmware shortly.\n'
                else:
                    yield '>>> Reboot command failed. The bootloader may not support software reset via DFU.\n'
            else:
                # For serial Katapult devices, connect to the bootloader and send
                # the COMPLETE command which makes Katapult jump to the application.
                yield f'>>> Sending Katapult COMPLETE command to {device_id}...\n'
                try:
                    from backend.katapult_protocol import (
                        restart_firmware_serial,
                    )

                    result = restart_firmware_serial(device_id)
                    yield result + '\n'
                    yield '>>> Restart command sent. Device should return to firmware.\n'
                except Exception as e:
                    yield f'>>> Failed to send restart command: {e}\n'

    async def reboot_to_katapult(
        self,
        device_id: str,
        method: str = 'can',
        interface: str = 'can0',
        is_bridge: bool = False,
        baudrate: int = 250000,
    ) -> AsyncGenerator[str, None]:
        """Sends a reboot command to a device to enter Katapult."""
        yield f'>>> Requesting reboot to Katapult for {device_id}...\n'
        method = method.lower()

        # Issue #16: USB-to-CAN bridges are configured as method="can" but
        # connect via USB serial.  Passing a /dev/ path to flashtool -u crashes
        # because it expects a hex CAN UUID.  Auto-correct to serial reboot.
        if method == 'can' and device_id.startswith('/dev/'):
            yield f'>>> Auto-correcting: {device_id} is a serial path, using serial reboot instead of CAN.\n'
            method = 'serial'

        if method == 'can':
            async with self._can_lock:
                yield f'>>> CAN Lock Acquired for rebooting {device_id}\n'
                # Using flashtool.py -r is much more reliable for all CAN nodes
                cmd: List[str] = [
                    'python3',
                    os.path.join(self.katapult_dir, 'scripts', 'flashtool.py'),
                    '-i',
                    interface,
                    '-u',
                    device_id,
                    '-r',
                ]
                process: Process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await process.communicate()
                yield stdout.decode()
                self._can_cache_time[interface] = 0.0  # Invalidate cache
                yield f'>>> CAN Lock Released\n'
                return

        # Serial method
        # 1. Try the 1200bps trick first (common for Katapult/CanBoot on Serial)
        yield f'>>> Attempting 1200bps magic baud on {device_id}...\n'
        try:
            import serial

            ser = serial.Serial(device_id, 1200)
            ser.close()
            await asyncio.sleep(2)  # Give it time to reboot

            # If the device path is gone, the trick worked and the device is rebooting
            if not os.path.exists(device_id):
                yield '>>> Device disconnected (1200bps trick successful). Waiting for bootloader...\n'
                return
        except Exception as e:
            yield f'>>> 1200bps trick skipped or failed: {str(e)}\n'

        # 2. Also try flashtool.py -r as a backup if the device still exists
        if os.path.exists(device_id):
            cmd: List[str] = [
                'python3',
                os.path.join(self.katapult_dir, 'scripts', 'flashtool.py'),
                '-d',
                device_id,
                '-b',
                str(baudrate),
                '-r',
            ]

            process: Process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            while True:
                if process.stdout is None:
                    break
                line: bytes = await process.stdout.readline()
                if not line:
                    break
                yield line.decode()

            await process.wait()
            if process.returncode == 0:
                yield '>>> Reboot command sent. Device should appear in Katapult mode shortly.\n'
            else:
                yield f'>>> Reboot command failed with return code {process.returncode}. Device might already be in Katapult or unreachable.\n'
        else:
            yield '>>> Device path not found. It may already be in bootloader mode.\n'

    async def reboot_to_dfu(self, device_id: str) -> AsyncGenerator[str, None]:
        """Attempts to reboot a device into DFU mode using the 1200bps magic baud rate."""
        # If device_id is a DFU ID, but the device is in Serial mode, resolve it first
        actual_id: str = await self.resolve_serial_id(device_id)
        yield f'>>> Attempting to reboot {actual_id} into DFU mode (1200bps trick)...\n'
        try:
            # The 1200bps trick: open and close the port at 1200bps
            import serial

            ser = serial.Serial(actual_id, 1200)
            ser.close()
            yield '>>> 1200bps magic baud sent. Waiting 3s for USB enumeration...\n'
            await asyncio.sleep(3)
        except Exception as e:
            yield f'>>> Error sending 1200bps magic baud: {str(e)}\n'
            yield '>>> Please manually enter DFU mode (BOOT0 + RESET) if the device does not appear.\n'

    async def flash_serial(
        self, device_id: str, firmware_path: str, baudrate: int = 250000
    ) -> AsyncGenerator[str, None]:
        """Flashes a device via Serial using Katapult."""
        yield f'>>> Flashing {firmware_path} to {device_id} via Serial (baud {baudrate})...\n'
        cmd: List[str] = [
            'python3',
            os.path.join(self.katapult_dir, 'scripts', 'flashtool.py'),
            '-f',
            firmware_path,
            '-d',
            device_id,
            '-b',
            str(baudrate),
        ]
        async for line in self._run_flash_command(cmd):
            yield line

    async def flash_make(
        self, device_id: str, firmware_path: str, config_path: str
    ) -> AsyncGenerator[str, None]:
        """Flashes a device using Klipper's 'make flash' (handles AVR, RP2040, etc.)."""
        import shutil

        yield f'>>> Flashing {device_id} via make flash...\n'

        # Copy the profile .config into klipper dir for make flash
        tmp_config: str = os.path.join(self.klipper_dir, '.config')
        try:
            shutil.copy(config_path, tmp_config)
        except Exception as e:
            yield f'!!! Error copying profile config: {e}\n'
            return

        # Ensure the firmware in out/ is up to date for this profile.
        # 'make flash' will rebuild only if necessary.
        yield f'>>> Running make flash FLASH_DEVICE={device_id}...\n'
        process: Process = await asyncio.create_subprocess_exec(
            'make',
            'flash',
            f'FLASH_DEVICE={device_id}',
            cwd=self.klipper_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        while True:
            if process.stdout is None:
                break
            chunk: bytes = await process.stdout.read(128)
            if not chunk:
                break
            yield chunk.decode(errors='replace')

        await process.wait()
        if process.returncode == 0:
            yield '>>> Flashing successful!\n'
        else:
            yield f'>>> Flashing failed with return code {process.returncode}\n'

    async def flash_can(
        self, uuid: str, firmware_path: str, interface: str = 'can0'
    ) -> AsyncGenerator[str, None]:
        """Flashes a device via CAN using Katapult."""
        # Issue #16: Guard against serial paths being passed as CAN UUIDs.
        # USB-to-CAN bridges sometimes have method="can" but their device_id is
        # a /dev/serial/by-id/ path.  flashtool.py -u expects a hex UUID.
        if uuid.startswith('/dev/'):
            raise ValueError(
                f"Cannot flash via CAN: '{uuid}' is a serial device path, not a CAN UUID. "
                'This is a USB-to-CAN bridge — use serial or DFU flash method instead.'
            )
        async with self._can_lock:
            yield f'>>> CAN Lock Acquired for flashing {uuid}\n'
            yield f'>>> Flashing {firmware_path} to {uuid} via {interface}...\n'
            cmd: List[str] = [
                'python3',
                os.path.join(self.katapult_dir, 'scripts', 'flashtool.py'),
                '-i',
                interface,
                '-u',
                uuid,
                '-f',
                firmware_path,
            ]
            async for line in self._run_flash_command(cmd):
                yield line
            self._can_cache_time[interface] = 0.0  # Invalidate cache
            yield f'>>> CAN Lock Released\n'

    async def flash_dfu(
        self,
        device_id: str,
        firmware_path: str,
        address: str = '0x08000000',
        leave: bool = True,
    ) -> AsyncGenerator[str, None]:
        """Flashes a device in DFU mode using dfu-util."""
        yield f'>>> Flashing {firmware_path} via DFU to {address} (Leave: {leave})...\n'

        # Prevent concurrent dfu-util calls (like UI polling dfu-util -l) while flashing.
        async with self._dfu_lock:
            # Invalidate any cached dfu-util -l results since the device will transition.
            self._dfu_cache_time = 0.0

            # We try to be specific if we have a serial or path
            # device_id here could be the serial number or the path from discover_dfu_devices

            # STRATEGY: Download WITHOUT :leave first, then issue a separate
            # DFU detach. Some STM32 bootloaders timeout on get_status after
            # long erase/write when :leave is present.
            cmd: List[str] = [
                'sudo',
                'dfu-util',
                '-a',
                '0',
                '-d',
                '0483:df11',
                '-s',
                address,
                '-D',
                firmware_path,
            ]

            # If device_id looks like a serial number (usually alphanumeric, long)
            # AND it is not a path (does not start with /dev/)
            if (
                device_id
                and len(device_id) > 5
                and not device_id.startswith('/dev/')
            ):
                cmd.extend(['-S', device_id])
            # If it looks like a path (e.g. 1-1.2)
            elif (
                device_id
                and '-' in device_id
                and not device_id.startswith('/dev/')
            ):
                cmd.extend(['-p', device_id])
            elif device_id and device_id.startswith('/dev/'):
                yield f">>> WARNING: Device ID '{device_id}' looks like a serial path, not a DFU ID. Skipping specific device selection.\n"

            # Retry mechanism for the download phase
            max_retries = 3
            flash_success = False
            for attempt in range(max_retries):
                try:
                    if attempt > 0:
                        yield f'>>> Retry attempt {attempt + 1}/{max_retries}...\n'
                        await asyncio.sleep(2)
                        # Re-resolve DFU device ID in case USB re-enumerated
                        self._dfu_cache_time = 0.0
                        new_devs = await self.discover_dfu_devices()
                        if new_devs:
                            # Rebuild the command with the potentially new device ID
                            cmd = [
                                'sudo',
                                'dfu-util',
                                '-a',
                                '0',
                                '-d',
                                '0483:df11',
                                '-s',
                                address,
                                '-D',
                                firmware_path,
                            ]
                            resolved = new_devs[
                                0
                            ]  # Best effort: pick the first DFU device
                            for d in new_devs:
                                if (
                                    d['id'] == device_id
                                    or d.get('serial') == device_id
                                ):
                                    resolved = d
                                    break
                            rid = resolved['id']
                            if (
                                rid
                                and len(rid) > 5
                                and not rid.startswith('/dev/')
                            ):
                                cmd.extend(['-S', rid])
                            elif (
                                rid
                                and '-' in rid
                                and not rid.startswith('/dev/')
                            ):
                                cmd.extend(['-p', rid])

                    current_success = False
                    async for line in self._run_flash_command(cmd):
                        yield line
                        if '>>> Flashing successful!' in line:
                            current_success = True

                    if current_success:
                        flash_success = True
                        break
                except Exception as e:
                    yield f'>>> Error during flash attempt {attempt + 1}: {e}\n'

            if not flash_success:
                yield '!!! Flash operation failed after multiple attempts.\n'
                return

            # If successful and leave is requested, send a separate tiny command to exit DFU
            if leave:
                yield '>>> Sending DFU leave request to reboot device...\n'
                # We use a 0-length download to the base address with :leave to trigger a reset
                # We use -R as well for extra robustness on some bootloaders
                leave_cmd: List[str] = [
                    'sudo',
                    'dfu-util',
                    '-a',
                    '0',
                    '-d',
                    '0483:df11',
                    '-R',
                    '-s',
                    f'{address}:leave',
                ]
                if (
                    device_id
                    and len(device_id) > 5
                    and not device_id.startswith('/dev/')
                ):
                    leave_cmd.extend(['-S', device_id])
                elif (
                    device_id
                    and '-' in device_id
                    and not device_id.startswith('/dev/')
                ):
                    leave_cmd.extend(['-p', device_id])

                # Run the leave command
                # dfu-util can return non-zero here (commonly 251) because the device disconnects
                # during detach/reset while switching back to runtime. Treat that as success.
                async for line in self._run_flash_command(
                    leave_cmd, ok_returncodes={0, 251}
                ):
                    if 'Flashing successful' in line or 'Done' in line:
                        yield '>>> Device rebooted successfully.\n'
                    else:
                        yield line

            yield '>>> Flash operation complete.\n'
            self._dfu_cache_time = 0.0

    async def _run_sudo_command(self, cmd: List[str]) -> tuple:
        """Runs a command via sudo -n (non-interactive). Returns (returncode, output)."""
        # Insert -n after sudo so it never blocks waiting for a password
        if cmd and cmd[0] == 'sudo' and '-n' not in cmd:
            cmd = [cmd[0], '-n'] + cmd[1:]
        process: Process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await process.communicate()
        return process.returncode, stdout.decode().strip()

    async def flash_linux(
        self, firmware_path: str
    ) -> AsyncGenerator[str, None]:
        """'Flashes' the Linux process by installing the binary to /usr/local/bin/klipper_mcu."""
        yield f'>>> Installing Linux MCU binary: {firmware_path}...\n'
        try:
            # 1. Stop klipper-mcu service if it's still running
            # (manage_klipper_services usually handles this, but be safe)
            rc, out = await self._run_sudo_command(
                ['sudo', 'systemctl', 'stop', 'klipper-mcu.service']
            )
            if rc != 0 and 'not loaded' not in out.lower():
                yield f'>>> WARNING: Could not stop klipper-mcu.service (rc={rc}): {out}\n'
                if (
                    'password is required' in out.lower()
                    or 'a terminal is required' in out.lower()
                ):
                    yield '!!! SUDO ERROR: Passwordless sudo is not configured for this user.\n'
                    yield '!!! Please run: sudo visudo -f /etc/sudoers.d/klipperfleet\n'
                    yield f'!!! Add: {os.environ.get("USER", "pi")} ALL=(ALL) NOPASSWD: /usr/bin/systemctl, /bin/cp, /bin/chmod, /usr/bin/fuser\n'
                    yield '!!! Or re-run the KlipperFleet installer (install.sh) which sets this up automatically.\n'
                    return

            # Kill any remaining processes using the file
            await self._run_sudo_command(
                ['sudo', 'fuser', '-k', '/usr/local/bin/klipper_mcu']
            )
            await asyncio.sleep(1)

            # 2. Copy to /usr/local/bin/klipper_mcu
            rc, out = await self._run_sudo_command(
                ['sudo', 'cp', firmware_path, '/usr/local/bin/klipper_mcu']
            )
            if rc != 0:
                if (
                    'password is required' in out.lower()
                    or 'a terminal is required' in out.lower()
                ):
                    yield '!!! SUDO ERROR: Passwordless sudo is not configured for this user.\n'
                    yield '!!! Please run: sudo visudo -f /etc/sudoers.d/klipperfleet\n'
                    yield f'!!! Add: {os.environ.get("USER", "pi")} ALL=(ALL) NOPASSWD: /usr/bin/systemctl, /bin/cp, /bin/chmod, /usr/bin/fuser\n'
                    yield '!!! Or re-run the KlipperFleet installer (install.sh) which sets this up automatically.\n'
                else:
                    yield f'!!! Error copying binary: {out}\n'
                return

            # 3. Ensure it's executable
            rc, out = await self._run_sudo_command(
                ['sudo', 'chmod', '+x', '/usr/local/bin/klipper_mcu']
            )
            if rc != 0:
                yield f'>>> WARNING: chmod failed (rc={rc}): {out}\n'

            yield '>>> Linux MCU binary installed successfully.\n'

            # 4. Restart klipper-mcu service so it's running before Klipper starts
            yield '>>> Restarting klipper-mcu.service...\n'
            rc, out = await self._run_sudo_command(
                ['sudo', 'systemctl', 'start', 'klipper-mcu.service']
            )
            if rc != 0 and 'not loaded' not in out.lower():
                yield f'>>> WARNING: Could not start klipper-mcu.service (rc={rc}): {out}\n'
            else:
                # Wait for the socket to appear so Klipper can connect immediately
                for _ in range(10):
                    if os.path.exists('/tmp/klipper_host_mcu'):
                        break
                    await asyncio.sleep(0.5)
                if os.path.exists('/tmp/klipper_host_mcu'):
                    yield '>>> klipper-mcu.service is running (socket ready).\n'
                else:
                    yield '>>> WARNING: klipper-mcu socket did not appear within 5s.\n'
        except Exception as e:
            yield f'!!! Error during Linux MCU installation: {str(e)}\n'

    async def _run_flash_command(
        self, cmd: list, ok_returncodes: Optional[Set[int]] = None
    ) -> AsyncGenerator[str, None]:
        if ok_returncodes is None:
            ok_returncodes = {0}
        process: Process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        while True:
            if process.stdout is None:
                break
            # Read in chunks to handle progress bars (\r)
            chunk: bytes = await process.stdout.read(128)
            if not chunk:
                break
            yield chunk.decode(errors='replace')

        await process.wait()
        if process.returncode in ok_returncodes:
            yield '>>> Flashing successful!\n'
        else:
            yield f'>>> Flashing failed with return code {process.returncode}\n'
