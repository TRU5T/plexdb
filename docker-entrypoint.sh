#!/bin/sh
# Ensure /data exists and is owned by PUID:PGID so the app (and host uploads) can write.
# Then run the main command as that user.

set -e
PUID="${PUID:-99}"
PGID="${PGID:-100}"

mkdir -p /data
chown -R "${PUID}:${PGID}" /data

exec gosu "${PUID}:${PGID}" "$@"
