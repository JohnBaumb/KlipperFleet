import subprocess
import os
import asyncio
import glob
import httpx
from typing import List, Dict, Any, AsyncGenerator

class FlashManager:
    def __init__(self, klipper_dir: str, katapult_dir: str):
        self.klipper_dir = klipper_dir
        self.katapult_dir = katapult_dir

    async def discover_serial_devices(self, skip_moonraker: bool = False) -> List[Dict[str, str]]:
        """Lists all serial devices in /dev/serial/by-id/ and common UART ports."""
        devices = []
        
        # 1. USB Serial devices (by-id is preferred for stability)
        usb_devs = glob.glob("/dev/serial/by-id/*")
        
        # 2. Common UART and CDC-ACM devices
        # We include /dev/ttyACM* and /dev/ttyUSB* because some devices (especially in Katapult)
        # might not immediately get a by-id link or might be generic.
        candidates = glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*") + ["/dev/ttyAMA0", "/dev/ttyS0"]
        
        moonraker_mcus = {}
        if not skip_moonraker:
            moonraker_mcus = await self._get_moonraker_mcus()
        
        # Combine and deduplicate
        # We use absolute paths for everything
        all_devs = list(set(usb_devs + [os.path.abspath(d) for d in candidates if os.path.exists(d)]))
        
        for dev in all_devs:
            name = os.path.basename(dev)
            is_configured = dev in moonraker_mcus
            
            # If it's a by-id device, it's almost certainly an MCU
            if dev.startswith("/dev/serial/by-id/"):
                if is_configured:
                    name = f"{moonraker_mcus[dev]} ({name})"
                devices.append({"id": dev, "name": name, "type": "usb"})
            
            # If it's a ttyACM/ttyUSB device, we show it if it's NOT already represented by a by-id link
            # or if it's configured in Klipper.
            elif dev.startswith("/dev/ttyACM") or dev.startswith("/dev/ttyUSB"):
                # Check if this physical device is already in devices via by-id
                # (This is a bit tricky, but usually by-id is a symlink to ttyACM/USB)
                real_path = os.path.realpath(dev)
                already_added = False
                for d in devices:
                    if os.path.realpath(d['id']) == real_path:
                        already_added = True
                        break
                
                if not already_added:
                    if is_configured:
                        name = f"{moonraker_mcus[dev]} ({name})"
                    devices.append({"id": dev, "name": name, "type": "usb"})

            # If it's a raw UART device, only show it if it's actually configured in Klipper
            elif is_configured:
                name = f"{moonraker_mcus[dev]} ({name})"
                devices.append({"id": dev, "name": name, "type": "uart"})
                
        return devices

    async def _get_moonraker_mcus(self) -> Dict[str, str]:
        """Queries Moonraker for configured MCUs (CAN UUIDs and Serial paths)."""
        mcus = {}
        try:
            async with httpx.AsyncClient() as client:
                # Query configfile to get all configured MCUs
                response = await client.get("http://localhost:7125/printer/objects/query?configfile", timeout=2.0)
                if response.status_code == 200:
                    data = response.json()
                    config = data.get("result", {}).get("status", {}).get("configfile", {}).get("config", {})
                    for section_name, section_data in config.items():
                        if not isinstance(section_data, dict):
                            continue
                        
                        if "canbus_uuid" in section_data:
                            uuid = section_data["canbus_uuid"].lower().strip()
                            mcus[uuid] = section_name
                        
                        if "serial" in section_data:
                            serial_path = section_data["serial"].strip()
                            mcus[serial_path] = section_name
        except Exception as e:
            print(f"Error querying Moonraker: {e}")
        return mcus

    async def trigger_firmware_restart(self):
        """Sends a FIRMWARE_RESTART command to Klipper via Moonraker."""
        try:
            async with httpx.AsyncClient() as client:
                await client.post("http://localhost:7125/printer/gcode/script?script=FIRMWARE_RESTART", timeout=2.0)
        except Exception as e:
            print(f"Error sending FIRMWARE_RESTART: {e}")

    async def ensure_canbus_up(self, interface: str = "can0", bitrate: int = 1000000):
        """Ensures the CAN interface is up."""
        try:
            # Check if up
            process = await asyncio.create_subprocess_exec(
                "ip", "link", "show", interface,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await process.communicate()
            if b"state UP" not in stdout:
                print(f"Bringing up {interface}...")
                await asyncio.create_subprocess_exec(
                    "sudo", "ip", "link", "set", interface, "up", "type", "can", "bitrate", str(bitrate)
                )
                await asyncio.sleep(1)
        except Exception as e:
            print(f"Error ensuring CAN up: {e}")

    async def discover_can_devices(self) -> List[Dict[str, str]]:
        """Discovers CAN devices using Klipper's canbus_query.py, Katapult's flashtool.py, and Moonraker API in parallel."""
        await self.ensure_canbus_up()
        seen_uuids = {} # uuid -> device_dict

        async def run_klipper_query():
            try:
                klipper_python = os.path.abspath(os.path.join(self.klipper_dir, "..", "klippy-env", "bin", "python3"))
                if not os.path.exists(klipper_python):
                    klipper_python = "python3"
                
                process = await asyncio.create_subprocess_exec(
                    klipper_python, os.path.join(self.klipper_dir, "scripts", "canbus_query.py"), "can0",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=2.0)
                results = []
                for line in stdout.decode().splitlines():
                    if "canbus_uuid=" in line:
                        uuid = line.split("canbus_uuid=")[1].split(",")[0].strip()
                        app = "Unknown"
                        if "Application:" in line:
                            app = line.split("Application:")[1].strip()
                        results.append((uuid, app))
                return results
            except Exception:
                return []

        async def run_katapult_query():
            try:
                process = await asyncio.create_subprocess_exec(
                    "python3", os.path.join(self.katapult_dir, "scripts", "flashtool.py"), "-i", "can0", "-q",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5.0)
                results = []
                output = stdout.decode()
                for line in output.splitlines():
                    if "UUID:" in line or "Detected UUID:" in line:
                        parts = line.replace("Detected UUID:", "UUID:").split(",")
                        uuid = parts[0].split("UUID:")[1].strip()
                        app = "Unknown"
                        if len(parts) > 1 and "Application:" in parts[1]:
                            app = parts[1].split("Application:")[1].strip()
                        results.append((uuid, app))
                return results
            except Exception as e:
                print(f"Katapult query error: {e}")
                return []

        # Run discovery methods sequentially to avoid CAN bus contention
        katapult_res = await run_katapult_query()
        klipper_res = await run_klipper_query()
        moonraker_res = await self._get_moonraker_mcus()

        # Merge results (Priority: Katapult > Klipper > Moonraker)
        # 1. Katapult results (most accurate for bootloader status)
        for uuid, app in katapult_res:
            # If application is Klipper, it's in service. If Katapult/CanBoot, it's ready.
            mode = "ready" if app.lower() in ["katapult", "canboot"] else "service"
            seen_uuids[uuid] = {
                "id": uuid, 
                "name": f"CAN Device ({uuid})", 
                "application": app,
                "mode": mode
            }

        # 2. Klipper results
        for uuid, app in klipper_res:
            if uuid not in seen_uuids:
                seen_uuids[uuid] = {
                    "id": uuid, 
                    "name": f"CAN Device ({uuid})", 
                    "application": app,
                    "mode": "service"
                }

        # 3. Moonraker results (fallback for active nodes and name enrichment)
        if isinstance(moonraker_res, dict):
            for identifier, section_name in moonraker_res.items():
                # If it's in seen_uuids, it's a CAN device we found via query
                if identifier in seen_uuids:
                    if "CAN Device" in seen_uuids[identifier]["name"]:
                        seen_uuids[identifier]["name"] = section_name
                # If it's not in seen_uuids but looks like a UUID, add it as a Klipper device
                elif len(identifier) == 12 and all(c in '0123456789abcdef' for c in identifier):
                    seen_uuids[identifier] = {
                        "id": identifier, 
                        "name": section_name, 
                        "application": "Klipper",
                        "mode": "service"
                    }

        return list(seen_uuids.values())

    def discover_linux_process(self) -> List[Dict[str, str]]:
        """Returns the local Linux process MCU if it exists or as a target."""
        # Klipper's host MCU usually uses /tmp/klipper_host_mcu
        # We'll return it as a discoverable 'device'
        return [{
            "id": "linux_process",
            "name": "Linux Process (Host MCU)"
        }]

    async def is_interface_up(self, interface: str = "can0") -> bool:
        """Checks if a network interface is UP."""
        try:
            process = await asyncio.create_subprocess_exec(
                "ip", "link", "show", interface,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await process.communicate()
            return b"state UP" in stdout or b"state UNKNOWN" in stdout # UNKNOWN can happen for some virtual/bridge interfaces
        except Exception:
            return False

    async def check_device_status(self, device_id: str, method: str) -> str:
        """Checks if a device is reachable and its current mode."""
        method = method.lower()
        if method == "serial":
            return "ready" if os.path.exists(device_id) else "offline"
        elif method == "can":
            devs = await self.discover_can_devices()
            for d in devs:
                if d['id'] == device_id:
                    return d.get('mode', 'offline')
            
            return "offline"
        elif method == "linux":
            return "ready" if os.path.exists("/tmp/klipper_host_mcu") else "offline"
        return "unknown"

    async def reboot_device(self, device_id: str, mode: str = "katapult", method: str = "can", interface: str = "can0", is_bridge: bool = False) -> AsyncGenerator[str, None]:
        """Reboots a device, either to Katapult or a regular reboot."""
        if mode == "katapult":
            async for line in self.reboot_to_katapult(device_id, method=method, interface=interface, is_bridge=is_bridge):
                yield line
        else:
            if method == "can":
                yield f">>> Requesting regular reboot for {device_id}...\n"
                # Regular reboot (Return to Service)
                # We send a Katapult 'COMPLETE' command to jump to the application.
                # This requires assigning a temporary node ID first.
                py_cmd = f"""
import socket
import struct
import time

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
            s.bind(("{interface}",))
            # CAN_FMT = "<IB3x8s"
            can_pkt = struct.pack("<IB3x8s", id, len(data), data.ljust(8, b'\\x00'))
            s.send(can_pkt)
    except Exception as e:
        print(f"Socket error: {{e}}")

uuid_bytes = bytes.fromhex("{device_id}")

# 1. Set Node ID to 0x200 (index 128)
# Katapult Admin ID is always 0x3f0
set_id_payload = bytes([0x11]) + uuid_bytes + bytes([128])
send_can(0x3f0, set_id_payload)
time.sleep(0.1)

# 2. Send COMPLETE command (0x15) to Node ID 0x200
# Katapult packet: [0x01, 0x88, 0x15, 0x00, CRC_L, CRC_H, 0x99, 0x03]
cmd_body = bytes([0x15, 0x00])
crc = crc16_ccitt(cmd_body)
pkt = bytes([0x01, 0x88]) + cmd_body + struct.pack("<H", crc) + bytes([0x99, 0x03])
send_can(0x200, pkt)
print("Jump command sent to UUID {device_id}")
"""
                process = await asyncio.create_subprocess_exec(
                    "python3", "-c", py_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT
                )
                stdout, _ = await process.communicate()
                yield stdout.decode()
                yield ">>> Regular reboot command sent.\n"
            else:
                # For serial, we can try sending the reboot command via flashtool
                # but usually serial devices jump to app after flash or timeout.
                yield f">>> Serial device {device_id} will return to service after flash or timeout.\n"

    async def reboot_to_katapult(self, device_id: str, method: str = "can", interface: str = "can0", is_bridge: bool = False) -> AsyncGenerator[str, None]:
        """Sends a reboot command to a device to enter Katapult."""
        yield f">>> Requesting reboot to Katapult for {device_id}...\n"
        method = method.lower()
        if method == "can":
            # For bridges, flashtool.py -r is much more reliable as it handles USB reconnects
            if is_bridge:
                yield ">>> Bridge detected, using flashtool.py for reboot...\n"
                cmd = [
                    "python3", os.path.join(self.katapult_dir, "scripts", "flashtool.py"),
                    "-i", interface,
                    "-u", device_id,
                    "-r"
                ]
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT
                )
                stdout, _ = await process.communicate()
                yield stdout.decode()
                return

            # For regular CAN nodes, we try to send the raw jump command first as it's faster
            py_cmd = f"""
import socket, struct, sys
try:
    s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    s.bind(("{interface}",))
    uuid = bytes.fromhex("{device_id}")
    msg = bytes([0x16]) + uuid
    msg = msg + bytes([0] * (8 - len(msg)))
    frame = struct.pack("<IB3x8s", 0x3f0, 7, msg)
    s.send(frame)
    print("Jump command sent")
except Exception as e:
    print(f"Error: {{e}}", file=sys.stderr)
    sys.exit(1)
"""
            process = await asyncio.create_subprocess_exec(
                "python3", "-c", py_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            stdout, stderr = await process.communicate()
            if process.returncode == 0:
                yield stdout.decode()
            else:
                yield f"!!! Error sending jump command: {stdout.decode()}\n"
                # Fallback to flashtool.py
                yield ">>> Falling back to flashtool.py...\n"
                cmd = [
                    "python3", os.path.join(self.katapult_dir, "scripts", "flashtool.py"),
                    "-i", interface,
                    "-u", device_id,
                    "-r"
                ]
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT
                )
                stdout, _ = await process.communicate()
                yield stdout.decode()
            return

        # Fallback/Serial method
        cmd = [
            "python3", os.path.join(self.katapult_dir, "scripts", "flashtool.py"),
            "-d", device_id,
            "-r"
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        
        while True:
            if process.stdout is None:
                break
            line = await process.stdout.readline()
            if not line:
                break
            yield line.decode()

        await process.wait()
        if process.returncode == 0:
            yield ">>> Reboot command sent. Device should appear in Katapult mode shortly.\n"
        else:
            yield f">>> Reboot command failed with return code {process.returncode}. Device might already be in Katapult or unreachable.\n"

    async def flash_serial(self, device_id: str, firmware_path: str) -> AsyncGenerator[str, None]:
        """Flashes a device via Serial using Katapult."""
        yield f">>> Flashing {firmware_path} to {device_id} via Serial...\n"
        cmd = [
            "python3", os.path.join(self.katapult_dir, "scripts", "flashtool.py"),
            "-f", firmware_path,
            "-d", device_id
        ]
        async for line in self._run_flash_command(cmd):
            yield line

    async def flash_can(self, uuid: str, firmware_path: str, interface: str = "can0") -> AsyncGenerator[str, None]:
        """Flashes a device via CAN using Katapult."""
        yield f">>> Flashing {firmware_path} to {uuid} via {interface}...\n"
        cmd = [
            "python3", os.path.join(self.katapult_dir, "scripts", "flashtool.py"),
            "-i", interface,
            "-u", uuid,
            "-f", firmware_path
        ]
        async for line in self._run_flash_command(cmd):
            yield line

    async def flash_linux(self, firmware_path: str) -> AsyncGenerator[str, None]:
        """'Flashes' the Linux process by installing the binary to /usr/local/bin/klipper_mcu."""
        yield f">>> Installing Linux MCU binary: {firmware_path}...\n"
        try:
            # 1. Copy to /usr/local/bin/klipper_mcu
            cmd = ["sudo", "cp", firmware_path, "/usr/local/bin/klipper_mcu"]
            process = await asyncio.create_subprocess_exec(
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

    async def _run_flash_command(self, cmd: list) -> AsyncGenerator[str, None]:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        while True:
            if process.stdout is None:
                break
            line = await process.stdout.readline()
            if not line:
                break
            yield line.decode()

        await process.wait()
        if process.returncode == 0:
            yield ">>> Flashing successful!\n"
        else:
            yield f">>> Flashing failed with return code {process.returncode}\n"
