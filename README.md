# KlipperFleet

> [!WARNING]
> **ALPHA SOFTWARE**: KlipperFleet is currently in alpha. It has only been extensively tested on **CAN bus** and **Linux Process** devices. Use with caution on Serial/USB devices. Contributions and bug reports are highly appreciated!

KlipperFleet is a "one-stop-shop" for managing Klipper firmware across your entire fleet of MCUs on a single printer. It provides a modern web interface (integrated into Mainsail) to configure, build, and flash firmware without ever touching the command line.

## Features

- **Dynamic Web Configurator**: Replaces `make menuconfig` with a reactive web form that parses Klipper's source code in real-time.
- **Fleet Management**: Register all your MCUs (Serial or CAN), assign them profiles, and manage them from a single dashboard.
- **One-Click Batch Operations**: Build firmware for your entire fleet, flash all ready devices, or perform a full "Build & Flash All" with a single click.
- **Automatic Katapult Reboot**: Intelligent detection of Klipper vs. Katapult modes. If a device is in service, KlipperFleet can automatically reboot it into Katapult mode for flashing.
- **Service Management**: Automatically stops and starts Klipper/Moonraker services during flashing to ensure exclusive access to the CAN bus, preventing "Request Block" errors.
- **Integrated Flashing**: Flash firmware via Serial or CAN (Katapult) directly from the browser with real-time log streaming.
- **Mainsail Integration**: Designed to look and feel like a native part of the Mainsail ecosystem.

## Screenshots

### Dashboard
![Dashboard](https://raw.githubusercontent.com/JohnBaumb/KlipperFleet/main/images/dashboard.png)

### Configurator
![Configurator](https://raw.githubusercontent.com/JohnBaumb/KlipperFleet/main/images/configurator.png)

### Fleet Manager
![Fleet Manager](https://raw.githubusercontent.com/JohnBaumb/KlipperFleet/main/images/fleet_manager.png)

## Installation

Run this one-liner on your Raspberry Pi:

```bash
wget -qO - https://raw.githubusercontent.com/JohnBaumb/KlipperFleet/main/install.sh | sudo bash
```

### Manual Installation
If you prefer to clone manually:
```bash
cd ~
git clone https://github.com/JohnBaumb/KlipperFleet.git
cd KlipperFleet
sudo chmod +x install.sh
sudo ./install.sh
```

## Moonraker Integration

To enable one-click updates and integrate KlipperFleet into your Mainsail sidebar, add the following to your `moonraker.conf`:

### 1. Update Manager
```conf
[update_manager klipperfleet]
type: git_repo
path: ~/KlipperFleet
origin: https://github.com/JohnBaumb/KlipperFleet.git
primary_branch: main
managed_services: klipperfleet
install_script: install.sh
is_system_service: False
```

### 2. Mainsail Sidebar Tab
The installer automatically integrates KlipperFleet into your Mainsail navigation by modifying `.theme/navi.json`. 

> [!TIP]
> The `.theme` folder is hidden. To see it in the Mainsail file manager, you must enable **Show Hidden Files** in the Mainsail settings.

If the entry does not appear, you can manually add it to `.theme/navi.json`:

```json
[
  { 
    "title": "KlipperFleet", 
    "href": "http://<your-pi-ip>:8321", 
    "target": "_self", 
    "icon": "M20,21V19L17,16H13V13H16V11H13V8H16V6H13V3H11V6H8V8H11V11H8V13H11V16H7L4,19V21H20Z", 
    "position": 86 
  }
]
```

## Usage

1. **Configurator**: Go to the Configurator tab, select a profile name, and configure your MCU settings. Click **Save**.
2. **Fleet Manager**: Go to the Fleet Manager tab and click the **Scan** icon. Add your discovered devices to the fleet and assign them the profiles you created.
3. **Dashboard**: 
   - **Build All**: Compiles firmware for every profile assigned to a device in your fleet.
   - **Flash Ready**: Flashes all devices currently in Katapult mode.
   - **Flash All**: Automatically reboots all "In Service" devices into Katapult and flashes them.
   - **Build & Flash All**: The "One-Click" solution to update your entire printer's firmware in one go.

## Technical Details

- **Backend**: FastAPI (Python 3)
- **Kconfig Engine**: `kconfiglib`
- **Frontend**: Vue.js 3, Tailwind CSS
- **Flashing**: Katapult (`flashtool.py`)

## License
GPLv3
