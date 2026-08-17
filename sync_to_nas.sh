#!/bin/bash
python3 code_exporter.py

set -e

NAS_USER="patrick"
NAS_IP="192.168.50.7"
NAS_PATH="~/phidget_release"
LOCAL_PATH="/usr/userapps/PhidgetProject"

echo "=== 1. Sichere aktuelle Systemd-Dienste in Projektordner ==="
mkdir -p ${LOCAL_PATH}/systemd
cp -u /etc/systemd/system/phidget-*.service ${LOCAL_PATH}/systemd/ 2>/dev/null || true

echo "=== 2. Starte Upload zur Synology (${NAS_IP}) ==="
rsync -avz --progress --delete -e ssh \
    --exclude '*.db' \
    --exclude '*.db-journal' \
    --exclude 'logs/' \
    --exclude '__pycache__/' \
    --exclude 'config/config.json' \
    --exclude 'config/time_state.json' \
    "${LOCAL_PATH}/" "${NAS_USER}@${NAS_IP}:${NAS_PATH}/"

echo "=== PUSH ZUR SYNOLOGY ERFOLGREICH ABGESCHLOSSEN ==="
