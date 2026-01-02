#!/bin/bash
# KlipperFleet Update Script

if [ -n "${BASH_SOURCE[0]:-}" ]; then
    SRCDIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
else
    SRCDIR="$(pwd)"
fi

echo "KlipperFleet: Pulling latest changes..."
cd "$SRCDIR" || exit
git fetch origin
git reset --hard origin/main

echo "KlipperFleet: Running installation script..."
chmod +x install.sh
./install.sh

echo "KlipperFleet: Update process finished."
