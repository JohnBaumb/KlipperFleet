#!/bin/bash
# KlipperFleet Update Script
set -Eeuo pipefail

if [ -n "${BASH_SOURCE[0]:-}" ]; then
    SRCDIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
else
    SRCDIR="$(pwd)"
fi

echo "KlipperFleet: Pulling latest changes..."
cd "$SRCDIR" || exit
git fetch origin
# Reset to the current branch instead of hardcoding main
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
git reset --hard "origin/$CURRENT_BRANCH"

echo "KlipperFleet: Running installation script..."
chmod +x install.sh
./install.sh

echo "KlipperFleet: Update process finished."
