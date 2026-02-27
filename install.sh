#!/bin/bash
set -eu

# KlipperFleet Installer
# Inspired by KRASH and TMC Autotune installers

if [ "$EUID" -ne 0 ]; then
    echo "KlipperFleet: Not running as root; re-running with sudo."
    exec sudo bash "$0" "$@"
fi

# 1. Environment & Path Discovery
if [ -n "${SUDO_USER:-}" ]; then
    USER=$SUDO_USER
elif [ "$EUID" -eq 0 ]; then
    # If running as root but no SUDO_USER (e.g. Moonraker update), 
    # use the owner of the script directory.
    if [ -n "${BASH_SOURCE[0]:-}" ]; then
        USER=$(stat -c '%U' "$(dirname "${BASH_SOURCE[0]}")")
    else
        USER=$(stat -c '%U' "$(pwd)")
    fi
else
    USER=$(whoami)
fi
USER_HOME=$(getent passwd $USER | cut -d: -f6)
USER_GROUP=$(id -gn $USER)

# Log for debugging automated installs
LOG_FILE="/tmp/klipperfleet-install.log"
echo "--- Install started at $(date) ---" > "$LOG_FILE"
echo "EUID: $EUID" >> "$LOG_FILE"
echo "USER: $USER" >> "$LOG_FILE"
echo "USER_HOME: $USER_HOME" >> "$LOG_FILE"

# Detect if we are running from within a KlipperFleet directory
if [ -n "${BASH_SOURCE[0]:-}" ]; then
    SRCDIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
else
    SRCDIR="$(pwd)"
fi

if [ -d "${SRCDIR}/.git" ]; then
    KF_PATH="${SRCDIR}"
else
    KF_PATH="${USER_HOME}/KlipperFleet"
fi

MOONRAKER_CONFIG_DIR="${USER_HOME}/printer_data/config"
KF_DATA_DIR="${MOONRAKER_CONFIG_DIR}/klipperfleet"

echo "KlipperFleet: Starting installation for user $USER..."

# 2. Self-Clone Support (for wget | bash)
if [ ! -d "${KF_PATH}/.git" ]; then
    echo "KlipperFleet: Repository not found at ${KF_PATH}. Cloning..."
    apt-get update && apt-get install -y git
    sudo -u $USER git clone https://github.com/JohnBaumb/KlipperFleet.git "${KF_PATH}"
fi

# Switch to the repo directory
cd "${KF_PATH}"
SRCDIR=$(pwd)

# Fix ownership of the repository to ensure the user can access it
echo "KlipperFleet: Fixing repository ownership..."
chown -R $USER:$USER_GROUP "$KF_PATH"

# Ensure all scripts are executable
chmod +x *.sh

# 3. Install System Dependencies
echo "KlipperFleet: Installing system dependencies..."
apt-get update && apt-get install -y python3-venv python3-pip git dfu-util

# Setup udev rules for DFU devices
echo "KlipperFleet: Setting up udev rules for DFU devices..."
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="df11", MODE="0666"' | sudo tee /etc/udev/rules.d/99-stm32-dfu.rules
sudo udevadm control --reload-rules
sudo udevadm trigger

# 4. Setup Python Virtual Environment
echo "KlipperFleet: Setting up Python virtual environment..."
KF_VENV="${SRCDIR}/venv"
if [ ! -d "$KF_VENV" ]; then
    sudo -u $USER python3 -m venv "$KF_VENV"
fi

# Install Python dependencies
echo "KlipperFleet: Installing Python dependencies from requirements.txt..."
sudo -u $USER "$KF_VENV/bin/pip" install -r "${SRCDIR}/backend/requirements.txt"

# Ensure kconfiglib is available for environments that require it
echo "KlipperFleet: Checking for kconfiglib..."
if sudo -u $USER "$KF_VENV/bin/python3" -c "import kconfiglib" >/dev/null 2>&1; then
    echo "KlipperFleet: kconfiglib already installed."
else
    echo "KlipperFleet: kconfiglib not found. Installing..."
    sudo -u $USER "$KF_VENV/bin/pip" install kconfiglib
    echo "KlipperFleet: kconfiglib installed successfully."
fi

# 5. Setup Data Directories
echo "KlipperFleet: Setting up data directories..."
sudo -u $USER mkdir -p "$KF_DATA_DIR/profiles"
sudo -u $USER mkdir -p "$KF_DATA_DIR/ui"

# 6. Deploy UI
echo "KlipperFleet: Deploying UI files..."
echo "Deploying UI from ${SRCDIR}/ui to $KF_DATA_DIR/ui/" >> "$LOG_FILE"
if [ -d "${SRCDIR}/ui" ]; then
    sudo -u $USER cp -r "${SRCDIR}/ui/"* "$KF_DATA_DIR/ui/"
    echo "UI deployment command executed." >> "$LOG_FILE"
else
    echo "UI directory not found in SRCDIR!" >> "$LOG_FILE"
fi

# 7. Moonraker Integration (Update Manager)
echo "KlipperFleet: Integrating with Moonraker..."
MOONRAKER_CONF="${USER_HOME}/printer_data/config/moonraker.conf"

if [ -f "$MOONRAKER_CONF" ]; then
    if ! grep -q "\[update_manager klipperfleet\]" "$MOONRAKER_CONF"; then
        echo "KlipperFleet: Adding update_manager to moonraker.conf..."
        cat >> "$MOONRAKER_CONF" << EOF

[update_manager klipperfleet]
type: git_repo
path: ${KF_PATH}
origin: https://github.com/JohnBaumb/KlipperFleet.git
primary_branch: main
managed_services: klipperfleet
install_script: install.sh
is_system_service: False
EOF
    fi
fi

# 8. Mainsail Navigation Integration
echo "KlipperFleet: Integrating with Mainsail navigation..."
NAVI_JSON="${MOONRAKER_CONFIG_DIR}/.theme/navi.json"
mkdir -p "${MOONRAKER_CONFIG_DIR}/.theme"

# Icon: ship (M20,21V19L17,16H13V13H16V11H13V8H16V6H13V3H11V6H8V8H11V11H8V13H11V16H7L4,19V21H20Z)
KF_HOSTNAME="$(hostname)"
if [ ! -f "$NAVI_JSON" ]; then
    echo "KlipperFleet: Creating navi.json..."
fi

python3 - "$NAVI_JSON" "$KF_HOSTNAME" << 'PY'
import json
import os
import sys

path = sys.argv[1]
hostname = sys.argv[2]

entry = {
    "title": "KlipperFleet",
    "href": f"http://{hostname}:8321",
    "target": "_self",
    "icon": "M20,21V19L17,16H13V13H16V11H13V8H16V6H13V3H11V6H8V8H11V11H8V13H11V16H7L4,19V21H20Z",
    "position": 86
}

data = []
if os.path.exists(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, list):
                data = loaded
    except Exception:
        data = []

# Remove stale KlipperFleet entries, then append current one
data = [item for item in data if not (isinstance(item, dict) and item.get("title") == "KlipperFleet")]
data.append(entry)

with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY

# 9. Systemd Service
echo "KlipperFleet: Creating systemd service..."
SERVICE_FILE="/etc/systemd/system/klipperfleet.service"
cat > "$SERVICE_FILE" << EOF
[Unit]
Description=KlipperFleet Backend Service
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=${SRCDIR}
ExecStart=${KF_VENV}/bin/python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8321
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable klipperfleet
systemctl restart klipperfleet

echo ""
echo "KlipperFleet: Installation complete!"
echo "Access the UI at: http://$(hostname -I | awk '{print $1}'):8321"
echo "Or check your Mainsail sidebar!"
