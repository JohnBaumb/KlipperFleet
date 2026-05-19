"""
Mock KlipperFleet backend for screenshot generation.
Serves realistic fake data so the real UI can render all views.
"""
import json
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

UI_DIR = Path(__file__).parent.parent / "ui"

# --- Fake Data ---

PROFILES = ["CR10_SpiderH7", "CR10_MMBCANV1", "Ender3_Spider3_F446", "CR10_Linux", "CR10_Beacon"]

PROFILE_INFO = {
    "CR10_SpiderH7": {"is_can_bridge": True, "is_linux": False, "is_avr": False},
    "CR10_MMBCANV1": {"is_can_bridge": False, "is_linux": False, "is_avr": False},
    "Ender3_Spider3_F446": {"is_can_bridge": False, "is_linux": False, "is_avr": False},
    "CR10_Linux": {"is_can_bridge": False, "is_linux": True, "is_avr": False},
    "CR10_Beacon": {"is_can_bridge": False, "is_linux": False, "is_avr": False},
}

FLEET_DEVICES = [
    {
        "name": "Spider H7 (CAN Bridge)",
        "id": "a1b2c3d4e5f6",
        "profile": "CR10_SpiderH7",
        "method": "can",
        "interface": "can0",
        "baudrate": 1000000,
        "notes": "Main CAN bridge on CR-10",
        "is_katapult": True,
        "is_bridge": True,
        "serial_id": None,
        "dfu_id": None,
        "magic_baud_tested": False,
        "use_magic_baud": False,
        "dfu_exit_tested": False,
        "use_dfu_exit": False,
        "exclude_from_batch": False,
        "custom_make_command": None,
        "status": "service",
        "flashed_version": "v0.12.0-340-g9f8e7d6c",
        "flashed_commit": "9f8e7d6c",
        "last_flashed": "2025-05-18T14:30:00Z",
    },
    {
        "name": "MMB CAN V1",
        "id": "f6e5d4c3b2a1",
        "profile": "CR10_MMBCANV1",
        "method": "can",
        "interface": "can0",
        "baudrate": 1000000,
        "notes": "Toolhead CAN node",
        "is_katapult": True,
        "is_bridge": False,
        "serial_id": None,
        "dfu_id": None,
        "magic_baud_tested": False,
        "use_magic_baud": False,
        "dfu_exit_tested": False,
        "use_dfu_exit": False,
        "exclude_from_batch": False,
        "custom_make_command": None,
        "status": "service",
        "flashed_version": "v0.12.0-340-g9f8e7d6c",
        "flashed_commit": "9f8e7d6c",
        "last_flashed": "2025-05-18T14:28:00Z",
    },
    {
        "name": "Linux MCU (Host)",
        "id": "linux_process",
        "profile": "CR10_Linux",
        "method": "linux",
        "interface": None,
        "baudrate": None,
        "notes": "Host MCU process",
        "is_katapult": False,
        "is_bridge": False,
        "serial_id": None,
        "dfu_id": None,
        "magic_baud_tested": False,
        "use_magic_baud": False,
        "dfu_exit_tested": False,
        "use_dfu_exit": False,
        "exclude_from_batch": False,
        "custom_make_command": None,
        "status": "service",
        "flashed_version": "v0.12.0-340-g9f8e7d6c",
        "flashed_commit": "9f8e7d6c",
        "last_flashed": "2025-05-18T14:32:00Z",
    },
]

FLEET_VERSIONS = {
    "a1b2c3d4e5f6": {
        "flashed_version": "v0.12.0-340-g9f8e7d6c",
        "flashed_commit": "9f8e7d6c",
        "last_flashed": "2025-05-18T14:30:00Z",
        "live_version": "v0.12.0-340-g9f8e7d6c",
        "method": "can",
        "remote_version": "v0.12.0-340-g9f8e7d6c",
    },
    "f6e5d4c3b2a1": {
        "flashed_version": "v0.12.0-340-g9f8e7d6c",
        "flashed_commit": "9f8e7d6c",
        "last_flashed": "2025-05-18T14:28:00Z",
        "live_version": "v0.12.0-340-g9f8e7d6c",
        "method": "can",
        "remote_version": "v0.12.0-340-g9f8e7d6c",
    },
    "linux_process": {
        "flashed_version": "v0.12.0-340-g9f8e7d6c",
        "flashed_commit": "9f8e7d6c",
        "last_flashed": "2025-05-18T14:32:00Z",
        "live_version": "v0.12.0-340-g9f8e7d6c",
        "method": "linux",
        "remote_version": "v0.12.0-340-g9f8e7d6c",
    },
}

DISCOVERY = {
    "serial": [
        {
            "id": "/dev/serial/by-id/usb-Klipper_stm32f446xx_12345-if00",
            "name": "Spider 3 (F446)",
            "type": "usb",
            "mode": "service",
            "managed": True,
        },
    ],
    "can": [
        {
            "id": "a1b2c3d4e5f6",
            "name": "SpiderH7 (CAN Bridge)",
            "application": "Klipper (Configured)",
            "mode": "service",
            "interface": "can0",
            "managed": True,
        },
        {
            "id": "f6e5d4c3b2a1",
            "name": "MMB CAN V1",
            "application": "Klipper (Configured)",
            "mode": "service",
            "interface": "can0",
            "managed": True,
        },
        {
            "id": "aa11bb22cc33",
            "name": "CAN Device (aa11bb22cc33)",
            "application": "Katapult",
            "mode": "ready",
            "interface": "can0",
            "managed": False,
        },
    ],
    "dfu": [],
    "linux": [
        {
            "id": "linux_process",
            "name": "Linux Process (Host MCU)",
            "mode": "service",
            "application": "Klipper (Linux)",
            "managed": True,
        },
    ],
    "beacon": [
        {
            "id": "/dev/serial/by-id/usb-Beacon_RevH_12345678-if00",
            "name": "Beacon RevH",
            "revision": "RevH",
            "serial": "12345678",
            "mode": "service",
            "interface": "usb",
            "application": "Beacon",
            "managed": True,
        },
    ],
}

SERVICES = [
    {"name": "klipper", "active": True, "status": "active (running)"},
    {"name": "klipper-mcu", "active": True, "status": "active (running)"},
    {"name": "moonraker", "active": True, "status": "active (running)"},
]

KCONFIG_TREE = [
    {
        "visible": True,
        "type": "menu",
        "prompt": "Micro-controller Architecture",
        "name": "MENU_MCU",
        "value": None,
        "readonly": False,
        "help": None,
        "children": [
            {
                "visible": True,
                "type": "choice",
                "prompt": "Architecture",
                "name": "ARCH",
                "value": "stm32",
                "readonly": False,
                "help": "Select the MCU architecture",
                "children": [],
                "choices": [
                    {"name": "ARCH_STM32", "prompt": "STMicroelectronics STM32"},
                    {"name": "ARCH_ATMEGA", "prompt": "Atmega AVR"},
                    {"name": "ARCH_LPC176X", "prompt": "LPC176x (Smoothieboard)"},
                    {"name": "ARCH_RP2040", "prompt": "Raspberry Pi RP2040/RP2350"},
                ],
            },
            {
                "visible": True,
                "type": "choice",
                "prompt": "Processor model",
                "name": "MCU",
                "value": "stm32h723xx",
                "readonly": False,
                "help": None,
                "children": [],
                "choices": [
                    {"name": "MCU_STM32H723", "prompt": "STM32H723"},
                    {"name": "MCU_STM32H743", "prompt": "STM32H743"},
                    {"name": "MCU_STM32F446", "prompt": "STM32F446"},
                    {"name": "MCU_STM32F407", "prompt": "STM32F407"},
                ],
            },
            {
                "visible": True,
                "type": "choice",
                "prompt": "Bootloader offset",
                "name": "BOOTLOADER",
                "value": "128KiB",
                "readonly": False,
                "help": "Offset from the start of flash for the application",
                "children": [],
                "choices": [
                    {"name": "BOOTLOADER_NONE", "prompt": "No bootloader"},
                    {"name": "BOOTLOADER_8KIB", "prompt": "8KiB bootloader"},
                    {"name": "BOOTLOADER_32KIB", "prompt": "32KiB bootloader"},
                    {"name": "BOOTLOADER_128KIB", "prompt": "128KiB bootloader"},
                ],
            },
            {
                "visible": True,
                "type": "choice",
                "prompt": "Clock Reference",
                "name": "CLOCK",
                "value": "25MHz",
                "readonly": False,
                "help": None,
                "children": [],
                "choices": [
                    {"name": "CLOCK_8MHZ", "prompt": "8 MHz crystal"},
                    {"name": "CLOCK_12MHZ", "prompt": "12 MHz crystal"},
                    {"name": "CLOCK_25MHZ", "prompt": "25 MHz crystal"},
                ],
            },
            {
                "visible": True,
                "type": "choice",
                "prompt": "Communication interface",
                "name": "COMM",
                "value": "can",
                "readonly": False,
                "help": None,
                "children": [],
                "choices": [
                    {"name": "COMM_USB", "prompt": "USB (on PA11/PA12)"},
                    {"name": "COMM_CAN", "prompt": "CAN bus (on PD0/PD1)"},
                    {"name": "COMM_SERIAL", "prompt": "Serial (on USART1 PA10/PA9)"},
                ],
            },
        ],
    },
    {
        "visible": True,
        "type": "menu",
        "prompt": "CAN bus",
        "name": "MENU_CAN",
        "value": None,
        "readonly": False,
        "help": None,
        "children": [
            {
                "visible": True,
                "type": "bool",
                "prompt": "USB to CAN bus bridge",
                "name": "USB_CAN_BRIDGE",
                "value": True,
                "readonly": False,
                "help": "Enable to use this device as a USB to CAN bus bridge",
                "children": [],
            },
            {
                "visible": True,
                "type": "int",
                "prompt": "CAN bus speed",
                "name": "CAN_SPEED",
                "value": "1000000",
                "readonly": False,
                "help": "CAN bus baud rate in bits/s",
                "children": [],
            },
        ],
    },
    {
        "visible": True,
        "type": "menu",
        "prompt": "Optional features",
        "name": "MENU_OPTIONAL",
        "value": None,
        "readonly": False,
        "help": None,
        "children": [
            {
                "visible": True,
                "type": "bool",
                "prompt": "GPIO startup state",
                "name": "INITIAL_PINS",
                "value": False,
                "readonly": False,
                "help": "Specify GPIO pins to set at MCU startup",
                "children": [],
            },
            {
                "visible": True,
                "type": "string",
                "prompt": "GPIO pins to set at micro-controller startup",
                "name": "INITIAL_PINS_VALUE",
                "value": "",
                "readonly": True,
                "help": None,
                "children": [],
            },
        ],
    },
]

# Fake batch build-flash-all log matching real output format
BATCH_BUILD_LOGS = [
    ">>> STARTING BATCH BUILD PHASE <<<\n",
    "\n",
    ">>> BATCH BUILD: Starting CR10_SpiderH7...\n",
    ">>> Loading profile: CR10_SpiderH7\n",
    ">>> Setting KCONFIG_CONFIG=/home/pi/KlipperFleet/profiles/CR10_SpiderH7.config\n",
    ">>> Running: make clean\n",
    ">>> Running: make -j4\n",
    "  Compiling out/src/sched.c\n",
    "  Compiling out/src/command.c\n",
    "  Compiling out/src/basecmd.c\n",
    "  Compiling out/src/stm32/main.c\n",
    "  Compiling out/src/stm32/gpio.c\n",
    "  Compiling out/src/stm32/canbus.c\n",
    "  Compiling out/src/generic/canbus.c\n",
    "  Linking out/klipper.elf\n",
    "  Creating bin file out/klipper.bin\n",
    ">>> Build successful! Firmware size: 28,432 bytes\n",
    ">>> Artifacts saved: CR10_SpiderH7.bin\n",
    ">>> BATCH BUILD: Finished CR10_SpiderH7\n",
    "\n",
    ">>> BATCH BUILD: Starting CR10_MMBCANV1...\n",
    ">>> Loading profile: CR10_MMBCANV1\n",
    ">>> Setting KCONFIG_CONFIG=/home/pi/KlipperFleet/profiles/CR10_MMBCANV1.config\n",
    ">>> Running: make clean\n",
    ">>> Running: make -j4\n",
    "  Compiling out/src/sched.c\n",
    "  Compiling out/src/command.c\n",
    "  Compiling out/src/basecmd.c\n",
    "  Compiling out/src/stm32/main.c\n",
    "  Compiling out/src/stm32/gpio.c\n",
    "  Compiling out/src/stm32/canserial.c\n",
    "  Compiling out/src/generic/canbus.c\n",
    "  Linking out/klipper.elf\n",
    "  Creating bin file out/klipper.bin\n",
    ">>> Build successful! Firmware size: 24,816 bytes\n",
    ">>> Artifacts saved: CR10_MMBCANV1.bin\n",
    ">>> BATCH BUILD: Finished CR10_MMBCANV1\n",
    "\n",
    ">>> BATCH BUILD: Starting CR10_Linux...\n",
    ">>> Loading profile: CR10_Linux\n",
    ">>> Setting KCONFIG_CONFIG=/home/pi/KlipperFleet/profiles/CR10_Linux.config\n",
    ">>> Running: make clean\n",
    ">>> Running: make flash\n",
    "  Compiling out/src/linux/main.c\n",
    "  Compiling out/src/linux/gpio.c\n",
    "  Compiling out/src/linux/spi.c\n",
    "  Compiling out/src/linux/i2c.c\n",
    "  Linking out/klipper.elf\n",
    ">>> Build successful! Firmware size: 18,204 bytes\n",
    ">>> Artifacts saved: CR10_Linux.bin\n",
    ">>> BATCH BUILD: Finished CR10_Linux\n",
    "\n",
    ">>> BATCH FLASH: Starting...\n",
    ">>> Stopping services: klipper-mcu.service, klipper.service, moonraker.service\n",
    ">>> Successfully Stopped: klipper-mcu.service, klipper.service, moonraker.service\n",
    "\n",
    ">>> Scanning CAN bus for devices in Katapult mode...\n",
    ">>> Rebooting MMB CAN V1 (f6e5d4c3b2a1) to Katapult...\n",
    ">>> Device f6e5d4c3b2a1 entered Katapult mode\n",
    "\n",
    ">>> BATCH FLASH: Flashing Linux MCU (Host)...\n",
    ">>> Running: make flash (Linux MCU)\n",
    "  Programming Complete\n",
    ">>> Flashing successful!\n",
    "\n",
    ">>> BATCH FLASH: Flashing MMB CAN V1 (f6e5d4c3b2a1)...\n",
    ">>> Running: python3 ~/katapult/scripts/flashtool.py -i can0 -u f6e5d4c3b2a1 -f ~/KlipperFleet/out/CR10_MMBCANV1.bin\n",
    "  Sending CAN bus ID query\n",
    "  Detected UUID: f6e5d4c3b2a1\n",
    "  Sending 24816 bytes...\n",
    "  [##################################################]\n",
    "  Programming Complete\n",
    ">>> Flashing successful!\n",
    "\n",
    ">>> BATCH FLASH: Flashing Spider H7 (CAN Bridge) (a1b2c3d4e5f6) [BRIDGE - LAST]...\n",
    ">>> Rebooting Spider H7 (CAN Bridge) to Katapult...\n",
    ">>> Device a1b2c3d4e5f6 entered Katapult mode\n",
    ">>> Running: python3 ~/katapult/scripts/flashtool.py -i can0 -u a1b2c3d4e5f6 -f ~/KlipperFleet/out/CR10_SpiderH7.bin\n",
    "  Sending CAN bus ID query\n",
    "  Detected UUID: a1b2c3d4e5f6\n",
    "  Sending 28432 bytes...\n",
    "  [##################################################]\n",
    "  Programming Complete\n",
    ">>> Flashing successful!\n",
    "\n",
    ">>> BATCH FLASH COMPLETED <<<\n",
    "\n",
    "======================== [SUMMARY] ========================\n",
    "\n",
    "  BUILD RESULTS:\n",
    "  [COLOR:GREEN]  - CR10_SpiderH7: SUCCESS[/COLOR]\n",
    "  [COLOR:GREEN]  - CR10_MMBCANV1: SUCCESS[/COLOR]\n",
    "  [COLOR:GREEN]  - CR10_Linux: SUCCESS[/COLOR]\n",
    "\n",
    "  FLASH RESULTS:\n",
    "  [COLOR:GREEN]  - Linux MCU (Host): SUCCESS[/COLOR]\n",
    "  [COLOR:GREEN]  - MMB CAN V1: SUCCESS[/COLOR]\n",
    "  [COLOR:GREEN]  - Spider H7 (CAN Bridge): SUCCESS[/COLOR]\n",
    "\n",
    "===========================================================\n",
    "\n",
    ">>> ALL BATCH OPERATIONS COMPLETED <<<\n",
    ">>> Returning to service...\n",
    ">>> Successfully Started: klipper-mcu.service, klipper.service, moonraker.service\n",
]


# --- Routes ---

@app.get("/profiles")
async def get_profiles():
    return {"profiles": PROFILES}


@app.get("/profiles/info")
async def get_profiles_info():
    return PROFILE_INFO


@app.post("/config/tree")
@app.get("/config/tree")
async def get_config_tree():
    return KCONFIG_TREE


@app.get("/fleet")
async def get_fleet(fast: bool = False):
    return FLEET_DEVICES


@app.get("/fleet/versions")
async def get_fleet_versions():
    return FLEET_VERSIONS


@app.get("/devices/discover")
async def get_discover():
    return DISCOVERY


@app.get("/services/status")
async def get_services():
    return SERVICES


@app.get("/api/status")
async def get_status():
    return {
        "message": "KlipperFleet is running",
        "klipper_dir": "/home/pi/klipper",
        "firmware_name": "Klipper",
        "is_klipper_kconfiglib": True,
        "commit": "1a2b3c4d",
        "branch": "master",
    }


@app.get("/api/print_status")
async def get_print_status():
    return {"printing": False, "state": "standby", "filename": ""}


@app.get("/api/health")
async def get_health():
    return {"healthy": True, "issues": []}


@app.get("/api/update-check")
async def get_update_check():
    return {
        "update_available": False,
        "commits_behind": 0,
        "branch": "main",
        "local_commit": "abc1234",
        "remote_commit": "abc1234",
    }


@app.get("/api/printer-ui")
async def get_printer_ui(port: int = 80):
    return {"uiName": "Mainsail"}


@app.get("/firmware/version")
@app.get("/klipper/version")
async def get_firmware_version():
    return {
        "version": "v0.12.0-340-g9f8e7d6c",
        "commit": "9f8e7d6c",
        "date": "2025-05-18",
    }


@app.get("/build/{profile}")
async def build_profile(profile: str):
    log = "".join(BATCH_BUILD_LOGS[:18])  # First profile build
    return PlainTextResponse(log, headers={"X-Task-Id": "mock-build-001"})


@app.get("/batch/{action}")
async def batch_action(action: str):
    return {"task_id": "mock-batch-001"}


@app.get("/task/status/{task_id}")
async def task_status(task_id: str):
    return {
        "status": "completed",
        "logs": BATCH_BUILD_LOGS,
        "completed": True,
        "cancelled": False,
        "device_statuses": {},
    }


@app.post("/task/cancel/{task_id}")
async def task_cancel(task_id: str):
    return {"message": "Task cancelled"}


@app.post("/config/save")
async def save_config():
    return {"message": "Profile saved"}


@app.delete("/profiles/{name}")
async def delete_profile(name: str):
    return {"message": f"Profile {name} deleted"}


@app.post("/profiles/{name}/rename")
async def rename_profile(name: str):
    return {"message": f"Profile renamed"}


@app.post("/fleet/device")
async def create_device():
    return {"message": "Device saved"}


@app.delete("/fleet/device")
async def delete_device():
    return {"message": "Device removed"}


@app.post("/fleet/attach")
async def attach_device():
    return {"message": "Device attached"}


@app.post("/flash")
async def flash_device():
    return PlainTextResponse(
        ">>> Flashing...\n>>> Flash successful!\n",
        headers={"X-Task-Id": "mock-flash-001"},
    )


@app.post("/flash/reboot")
async def reboot_device():
    return PlainTextResponse(
        ">>> Rebooting device...\n>>> Device rebooted successfully\n",
        headers={"X-Task-Id": "mock-reboot-001"},
    )


@app.post("/services/manage")
async def manage_services():
    return {"message": "Service action completed"}


# Serve the UI static files
app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8321)
