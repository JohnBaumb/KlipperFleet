#!/bin/bash
set -eu

# KlipperFleet Uninstaller

if [ "$EUID" -ne 0 ]; then
    echo "KlipperFleet: Not running as root; re-running with sudo."
    exec sudo bash "$0" "$@"
fi

# 1. Environment & Path Discovery
if [ -n "${SUDO_USER:-}" ]; then
    USER=$SUDO_USER
elif [ "$EUID" -eq 0 ]; then
    if [ -n "${BASH_SOURCE[0]:-}" ]; then
        USER=$(stat -c '%U' "$(dirname "${BASH_SOURCE[0]}")")
    else
        USER=$(stat -c '%U' "$(pwd)")
    fi
else
    USER=$(whoami)
fi
USER_HOME=$(getent passwd $USER | cut -d: -f6)

if [ -n "${BASH_SOURCE[0]:-}" ]; then
    SRCDIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
else
    SRCDIR="$(pwd)"
fi
MOONRAKER_CONFIG_DIR="${USER_HOME}/printer_data/config"
KF_DATA_DIR="${MOONRAKER_CONFIG_DIR}/klipperfleet"

echo "KlipperFleet: Starting uninstallation for user $USER..."

# 2. Stop and Remove Systemd Service
echo "KlipperFleet: Stopping and removing systemd service..."
if systemctl is-active --quiet klipperfleet; then
    systemctl stop klipperfleet
fi
if systemctl is-enabled --quiet klipperfleet; then
    systemctl disable klipperfleet
fi

if [ -f "/etc/systemd/system/klipperfleet.service" ]; then
    rm "/etc/systemd/system/klipperfleet.service"
    systemctl daemon-reload
    echo "KlipperFleet: Systemd service removed."
fi

# 3. Remove Virtual Environment
echo "KlipperFleet: Removing Python virtual environment..."
if [ -d "${SRCDIR}/venv" ]; then
    rm -rf "${SRCDIR}/venv"
    echo "KlipperFleet: Virtual environment removed."
fi

# 4. Remove Data and Artifacts
echo "KlipperFleet: Removing data and artifacts from ${KF_DATA_DIR}..."
if [ -d "$KF_DATA_DIR" ]; then
    rm -rf "$KF_DATA_DIR"
    echo "KlipperFleet: Data directory removed."
fi

# 5. Remove Moonraker Integration
echo "KlipperFleet: Removing Moonraker integration..."
MOONRAKER_CONF="${MOONRAKER_CONFIG_DIR}/moonraker.conf"
if [ -f "$MOONRAKER_CONF" ]; then
    # Remove the update_manager section and its contents
    # This uses sed to find the block starting with [update_manager klipperfleet] and delete until the next blank line or section
    sed -i '/\[update_manager klipperfleet\]/,/^$/d' "$MOONRAKER_CONF"
    echo "KlipperFleet: Moonraker update_manager section removed."
fi

# 6. Remove Mainsail Navigation Integration
echo "KlipperFleet: Removing Mainsail navigation entry..."
NAVI_JSON="${MOONRAKER_CONFIG_DIR}/.theme/navi.json"
if [ -f "$NAVI_JSON" ]; then
    # This is a bit tricky with sed because of JSON formatting. 
    # We'll use a temporary file to rebuild the JSON without the KlipperFleet entry.
    # We look for the line containing "KlipperFleet" and remove it, then fix commas.
    
    # 1. Remove the line containing KlipperFleet
    sed -i '/"title": "KlipperFleet"/d' "$NAVI_JSON"
    
    # 2. Fix potential trailing commas or empty arrays
    # Remove comma before the closing bracket if it exists
    sed -i 'N;s/,\n\]/\n\]/;P;D' "$NAVI_JSON"
    # If the array is now empty [ ], we can just remove the file or leave it as []
    if [ "$(grep -c "{" "$NAVI_JSON")" -eq 0 ]; then
        echo "[]" > "$NAVI_JSON"
    fi
    echo "KlipperFleet: Mainsail navigation entry removed."
fi

echo "KlipperFleet: Uninstallation complete."
echo "Note: The repository at ${SRCDIR} has not been removed. You can delete it manually if desired."
