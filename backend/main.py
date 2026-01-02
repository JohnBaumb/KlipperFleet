from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import os
import json
import asyncio
import subprocess
import kconfiglib
import sys

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

app = FastAPI(title="KlipperFleet API")

# Configuration
KLIPPER_DIR = os.path.abspath(os.path.expanduser(os.getenv("KLIPPER_DIR", "~/klipper")))
KATAPULT_DIR = os.path.abspath(os.path.expanduser(os.getenv("KATAPULT_DIR", "~/katapult")))
DATA_DIR = os.path.abspath(os.path.expanduser(os.getenv("DATA_DIR", "~/printer_data/config/klipperfleet")))
PROFILES_DIR = os.path.join(DATA_DIR, "profiles")
ARTIFACTS_DIR = os.path.join(DATA_DIR, "artifacts")

# Ensure directories exist
os.makedirs(PROFILES_DIR, exist_ok=True)
os.makedirs(ARTIFACTS_DIR, exist_ok=True)

kconfig_mgr = KconfigManager(KLIPPER_DIR)
build_mgr = BuildManager(KLIPPER_DIR, ARTIFACTS_DIR)
flash_mgr = FlashManager(KLIPPER_DIR, KATAPULT_DIR)
fleet_mgr = FleetManager(DATA_DIR)

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
    profile: str
    method: str
    notes: Optional[str] = ""

class FlashRequest(BaseModel):
    profile: str
    device_id: str
    method: str # "serial" or "can"

@app.get("/api/status")
async def get_status():
    return {"message": "KlipperFleet API is running", "klipper_dir": KLIPPER_DIR}

class ConfigPreview(BaseModel):
    profile: Optional[str] = None
    values: List[ConfigValue] = []

@app.get("/config/tree")
async def get_config_tree(request: Request, profile: Optional[str] = None):
    """Returns the full Kconfig tree, optionally loaded with a profile's values."""
    return await post_config_tree(ConfigPreview(profile=profile), request)

@app.post("/config/tree")
async def post_config_tree(preview: ConfigPreview, request: Request):
    """Returns the Kconfig tree with unsaved values applied for live preview."""
    config_path = None
    if preview.profile:
        config_path = os.path.join(PROFILES_DIR, f"{preview.profile}.config")
        if not os.path.exists(config_path):
            raise HTTPException(status_code=404, detail=f"Profile {preview.profile} not found")
    
    try:
        kconfig_mgr.load_kconfig(config_path)
        # Apply unsaved values in multiple passes to handle deep dependencies
        # (e.g. Architecture -> Processor -> Comm Interface -> CAN Pins)
        for i in range(10):
            for item in preview.values:
                try:
                    kconfig_mgr.set_value(item.name, item.value)
                except Exception:
                    pass
            
        return kconfig_mgr.get_menu_tree()
    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        raise HTTPException(status_code=500, detail=error_detail)

@app.post("/config/save")
async def save_profile(profile: ProfileSave):
    """Saves a set of configuration values to a profile file."""
    try:
        config_path = None
        if profile.base_profile:
            config_path = os.path.join(PROFILES_DIR, f"{profile.base_profile}.config")
            if not os.path.exists(config_path):
                config_path = None
        
        kconfig_mgr.load_kconfig(config_path)
        for item in profile.values:
            kconfig_mgr.set_value(item.name, item.value)
        
        save_path = os.path.join(PROFILES_DIR, f"{profile.name}.config")
        kconfig_mgr.save_config(save_path)
        return {"message": f"Profile {profile.name} saved successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/profiles")
async def list_profiles():
    """Lists all saved configuration profiles."""
    profiles = [f.replace(".config", "") for f in os.listdir(PROFILES_DIR) if f.endswith(".config")]
    return {"profiles": profiles}

@app.delete("/profiles/{name}")
async def delete_profile(name: str):
    """Deletes a saved configuration profile."""
    config_path = os.path.join(PROFILES_DIR, f"{name}.config")
    if os.path.exists(config_path):
        os.remove(config_path)
        return {"message": f"Profile {name} deleted successfully"}
    else:
        raise HTTPException(status_code=404, detail=f"Profile {name} not found")

@app.get("/build/{profile}")
async def build_profile(profile: str):
    """Starts a build for the specified profile and streams the output."""
    config_path = os.path.join(PROFILES_DIR, f"{profile}.config")
    if not os.path.exists(config_path):
        raise HTTPException(status_code=404, detail="Profile not found")
    
    return StreamingResponse(build_mgr.run_build(config_path), media_type="text/plain")

async def manage_klipper_services(action: str):
    """Stops or starts all Klipper-related services."""
    try:
        # Find all services starting with klipper (but not klipperfleet) or moonraker
        cmd = "systemctl list-units --type=service --all --no-legend 'klipper*' 'moonraker*' | awk '{print $1}'"
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        services = stdout.decode().splitlines()
        
        target_services = [s for s in services if s and s != "klipperfleet.service" and s.endswith(".service")]
        
        if not target_services:
            return f">>> No Klipper/Moonraker services found to {action}.\n"
        
        for service in target_services:
            cmd = ["sudo", "systemctl", action, service]
            proc = await asyncio.create_subprocess_exec(*cmd)
            await proc.wait()
        
        return f">>> Successfully {action}ed: {', '.join(target_services)}\n"
    except Exception as e:
        return f">>> Error managing services: {str(e)}\n"

@app.get("/batch/{action}")
async def batch_operation(action: str):
    """Performs batch operations (build, flash-ready, flash-all, etc.)"""
    async def generate():
        try:
            devices = fleet_mgr.get_fleet()
            
            # 1. Build phase
            if "build" in action:
                profiles_to_build = list(set(d['profile'] for d in devices if d['profile']))
                if not profiles_to_build:
                    yield ">>> No profiles assigned to fleet devices. Skipping build.\n"
                else:
                    for profile in profiles_to_build:
                        yield f"\n>>> BATCH BUILD: Starting {profile}...\n"
                        config_path = os.path.join(PROFILES_DIR, f"{profile}.config")
                        async for log in build_mgr.run_build(config_path):
                            yield log
                        yield f">>> BATCH BUILD: Finished {profile}\n"
            
            # 2. Flash phase
            if "flash" in action:
                yield "\n>>> BATCH FLASH: Starting...\n"
                
                # 2a. Pre-flash: Identify devices needing reboot while services are still running
                reboot_tasks = []
                if action == "build-flash-all" or action == "flash-all":
                    yield ">>> Checking for devices in service that need rebooting to Katapult...\n"
                    for dev in devices:
                        if not dev['profile']:
                            continue
                        status = await flash_mgr.check_device_status(dev['id'], dev['method'])
                        if status == "service" and dev['method'] in ["can", "serial"]:
                            reboot_tasks.append({"id": dev['id'], "method": dev['method'], "name": dev['name']})
                
                # Now stop services to clear the bus
                yield await manage_klipper_services("stop")
                await asyncio.sleep(2) # Let the bus settle
                
                if reboot_tasks:
                    for dev_info in reboot_tasks:
                        yield f">>> Requesting Katapult reboot for {dev_info['name']} ({dev_info['id']})...\n"
                        async for log in flash_mgr.reboot_to_katapult(dev_info['id'], dev_info['method']):
                            yield log
                    
                    yield ">>> Waiting for devices to enter Katapult mode (up to 30s)...\n"
                    for i in range(15): # 15 * 2s = 30s
                        await asyncio.sleep(2)
                        ready_count = 0
                        for dev_info in reboot_tasks:
                            status = await flash_mgr.check_device_status(dev_info['id'], dev_info['method'])
                            if status == "ready":
                                ready_count += 1
                            elif status == "service" and i > 0 and i % 3 == 0:
                                yield f">>> Device {dev_info['name']} still in service. Retrying reboot...\n"
                                async for log in flash_mgr.reboot_to_katapult(dev_info['id'], dev_info['method']):
                                    yield log
                        
                        if ready_count == len(reboot_tasks):
                            yield f">>> All {ready_count} devices are ready!\n"
                            break
                        yield f">>> {ready_count}/{len(reboot_tasks)} devices ready... (waiting)\n"
                    else:
                        yield ">>> Timeout reached. Some devices may not have entered Katapult mode.\n"

                # 2b. Actual flashing
                for dev in devices:
                    if not dev['profile']:
                        continue
                    
                    # Check status if "ready" is specified
                    status = await flash_mgr.check_device_status(dev['id'], dev['method'])
                    
                    should_flash = False
                    if action == "flash-all" or action == "build-flash-all":
                        should_flash = True
                    elif (action == "flash-ready" or action == "build-flash-ready") and status == "ready":
                        should_flash = True
                    
                    if should_flash:
                        if status != "ready" and dev['method'] != "linux":
                            yield f"!!! Skipping {dev['name']} ({dev['id']}) - Device is {status}, not ready for flashing.\n"
                            continue

                        yield f"\n>>> FLASHING {dev['name']} ({dev['id']}) with {dev['profile']}...\n"
                        # Determine firmware path
                        if dev['method'] == "linux":
                            firmware_path = os.path.join(ARTIFACTS_DIR, f"{dev['profile']}.elf")
                        else:
                            firmware_path = os.path.join(ARTIFACTS_DIR, f"{dev['profile']}.bin")
                        
                        if not os.path.exists(firmware_path):
                            yield f"!!! Error: Firmware for {dev['profile']} not found. Skipping.\n"
                            continue
                            
                        if dev['method'] == "serial":
                            async for log in flash_mgr.flash_serial(dev['id'], firmware_path):
                                yield log
                        elif dev['method'] == "can":
                            async for log in flash_mgr.flash_can(dev['id'], firmware_path):
                                yield log
                        elif dev['method'] == "linux":
                            yield ">>> Linux Process 'flashing' is handled by the build process.\n"
                    else:
                        yield f">>> Skipping {dev['name']} (Status: {status})\n"
                
                yield "\n>>> BATCH FLASH COMPLETED <<<\n"
                
            yield "\n>>> ALL BATCH OPERATIONS COMPLETED <<<\n"
            
            # 3. Return to service phase
            if "flash" in action:
                yield ">>> Returning to service...\n"
                for dev in devices:
                    if dev['method'] == "can" and dev['profile']:
                        async for _ in flash_mgr.reboot_device(dev['id'], mode="service"):
                            pass
        finally:
            if "flash" in action:
                yield await manage_klipper_services("start")

    return StreamingResponse(generate(), media_type="text/plain")

@app.get("/download/{profile}")
async def download_firmware(profile: str):
    """Downloads the klipper.bin for the specified profile."""
    bin_path = os.path.join(ARTIFACTS_DIR, f"{profile}.bin")
    if not os.path.exists(bin_path):
        # Fallback to .elf if .bin doesn't exist (for Linux MCUs)
        bin_path = os.path.join(ARTIFACTS_DIR, f"{profile}.elf")
        
    if not os.path.exists(bin_path):
        raise HTTPException(status_code=404, detail="Firmware binary not found. Please build first.")
    
    ext = ".elf" if bin_path.endswith(".elf") else ".bin"
    return FileResponse(
        path=bin_path, 
        filename=f"{profile}{ext}",
        media_type='application/octet-stream'
    )

@app.get("/fleet")
async def get_fleet():
    """Returns the registered fleet of devices with status."""
    fleet = fleet_mgr.get_fleet()
    
    # Perform bulk discovery to avoid redundant tool calls
    can_devs = await flash_mgr.discover_can_devices()
    serial_devs = await flash_mgr.discover_serial_devices()
    linux_ready = os.path.exists("/tmp/klipper_host_mcu")
    
    # Create a lookup for CAN devices
    can_status_map = {d['id']: d.get('mode', 'offline') for d in can_devs}
    serial_ids = [d['id'] for d in serial_devs]

    # Add status to each device
    for dev in fleet:
        if dev['method'] == "can":
            dev['status'] = can_status_map.get(dev['id'], "offline")
        elif dev['method'] == "serial":
            dev['status'] = "ready" if dev['id'] in serial_ids else "offline"
        elif dev['method'] == "linux":
            dev['status'] = "ready" if linux_ready else "offline"
        else:
            dev['status'] = "unknown"
            
    return fleet

@app.post("/fleet/device")
async def save_device(device: Device):
    """Registers or updates a device in the fleet."""
    fleet_mgr.save_device(device.dict())
    return {"message": "Device saved to fleet"}

@app.delete("/fleet/device/{device_id}")
async def remove_device(device_id: str):
    """Removes a device from the fleet."""
    fleet_mgr.remove_device(device_id)
    return {"message": "Device removed from fleet"}

@app.get("/devices/discover")
async def discover_devices():
    """Discovers Serial, CAN, and Linux process devices."""
    serial_devs = await flash_mgr.discover_serial_devices()
    can_devs = await flash_mgr.discover_can_devices()
    linux_devs = flash_mgr.discover_linux_process()
    return {"serial": serial_devs, "can": can_devs, "linux": linux_devs}

@app.post("/flash")
async def flash_device(req: FlashRequest):
    """Flashes the specified profile to a device."""
    # Use the saved artifact for this profile
    if req.method == "linux":
        firmware_path = os.path.join(ARTIFACTS_DIR, f"{req.profile}.elf")
    else:
        firmware_path = os.path.join(ARTIFACTS_DIR, f"{req.profile}.bin")

    if not os.path.exists(firmware_path):
        raise HTTPException(status_code=400, detail=f"Firmware for profile '{req.profile}' not found. Please build first.")
    
    async def generate():
        try:
            yield await manage_klipper_services("stop")
            if req.method == "serial":
                async for log in flash_mgr.flash_serial(req.device_id, firmware_path):
                    yield log
            elif req.method == "can":
                async for log in flash_mgr.flash_can(req.device_id, firmware_path):
                    yield log
            elif req.method == "linux":
                yield ">>> Linux Process 'flashing' is handled by the build process (klipper-mcu service).\n"
                yield ">>> Ensure you have run 'make flash' manually or the klipper-mcu service is configured.\n"
                yield ">>> Success!\n"
        finally:
            yield await manage_klipper_services("start")

    return StreamingResponse(generate(), media_type="text/plain")

@app.post("/flash/reboot")
async def reboot_device(device_id: str, mode: str = "katapult"):
    """Reboots a CAN device."""
    return StreamingResponse(flash_mgr.reboot_device(device_id, mode), media_type="text/plain")

@app.post("/api/self-update")
async def self_update(background_tasks: BackgroundTasks):
    """Runs the update.sh script in the background."""
    update_script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "update.sh")
    if not os.path.exists(update_script):
        raise HTTPException(status_code=404, detail="Update script not found")
    
    # Run git fetch/reset immediately to see if it works
    try:
        import subprocess
        subprocess.check_call(["git", "fetch", "origin"], cwd=os.path.dirname(os.path.dirname(__file__)))
        subprocess.check_call(["git", "reset", "--hard", "origin/main"], cwd=os.path.dirname(os.path.dirname(__file__)))
    except Exception:
        pass

    def run_update():
        # Use nohup or similar to ensure the script continues after the service restarts
        subprocess.Popen(["bash", update_script], start_new_session=True)

    background_tasks.add_task(run_update)
    return {"message": "Update started. The service will restart shortly."}

# Serve UI
# Try to serve from the repository's ui folder first (for easier updates)
# Fallback to the data directory if not found
REPO_UI_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ui")
DATA_UI_DIR = os.path.join(DATA_DIR, "ui")

if os.path.exists(REPO_UI_DIR):
    app.mount("/", StaticFiles(directory=REPO_UI_DIR, html=True), name="ui")
elif os.path.exists(DATA_UI_DIR):
    app.mount("/", StaticFiles(directory=DATA_UI_DIR, html=True), name="ui")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8321)

