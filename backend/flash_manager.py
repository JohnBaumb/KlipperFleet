import os
import asyncio
import glob
import httpx
from typing import List, Dict, AsyncGenerator, Optional, Any, Set
from asyncio.subprocess import Process

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
        self._can_cache_ttl_s: float = 2.0 # Short TTL to keep status feeling "live"

    async def discover_serial_devices(self, skip_moonraker: bool = False) -> List[Dict[str, str]]:
        """Lists all serial devices in /dev/serial/by-id/ and common UART ports."""
        devices = []
        
        # 1. USB Serial devices (by-id is preferred for stability)
        usb_devs: List[str] = glob.glob("/dev/serial/by-id/*")
        
        # 2. Common UART and CDC-ACM devices
        # We include /dev/ttyACM* and /dev/ttyUSB* because some devices (especially in Katapult)
        # might not immediately get a by-id link or might be generic.
        candidates: List[str] = glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*") + ["/dev/ttyAMA0", "/dev/ttyS0"]
        
        moonraker_mcus = {}
        if not skip_moonraker:
            moonraker_mcus: Dict[str, Dict[str, str]] = await self._get_moonraker_mcus()
        
        # Combine and deduplicate
        # We use absolute paths for everything
        all_devs: List[str] = list(set(usb_devs + [os.path.abspath(d) for d in candidates if os.path.exists(d)]))
        
        for dev in all_devs:
            name: str = os.path.basename(dev)
            is_configured: bool = dev in moonraker_mcus
            
            # If it's a by-id device, it's almost certainly an MCU
            if dev.startswith("/dev/serial/by-id/"):
                # If it has "klipper" or "kalico" in the name, it's in firmware mode (service)
                # If it has "katapult" or "canboot", it's in bootloader mode (ready)
                mode = "ready"
                dev_lower = dev.lower()
                if "klipper" in dev_lower or "kalico" in dev_lower:
                    mode = "service"
                elif "katapult" in dev_lower or "canboot" in dev_lower:
                    mode = "ready"
                elif is_configured:
                    mode = "service"
                
                if is_configured:
                    name = f"{moonraker_mcus[dev]['name']} ({name})"
                devices.append({"id": dev, "name": name, "type": "usb", "mode": mode})
            
            # If it's a ttyACM/ttyUSB device, we show it if it's NOT already represented by a by-id link
            # or if it's configured in Klipper.
            elif dev.startswith("/dev/ttyACM") or dev.startswith("/dev/ttyUSB"):
                # Check if this physical device is already in devices via by-id
                # (This is a bit tricky, but usually by-id is a symlink to ttyACM/USB)
                real_path: str = os.path.realpath(dev)
                already_added = False
                for d in devices:
                    if os.path.realpath(d['id']) == real_path:
                        already_added = True
                        break
                
                if not already_added:
                    # For generic tty devices, we rely on is_configured or name hints
                    mode = "ready"
                    dev_lower = dev.lower()
                    if "klipper" in dev_lower or "kalico" in dev_lower:
                        mode = "service"
                    elif "katapult" in dev_lower or "canboot" in dev_lower:
                        mode = "ready"
                    elif is_configured:
                        mode = "service"
                    
                    if is_configured:
                        name = f"{moonraker_mcus[dev]['name']} ({name})"
                    devices.append({"id": dev, "name": name, "type": "usb", "mode": mode})

            # If it's a raw UART device, only show it if it's actually configured in Klipper
            elif is_configured:
                name = f"{moonraker_mcus[dev]['name']} ({name})"
                devices.append({"id": dev, "name": name, "type": "uart", "mode": "service"})
                
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
                    "sudo", "dfu-util", "-l",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await process.communicate()
                lines: List[str] = stdout.decode().splitlines()

                # Example line: Found DFU: [0483:df11] ver=0200, devnum=12, cfg=1, intf=0, path="1-1.2", alt=0, name="@Internal Flash  /0x08000000/064*0002Kg", serial="357236543131"
                for line in lines:
                    if "Found DFU:" in line:
                        # Extract VID:PID
                        vid_pid: str = ""
                        if "[" in line and "]" in line:
                            vid_pid = line.split("[")[1].split("]")[0]

                        # Extract serial
                        serial: str = ""
                        if 'serial="' in line:
                            serial = line.split('serial="')[1].split('"')[0]

                        # Extract path
                        path: str = ""
                        if 'path="' in line:
                            path = line.split('path="')[1].split('"')[0]

                        name: str = f"DFU Device ({vid_pid})"
                        if serial:
                            name += f" S/N: {serial}"

                        # We use the serial or path as the ID for disambiguation.
                        # If serial is "UNKNOWN" or empty, we MUST use the path.
                        dev_id: str = serial if (serial and serial != "UNKNOWN") else path

                        # Deduplicate: dfu-util -l lists multiple alt settings for the same device
                        if any(d['id'] == dev_id for d in devices):
                            continue

                        devices.append({
                            "id": dev_id,
                            "name": name,
                            "type": "dfu",
                            "vid_pid": vid_pid,
                            "path": path,
                            "serial": serial,
                            "mode": "ready"
                        })
            except Exception as e:
                print(f"Error discovering DFU devices: {e}")

            self._dfu_cache = list(devices)
            self._dfu_cache_time = now
            return list(devices)

    async def _get_moonraker_mcus(self) -> Dict[str, Dict[str, str]]:
        """Queries Moonraker for configured MCUs and their current status."""
        mcus = {}
        try:
            async with httpx.AsyncClient() as client:
                # 1. Query configfile to get all configured MCUs
                response: httpx.Response = await client.get("http://127.0.0.1:7125/printer/objects/query?configfile", timeout=2.0)
                config = {}
                if response.status_code == 200:
                    data = response.json()
                    config = data.get("result", {}).get("status", {}).get("configfile", {}).get("config", {})
                
                # 2. Query all mcu and canbus_stats objects
                # We first need to know which ones exist
                list_response: httpx.Response = await client.get("http://127.0.0.1:7125/printer/objects/list", timeout=2.0)
                can_stats = {}
                mcu_statuses = {}
                if list_response.status_code == 200:
                    all_objects = list_response.json().get("result", {}).get("objects", [])
                    stat_objects = [obj for obj in all_objects if obj.startswith("canbus_stats")]
                    mcu_objects = [obj for obj in all_objects if obj.startswith("mcu")]
                    
                    query_objects = stat_objects + mcu_objects
                    if query_objects:
                        query_url = f"http://127.0.0.1:7125/printer/objects/query?{'&'.join(query_objects)}"
                        stats_response: httpx.Response = await client.get(query_url, timeout=2.0)
                        if stats_response.status_code == 200:
                            raw_data = stats_response.json().get("result", {}).get("status", {})
                            for obj_name, obj_data in raw_data.items():
                                if obj_name.startswith("canbus_stats"):
                                    section = obj_name.replace("canbus_stats ", "").strip()
                                    can_stats[section] = obj_data
                                elif obj_name.startswith("mcu"):
                                    section = obj_name # e.g. "mcu" or "mcu toolhead"
                                    mcu_statuses[section] = obj_data

                for section_name, section_data in config.items():
                    if not isinstance(section_data, dict):
                        continue
                    
                    identifier = None
                    if "canbus_uuid" in section_data:
                        identifier = section_data["canbus_uuid"].lower().strip()
                    elif "serial" in section_data:
                        identifier = section_data["serial"].strip()
                    
                    if identifier:
                        # Check if this MCU is active
                        is_active = False
                        stats = {}
                        
                        # 1. Check canbus_stats (for CAN nodes)
                        stats_key = section_name
                        if section_name.startswith("mcu "):
                            stats_key = section_name[4:].strip()
                        
                        if stats_key in can_stats:
                            stats = can_stats[stats_key]
                            if stats.get('bus_state') == "Connected":
                                is_active = True
                        elif identifier in can_stats:
                            stats = can_stats[identifier]
                            if stats.get('bus_state') == "Connected":
                                is_active = True
                        
                        # 2. Check mcu status (for serial/all nodes)
                        # If it has an mcu_version, it's connected and active
                        if not is_active:
                            mcu_key = section_name
                            if mcu_key in mcu_statuses:
                                if mcu_statuses[mcu_key].get('mcu_version'):
                                    is_active = True
                        
                        mcus[identifier] = {
                            "name": section_name,
                            "active": is_active,
                            "stats": stats
                        }
        except Exception as e:
            print(f"Error querying Moonraker: {e}")
        return mcus

    async def get_mcu_versions(self) -> Dict[str, Dict[str, Any]]:
        """Queries Moonraker for MCU version information."""
        versions: Dict[str, Dict[str, Any]] = {}
        try:
            async with httpx.AsyncClient() as client:
                # Get list of all MCU objects
                list_response: httpx.Response = await client.get("http://127.0.0.1:7125/printer/objects/list", timeout=2.0)
                if list_response.status_code != 200:
                    return versions
                    
                all_objects = list_response.json().get("result", {}).get("objects", [])
                mcu_objects = [obj for obj in all_objects if obj.startswith("mcu")]
                
                if not mcu_objects:
                    return versions
                
                # Query all MCU objects for version info
                query_url = f"http://127.0.0.1:7125/printer/objects/query?{'&'.join(mcu_objects)}"
                mcu_response: httpx.Response = await client.get(query_url, timeout=2.0)
                if mcu_response.status_code != 200:
                    return versions
                    
                mcu_data = mcu_response.json().get("result", {}).get("status", {})
                
                # Also get configfile to map MCU names to identifiers
                config_response: httpx.Response = await client.get("http://127.0.0.1:7125/printer/objects/query?configfile", timeout=2.0)
                config = {}
                if config_response.status_code == 200:
                    config = config_response.json().get("result", {}).get("status", {}).get("configfile", {}).get("config", {})
                
                for mcu_name, mcu_info in mcu_data.items():
                    version = mcu_info.get("mcu_version", "unknown")
                    
                    # Find the identifier (canbus_uuid or serial) for this MCU
                    identifier = None
                    config_section = config.get(mcu_name, {})
                    if "canbus_uuid" in config_section:
                        identifier = config_section["canbus_uuid"].lower().strip()
                    elif "serial" in config_section:
                        identifier = config_section["serial"].strip()
                    
                    if identifier:
                        versions[identifier] = {
                            "name": mcu_name,
                            "version": version,
                            "mcu_constants": mcu_info.get("mcu_constants", {})
                        }
                    
                    # Also store by name for easy lookup
                    versions[mcu_name] = {
                        "name": mcu_name,
                        "version": version,
                        "identifier": identifier,
                        "mcu_constants": mcu_info.get("mcu_constants", {})
                    }
                    
        except Exception as e:
            print(f"Error querying MCU versions: {e}")
        return versions

    async def check_printer_printing(self) -> Dict[str, Any]:
        """Queries Moonraker's print_stats to determine if a print is in progress.
        
        Returns a dict with:
          - printing (bool): True if the printer is actively printing or paused.
          - state (str): The raw print_stats state (e.g. 'printing', 'paused', 'standby', 'complete', 'error', 'unknown').
          - filename (str): The filename being printed, if any.
        """
        try:
            async with httpx.AsyncClient() as client:
                response: httpx.Response = await client.get(
                    "http://127.0.0.1:7125/printer/objects/query?print_stats", timeout=2.0
                )
                if response.status_code == 200:
                    data = response.json()
                    stats = data.get("result", {}).get("status", {}).get("print_stats", {})
                    state = stats.get("state", "unknown")
                    filename = stats.get("filename", "")
                    return {
                        "printing": state in ("printing", "paused"),
                        "state": state,
                        "filename": filename,
                    }
        except Exception:
            pass
        return {"printing": False, "state": "unknown", "filename": ""}

    async def trigger_firmware_restart(self) -> None:
        """Sends a FIRMWARE_RESTART command to Klipper via Moonraker."""
        try:
            async with httpx.AsyncClient() as client:
                await client.post("http://localhost:7125/printer/gcode/script?script=FIRMWARE_RESTART", timeout=2.0)
        except Exception as e:
            print(f"Error sending FIRMWARE_RESTART: {e}")

    async def ensure_canbus_up(self, interface: str = "can0", bitrate: int = 1000000) -> None:
        """Ensures the CAN interface is up."""
        try:
            # Check if up
            process: Process = await asyncio.create_subprocess_exec(
                "ip", "link", "show", interface,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await process.communicate()
            if b"state UP" not in stdout:
                print(f"Bringing up {interface}...")
                process = await asyncio.create_subprocess_exec(
                    "sudo", "ip", "link", "set", interface, "up", "type", "can", "bitrate", str(bitrate)
                )
                await process.wait()
                await asyncio.sleep(1)
        except Exception as e:
            print(f"Error ensuring CAN up: {e}")

    async def list_can_interfaces(self) -> List[str]:
        """Lists all CAN interfaces present in the system."""
        try:
            can_interfaces: List[str] = []
            process: Process = await asyncio.create_subprocess_exec(
                "ip", "link", "show", "type", "can",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await process.communicate()
            for line in stdout.decode().splitlines():
                if ": " in line:
                    iface: str = line.split(":")[1].strip().split("@")[0]
                    can_interfaces.append(iface)
            return can_interfaces
        except Exception as e:
            print(f"Error listing CAN interfaces: {e}")
            return []

    async def discover_can_devices(self, skip_moonraker: bool = False, force: bool = False) -> List[Dict[str, str]]:
        """List canbus devices present in the system, and collect all the devices on each bus"""
        try:
            # use the ip tool to get each can interface
            can_interfaces: List[str] = await self.list_can_interfaces()
            devices: List[Dict[str, str]] = []
            for iface in can_interfaces:
                devices_on_iface: List[Dict[str, str]] = await self.discover_can_devices_with_interface(skip_moonraker=skip_moonraker, force=force, interface=iface)
                for dev in devices_on_iface:
                    devices.append(dev)
                skip_moonraker = True # Only query Moonraker once for the first interface if at all
            return devices
        except Exception as e:
            print(f"Error discovering CAN devices: {e}")
            return []

    async def discover_can_devices_with_interface(self, skip_moonraker: bool = False, force: bool = False, interface: str = "can0") -> List[Dict[str, str]]:
        """Discovers CAN devices using Klipper's canbus_query.py, Katapult's flashtool.py, and Moonraker API in parallel."""
        await self.ensure_canbus_up(interface=interface)
        
        now: float = asyncio.get_event_loop().time()
        if not force and (now - self._can_cache_time.get(interface, 0.0)) < self._can_cache_ttl_s:
            return list(self._can_cache.get(interface, []))

        async with self._can_lock:
            print(">>> CAN Lock Acquired for discovery")
            seen_uuids = {} # uuid -> device_dict

            async def run_klipper_query():
                try:
                    klipper_python: str = os.path.abspath(os.path.join(self.klipper_dir, "..", "klippy-env", "bin", "python3"))
                    if not os.path.exists(klipper_python):
                        klipper_python = "python3"
                    
                    process: Process = await asyncio.create_subprocess_exec(
                        klipper_python, os.path.join(self.klipper_dir, "scripts", "canbus_query.py"), interface,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, _ = await asyncio.wait_for(process.communicate(), timeout=2.0)
                    results = []
                    for line in stdout.decode().splitlines():
                        if "canbus_uuid=" in line:
                            uuid: str = line.split("canbus_uuid=")[1].split(",")[0].strip()
                            app = "Unknown"
                            if "Application:" in line:
                                app: str = line.split("Application:")[1].strip()
                            results.append((uuid, app))
                    return results
                except Exception:
                    return []

            async def run_katapult_query():
                try:
                    process: Process = await asyncio.create_subprocess_exec(
                        "python3", os.path.join(self.katapult_dir, "scripts", "flashtool.py"), "-i", interface, "-q",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5.0)
                    results = []
                    output: str = stdout.decode()
                    for line in output.splitlines():
                        if "UUID:" in line or "Detected UUID:" in line:
                            parts: List[str] = line.replace("Detected UUID:", "UUID:").split(",")
                            uuid: str = parts[0].split("UUID:")[1].strip()
                            app = "Unknown"
                            if len(parts) > 1 and "Application:" in parts[1]:
                                app: str = parts[1].split("Application:")[1].strip()
                            results.append((uuid, app))
                    return results
                except Exception as e:
                    print(f"Katapult query error: {e}")
                    return []

            # Run discovery methods sequentially to avoid CAN bus contention
            katapult_res = await run_katapult_query()
            klipper_res = await run_klipper_query()
            
            moonraker_res = {}
            if not skip_moonraker:
                moonraker_res: Dict[str, Dict[str, str]] = await self._get_moonraker_mcus()

            # Merge results (Priority: Katapult > Klipper > Moonraker)
            # 1. Katapult results (most accurate for bootloader status)
            for uuid, app in katapult_res:
                # If application is Klipper, it's in service. If Katapult/CanBoot, it's ready.
                mode: str = "ready" if app.lower() in ["katapult", "canboot"] else "service"
                seen_uuids[uuid] = {
                    "id": uuid, 
                    "name": f"CAN Device ({uuid})", 
                    "application": app,
                    "mode": mode,
                    "interface": interface
                }

            # 2. Klipper results
            for uuid, app in klipper_res:
                if uuid not in seen_uuids:
                    seen_uuids[uuid] = {
                        "id": uuid, 
                        "name": f"CAN Device ({uuid})", 
                        "application": app,
                        "mode": "service",
                        "interface": interface
                    }

            # 3. Moonraker results (name enrichment and fallback)
            if isinstance(moonraker_res, dict):
                for identifier, info in moonraker_res.items():
                    # Check if identifier looks like a UUID (12 hex chars)
                    if len(identifier) == 12 and all(c in '0123456789abcdef' for c in identifier):
                        section_name = info["name"]
                        if identifier in seen_uuids:
                            if "CAN Device" in seen_uuids[identifier]["name"]:
                                seen_uuids[identifier]["name"] = section_name
                        else:
                            # Fallback: Add it as 'service' if Moonraker knows about it
                            # But only if it's actually active, otherwise mark as offline
                            mode = "service" if info.get("active") else "offline"
                            seen_uuids[identifier] = {
                                "id": identifier,
                                "name": section_name,
                                "application": "Klipper (Configured)" if info.get("active") else "Klipper (Offline)",
                                "mode": mode,
                                "interface": interface
                            }
                        
                        # Add stats if available
                        if info.get("stats"):
                            seen_uuids[identifier]["stats"] = info["stats"]

            results = list(seen_uuids.values())
            self._can_cache[interface] = results
            self._can_cache_time[interface] = asyncio.get_event_loop().time()
            
            print(">>> CAN Lock Released")
            return results

    def discover_linux_process(self) -> List[Dict[str, str]]:
        """Returns the local Linux process MCU if it exists or as a target."""
        # Klipper's host MCU usually uses /tmp/klipper_host_mcu
        # We'll return it as a discoverable 'device'
        return [{
            "id": "linux_process",
            "name": "Linux Process (Host MCU)"
        }]

    async def is_interface_up(self, interface: str = "can0") -> bool:
        """Checks if a network interface is UP and has a carrier."""
        try:
            process: Process = await asyncio.create_subprocess_exec(
                "ip", "link", "show", interface,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await process.communicate()
            output = stdout.decode()
            # Interface must be UP and NOT have NO-CARRIER
            is_up = "state UP" in output or "state UNKNOWN" in output
            has_carrier = "NO-CARRIER" not in output
            return is_up and has_carrier
        except Exception:
            return False

    def _extract_serial_from_id(self, device_id: str) -> Optional[str]:
        """Helper to extract a potential serial number from a device ID or path."""
        if not device_id:
            return None
            
        # 1. If it's a path, extract from filename
        if device_id.startswith("/dev/serial/by-id/"):
            filename = os.path.basename(device_id)
            # Heuristic: the serial is usually the longest part between underscores or before -if
            parts = filename.replace("-if", "_").split("_")
            # Filter out common prefixes/suffixes
            candidates = [p for p in parts if p not in ["usb", "Klipper", "katapult", "CanBoot", "00"]]
            if candidates:
                # Sort by length, longest is likely the serial
                return sorted(candidates, key=len, reverse=True)[0]
        
        # 2. If it's not a path and looks like a serial number (long enough, no slashes)
        if not "/" in device_id and len(device_id) > 5:
            return device_id
            
        return None

    async def resolve_dfu_id(self, device_id: str, known_dfu_id: Optional[str] = None, strict: bool = False) -> str:
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
            if d.get('serial') and target_serial and d['serial'] == target_serial:
                return d['id']
        
        # 4. Fallback: If there is only ONE DFU device connected, assume it is the target.
        # This handles cases where the DFU serial is generic (e.g. "STM32FxSTM32") or
        # does not match the Klipper serial number.
        # Skipped in strict mode to avoid flashing the wrong device.
        if not strict and len(devs) == 1:
            return devs[0]['id']

        return device_id

    async def resolve_serial_id(self, device_id: str, known_serial_id: Optional[str] = None) -> str:
        """Attempts to find a Serial ID that matches a DFU ID or a Klipper ID (via serial number)."""
        if known_serial_id and os.path.exists(known_serial_id):
            return known_serial_id

        # If it's already a serial device that exists, return it
        if os.path.exists(device_id):
            return device_id
            
        serials: List[Dict[str, str]] = await self.discover_serial_devices(skip_moonraker=True)
        
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

    async def check_device_status(self, device_id: str, method: str, dfu_id: Optional[str] = None, skip_moonraker: bool = False, is_bridge: bool = False, interface: str = "can0") -> str:
        """Checks if a device is reachable and its current mode."""
        method = method.lower()
        
        # Special handling for bridges
        if is_bridge:
            # 1. Check if it's in Katapult mode (Serial)
            serials = await self.discover_serial_devices(skip_moonraker=True)
            target_serial = self._extract_serial_from_id(device_id)
            for s in serials:
                # Match by ID exactly
                if s['id'] == device_id:
                    return "ready"
                # Or match by serial number if we have one
                if target_serial and target_serial in s['id']:
                    return "ready"
            
            # 2. Check if it's in DFU mode
            # Only if we have a reason to look for DFU (e.g. dfu_id provided)
            if dfu_id:
                dfus = await self.discover_dfu_devices()
                resolved_dfu_id = await self.resolve_dfu_id(device_id, known_dfu_id=dfu_id)
                if any(d['id'] == resolved_dfu_id for d in dfus):
                    return "ready"

            # 3. Check if the interface is up (In Service)
            # But only if the hardware is actually present
            if method == "serial" or device_id.startswith("/dev/"):
                # For serial bridges, the serial device MUST exist
                if os.path.exists(device_id) and await self.is_interface_up(interface):
                    return "service"
            else:
                # For CAN-based bridges (identified by UUID), we check Moonraker
                # or if the interface is up (if Moonraker is skipped)
                if await self.is_interface_up(interface):
                    if not skip_moonraker:
                        mcus = await self._get_moonraker_mcus()
                        if device_id in mcus and mcus[device_id]['active']:
                            return "service"
                        # If Moonraker says it's NOT active, then it's not in service
                        # even if the interface is up (might be another device)
                        return "offline"
                    
                    # If skipping moonraker, we can't be 100% sure, but if the interface is up
                    # and it's a bridge, it's likely "in service" (providing the bus)
                    return "service"
            
            return "offline"

        if method == "serial":
            if os.path.exists(device_id):
                # Check if it's Klipper or Katapult
                if "katapult" in device_id.lower() or "canboot" in device_id.lower():
                    return "ready"
                return "service" # Assume Klipper if it exists and isn't katapult
            
            # 1. Check if it's currently in DFU mode
            resolved_dfu_id: str = await self.resolve_dfu_id(device_id, known_dfu_id=dfu_id)
            if resolved_dfu_id != device_id:
                return "dfu"

            # 2. If the path doesn't exist, it might have changed ID (e.g. Klipper -> Katapult)
            resolved_id: str = await self.resolve_serial_id(device_id)
            if resolved_id != device_id and os.path.exists(resolved_id):
                if "katapult" in resolved_id.lower() or "canboot" in resolved_id.lower():
                    return "ready"
                return "service"

            return "offline"
        elif method == "can":
            devs: List[Dict[str, str]] = await self.discover_can_devices(skip_moonraker=skip_moonraker)
            for d in devs:
                if d['id'] == device_id:
                    return d.get('mode', 'offline')
            
            # Check if it's currently in DFU mode (if it has a dfu_id)
            if dfu_id:
                resolved_dfu_id: str = await self.resolve_dfu_id(device_id, known_dfu_id=dfu_id)
                if resolved_dfu_id != device_id:
                    return "dfu"

            return "offline"
        elif method == "dfu":
            # 1. Check if it's actually in DFU mode
            resolved_dfu_id: str = await self.resolve_dfu_id(device_id, known_dfu_id=dfu_id)
            devs: List[Dict[str, str]] = await self.discover_dfu_devices()
            if any(d['id'] == resolved_dfu_id for d in devs):
                return "dfu"
            
            # 2. Check if it's in Serial mode (In Service)
            serial_id: str = await self.resolve_serial_id(device_id)
            if os.path.exists(serial_id):
                return "service"
                
            return "offline"
        elif method == "linux":
            return "service" if os.path.exists("/tmp/klipper_host_mcu") else "ready"
        return "unknown"

    async def reboot_device(self, device_id: str, mode: str = "katapult", method: str = "can", interface: str = "can0", is_bridge: bool = False) -> AsyncGenerator[str, None]:
        """Reboots a device, either to Katapult, DFU, or a regular reboot."""
        if mode == "katapult":
            async for line in self.reboot_to_katapult(device_id, method=method, interface=interface, is_bridge=is_bridge):
                yield line
        elif mode == "dfu":
            async for line in self.reboot_to_dfu(device_id):
                yield line
        else:
            # Regular reboot (Return to Service)
            
            # If it's a serial device, check if it's actually in DFU mode right now
            if method == "serial":
                dfus = await self.discover_dfu_devices()
                resolved_dfu_id = await self.resolve_dfu_id(device_id)
                if any(d['id'] == resolved_dfu_id for d in dfus):
                    method = "dfu"
                    device_id = resolved_dfu_id
                    yield f">>> Detected {device_id} in DFU mode. Using DFU reboot.\n"

            if method == "can":
                yield f">>> Requesting regular reboot for {device_id}...\n"
                # Regular reboot (Return to Service)
                # We send a Katapult 'COMPLETE' command to jump to the application.
                # This requires assigning a temporary node ID first.
                # Pass device_id and interface as command-line args to avoid injection.
                py_cmd: str = """
import socket
import struct
import sys
import time

interface = sys.argv[1]
device_id = sys.argv[2]

def crc16_ccitt(buf):
    crc = 0xffff
    for data in buf:
        data ^= crc & 0xff
        data ^= (data & 0x0f) << 4
        crc = ((data << 8) | (crc >> 8)) ^ (data >> 4) ^ (data << 3)
    return crc & 0xFFFF

def send_can(id, data):
    try:
        with socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW) as s:
            s.bind((interface,))
            can_pkt = struct.pack("<IB3x8s", id, len(data), data.ljust(8, b'\\x00'))
            s.send(can_pkt)
    except Exception as e:
        print(f"Socket error: {e}")

uuid_bytes = bytes.fromhex(device_id)

set_id_payload = bytes([0x11]) + uuid_bytes + bytes([128])
send_can(0x3f0, set_id_payload)
time.sleep(0.1)

cmd_body = bytes([0x15, 0x00])
crc = crc16_ccitt(cmd_body)
pkt = bytes([0x01, 0x88]) + cmd_body + struct.pack("<H", crc) + bytes([0x99, 0x03])
send_can(0x200, pkt)
print(f"Jump command sent to UUID {device_id}")
"""
                process: Process = await asyncio.create_subprocess_exec(
                    "python3", "-c", py_cmd, interface, device_id,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT
                )
                stdout, _ = await process.communicate()
                yield stdout.decode()
                yield ">>> Regular reboot command sent.\n"
            elif method == "dfu":
                yield f">>> Requesting reboot for DFU device {device_id}...\n"
                # For STM32 DFU, the most reliable way to "leave" DFU mode without flashing
                # is to perform a dummy upload with the ':leave' flag.
                # We use 0x08000000 as the default start address for STM32.
                cmd: List[str] = ["dfu-util", "-d", "0483:df11", "-a", "0", "-s", "0x08000000:leave", "-U", "/tmp/reboot_dummy", "-Z", "1"]
                
                # Try to disambiguate if possible
                if ":" not in device_id and "/" in device_id: # Path
                    cmd.extend(["-p", device_id])
                elif ":" not in device_id: # Serial
                    cmd.extend(["-S", device_id])

                # Ensure the dummy file doesn't block the upload
                if os.path.exists("/tmp/reboot_dummy"):
                    os.remove("/tmp/reboot_dummy")

                process: Process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT
                )
                stdout, _ = await process.communicate()
                yield stdout.decode()
                if process.returncode == 0:
                    yield ">>> Leave request sent. Device should reboot into firmware shortly.\n"
                else:
                    yield ">>> Reboot command failed. The bootloader may not support software reset via DFU.\n"
            else:
                # For serial, we can try sending the reboot command via flashtool
                # but usually serial devices jump to app after flash or timeout.
                yield f">>> Serial device {device_id} will return to service after flash or timeout.\n"

    async def reboot_to_katapult(self, device_id: str, method: str = "can", interface: str = "can0", is_bridge: bool = False, baudrate: int = 250000) -> AsyncGenerator[str, None]:
        """Sends a reboot command to a device to enter Katapult."""
        yield f">>> Requesting reboot to Katapult for {device_id}...\n"
        method = method.lower()
        if method == "can":
            async with self._can_lock:
                yield f">>> CAN Lock Acquired for rebooting {device_id}\n"
                # Using flashtool.py -r is much more reliable for all CAN nodes
                cmd: List[str] = [
                    "python3", os.path.join(self.katapult_dir, "scripts", "flashtool.py"),
                    "-i", interface,
                    "-u", device_id,
                    "-r"
                ]
                process: Process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT
                )
                stdout, _ = await process.communicate()
                yield stdout.decode()
                self._can_cache_time[interface] = 0.0 # Invalidate cache
                yield f">>> CAN Lock Released\n"
                return

        # Serial method
        # 1. Try the 1200bps trick first (common for Katapult/CanBoot on Serial)
        yield f">>> Attempting 1200bps magic baud on {device_id}...\n"
        try:
            import serial
            ser = serial.Serial(device_id, 1200)
            ser.close()
            await asyncio.sleep(2) # Give it time to reboot
            
            # If the device path is gone, the trick worked and the device is rebooting
            if not os.path.exists(device_id):
                yield ">>> Device disconnected (1200bps trick successful). Waiting for bootloader...\n"
                return
        except Exception as e:
            yield f">>> 1200bps trick skipped or failed: {str(e)}\n"

        # 2. Also try flashtool.py -r as a backup if the device still exists
        if os.path.exists(device_id):
            cmd: List[str] = [
                "python3", os.path.join(self.katapult_dir, "scripts", "flashtool.py"),
                "-d", device_id,
                "-b", str(baudrate),
                "-r"
            ]
            
            process: Process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
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
                yield ">>> Reboot command sent. Device should appear in Katapult mode shortly.\n"
            else:
                yield f">>> Reboot command failed with return code {process.returncode}. Device might already be in Katapult or unreachable.\n"
        else:
            yield ">>> Device path not found. It may already be in bootloader mode.\n"

    async def reboot_to_dfu(self, device_id: str) -> AsyncGenerator[str, None]:
        """Attempts to reboot a device into DFU mode using the 1200bps magic baud rate."""
        # If device_id is a DFU ID, but the device is in Serial mode, resolve it first
        actual_id: str = await self.resolve_serial_id(device_id)
        yield f">>> Attempting to reboot {actual_id} into DFU mode (1200bps trick)...\n"
        try:
            # The 1200bps trick: open and close the port at 1200bps
            import serial
            ser = serial.Serial(actual_id, 1200)
            ser.close()
            yield ">>> 1200bps magic baud sent. Waiting 3s for USB enumeration...\n"
            await asyncio.sleep(3)
        except Exception as e:
            yield f">>> Error sending 1200bps magic baud: {str(e)}\n"
            yield ">>> Please manually enter DFU mode (BOOT0 + RESET) if the device does not appear.\n"

    async def flash_serial(self, device_id: str, firmware_path: str, baudrate: int = 250000) -> AsyncGenerator[str, None]:
        """Flashes a device via Serial using Katapult."""
        yield f">>> Flashing {firmware_path} to {device_id} via Serial (baud {baudrate})...\n"
        cmd: List[str] = [
            "python3", os.path.join(self.katapult_dir, "scripts", "flashtool.py"),
            "-f", firmware_path,
            "-d", device_id,
            "-b", str(baudrate)
        ]
        async for line in self._run_flash_command(cmd):
            yield line

    async def flash_can(self, uuid: str, firmware_path: str, interface: str = "can0") -> AsyncGenerator[str, None]:
        """Flashes a device via CAN using Katapult."""
        async with self._can_lock:
            yield f">>> CAN Lock Acquired for flashing {uuid}\n"
            yield f">>> Flashing {firmware_path} to {uuid} via {interface}...\n"
            cmd: List[str] = [
                "python3", os.path.join(self.katapult_dir, "scripts", "flashtool.py"),
                "-i", interface,
                "-u", uuid,
                "-f", firmware_path
            ]
            async for line in self._run_flash_command(cmd):
                yield line
            self._can_cache_time[interface] = 0.0 # Invalidate cache
            yield f">>> CAN Lock Released\n"

    async def flash_dfu(self, device_id: str, firmware_path: str, address: str = "0x08000000", leave: bool = True) -> AsyncGenerator[str, None]:
        """Flashes a device in DFU mode using dfu-util."""
        yield f">>> Flashing {firmware_path} via DFU to {address} (Leave: {leave})...\n"

        # Prevent concurrent dfu-util calls (like UI polling dfu-util -l) while flashing.
        async with self._dfu_lock:
            # Invalidate any cached dfu-util -l results since the device will transition.
            self._dfu_cache_time = 0.0

            # We try to be specific if we have a serial or path
            # device_id here could be the serial number or the path from discover_dfu_devices

            # STRATEGY: We perform the download WITHOUT the :leave modifier first.
            # Some STM32 bootloaders (like F446) can timeout on the 'get_status' call
            # after a long erase/write if the :leave modifier is present.
            # We also avoid mass-erase by default to preserve bootloaders.
            cmd: List[str] = ["sudo", "dfu-util", "-a", "0", "-d", "0483:df11", "-s", address, "-D", firmware_path]

            # If device_id looks like a serial number (usually alphanumeric, long)
            # AND it is not a path (does not start with /dev/)
            if device_id and len(device_id) > 5 and not device_id.startswith("/dev/"):
                cmd.extend(["-S", device_id])
            # If it looks like a path (e.g. 1-1.2)
            elif device_id and "-" in device_id and not device_id.startswith("/dev/"):
                cmd.extend(["-p", device_id])
            elif device_id and device_id.startswith("/dev/"):
                yield f">>> WARNING: Device ID '{device_id}' looks like a serial path, not a DFU ID. Skipping specific device selection.\n"

            # Retry mechanism for the download phase
            max_retries = 3
            flash_success = False
            for attempt in range(max_retries):
                try:
                    if attempt > 0:
                        yield f">>> Retry attempt {attempt + 1}/{max_retries}...\n"
                        await asyncio.sleep(2)
                        # Re-resolve DFU device ID in case USB re-enumerated
                        self._dfu_cache_time = 0.0
                        new_devs = await self.discover_dfu_devices()
                        if new_devs:
                            # Rebuild the command with the potentially new device ID
                            cmd = ["sudo", "dfu-util", "-a", "0", "-d", "0483:df11", "-s", address, "-D", firmware_path]
                            resolved = new_devs[0]  # Best effort: pick the first DFU device
                            for d in new_devs:
                                if d['id'] == device_id or d.get('serial') == device_id:
                                    resolved = d
                                    break
                            rid = resolved['id']
                            if rid and len(rid) > 5 and not rid.startswith("/dev/"):
                                cmd.extend(["-S", rid])
                            elif rid and "-" in rid and not rid.startswith("/dev/"):
                                cmd.extend(["-p", rid])

                    current_success = False
                    async for line in self._run_flash_command(cmd):
                        yield line
                        if ">>> Flashing successful!" in line:
                            current_success = True

                    if current_success:
                        flash_success = True
                        break
                except Exception as e:
                    yield f">>> Error during flash attempt {attempt + 1}: {e}\n"

            if not flash_success:
                yield "!!! Flash operation failed after multiple attempts.\n"
                return

            # If successful and leave is requested, send a separate tiny command to exit DFU
            if leave:
                yield ">>> Sending DFU leave request to reboot device...\n"
                # We use a 0-length download to the base address with :leave to trigger a reset
                # We use -R as well for extra robustness on some bootloaders
                leave_cmd: List[str] = ["sudo", "dfu-util", "-a", "0", "-d", "0483:df11", "-R", "-s", f"{address}:leave"]
                if device_id and len(device_id) > 5 and not device_id.startswith("/dev/"):
                    leave_cmd.extend(["-S", device_id])
                elif device_id and "-" in device_id and not device_id.startswith("/dev/"):
                    leave_cmd.extend(["-p", device_id])

                # Run the leave command
                # dfu-util can return non-zero here (commonly 251) because the device disconnects
                # during detach/reset while switching back to runtime. Treat that as success.
                async for line in self._run_flash_command(leave_cmd, ok_returncodes={0, 251}):
                    if "Flashing successful" in line or "Done" in line:
                        yield ">>> Device rebooted successfully.\n"
                    else:
                        yield line

            yield ">>> Flash operation complete.\n"
            self._dfu_cache_time = 0.0

    async def flash_linux(self, firmware_path: str) -> AsyncGenerator[str, None]:
        """'Flashes' the Linux process by installing the binary to /usr/local/bin/klipper_mcu."""
        yield f">>> Installing Linux MCU binary: {firmware_path}...\n"
        try:
            # 1. Ensure service is stopped and file is not busy
            stop_proc: Process = await asyncio.create_subprocess_exec(
                "sudo", "systemctl", "stop", "klipper-mcu.service",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            stop_out, _ = await stop_proc.communicate()
            if stop_proc.returncode != 0:
                yield f">>> WARNING: Could not stop klipper-mcu.service (rc={stop_proc.returncode}): {stop_out.decode().strip()}\n"
            
            # Kill any remaining processes using the file
            fuser_proc: Process = await asyncio.create_subprocess_exec(
                "sudo", "fuser", "-k", "/usr/local/bin/klipper_mcu",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            await fuser_proc.communicate()
            await asyncio.sleep(2)
            
            # 2. Copy to /usr/local/bin/klipper_mcu
            cmd: List[str] = ["sudo", "cp", firmware_path, "/usr/local/bin/klipper_mcu"]
            process: Process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            stdout, _ = await process.communicate()
            if process.returncode != 0:
                yield f"!!! Error copying binary: {stdout.decode()}\n"
                return

            # 2. Ensure it's executable
            cmd = ["sudo", "chmod", "+x", "/usr/local/bin/klipper_mcu"]
            process = await asyncio.create_subprocess_exec(*cmd)
            await process.wait()

            yield ">>> Linux MCU binary installed successfully.\n"
        except Exception as e:
            yield f"!!! Error during Linux MCU installation: {str(e)}\n"

    async def _run_flash_command(self, cmd: list, ok_returncodes: Optional[Set[int]] = None) -> AsyncGenerator[str, None]:
        if ok_returncodes is None:
            ok_returncodes = {0}
        process: Process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
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
            yield ">>> Flashing successful!\n"
        else:
            yield f">>> Flashing failed with return code {process.returncode}\n"
