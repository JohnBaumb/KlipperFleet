#!/bin/bash
set -Eeuo pipefail

# KlipperFleet Installer

log_info() { echo "KlipperFleet: $*"; }
log_warn() { echo "KlipperFleet: WARNING: $*" >&2; }
log_error() { echo "KlipperFleet: ERROR: $*" >&2; }

on_error() {
    local exit_code=$?
    log_error "Install failed at line ${BASH_LINENO[0]} while running: ${BASH_COMMAND}"
    log_error "See log: ${LOG_FILE:-/tmp/klipperfleet-install.log}"
    exit "$exit_code"
}
trap on_error ERR

if [ "$EUID" -ne 0 ]; then
    log_info "Not running as root; re-running with sudo."
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

log_info "Starting installation for user $USER..."

# 2. Self-Clone Support (for wget | bash)
if [ ! -d "${KF_PATH}/.git" ]; then
    log_info "Repository not found at ${KF_PATH}. Cloning..."
    apt-get update && apt-get install -y git
    sudo -u $USER git clone https://github.com/JohnBaumb/KlipperFleet.git "${KF_PATH}"
fi

# Switch to the repo directory
cd "${KF_PATH}"
SRCDIR=$(pwd)

# Fix ownership of the repository to ensure the user can access it
log_info "Fixing repository ownership..."
chown -R "$USER:$USER_GROUP" "$KF_PATH"

# Ensure all scripts are executable
chmod +x *.sh

# 3. Install System Dependencies
log_info "Installing system dependencies..."
DEPS=$(python3 -c "import json; print(' '.join(json.load(open('${SRCDIR}/install_scripts/system-dependencies.json'))['debian']))")
apt-get update && apt-get install -y $DEPS

# Setup udev rules for DFU devices
log_info "Setting up udev rules for DFU devices..."
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="df11", MODE="0666"' | sudo tee /etc/udev/rules.d/99-stm32-dfu.rules
sudo udevadm control --reload-rules
sudo udevadm trigger

# Setup passwordless sudo for commands KlipperFleet needs at runtime
# This is required on Ubuntu and other distros where the user doesn't have NOPASSWD by default.
log_info "Configuring sudoers for runtime commands..."
python3 "${SRCDIR}/install_scripts/setup_sudoers.py" "$USER"

# 4. Setup Python Virtual Environment
log_info "Setting up Python virtual environment..."
KF_VENV="${SRCDIR}/venv"
if [ ! -d "$KF_VENV" ]; then
    sudo -u $USER python3 -m venv "$KF_VENV"
fi

# Install Python dependencies
log_info "Installing Python dependencies from requirements.txt..."
sudo -u $USER "$KF_VENV/bin/pip" install -r "${SRCDIR}/backend/requirements.txt"
# Explicitly uninstall pip kconfiglib in production installs.
# KlipperFleet should prefer Klipper's bundled lib/kconfiglib at runtime.
sudo -u $USER "$KF_VENV/bin/pip" uninstall -y kconfiglib || true

# 5. Setup Data Directories
log_info "Setting up data directories..."
sudo -u $USER mkdir -p "$KF_DATA_DIR/profiles"
sudo -u $USER mkdir -p "$KF_DATA_DIR/ui"

# 6. Deploy UI
log_info "Deploying UI files..."
echo "Deploying UI from ${SRCDIR}/ui to $KF_DATA_DIR/ui/" >> "$LOG_FILE"
if [ -d "${SRCDIR}/ui" ]; then
    if [ -L "$KF_DATA_DIR/ui/index.html" ]; then
        echo "UI is symlinked; skipping copy." >> "$LOG_FILE"
    else
        sudo -u $USER cp -r "${SRCDIR}/ui/"* "$KF_DATA_DIR/ui/"
        echo "UI deployment command executed." >> "$LOG_FILE"
    fi
else
    echo "UI directory not found in SRCDIR!" >> "$LOG_FILE"
    log_warn "UI directory not found at ${SRCDIR}/ui (continuing)."
fi

# 7. Moonraker Integration (Update Manager)
log_info "Integrating with Moonraker..."
python3 "${SRCDIR}/install_scripts/setup_moonraker.py" "${USER_HOME}/printer_data/config/moonraker.conf" "$KF_PATH"

# 8. Mainsail Navigation Integration
log_info "Integrating with Mainsail navigation..."
NAVI_JSON="${MOONRAKER_CONFIG_DIR}/.theme/navi.json"
mkdir -p "${MOONRAKER_CONFIG_DIR}/.theme"
python3 "${SRCDIR}/install_scripts/setup_mainsail_navi.py" "$NAVI_JSON"

# Deploy redirect shim so the navi link preserves the user's hostname/IP.
MAINSAIL_ROOT="/home/${USER}/mainsail"
if [ -d "$MAINSAIL_ROOT" ]; then
    cp "${SRCDIR}/install_scripts/klipperfleet.html" "$MAINSAIL_ROOT/klipperfleet.html"
    chown "$USER:$USER_GROUP" "$MAINSAIL_ROOT/klipperfleet.html"
    chmod 644 "$MAINSAIL_ROOT/klipperfleet.html"
else
    log_warn "Mainsail web root not found at $MAINSAIL_ROOT; redirect shim not deployed."
fi

# Ensure mainsail theme paths are writable by the runtime user.
if ! chown -R "$USER:$USER_GROUP" "${MOONRAKER_CONFIG_DIR}/.theme"; then
    log_warn "Could not set owner on ${MOONRAKER_CONFIG_DIR}/.theme."
fi
if ! chmod 755 "${MOONRAKER_CONFIG_DIR}/.theme"; then
    log_warn "Could not set permissions on ${MOONRAKER_CONFIG_DIR}/.theme."
fi
if [ -f "$NAVI_JSON" ]; then
    if ! chown "$USER:$USER_GROUP" "$NAVI_JSON"; then
        log_warn "Could not set owner on ${NAVI_JSON}."
    fi
    if ! chmod 664 "$NAVI_JSON"; then
        log_warn "Could not set permissions on ${NAVI_JSON}."
    fi
fi

# 9. Fluidd Navigation Integration (requires fluidd-core/fluidd#1786)
log_info "Integrating with Fluidd navigation..."
python3 "${SRCDIR}/install_scripts/setup_fluidd_navi.py" || true

# Deploy redirect shim to Fluidd web root and register as persistent_files
# so Moonraker preserves it across Fluidd updates.
FLUIDD_ROOT="/home/${USER}/fluidd"
if [ -d "$FLUIDD_ROOT" ]; then
    cp "${SRCDIR}/install_scripts/klipperfleet.html" "$FLUIDD_ROOT/klipperfleet.html"
    chown "$USER:$USER_GROUP" "$FLUIDD_ROOT/klipperfleet.html"
    chmod 644 "$FLUIDD_ROOT/klipperfleet.html"
    log_info "Adding klipperfleet.html to Fluidd persistent_files..."
    python3 "${SRCDIR}/install_scripts/setup_moonraker.py" \
        --add-persistent-file fluidd klipperfleet.html \
        "${USER_HOME}/printer_data/config/moonraker.conf"
else
    log_warn "Fluidd web root not found at $FLUIDD_ROOT; redirect shim not deployed."
fi

# 10. Systemd Service
log_info "Creating systemd service..."
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
log_info "Installation complete!"
echo "Access the UI at: http://$(hostname -I | awk '{print $1}'):8321"
echo "Or check your Mainsail sidebar!"
