from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Dict, Any, Optional, AsyncGenerator
import os
import asyncio
import logging
import subprocess
import sys
import re
import uuid
from asyncio.subprocess import Process

logger = logging.getLogger("klipperfleet")

# Ensure the backend package directory is first on sys.path so local module
# imports (kconfig_manager, build_manager, etc.) work whether uvicorn
# imports this as 'backend.main' or the module is run interactively.
sys.path.insert(0, os.path.dirname(__file__))
try:
    # Use package-qualified imports so uvicorn can import when run as a module
    from backend.kconfig_manager import KconfigManager
    from backend.build_manager import BuildManager
    from backend.flash_manager import FlashManager
    from backend.fleet_manager import FleetManager
except Exception:
    # Fallback to local imports for interactive runs
    from kconfig_manager import KconfigManager
    from build_manager import BuildManager
    from flash_manager import FlashManager
    from fleet_manager import FleetManager

app = FastAPI(title="KlipperFleet API", version="1.1.0-alpha")

# Configuration
KLIPPER_DIR: str = os.path.abspath(os.path.expanduser(os.getenv("KLIPPER_DIR", "~/klipper")))
KATAPULT_DIR: str = os.path.abspath(os.path.expanduser(os.getenv("KATAPULT_DIR", "~/katapult")))
DATA_DIR: str = os.path.abspath(os.path.expanduser(os.getenv("DATA_DIR", "~/printer_data/config/klipperfleet")))
PROFILES_DIR: str = os.path.join(DATA_DIR, "profiles")
ARTIFACTS_DIR: str = os.path.join(DATA_DIR, "artifacts")

# Ensure directories exist
os.makedirs(PROFILES_DIR, exist_ok=True)
os.makedirs(ARTIFACTS_DIR, exist_ok=True)

kconfig_mgr = KconfigManager(KLIPPER_DIR)
build_mgr = BuildManager(KLIPPER_DIR, ARTIFACTS_DIR)
flash_mgr = FlashManager(KLIPPER_DIR, KATAPULT_DIR)
fleet_mgr = FleetManager(DATA_DIR)

# Profile name validation: only alphanumeric, underscores, hyphens, and dots
_PROFILE_NAME_RE = re.compile(r'^[a-zA-Z0-9_.-]+$')

def validate_profile_name(name: str) -> None:
    """Validates a profile name to prevent path traversal."""
    if not name or not _PROFILE_NAME_RE.match(name) or '..' in name:
        raise HTTPException(status_code=400, detail=f"Invalid profile name: '{name}'. Only alphanumeric characters, underscores, hyphens, and dots are allowed.")

def get_flash_offset(profile_name: str) -> str:
    """Extracts the flash offset address from a profile's .config file."""
    config_path: str = os.path.join(PROFILES_DIR, f"{profile_name}.config")
    if not os.path.exists(config_path):
        return "0x08000000"
    
    # Common Klipper offsets (handles both CONFIG_FLASH_START and CONFIG_STM32_FLASH_START)
    offsets: Dict[str, str] = {
        "_FLASH_START_800": "0x08000800",   # 2KiB
        "_FLASH_START_2000": "0x08002000",  # 8KiB
        "_FLASH_START_4000": "0x08004000",  # 16KiB
        "_FLASH_START_8000": "0x08008000",  # 32KiB
        "_FLASH_START_10000": "0x08010000", # 64KiB
        "_FLASH_START_20000": "0x08020000", # 128KiB
        "_FLASH_START_0": "0x08000000",
    }
    
    try:
        with open(config_path, 'r') as f:
            content: str = f.read()
            for key, addr in offsets.items():
                if f"{key}=y" in content:
                    return addr
    except Exception:
        logger.warning("Failed to read config file %s for bootloader offset, using default", config_path, exc_info=True)
    return "0x08000000"

class TaskStore:
    MAX_COMPLETED_TASKS: int = 50

    def __init__(self) -> None:
        self.tasks: Dict[str, Dict[str, Any]] = {}

    def _cleanup(self) -> None:
        """Purge oldest completed tasks when the limit is exceeded."""
        completed = [
            (tid, t) for tid, t in self.tasks.items() if t.get("completed")
        ]
        if len(completed) <= self.MAX_COMPLETED_TASKS:
            return
        # Keep the most recent ones (dict insertion order is preserved in 3.7+)
        to_remove = len(completed) - self.MAX_COMPLETED_TASKS
        for tid, _ in completed[:to_remove]:
            del self.tasks[tid]

    def create_task(self, task_id: str) -> None:
        self._cleanup()
        self.tasks[task_id] = {
            "status": "running", 
            "logs": [], 
            "completed": False, 
            "cancelled": False,
            "device_statuses": {} # Real-time status overrides (id -> status)
        }

    def add_log(self, task_id: str, log: str) -> None:
        if task_id in self.tasks:
            self.tasks[task_id]["logs"].append(log)

    def update_device_status(self, task_id: str, device_id: str, status: str) -> None:
        if task_id in self.tasks:
            self.tasks[task_id]["device_statuses"][device_id] = status

    def get_device_status(self, task_id: str, device_id: str) -> Optional[str]:
        if task_id in self.tasks:
            return self.tasks[task_id]["device_statuses"].get(device_id)
        return None

    def cancel_task(self, task_id: str) -> None:
        if task_id in self.tasks:
            self.tasks[task_id]["cancelled"] = True
            self.tasks[task_id]["status"] = "cancelled"
            self.tasks[task_id]["logs"].append("\n!!! TASK CANCELLED BY USER !!!\n")

    def is_cancelled(self, task_id: str) -> bool:
        return self.tasks.get(task_id, {}).get("cancelled", False)

    def complete_task(self, task_id: str, status: str = "completed") -> None:
        if task_id in self.tasks:
            if not self.tasks[task_id]["cancelled"]:
                self.tasks[task_id]["status"] = status
            self.tasks[task_id]["completed"] = True

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.tasks.get(task_id)

task_store = TaskStore()

class ConfigValue(BaseModel):
    name: str
    value: str

class ProfileSave(BaseModel):
    name: str
    values: List[ConfigValue]
    base_profile: Optional[str] = None

class Device(BaseModel):
    name: str
    id: str
    old_id: Optional[str] = None
    profile: str
    method: str
    interface: Optional[str] = "can0"
    baudrate: Optional[int] = 250000  # Serial baudrate for Katapult flashtool.py (common: 115200, 250000, 500000)
    notes: Optional[str] = ""
    is_katapult: bool = False
    is_bridge: bool = False
    dfu_id: Optional[str] = None
    magic_baud_tested: bool = False
    use_magic_baud: bool = False
    dfu_exit_tested: bool = False
    use_dfu_exit: bool = False
    exclude_from_batch: bool = False

class FlashRequest(BaseModel):
    profile: str
    device_id: str
    method: str # "serial", "can", "dfu", "linux"
    dfu_id: Optional[str] = None
    baudrate: Optional[int] = 250000  # Serial baudrate for Katapult
    use_magic_baud: Optional[bool] = False
    use_dfu_exit: Optional[bool] = True

class AttachRequest(BaseModel):
    fleet_id: str
    hardware_id: str
    method: str

@app.get("/api/status")
async def get_status() -> Dict[str, Any]:
    return {
        "message": "KlipperFleet API is running", 
        "klipper_dir": KLIPPER_DIR,
        "is_klipper_kconfiglib": kconfig_mgr.is_klipper_kconfiglib
    }

@app.get("/klipper/version")
async def get_klipper_version() -> Dict[str, str]:
    """Returns the host Klipper git version information."""
    return await build_mgr.get_klipper_version()

class ConfigPreview(BaseModel):
    profile: Optional[str] = None
    values: List[ConfigValue] = []
    show_optional: bool = False

@app.post("/config/tree")
async def post_config_tree(preview: ConfigPreview, request: Request) -> List[Dict[str, Any]]:
    """Returns the Kconfig tree with unsaved values applied for live preview."""
    config_path: Optional[str] = None
    if preview.profile:
        config_path = os.path.join(PROFILES_DIR, f"{preview.profile}.config")
        if not os.path.exists(config_path):
            raise HTTPException(status_code=404, detail=f"Profile {preview.profile} not found")
    
    try:
        await kconfig_mgr.load_kconfig(config_path)
        # Apply unsaved values in multiple passes to handle deep dependencies
        for i in range(10):
            for item in preview.values:
                try:
                    kconfig_mgr.set_value(item.name, item.value)
                except Exception:
                    # Expected: values may fail on early passes due to unresolved dependencies.
                    # They will succeed on later passes as cascading deps resolve.
                    if i == 9:
                        logger.debug("Kconfig value %s=%s still failing after final pass", item.name, item.value)
            
        return kconfig_mgr.get_menu_tree(show_optional=preview.show_optional)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404, 
            detail="Kconfig file not found. This is usually caused by a user running Kalico (unsupported but on the roadmap) or Klipper is not installed in the default location."
        )
    except Exception as e:
        import traceback
        error_detail: str = traceback.format_exc()
        raise HTTPException(status_code=500, detail=error_detail)

@app.get("/config/tree")
async def get_config_tree(request: Request, profile: Optional[str] = None, show_optional: bool = False) -> List[Dict[str, Any]]:
    """Returns the full Kconfig tree, optionally loaded with a profile's values."""
    return await post_config_tree(ConfigPreview(profile=profile, show_optional=show_optional), request)

@app.post("/config/save")
async def save_profile(profile: ProfileSave) -> Dict[str, str]:
    """Saves a set of configuration values to a profile file."""
    validate_profile_name(profile.name)
    if profile.base_profile:
        validate_profile_name(profile.base_profile)
    try:
        config_path: Optional[str] = None
        if profile.base_profile:
            config_path = os.path.join(PROFILES_DIR, f"{profile.base_profile}.config")
            if not os.path.exists(config_path):
                config_path = None
        
        await kconfig_mgr.load_kconfig(config_path)
        for item in profile.values:
            kconfig_mgr.set_value(item.name, item.value)
        
        save_path: str = os.path.join(PROFILES_DIR, f"{profile.name}.config")
        kconfig_mgr.save_config(save_path)
        return {"message": f"Profile {profile.name} saved successfully"}
    except FileNotFoundError:
        raise HTTPException(
            status_code=404, 
            detail="Kconfig file not found. This is usually caused by a user running Kalico (unsupported but on the roadmap) or Klipper is not installed in the default location."
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/profiles")
async def list_profiles() -> Dict[str, List[str]]:
    """Lists all saved configuration profiles."""
    profiles: List[str] = [f.replace(".config", "") for f in os.listdir(PROFILES_DIR) if f.endswith(".config")]
    return {"profiles": profiles}

@app.delete("/profiles/{name}")
async def delete_profile(name: str) -> Dict[str, str]:
    """Deletes a saved configuration profile."""
    validate_profile_name(name)
    config_path: str = os.path.join(PROFILES_DIR, f"{name}.config")
    if os.path.exists(config_path):
        os.remove(config_path)
        return {"message": f"Profile {name} deleted successfully"}
    else:
        raise HTTPException(status_code=404, detail=f"Profile {name} not found")

@app.get("/build/{profile}")
async def build_profile(profile: str) -> StreamingResponse:
    """Starts a build for the specified profile and streams the output."""
    validate_profile_name(profile)
    task_id: str = f"task_{uuid.uuid4().hex[:12]}"
    task_store.create_task(task_id)

    config_path: str = os.path.join(PROFILES_DIR, f"{profile}.config")
    if not os.path.exists(config_path):
        raise HTTPException(status_code=404, detail="Profile not found")
    
    async def generate() -> AsyncGenerator[str, None]:
        async for log in build_mgr.run_build(config_path):
            if task_store.is_cancelled(task_id): break
            yield log
        task_store.complete_task(task_id)

    return StreamingResponse(generate(), media_type="text/plain", headers={"X-Task-Id": task_id})

async def manage_klipper_services(action: str) -> str:
    """Stops or starts all Klipper-related services."""
    try:
        cmd_list_str: str = "systemctl list-units --type=service --all --no-legend 'klipper*' 'moonraker*' | awk '{print $1}'"
        process: Process = await asyncio.create_subprocess_shell(
            cmd_list_str,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        services: List[str] = stdout.decode().splitlines()
        
        target_services: List[str] = [s for s in services if s and s != "klipperfleet.service" and s.endswith(".service")]
        
        if not target_services:
            return f">>> No Klipper/Moonraker services found to {action}.\n"
        
        for service in target_services:
            cmd: List[str] = ["sudo", "systemctl", action, service]
            proc: Process = await asyncio.create_subprocess_exec(*cmd)
            await proc.wait()
        
        past_tense_action = f"{action}ped" if action == "stop" else f"{action}ed"
        return f">>> Successfully {past_tense_action}: {', '.join(target_services)}\n"
    except Exception as e:
        return f">>> Error managing services: {str(e)}\n"

async def get_services_status():
    """Returns the status of Klipper and Moonraker services."""
    try:
        cmd = "systemctl list-units --type=service --all --no-legend 'klipper*' 'moonraker*' | awk '{print $1, $3, $4}'"
        process: Process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        lines: List[str] = stdout.decode().splitlines()
        
        status = []
        for line in lines:
            parts: List[str] = line.split()
            if len(parts) >= 3:
                name, active_state, sub_state = parts[0], parts[1], parts[2]
                if name == "klipperfleet.service": continue
                status.append({
                    "name": name,
                    "active": active_state == "active",
                    "status": sub_state
                })
        return status
    except Exception:
        return []

@app.get("/services/status")
async def services_status():
    return await get_services_status()

@app.post("/services/manage")
async def services_manage(action: str) -> Dict[str, str]:
    if action not in ["start", "stop", "restart"]:
        raise HTTPException(status_code=400, detail="Invalid action")
    log: str = await manage_klipper_services(action)
    return {"message": log}

@app.get("/task/status/{task_id}")
async def get_task_status(task_id: str):
    task = task_store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task

@app.post("/task/cancel/{task_id}")
async def cancel_task_operation(task_id: str) -> Dict[str, str]:
    task = task_store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task_store.cancel_task(task_id)
    return {"message": "Cancellation requested"}

@app.get("/batch/{action}")
async def batch_operation(action: str, background_tasks: BackgroundTasks) -> Dict[str, str]:
    """Performs batch operations (build, flash-ready, flash-all, etc.)"""
    task_id: str = f"task_{uuid.uuid4().hex[:12]}"
    task_store.create_task(task_id)

    async def run_task() -> None:
        services_stopped = False
        # Result tracking for summary
        build_results: Dict[str, str] = {}  # profile -> "SUCCESS"/"FAILED"
        flash_results: Dict[str, str] = {}  # device_name -> "SUCCESS"/"SKIPPED"/"FAILED"
        
        try:
            devices: List[Dict[str, Any]] = fleet_mgr.get_fleet()
            
            # 1. Build phase
            if "build" in action:
                if task_store.is_cancelled(task_id): return
                task_store.add_log(task_id, ">>> STARTING BATCH BUILD PHASE <<<\n")
                profiles_to_build: List[Any] = list(set(d['profile'] for d in devices if d['profile']))
                if not profiles_to_build:
                    task_store.add_log(task_id, ">>> No profiles assigned to fleet devices. Skipping build.\n")
                else:
                    for profile in profiles_to_build:
                        if task_store.is_cancelled(task_id): return
                        task_store.add_log(task_id, f"\n>>> BATCH BUILD: Starting {profile}...\n")
                        config_path: str = os.path.join(PROFILES_DIR, f"{profile}.config")
                        build_success = True
                        async for log in build_mgr.run_build(config_path):
                            if task_store.is_cancelled(task_id): return
                            task_store.add_log(task_id, log)
                            if "!!! Error" in log or "Build failed" in log:
                                build_success = False
                        build_results[profile] = "SUCCESS" if build_success else "FAILED"
                        task_store.add_log(task_id, f">>> BATCH BUILD: Finished {profile}\n")
            
            # 2. Flash phase
            if "flash" in action:
                if task_store.is_cancelled(task_id): return
                task_store.tasks[task_id]["is_bus_task"] = True
                task_store.add_log(task_id, "\n>>> BATCH FLASH: Starting...\n")
                
                # Filter out devices excluded from batch operations
                excluded_devices = [d for d in devices if d.get('exclude_from_batch', False)]
                devices = [d for d in devices if not d.get('exclude_from_batch', False)]
                if excluded_devices:
                    excluded_names = ', '.join(d['name'] for d in excluded_devices)
                    task_store.add_log(task_id, f">>> Excluding from batch: {excluded_names}\n")
                    for excl_dev in excluded_devices:
                        flash_results[excl_dev['name']] = "EXCLUDED"
                
                task_store.add_log(task_id, ">>> Checking device statuses before stopping services...\n")
                # Pre-discover CAN devices while Moonraker is still running to identify "In Service" nodes
                can_discovery: List[Dict[str, str]] = await flash_mgr.discover_can_devices()
                can_status_map: Dict[str, str] = {d['id']: d.get('mode', 'offline') for d in can_discovery}
                
                reboot_tasks = []
                device_statuses = {}
                for dev in devices:
                    if task_store.is_cancelled(task_id): return
                    if not dev.get('profile'):
                        continue
                    
                    # Use cached CAN status if possible
                    if dev['method'] == 'can':
                        status: str = can_status_map.get(dev['id'], 'offline')
                    else:
                        status: str = await flash_mgr.check_device_status(dev['id'], dev['method'])
                    
                    device_statuses[dev['id']] = status
                    task_store.update_device_status(task_id, dev['id'], status)
                    
                    if status == "service":
                        # Reboot non-bridge CAN devices and non-bridge Katapult-capable serial devices.
                        # Bridges are handled in the second phase to avoid killing the CAN bus prematurely.
                        # We also include serial devices if they are being flashed, as they MUST be in Katapult mode.
                        if not dev.get('is_bridge') and (dev['method'] == 'can' or dev['method'] == 'serial' or dev['method'] == 'dfu'):
                            reboot_tasks.append({
                                "original_id": dev['id'], # Keep the original ID to find it in the devices list
                                "id": dev['id'], 
                                "method": dev['method'], 
                                "name": dev['name'],
                                "use_magic_baud": dev.get('use_magic_baud', False),
                                "interface": dev.get('interface', 'can0'),
                                "baudrate": dev.get('baudrate', 250000),
                                "dfu_id": dev.get('dfu_id')
                            })

                # Stop services early to clear the bus for flashing
                task_store.add_log(task_id, await manage_klipper_services("stop"))
                services_stopped = True

                # Record initial serial devices to avoid misidentifying bridges later
                initial_serials: List[str] = [d['id'] for d in await flash_mgr.discover_serial_devices(skip_moonraker=True)]
                
                if reboot_tasks:
                    if task_store.is_cancelled(task_id): return
                    await asyncio.sleep(2) 
                    
                    has_manual_dfu = False
                    for dev_info in reboot_tasks:
                        if task_store.is_cancelled(task_id): return
                        if dev_info['method'] == 'dfu':
                            if dev_info.get('use_magic_baud'):
                                task_store.add_log(task_id, f">>> Requesting DFU reboot for {dev_info['name']} ({dev_info['id']})...\n")
                                async for log in flash_mgr.reboot_to_dfu(dev_info['id']):
                                    if task_store.is_cancelled(task_id): return
                                    task_store.add_log(task_id, log)
                            else:
                                task_store.add_log(task_id, f">>> MANUAL DFU ENTRY REQUIRED for {dev_info['name']}. Please trigger DFU mode now (button/jumper).\n")
                                has_manual_dfu = True
                        else:
                            task_store.add_log(task_id, f">>> Requesting Katapult reboot for {dev_info['name']} ({dev_info['id']})...\n")
                            async for log in flash_mgr.reboot_to_katapult(dev_info['id'], dev_info['method'], is_bridge=False, baudrate=dev_info.get('baudrate', 250000)):
                                if task_store.is_cancelled(task_id): return
                                task_store.add_log(task_id, log)
                    
                    wait_time: int = 60 if has_manual_dfu else 30
                    task_store.add_log(task_id, f">>> Waiting for devices to enter flash mode (up to {wait_time}s)...\n")
                    for i in range(wait_time // 2): 
                        if task_store.is_cancelled(task_id): return
                        await asyncio.sleep(2)

                        interfaces_in_task = set(d['interface'] for d in reboot_tasks if d['method'] == 'can')
                        
                        # Check if CAN interface is still up
                        for interface in interfaces_in_task:
                            if not await flash_mgr.is_interface_up(interface):
                                # If we are waiting for CAN devices, this is a problem
                                task_store.add_log(task_id, f"!!! CAN interface ({interface}) is DOWN. A bridge may have rebooted unexpectedly.\n")
                                task_store.add_log(task_id, f">>> Attempting to bring {interface} back up...\n")
                                await flash_mgr.ensure_canbus_up(interface)

                        ready_count = 0
                        for dev_info in reboot_tasks:
                            # Handle mode switching (Serial -> DFU)
                            current_method = dev_info['method']
                            current_id = dev_info['id']
                            original_id = dev_info['original_id']
                            
                            # 1. Check if it switched to DFU mode
                            resolved_dfu_id: str = await flash_mgr.resolve_dfu_id(current_id, known_dfu_id=dev_info.get('dfu_id'))
                            if resolved_dfu_id != current_id:
                                # It's in DFU mode now!
                                task_store.add_log(task_id, f">>> Device {dev_info['name']} detected in DFU mode: {resolved_dfu_id}\n")
                                
                                # Update the main devices list using the original_id
                                for d in devices:
                                    if d['id'] == original_id:
                                        d['id'] = resolved_dfu_id
                                        d['method'] = 'dfu'
                                        break
                                
                                # Update dev_info for the rest of this loop and future iterations
                                dev_info['id'] = resolved_dfu_id
                                dev_info['method'] = 'dfu'
                                current_id = resolved_dfu_id
                                current_method = 'dfu'
                            
                            # 2. If still serial, check if the ID changed (e.g. Klipper -> Katapult)
                            elif current_method == 'serial':
                                new_id: str = await flash_mgr.resolve_serial_id(current_id)
                                if new_id != current_id:
                                    task_store.add_log(task_id, f">>> Device {dev_info['name']} serial ID changed: {new_id}\n")
                                    # Update the main devices list using the original_id
                                    for d in devices:
                                        if d['id'] == original_id:
                                            d['id'] = new_id
                                            break
                                    dev_info['id'] = new_id
                                    current_id = new_id

                            status: str = await flash_mgr.check_device_status(
                                current_id, 
                                current_method, 
                                dfu_id=dev_info.get('dfu_id'), 
                                skip_moonraker=True,
                                is_bridge=dev_info.get('is_bridge', False),
                                interface=dev_info.get('interface', 'can0')
                            )
                            task_store.update_device_status(task_id, current_id, status)
                            if status in ["ready", "dfu"]:
                                ready_count += 1
                        
                        if ready_count == len(reboot_tasks):
                            # Count how many hardware devices are actually ready to be flashed
                            hw_ready_count = 0
                            for d in devices:
                                if d.get('profile') and d['method'] != 'linux':
                                    # Check status from task_store (which we just updated)
                                    if task_store.get_device_status(task_id, d['id']) in ["ready", "dfu"]:
                                        hw_ready_count += 1
                            
                            linux_count = len([d for d in devices if d.get('profile') and d['method'] == 'linux'])
                            
                            msg = f">>> All {hw_ready_count} hardware device{'s' if hw_ready_count != 1 else ''}"
                            if linux_count > 0:
                                msg += f" and {linux_count} Linux Process{'es' if linux_count > 1 else ''}"
                            
                            verb = "is" if (hw_ready_count + linux_count) == 1 else "are"
                            msg += f" {verb} ready!\n"
                            
                            task_store.add_log(task_id, msg)
                            break
                        task_store.add_log(task_id, f">>> {ready_count}/{len(reboot_tasks)} hardware devices ready... (waiting)\n")

                # 2b. Actual flashing
                # Sort: Non-bridges first, Bridges last
                sorted_devices: List[Dict[str, Any]] = sorted(devices, key=lambda x: 1 if x.get('is_bridge') else 0)
                
                for dev in sorted_devices:
                    if task_store.is_cancelled(task_id): return
                    if not dev.get('profile'):
                        continue
                    
                    # Check status (now bridge-aware)
                    status: str = await flash_mgr.check_device_status(
                        dev['id'], 
                        dev['method'], 
                        dfu_id=dev.get('dfu_id'), 
                        skip_moonraker=True,
                        is_bridge=dev.get('is_bridge', False),
                        interface=dev.get('interface', 'can0')
                    )
                    task_store.update_device_status(task_id, dev['id'], status)
                    
                    should_flash = False
                    if "flash-all" in action:
                        should_flash = True
                    elif "flash-ready" in action and status == "ready":
                        should_flash = True
                    
                    if should_flash:
                        if dev.get('is_bridge') and status == "service":
                            if dev['method'] == 'dfu':
                                task_store.add_log(task_id, f">>> Rebooting Bridge Host {dev['name']} to DFU mode...\n")
                                # Resolve the serial ID to trigger the reboot
                                serial_id: str = await flash_mgr.resolve_serial_id(dev['id'])
                                async for log in flash_mgr.reboot_to_dfu(serial_id):
                                    if task_store.is_cancelled(task_id): return
                                    task_store.add_log(task_id, log)
                                
                                task_store.add_log(task_id, ">>> Waiting for bridge to enter DFU mode...\n")
                                await asyncio.sleep(2)
                                dfu_device: Optional[str] = None
                                for _ in range(30):
                                    if task_store.is_cancelled(task_id): return
                                    await asyncio.sleep(1)
                                    current_dfus: List[Dict[str, str]] = await flash_mgr.discover_dfu_devices()
                                    if current_dfus:
                                        # If there's only one, it's ours. If multiple, we'd need better matching, 
                                        # but usually there's only one bridge being flashed at a time.
                                        dfu_device = current_dfus[0]['id']
                                        break
                                
                                if dfu_device:
                                    dev['id'] = dfu_device
                                    status = "ready"
                                    task_store.add_log(task_id, f">>> Bridge is now in DFU mode: {dev['id']}\n")
                                else:
                                    task_store.add_log(task_id, "!!! Bridge did not enter DFU mode. Skipping.\n")
                                    flash_results[dev['name']] = "FAILED (DFU timeout)"
                                    continue
                            else:
                                task_store.add_log(task_id, f">>> Rebooting Bridge Host {dev['name']} to Katapult...\n")
                                
                                # 1. Trigger the reboot
                                async for log in flash_mgr.reboot_to_katapult(dev['id'], dev['method'], dev.get('interface', 'can0'), is_bridge=True, baudrate=dev.get('baudrate', 250000)):
                                    if task_store.is_cancelled(task_id): return
                                    task_store.add_log(task_id, log)

                                # 2. Wait for it to reappear as a SERIAL device (Katapult mode)
                                task_store.add_log(task_id, ">>> Waiting for bridge to enter Katapult mode (Serial)...\n")
                                await asyncio.sleep(2)
                                new_device: Optional[str] = None
                                for _ in range(30):
                                    if task_store.is_cancelled(task_id): return
                                    await asyncio.sleep(1)
                                    current_serials: List[Dict[str, str]] = await flash_mgr.discover_serial_devices(skip_moonraker=True)
                                    current_ids: List[str] = [d['id'] for d in current_serials]
                                    
                                    # Look for a NEW serial device
                                    for cid in current_ids:
                                        if cid not in initial_serials:
                                            new_device = cid
                                            break
                                    if new_device: break
                                    
                                    # Fallback: look for ANY Katapult device
                                    for d in current_serials:
                                        if "katapult" in d['id'].lower() or "canboot" in d['id'].lower():
                                            new_device = d['id']
                                            break
                                    if new_device: break
                                
                                if new_device:
                                    dev['id'] = new_device
                                    dev['method'] = "serial"
                                    status = "ready"
                                    task_store.add_log(task_id, f">>> Bridge is now ready: {dev['id']}\n")
                                else:
                                    task_store.add_log(task_id, "!!! Bridge did not enter Katapult mode. Skipping.\n")
                                    flash_results[dev['name']] = "FAILED (Katapult timeout)"
                                    continue

                        if status not in ["ready", "dfu"] and dev['method'] != "linux":
                            task_store.add_log(task_id, f"!!! Skipping {dev['name']} ({dev['id']}) - Device is {status}, not ready for flashing.\n")
                            flash_results[dev['name']] = f"SKIPPED ({status})"
                            continue

                        task_store.add_log(task_id, f"\n>>> FLASHING {dev['name']} ({dev['id']}) with {dev['profile']}...\n")
                        firmware_path: str = os.path.join(ARTIFACTS_DIR, f"{dev['profile']}.elf" if dev['method'] == "linux" else f"{dev['profile']}.bin")
                        
                        if not os.path.exists(firmware_path):
                            task_store.add_log(task_id, f"!!! Error: Firmware for {dev['profile']} not found. Skipping.\n")
                            flash_results[dev['name']] = "FAILED (no firmware)"
                            continue
                            
                        task_store.update_device_status(task_id, dev['id'], "flashing")
                        try:
                            if dev['method'] == "serial":
                                # Resolve ID in case it changed during reboot (e.g. Klipper -> Katapult)
                                resolved_id: str = await flash_mgr.resolve_serial_id(dev['id'])
                                if resolved_id != dev['id']:
                                    task_store.add_log(task_id, f">>> Resolved serial ID: {dev['id']} -> {resolved_id}\n")
                                
                                async for log in flash_mgr.flash_serial(resolved_id, firmware_path, baudrate=dev.get('baudrate', 250000)):
                                    if task_store.is_cancelled(task_id): return
                                    task_store.add_log(task_id, log)
                            elif dev['method'] == "can":
                                interface = dev.get('interface', 'can0')
                                if not await flash_mgr.is_interface_up(interface):
                                    raise IOError(f"CAN interface ({interface}) is DOWN. Cannot flash device.")
                                async for log in flash_mgr.flash_can(dev['id'], firmware_path, interface):
                                    if task_store.is_cancelled(task_id): return
                                    task_store.add_log(task_id, log)
                            elif dev['method'] == "dfu":
                                resolved_id: str = await flash_mgr.resolve_dfu_id(dev['id'], known_dfu_id=dev.get('dfu_id'))
                                offset: str = get_flash_offset(dev['profile'])
                                async for log in flash_mgr.flash_dfu(resolved_id, firmware_path, address=offset, leave=dev.get('use_dfu_exit', True)):
                                    if task_store.is_cancelled(task_id): return
                                    task_store.add_log(task_id, log)
                            elif dev['method'] == "linux":
                                async for log in flash_mgr.flash_linux(firmware_path):
                                    if task_store.is_cancelled(task_id): return
                                    task_store.add_log(task_id, log)
                            
                            task_store.update_device_status(task_id, dev['id'], "ready")
                            flash_results[dev['name']] = "SUCCESS"
                        except Exception as e:
                            task_store.add_log(task_id, f"!!! Error flashing {dev['name']}: {str(e)}\n")
                            task_store.update_device_status(task_id, dev['id'], "failed")
                            flash_results[dev['name']] = "FAILED"
                    else:
                        task_store.add_log(task_id, f">>> Skipping {dev['name']} (Status: {status})\n")
                        flash_results[dev['name']] = "SKIPPED"
                
                task_store.add_log(task_id, "\n>>> BATCH FLASH COMPLETED <<<\n")
            
            # Generate summary
            task_store.add_log(task_id, "\n")
            task_store.add_log(task_id, "======================== [SUMMARY] ========================\n")
            
            # Build summary
            if build_results:
                task_store.add_log(task_id, "\n  BUILD RESULTS:\n")
                for profile, result in build_results.items():
                    if result == "SUCCESS":
                        task_store.add_log(task_id, f"  [COLOR:GREEN]  - {profile}: {result}[/COLOR]\n")
                    else:
                        task_store.add_log(task_id, f"  [COLOR:RED]  - {profile}: {result}[/COLOR]\n")
            
            # Flash summary
            if flash_results:
                task_store.add_log(task_id, "\n  FLASH RESULTS:\n")
                for device_name, result in flash_results.items():
                    if result == "SUCCESS":
                        task_store.add_log(task_id, f"  [COLOR:GREEN]  - {device_name}: {result}[/COLOR]\n")
                    elif result.startswith("SKIPPED") or result == "EXCLUDED":
                        task_store.add_log(task_id, f"  [COLOR:YELLOW]  - {device_name}: {result}[/COLOR]\n")
                    else:
                        task_store.add_log(task_id, f"  [COLOR:RED]  - {device_name}: {result}[/COLOR]\n")
            
            task_store.add_log(task_id, "\n===========================================================\n")
                
            task_store.add_log(task_id, "\n>>> ALL BATCH OPERATIONS COMPLETED <<<\n")
            
        except Exception as e:
            task_store.add_log(task_id, f"!!! CRITICAL ERROR: {str(e)}\n")
            task_store.complete_task(task_id, status="failed")
        finally:
            if services_stopped:
                task_store.add_log(task_id, ">>> Returning to service...\n")
                task_store.add_log(task_id, await manage_klipper_services("start"))
            task_store.complete_task(task_id)

    background_tasks.add_task(run_task)
    return {"task_id": task_id}

@app.get("/download/{profile}")
async def download_firmware(profile: str) -> FileResponse:
    """Downloads the klipper.bin for the specified profile."""
    bin_path: str = os.path.join(ARTIFACTS_DIR, f"{profile}.bin")
    if not os.path.exists(bin_path):
        bin_path: str = os.path.join(ARTIFACTS_DIR, f"{profile}.elf")
        
    if not os.path.exists(bin_path):
        raise HTTPException(status_code=404, detail="Firmware binary not found. Please build first.")
    
    ext: str = ".elf" if bin_path.endswith(".elf") else ".bin"
    return FileResponse(
        path=bin_path, 
        filename=f"{profile}{ext}",
        media_type='application/octet-stream'
    )

@app.get("/fleet")
async def get_fleet(fast: bool = False) -> List[Dict[str, Any]]:
    """Returns the registered fleet of devices with status."""
    fleet: List[Dict[str, Any]] = fleet_mgr.get_fleet()
    
    # Check for active tasks to get real-time status overrides
    status_overrides = {}
    is_task_running = False
    is_bus_task_running = False
    for tid, task in task_store.tasks.items():
        if task.get("status") == "running":
            status_overrides.update(task.get("device_statuses", {}))
            is_task_running = True
            if task.get("is_bus_task"):
                is_bus_task_running = True

    # Check if locks are held
    can_locked = flash_mgr._can_lock.locked()
    dfu_locked = flash_mgr._dfu_lock.locked()
    
    if fast:
        for dev in fleet:
            # 1. Check overrides (e.g. "Flashing...")
            if dev['id'] in status_overrides:
                dev['status'] = status_overrides[dev['id']]
            # 2. Check if bus is busy (only if a task is actually running)
            elif dev['method'] == 'can' and can_locked and is_bus_task_running:
                dev['status'] = 'bus_busy'
            # 3. Default to querying
            else:
                dev['status'] = 'querying'
            
            if dev.get('dfu_id'):
                if dev['dfu_id'] in status_overrides:
                    dev['dfu_status'] = status_overrides[dev['dfu_id']]
                elif dfu_locked and is_bus_task_running:
                    dev['dfu_status'] = 'bus_busy'
                else:
                    dev['dfu_status'] = 'querying'
        return fleet

    for dev in fleet:
        # If we have a real-time override from an active task, use it
        if dev['id'] in status_overrides:
            dev['status'] = status_overrides[dev['id']]
        elif dev['method'] == 'can' and can_locked and is_bus_task_running:
            dev['status'] = 'bus_busy'
        else:
            # Skip moonraker if a bus task is running (services likely stopped)
            dev['status'] = await flash_mgr.check_device_status(
                dev['id'], 
                dev['method'], 
                dfu_id=dev.get('dfu_id'),
                skip_moonraker=is_bus_task_running,
                is_bridge=dev.get('is_bridge', False),
                interface=dev.get('interface', 'can0')
            )
            
        if dev.get('dfu_id'):
            if dev['dfu_id'] in status_overrides:
                dev['dfu_status'] = status_overrides[dev['dfu_id']]
            elif dfu_locked and is_bus_task_running:
                dev['dfu_status'] = 'bus_busy'
            else:
                dev['dfu_status'] = await flash_mgr.check_device_status(
                    dev['dfu_id'],
                    "dfu",
                    skip_moonraker=is_task_running
                )
            
            # If the DFU device is offline but the parent is in service, mark as inactive
            if dev['dfu_status'] == 'offline' and dev['status'] == 'service':
                dev['dfu_status'] = 'inactive'
    return fleet

@app.post("/fleet/device")
async def save_device(device: Device) -> Dict[str, str]:
    """Registers or updates a device in the fleet."""
    fleet_mgr.save_device(device.dict())
    return {"message": "Device saved to fleet"}

@app.post("/fleet/attach")
async def post_fleet_attach(req: AttachRequest) -> Dict[str, str]:
    """Attaches a discovered hardware ID to an existing fleet entry."""
    fleet: List[Dict[str, Any]] = fleet_mgr.get_fleet()
    for dev in fleet:
        if dev['id'] == req.fleet_id:
            if req.method == 'dfu':
                dev['dfu_id'] = req.hardware_id
            elif req.method == 'serial':
                # If we are attaching a serial device to a fleet entry, 
                # we update the primary ID to the serial path.
                dev['id'] = req.hardware_id
            fleet_mgr.save_device(dev)
            return {"message": "Device attached"}
    raise HTTPException(status_code=404, detail="Fleet device not found")

@app.delete("/fleet/device")
async def remove_device(device_id: str) -> Dict[str, str]:
    """Removes a device from the fleet."""
    fleet_mgr.remove_device(device_id)
    return {"message": "Device removed from fleet"}

@app.get("/fleet/versions")
async def get_fleet_versions() -> Dict[str, Any]:
    """Gets live version information for all fleet devices that are in service."""
    fleet = fleet_mgr.get_fleet()
    mcu_versions = await flash_mgr.get_mcu_versions()
    
    version_info: Dict[str, Any] = {}
    for dev in fleet:
        device_id = dev['id']
        dev_info: Dict[str, Any] = {
            "flashed_version": dev.get('flashed_version'),
            "flashed_commit": dev.get('flashed_commit'),
            "last_flashed": dev.get('last_flashed'),
            "live_version": None
        }
        
        # Try to find live version by device ID or check all MCU identifiers
        if device_id in mcu_versions:
            dev_info["live_version"] = mcu_versions[device_id].get("version")
        else:
            # Try to match by looking at all MCU identifiers
            for mcu_id, mcu_info in mcu_versions.items():
                if mcu_info.get("identifier") == device_id:
                    dev_info["live_version"] = mcu_info.get("version")
                    break
        
        # Special handling for Linux MCU - look for "mcu rpi" or any MCU with MCU="linux"
        if dev.get('method') == 'linux' and dev_info["live_version"] is None:
            for mcu_id, mcu_info in mcu_versions.items():
                mcu_constants = mcu_info.get("mcu_constants", {})
                if mcu_constants.get("MCU") == "linux" or "rpi" in mcu_id.lower() or "host" in mcu_id.lower():
                    dev_info["live_version"] = mcu_info.get("version")
                    break
        
        version_info[device_id] = dev_info
    
    return version_info

@app.get("/devices/discover")
async def discover_devices() -> Dict[str, List[Dict[str, Any]]]:
    """Discovers Serial, CAN, DFU, and Linux process devices."""
    serial_devs: List[Dict[str, Any]] = await flash_mgr.discover_serial_devices()
    can_devs: List[Dict[str, Any]] = await flash_mgr.discover_can_devices(force=True)
    dfu_devs: List[Dict[str, Any]] = await flash_mgr.discover_dfu_devices()
    linux_devs: List[Dict[str, Any]] = flash_mgr.discover_linux_process()
    
    # Mark managed devices
    fleet = fleet_mgr.get_fleet()
    managed_ids = set()
    for d in fleet:
        managed_ids.add(d['id'])
        if d.get('dfu_id'):
            managed_ids.add(d['dfu_id'])
            
    for category in [serial_devs, can_devs, dfu_devs, linux_devs]:
        for dev in category:
            dev['managed'] = dev['id'] in managed_ids

    return {"serial": serial_devs, "can": can_devs, "dfu": dfu_devs, "linux": linux_devs}

@app.post("/flash")
async def flash_device(req: FlashRequest) -> StreamingResponse:
    """Flashes the specified profile to a device."""
    task_id: str = f"task_{uuid.uuid4().hex[:12]}"
    task_store.create_task(task_id)
    task_store.tasks[task_id]["is_bus_task"] = True

    if req.method == "linux":
        firmware_path: str = os.path.join(ARTIFACTS_DIR, f"{req.profile}.elf")
    else:
        firmware_path: str = os.path.join(ARTIFACTS_DIR, f"{req.profile}.bin")

    if not os.path.exists(firmware_path):
        raise HTTPException(status_code=400, detail=f"Firmware for profile '{req.profile}' not found. Please build first.")
    
    async def generate() -> AsyncGenerator[str, None]:
        services_stopped = False
        try:
            if task_store.is_cancelled(task_id): return

            # Stop services early to clear the bus
            yield await manage_klipper_services("stop")
            services_stopped = True

            # Query the fleet to determine the interface and baudrate for this device
            interface = "can0"
            baudrate = req.baudrate if req.baudrate else 250000
            try:
                fleet = fleet_mgr.get_fleet()
                for d in fleet:
                    if d.get("id") == req.device_id:
                        interface = d.get("interface", interface)
                        baudrate = d.get("baudrate", baudrate)
                        break
            except Exception:
                logger.warning("Failed to read fleet for device %s, using defaults (interface=%s, baudrate=%s)",
                               req.device_id, interface, baudrate, exc_info=True)

            # Snapshot current serial devices BEFORE reboot (for diff-based detection)
            initial_serials: List[str] = [d['id'] for d in await flash_mgr.discover_serial_devices(skip_moonraker=True)]
            new_serial_device: Optional[str] = None

            # 1. Check current status
            status: str = await flash_mgr.check_device_status(req.device_id, req.method, dfu_id=req.dfu_id, interface=interface)
            task_store.update_device_status(task_id, req.device_id, status)
            
            # 2. Reboot if in service
            if status == "service":
                if req.method == "dfu" or (req.method == "serial" and req.dfu_id):
                    if req.use_magic_baud:
                        yield f">>> Rebooting {req.device_id} to DFU mode (Magic Baud)...\n"
                        async for log in flash_mgr.reboot_to_dfu(req.device_id):
                            if task_store.is_cancelled(task_id): return
                            yield log
                    else:
                        yield f"!!! MANUAL ACTION REQUIRED: Please put {req.device_id} into DFU mode now (BOOT0 + RESET).\n"
                        yield ">>> Waiting for DFU device to appear...\n"
                        # Wait up to 60 seconds for manual entry
                        found = False
                        for _ in range(30):
                            if task_store.is_cancelled(task_id): return
                            await asyncio.sleep(2)
                            resolved_dfu_id: str = await flash_mgr.resolve_dfu_id(req.device_id, known_dfu_id=req.dfu_id)
                            dfu_devs: List[Dict[str, str]] = await flash_mgr.discover_dfu_devices()
                            if any(d['id'] == resolved_dfu_id for d in dfu_devs):
                                yield ">>> DFU device detected!\n"
                                found = True
                                break
                        if not found:
                            yield "!!! TIMEOUT: DFU device not found. Aborting flash.\n"
                            return
                else:
                    yield f">>> Rebooting {req.device_id} to Katapult mode...\n"
                    async for log in flash_mgr.reboot_to_katapult(req.device_id, method=req.method, baudrate=baudrate):
                        if task_store.is_cancelled(task_id): return
                        yield log
                
                yield ">>> Waiting for device to enter bootloader mode...\n"
                await asyncio.sleep(2) # Initial wait for USB bus to settle
                
                # Active wait for bootloader (up to 30s) - check both DFU and new serial devices
                for _ in range(30):
                    if task_store.is_cancelled(task_id): return
                    await asyncio.sleep(1)
                    
                    # Check if DFU device appeared
                    dfu_devs = await flash_mgr.discover_dfu_devices()
                    resolved = await flash_mgr.resolve_dfu_id(req.device_id, known_dfu_id=req.dfu_id)
                    if any(d['id'] == resolved for d in dfu_devs):
                        await asyncio.sleep(1)
                        break
                    
                    # Check for NEW serial device (Katapult mode) using snapshot diff
                    if req.method == "serial":
                        current_serials: List[Dict[str, str]] = await flash_mgr.discover_serial_devices(skip_moonraker=True)
                        current_ids: List[str] = [d['id'] for d in current_serials]
                        
                        # Look for a NEW serial device that wasn't there before
                        for cid in current_ids:
                            if cid not in initial_serials:
                                new_serial_device = cid
                                yield f">>> New serial device detected: {cid}\n"
                                break
                        if new_serial_device:
                            break
                        
                        # Fallback: look for ANY Katapult/CanBoot device
                        for d in current_serials:
                            if "katapult" in d['id'].lower() or "canboot" in d['id'].lower():
                                new_serial_device = d['id']
                                yield f">>> Katapult device detected: {d['id']}\n"
                                break
                        if new_serial_device:
                            break

            if task_store.is_cancelled(task_id): return

            # 3. Resolve ID and Method (in case it changed during reboot or is in a different mode)
            target_id: str = req.device_id
            actual_method: str = req.method
            
            # If the initial check already found it in DFU mode, lock to DFU immediately
            if status == "dfu":
                resolved_dfu_id: str = await flash_mgr.resolve_dfu_id(req.device_id, known_dfu_id=req.dfu_id)
                target_id = resolved_dfu_id
                actual_method = "dfu"
                yield f">>> Device detected in DFU mode. Switching to DFU flash method.\n"
            elif req.method in ["serial", "dfu"]:
                # Check DFU status first
                resolved_dfu_id: str = await flash_mgr.resolve_dfu_id(req.device_id, known_dfu_id=req.dfu_id)
                dfu_devs: List[Dict[str, str]] = await flash_mgr.discover_dfu_devices()
                is_in_dfu: bool = any(d['id'] == resolved_dfu_id for d in dfu_devs)
                
                if is_in_dfu:
                    target_id: str = resolved_dfu_id
                    actual_method = "dfu"
                    if actual_method != req.method:
                        yield f">>> Device detected in DFU mode. Switching to DFU flash method.\n"
                elif new_serial_device:
                    # Use the new device we found via snapshot diff
                    target_id = new_serial_device
                    actual_method = "serial"
                    yield f">>> Using detected Katapult device: {target_id}\n"
                else:
                    # Fallback to resolve_serial_id for cases where device ID didn't change
                    resolved_serial_id: str = await flash_mgr.resolve_serial_id(req.device_id)
                    if os.path.exists(resolved_serial_id):
                        target_id: str = resolved_serial_id
                        actual_method = "serial"
                        if target_id != req.device_id:
                            yield f">>> Resolved serial ID: {req.device_id} -> {target_id}\n"

            if task_store.is_cancelled(task_id): return

            # 4. Flash
            task_store.update_device_status(task_id, req.device_id, "flashing")
            try:
                if actual_method == "serial":
                    async for log in flash_mgr.flash_serial(target_id, firmware_path, baudrate=baudrate):
                        if task_store.is_cancelled(task_id): return
                        yield log
                elif actual_method == "can":
                    async for log in flash_mgr.flash_can(target_id, firmware_path, interface=interface):
                        if task_store.is_cancelled(task_id): return
                        yield log
                elif actual_method == "dfu":
                    offset: str = get_flash_offset(req.profile)
                    async for log in flash_mgr.flash_dfu(target_id, firmware_path, address=offset, leave=req.use_dfu_exit if req.use_dfu_exit is not None else True):
                        if task_store.is_cancelled(task_id): return
                        yield log
                elif actual_method == "linux":
                    async for log in flash_mgr.flash_linux(firmware_path):
                        if task_store.is_cancelled(task_id): return
                        yield log
                
                task_store.update_device_status(task_id, req.device_id, "ready")
                
                # Update version info in fleet after successful flash
                build_info = build_mgr.get_last_build_info(req.profile)
                if build_info:
                    fleet_mgr.update_device_version(req.device_id, build_info)
                    yield f">>> Version recorded: {build_info.get('version', 'unknown')} ({build_info.get('commit', 'unknown')})\n"
            except Exception as e:
                yield f"!!! Error during flash: {str(e)}\n"
                task_store.update_device_status(task_id, req.device_id, "failed")
        except Exception as e:
            yield f"!!! Error during flash: {str(e)}\n"
        finally:
            if services_stopped:
                yield await manage_klipper_services("start")
            task_store.complete_task(task_id)

    return StreamingResponse(generate(), media_type="text/plain", headers={"X-Task-Id": task_id})

@app.post("/flash/reboot")
async def reboot_device(device_id: str, mode: str = "katapult", method: Optional[str] = None) -> StreamingResponse:
    """Reboots a device."""
    task_id: str = f"task_{uuid.uuid4().hex[:12]}"
    task_store.create_task(task_id)
    task_store.tasks[task_id]["is_bus_task"] = True

    # Find device in fleet to check if it's a bridge
    fleet: List[Dict[str, Any]] = fleet_mgr.get_fleet()
    dev: Dict[str, Any] = next((d for d in fleet if d['id'] == device_id), {})
    is_bridge = dev.get('is_bridge', False)
    
    # Use provided method or fall back to fleet entry or default to 'can'
    actual_method: str = method if method else dev.get('method', 'can')
    interface = dev.get('interface', 'can0')
    
    async def generate() -> AsyncGenerator[str, None]:
        async for log in flash_mgr.reboot_device(device_id, mode, method=actual_method, interface=interface, is_bridge=is_bridge):
            if task_store.is_cancelled(task_id): break
            yield log
        task_store.complete_task(task_id)

    return StreamingResponse(generate(), media_type="text/plain", headers={"X-Task-Id": task_id})

@app.post("/debug/test_magic_baud")
async def test_magic_baud(device_id: str, full_cycle: bool = False) -> StreamingResponse:
    """Tests the 1200bps magic baud trick on a device, optionally testing the full cycle."""
    task_id: str = f"task_{uuid.uuid4().hex[:12]}"
    task_store.create_task(task_id)

    async def generate() -> AsyncGenerator[str, None]:
        try:
            yield f">>> Testing DFU Cycle on {device_id} (Full Cycle: {full_cycle})...\n"
            
            # 0. Check if already in DFU mode
            dfu_devs: List[Dict[str, str]] = await flash_mgr.discover_dfu_devices()
            found_dfu_id = None
            
            if dfu_devs and not os.path.exists(device_id):
                yield ">>> Device is already in DFU mode (or serial port is missing).\n"
                yield ">>> SUCCESS: DFU device detected.\n"
                yield ">>> PHASE1_SUCCESS\n"
                found_dfu_id = dfu_devs[0]['id']
                if not full_cycle:
                    return
            else:
                # 1. Try the trick
                try:
                    import serial
                    ser = serial.Serial(device_id, 1200)
                    ser.close()
                    yield ">>> 1200bps signal sent. Waiting 10s for device to reappear in DFU mode...\n"
                except Exception as e:
                    yield f"!!! Error sending signal: {str(e)}\n"
                    return

                # 2. Wait and check for DFU
                for i in range(10):
                    if task_store.is_cancelled(task_id): return
                    await asyncio.sleep(1)
                    dfu_devs: List[Dict[str, str]] = await flash_mgr.discover_dfu_devices()
                    if dfu_devs:
                        found_dfu_id = dfu_devs[0]['id']
                        yield f">>> SUCCESS: DFU device detected ({found_dfu_id}).\n"
                        yield ">>> PHASE1_SUCCESS\n"
                        break
                    
                    if not os.path.exists(device_id):
                        yield f">>> Device {device_id} disconnected. Waiting for DFU...\n"
                
                if not found_dfu_id:
                    yield "!!! TIMEOUT: No DFU device detected after 10s. Magic baud might not be supported.\n"
                    return

            if full_cycle:
                yield ">>> Phase 2: Testing 'Restart to Firmware' (DFU Exit)...\n"
                async for log in flash_mgr.reboot_device(found_dfu_id, mode="service", method="dfu"):
                    if task_store.is_cancelled(task_id): return
                    yield log
                
                yield f">>> Waiting 10s for serial device {device_id} to return...\n"
                for i in range(10):
                    if task_store.is_cancelled(task_id): return
                    await asyncio.sleep(1)
                    if os.path.exists(device_id):
                        yield f">>> SUCCESS: Device {device_id} is back online!\n"
                        yield ">>> PHASE2_SUCCESS\n"
                        yield ">>> FULL CYCLE SUCCESSFUL.\n"
                        return
                
                yield "!!! TIMEOUT: Device did not return to serial mode. You may need to manually reset it.\n"
        finally:
            task_store.complete_task(task_id)
            
    return StreamingResponse(generate(), media_type="text/plain", headers={"X-Task-Id": task_id})

@app.post("/api/self-update")
async def self_update(background_tasks: BackgroundTasks) -> Dict[str, str]:
    """Runs the update.sh script in the background."""
    update_script: str = os.path.join(os.path.dirname(os.path.dirname(__file__)), "update.sh")
    if not os.path.exists(update_script):
        raise HTTPException(status_code=404, detail="Update script not found")
    
    try:
        repo_dir = os.path.dirname(os.path.dirname(__file__))
        fetch_proc = await asyncio.create_subprocess_exec("git", "fetch", "origin", cwd=repo_dir, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await fetch_proc.wait()
        reset_proc = await asyncio.create_subprocess_exec("git", "reset", "--hard", "origin/main", cwd=repo_dir, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await reset_proc.wait()
    except Exception:
        logger.exception("Git fetch/reset failed during self-update, proceeding with update.sh anyway")

    def run_update() -> None:
        subprocess.Popen(["bash", update_script], start_new_session=True)

    background_tasks.add_task(run_update)
    return {"message": "Update started. The service will restart shortly."}

REPO_UI_DIR: str = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ui")
DATA_UI_DIR: str = os.path.join(DATA_DIR, "ui")

if os.path.exists(REPO_UI_DIR):
    app.mount("/", StaticFiles(directory=REPO_UI_DIR, html=True), name="ui")
elif os.path.exists(DATA_UI_DIR):
    app.mount("/", StaticFiles(directory=DATA_UI_DIR, html=True), name="ui")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8321)
