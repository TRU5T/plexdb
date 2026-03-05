#!/bin/sh
# Fix permissions for the plexdb data directory on Linux (e.g. Ubuntu VM).
# Run once so the app and your user can read/write (e.g. upload files).
# Usage: ./scripts/fix-data-permissions.sh [DIR]
#   DIR defaults to /mnt/user/appdata/plexdb

set -e
DATA_DIR="${1:-/mnt/user/appdata/plexdb}"
WHO="${SUDO_USER:-$USER}"

echo "Data directory: $DATA_DIR"
echo "Owner (user:group): $WHO"

if [ ! -d "$DATA_DIR" ]; then
  echo "Creating $DATA_DIR ..."
  sudo mkdir -p "$DATA_DIR"
fi

echo "Setting ownership to $WHO ..."
sudo chown -R "$WHO:$WHO" "$DATA_DIR"
sudo chmod -R u+rwX,go+rwX "$DATA_DIR"

echo "Done. You can now run the app and upload/copy files to $DATA_DIR"
