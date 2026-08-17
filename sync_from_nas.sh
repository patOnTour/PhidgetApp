#!/bin/bash
set -e

NAS_USER="patrick"
NAS_IP="192.168.50.7"
NAS_PATH="~/phidget_release"
LOCAL_PATH="/usr/userapps/PhidgetProject"

SERVICES="phidget-app.service phidget-analyzer.service phidget-web.service phidget-sync.service"

echo "=== 1. Prüfe und synchronisiere Dateien von Synology ==="
# --itemize-changes (-i) gibt Änderungen präzise aus
SYNC_OUTPUT=$(rsync -avzi --delete -e ssh \
    --exclude '*.db' \
    --exclude '*.db-journal' \
    --exclude 'logs/' \
    --exclude '__pycache__/' \
    --exclude 'project_export.txt' \
    --exclude 'config/config.json' \
    --exclude 'config/time_state.json' \
    --exclude 'config/channel_mapping.json' \
	--exclude 'config/secrets.json' \
    "${NAS_USER}@${NAS_IP}:${NAS_PATH}/" "${LOCAL_PATH}/")

echo "$SYNC_OUTPUT"

# Prüft, ob Zeilen mit Dateiänderungen vorhanden sind
CHANGED_FILES=$(echo "$SYNC_OUTPUT" | grep -E '^(\*deleting|<|>|c|\.d|\.f)' || true)

if [ -z "$CHANGED_FILES" ]; then
    echo "=== Keine Änderungen vorhanden. Dienste laufen ohne Unterbrechung weiter. ==="
    exit 0
fi

echo "=== 2. Änderungen erkannt. Setze Berechtigungen ==="
chmod +x ${LOCAL_PATH}/AppData/*.py 2>/dev/null || true
chmod +x ${LOCAL_PATH}/*.sh 2>/dev/null || true

echo "=== 3. Systemd-Dienste aktualisieren ==="
if [ -d "${LOCAL_PATH}/systemd" ]; then
    cp ${LOCAL_PATH}/systemd/*.service /etc/systemd/system/ 2>/dev/null || true
    systemctl daemon-reload
    systemctl enable $SERVICES 2>/dev/null || true
fi

echo "=== 4. Starte geänderte Dienste sauber neu ==="
systemctl restart $SERVICES

echo "=== 5. Status-Check ==="
sleep 2
systemctl --no-pager status phidget-app.service phidget-sync.service

echo "=== SYNC & UPDATE ERFOLGREICH BEENDET ==="