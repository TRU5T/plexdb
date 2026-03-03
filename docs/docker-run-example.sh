#!/bin/bash
# Build the image locally, then run with the same settings as Unraid.
# Use this until you push the image to GHCR.

cd "$(dirname "$0")/.."
docker build -t ghcr.io/tru5t/plexdb-merge:latest .

docker run -d \
  --name='PlexDBMerge' \
  --net='bridge' \
  --pids-limit 2048 \
  -e TZ="Australia/Perth" \
  -e HOST_OS="Unraid" \
  -e HOST_HOSTNAME="Tower" \
  -e HOST_CONTAINERNAME="PlexDBMerge" \
  -e 'PUID'='99' \
  -e 'PGID'='100' \
  -l net.unraid.docker.managed=dockerman \
  -l net.unraid.docker.webui='http://[IP]:[PORT:5000]/' \
  -l net.unraid.docker.icon='https://raw.githubusercontent.com/TRU5T/plexdb/main/docs/icon.png' \
  -p '2000:5000/tcp' \
  -v '/mnt/user/appdata/plexdb':'/data':'rw' \
  ghcr.io/tru5t/plexdb-merge:latest

echo "Web UI: http://Tower:2000/  (or http://YOUR-IP:2000/)"
