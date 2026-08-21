#!/bin/bash

# ==============================================================================
# Automatisches Installationsskript für PhidgetSBC4 (Beton-Messkoffer)
# Version: 2.5.0 (Python-Setup-Wizard, Safe-Pip, Full Secrets JSON, Systemd)
# Voraussetzung: Login als root
# ==============================================================================

if [ "$EUID" -ne 0 ]; then
  echo "❌ Fehler: Dieses Skript muss als root ausgeführt werden!"
  exit 1
fi

REPO_DIR="/usr/userapps/PhidgetProject"
REPO_URL="git@github.com:patOnTour/PhidgetApp.git"
CONFIG_DIR="$REPO_DIR/config"

echo "================================================================================"
echo "1. Bereinige Reste & Legacy-Dateien..."
echo "================================================================================"
rm -rf /root/venv /usr/userapps/venv "$REPO_DIR/venv"

echo "================================================================================"
echo "2. Aktualisiere Paketquellen und System..."
echo "================================================================================"
apt-get update && apt-get upgrade -y

echo "================================================================================"
echo "3. Installiere Debian-Systempakete..."
echo "================================================================================"
apt-get install -y \
    python3 \
    rsync \
    curl \
    python3-pip \
    python3-dev \
    git \
    sqlite3 \
    libsqlite3-dev \
    libatlas-base-dev \
    libfreetype6-dev \
    libpng-dev \
    pkg-config \
    build-essential \
    python3-requests \
    python3-pam \
    python3-numpy \
    python3-pandas \
    python3-matplotlib \
    python3-simplejson \
    wireless-tools

echo "================================================================================"
echo "4. Installiere Python-Pakete (mit Flag-Sicherheitspruefung)..."
echo "================================================================================"
PIP_BREAK_FLAG=""
if /usr/bin/python3 -m pip install --help 2>&1 | grep -q -- '--break-system-packages'; then
    PIP_BREAK_FLAG="--break-system-packages"
fi

/usr/bin/python3 -m pip install $PIP_BREAK_FLAG --upgrade pip
/usr/bin/python3 -m pip install $PIP_BREAK_FLAG \
    Flask \
    flask_login \
    Phidget22 \
    tb-device-mqtt \
    simplepam \
    requests \
    numpy \
    pandas \
    matplotlib

echo "================================================================================"
echo "5. SSH-Schluessel für GitHub & Repository Klonen..."
echo "================================================================================"
if [ ! -f /root/.ssh/id_ed25519 ]; then
    ssh-keygen -t ed25519 -C "phidget-box-$(hostname)" -N "" -f /root/.ssh/id_ed25519
fi

echo ""
echo "👉 Bitte diesen SSH-Schluessel als Deploy Key auf GitHub eintragen:"
echo "----------------------------------------------------------------------"
cat /root/.ssh/id_ed25519.pub
echo "----------------------------------------------------------------------"
printf "Druecke ENTER, sobald der Key auf GitHub hinterlegt ist..."
read -r _unused

ssh -T git@github.com -o StrictHostKeyChecking=accept-new || true

mkdir -p /usr/userapps
git config --global --add safe.directory "$REPO_DIR"

if [ ! -d "$REPO_DIR/.git" ]; then
    if ! git clone "$REPO_URL" "$REPO_DIR"; then
        echo "❌ Fehler: Git-Clone fehlgeschlagen! Bitte pruefe den SSH-Key."
        exit 1
    fi
fi

cd "$REPO_DIR" || exit 1
LATEST_TAG=$(git tag -l "v*" | sort -V | tail -n 1 || echo "main")
git checkout -f "$LATEST_TAG"

# Verzeichnisse sicherstellen
mkdir -p "$CONFIG_DIR"
mkdir -p "$REPO_DIR/AppData/logs"

# .gitignore schreiben
cat << 'EOF' > "$REPO_DIR/.gitignore"
config/*.json
AppData/telemetry_buffer.db*
AppData/data.db*
AppData/logs/
**/__pycache__/
*.pyc
EOF

echo "================================================================================"
echo "6. Konfigurations-Assistent ausfuehren..."
echo "================================================================================"
cat << 'EOF' > /tmp/setup_config_runner.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os

REPO_DIR = "/usr/userapps/PhidgetProject"
CONFIG_DIR = os.path.join(REPO_DIR, "config")
os.makedirs(CONFIG_DIR, exist_ok=True)

print("=" * 80)
print("Konfigurations-Assistent fuer PhidgetSBC4")
print("=" * 80)

device_name = ""
while not device_name.strip():
    device_name = input("👉 Geraetename eingeben (device_name, z. B. ccssite01): ").strip()

friendly_name = ""
while not friendly_name.strip():
    friendly_name = input("👉 Friendly Name eingeben (z. B. Baustellenkoffer 1): ").strip()

phidget_serial = None
while phidget_serial is None:
    serial_in = input("👉 Phidget Seriennummer eingeben (phidget_serial): ").strip()
    if serial_in.isdigit():
        phidget_serial = int(serial_in)
    else:
        print("⚠️ Ungueltige Seriennummer. Bitte nur Ganzzahlen eingeben.")

channel_name = ""
while not channel_name.strip():
    channel_name = input("👉 ntfy Kanalname / Topic eingeben (channel_name): ").strip()

print("\n--- Belegung der VINT-Ports (0 bis 5) festlegen ---")
sensors = []
for port in range(6):
    while True:
        print(f"\nPort {port} Belegung:")
        print("  [1] humidity_temp (telemetry_key: Umgebung)")
        print("  [2] tc_4port      (telemetry_key: temp)")
        print("  [3] none          (telemetry_key: Unbelegt)")
        choice = input(f"Auswahl fuer Port {port} [1-3]: ").strip()

        if choice == "1":
            sensors.append({
                "port": port,
                "sensor_type": "humidity_temp",
                "telemetry_key": "Umgebung"
            })
            break
        elif choice == "2":
            sensors.append({
                "port": port,
                "sensor_type": "tc_4port",
                "telemetry_key": "temp"
            })
            break
        elif choice == "3":
            sensors.append({
                "port": port,
                "sensor_type": "none",
                "telemetry_key": "Unbelegt"
            })
            break
        else:
            print("⚠️ Ungueltige Auswahl. Bitte 1, 2 oder 3 waehlen.")

# config.json
config_path = os.path.join(CONFIG_DIR, "config.json")
config_data = {
    "device_name": device_name,
    "systemd_service": "phidget-app.service",
    "phidget_serial": phidget_serial,
    "temp_delta_min": 0.6,
    "temp_delta_max": 1.0,
    "interval_minutes": 2,
    "sensors": sensors
}
with open(config_path, "w", encoding="utf-8") as f:
    json.dump(config_data, f, indent=4, ensure_ascii=False)

# device_mapping.json
device_mapping_path = os.path.join(CONFIG_DIR, "device_mapping.json")
device_mapping = {}
if os.path.exists(device_mapping_path):
    try:
        with open(device_mapping_path, "r", encoding="utf-8") as f:
            device_mapping = json.load(f)
    except Exception:
        device_mapping = {}

device_mapping[device_name] = friendly_name

with open(device_mapping_path, "w", encoding="utf-8") as f:
    json.dump(device_mapping, f, indent=4, ensure_ascii=False)

# secrets.json
secrets_path = os.path.join(CONFIG_DIR, "secrets.json")
secrets_data = {
    "notify": {
        "channel_name": channel_name
    },
    "admin_notify": {
        "channel_name": "CCSPhidgetAdmin"
    },
    "thingsboard": {
        "host": "mqtt.thingsboard.cloud",
        "port": 1883,
        "username": "40wvw4o5z9hiua30e340",
        "password": "aenma0lxe1ufe5cqa41a"
    }
}

if os.path.exists(secrets_path):
    try:
        with open(secrets_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
            if "admin_notify" in existing:
                secrets_data["admin_notify"] = existing["admin_notify"]
            if "thingsboard" in existing:
                secrets_data["thingsboard"] = existing["thingsboard"]
    except Exception:
        pass

with open(secrets_path, "w", encoding="utf-8") as f:
    json.dump(secrets_data, f, indent=4, ensure_ascii=False)

# paths.json
paths_path = os.path.join(CONFIG_DIR, "paths.json")
paths_data = {
    "base_directory": REPO_DIR,
    "app_data_dir": os.path.join(REPO_DIR, "AppData"),
    "config_files": {
        "main_config": os.path.join(CONFIG_DIR, "config.json"),
        "secrets": os.path.join(CONFIG_DIR, "secrets.json"),
        "channel_mapping": os.path.join(CONFIG_DIR, "channel_mapping.json"),
        "device_mapping": os.path.join(CONFIG_DIR, "device_mapping.json")
    }
}
with open(paths_path, "w", encoding="utf-8") as f:
    json.dump(paths_data, f, indent=4, ensure_ascii=False)

# channel_mapping.json
channel_mapping_path = os.path.join(CONFIG_DIR, "channel_mapping.json")
if not os.path.exists(channel_mapping_path):
    channel_mapping_data = {
        "ambient": "Umgebung",
        "humidity": "Luftfeuchtigkeit",
        "temp0": "Kanal 1",
        "temp1": "Kanal 2",
        "temp2": "Kanal 3",
		"temp3": "Kanal 4",
		"temp4": "Kanal 5",
		"temp5": "Kanal 6",
		"temp6": "Kanal 7",
		"temp7": "Kanal 8"
    }
    with open(channel_mapping_path, "w", encoding="utf-8") as f:
        json.dump(channel_mapping_data, f, indent=4, ensure_ascii=False)

print(f"\n✅ Alle Konfigurationsdateien erfolgreich in '{CONFIG_DIR}' geschrieben.")
EOF

/usr/bin/python3 /tmp/setup_config_runner.py
rm -f /tmp/setup_config_runner.py

echo "================================================================================"
echo "7. Richte Auto-Timezone ein..."
echo "================================================================================"
cat << 'EOF' > /usr/local/bin/auto-timezone.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import urllib.request
import json
import subprocess

FALLBACK_TIMEZONE = "Europe/Zurich"

def update_timezone():
    timezone = None
    try:
        req = urllib.request.Request("http://ip-api.com/json/", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            timezone = data.get('timezone')
    except Exception as e:
        print(f"Fehler bei der IP-Geolokalisierung: {e}")

    if not timezone:
        timezone = FALLBACK_TIMEZONE

    try:
        subprocess.run(["timedatectl", "set-timezone", timezone], check=True)
        print(f"System-Zeitzone auf {timezone} gesetzt.")
    except Exception as e:
        print(f"Fehler beim Setzen von timedatectl: {e}")

if __name__ == "__main__":
    update_timezone()
EOF

chmod +x /usr/local/bin/auto-timezone.py

cat << 'EOF' > /etc/systemd/system/auto-timezone.service
[Unit]
Description=Auto Timezone Service via IP Geolocation
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/bin/auto-timezone.py
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

echo "================================================================================"
echo "8. Richte Auto-Updater & Timer ein..."
echo "================================================================================"
cat << 'EOF' > /usr/local/bin/phidget-autoupdate.sh
#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/usr/userapps/PhidgetProject"
DB_PATH="$REPO_DIR/AppData/telemetry_buffer.db"
SECRETS_FILE="$REPO_DIR/config/secrets.json"
CONFIG_FILE="$REPO_DIR/config/config.json"
SERVICES=("phidget-analyzer.service" "phidget-app.service" "phidget-sync.service" "phidget-web.service")

cd "$REPO_DIR"

NTFY_CHANNEL=""
DEVICE_NAME="PhidgetBox"
if [ -f "$SECRETS_FILE" ]; then
    NTFY_CHANNEL=$(python3 -c 'import json; d=json.load(open("'$SECRETS_FILE'")); print(d.get("notify", {}).get("channel_name", ""))' 2>/dev/null || true)
fi
if [ -f "$CONFIG_FILE" ]; then
    DEVICE_NAME=$(python3 -c 'import json; d=json.load(open("'$CONFIG_FILE'")); print(d.get("device_name", "PhidgetBox"))' 2>/dev/null || echo "PhidgetBox")
fi

send_ntfy() {
    local title="$1"
    local msg="$2"
    local tags="$3"
    local priority="${4:-default}"
    if [ -n "$NTFY_CHANNEL" ]; then
        curl -s \
            -H "Title: $title" \
            -H "Tags: $tags" \
            -H "Priority: $priority" \
            -d "$msg" \
            "https://ntfy.sh/$NTFY_CHANNEL" >/dev/null 2>&1 || true
    fi
}

if [ -f "$DB_PATH" ]; then
    ACTIVE_COUNT=$(sqlite3 "$DB_PATH" "
        SELECT 
            (SELECT COUNT(*) FROM channel_control WHERE UPPER(status) IN ('RUNNING', 'RUNN', 'TRIGGERED') OR force_export = 1)
            +
            (SELECT COUNT(*) FROM setting_state WHERE trigger_fired = 1 AND (export_status IS NULL OR export_status IN ('PENDING', 'RUNNING')));
    " 2>/dev/null || echo "0")

    if [ "$ACTIVE_COUNT" -gt 0 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Messung oder Export aktiv ($ACTIVE_COUNT Sperren). Update verschoben."
        exit 0
    fi
fi

git fetch --tags origin >/dev/null 2>&1

CURRENT_TAG=$(git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD)
LATEST_TAG=$(git tag -l "v*" | sort -V | tail -n 1)

if [ -z "$LATEST_TAG" ] || [ "$CURRENT_TAG" == "$LATEST_TAG" ]; then
    exit 0
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Update gefunden: $CURRENT_TAG -> $LATEST_TAG. Starte Rollout..."
git checkout "$LATEST_TAG"

if [ -f "requirements.txt" ]; then
    PIP_FLAG=""
    if pip3 install --help 2>&1 | grep -q -- '--break-system-packages'; then
        PIP_FLAG="--break-system-packages"
    fi
    pip3 install $PIP_FLAG -r requirements.txt --quiet
fi

FAILED_SERVICES=()
for s in "${SERVICES[@]}"; do
    if systemctl list-unit-files | grep -q "^$s"; then
        systemctl restart "$s" || FAILED_SERVICES+=("$s")
    fi
done

sleep 5

for s in "${SERVICES[@]}"; do
    if systemctl list-unit-files | grep -q "^$s"; then
        if ! systemctl is-active --quiet "$s"; then
            FAILED_SERVICES+=("$s")
        fi
    fi
done

if [ ${#FAILED_SERVICES[@]} -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Update auf $LATEST_TAG erfolgreich abgeschlossen."
    send_ntfy "📦 Update erfolgreich [$DEVICE_NAME]" "Software erfolgreich auf Version $LATEST_TAG aktualisiert." "package,white_check_mark"
else
    ERROR_MSG="Fehler bei Diensten: ${FAILED_SERVICES[*]} nach Update auf $LATEST_TAG. Starte Rollback auf $CURRENT_TAG..."
    echo "[ERROR] $ERROR_MSG"
    send_ntfy "🚨 Update FEHLGESCHLAGEN [$DEVICE_NAME]" "$ERROR_MSG" "warning,rotating_light" "high"
    
    git checkout "$CURRENT_TAG"
    for s in "${SERVICES[@]}"; do
        if systemctl list-unit-files | grep -q "^$s"; then
            systemctl restart "$s" || true
        fi
    done
    exit 1
fi
EOF

chmod +x /usr/local/bin/phidget-autoupdate.sh

cat << 'EOF' > /etc/systemd/system/phidget-updater.service
[Unit]
Description=Phidget Project Auto-Updater
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/phidget-autoupdate.sh
StandardOutput=journal
StandardError=journal
EOF

cat << 'EOF' > /etc/systemd/system/phidget-updater.timer
[Unit]
Description=Periodische Pruefung auf Phidget Softwareupdates

[Timer]
OnBootSec=3min
OnUnitActiveSec=15min
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
EOF

echo "================================================================================"
echo "9. Standard-Dienste einrichten..."
echo "================================================================================"

# WebGUI
cat << 'EOF' > /etc/systemd/system/phidget-web.service
[Unit]
Description=Phidget Web Management GUI
After=network.target auto-timezone.service

[Service]
User=root
WorkingDirectory=/usr/userapps/PhidgetProject/Web
ExecStart=/usr/bin/python3 /usr/userapps/PhidgetProject/Web/web_app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# App Service
cat << 'EOF' > /etc/systemd/system/phidget-app.service
[Unit]
Description=Phidget Measurement & Telemetry App
After=network.target auto-timezone.service

[Service]
User=root
WorkingDirectory=/usr/userapps/PhidgetProject/AppData
ExecStart=/usr/bin/python3 /usr/userapps/PhidgetProject/AppData/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Analyzer Service
cat << 'EOF' > /etc/systemd/system/phidget-analyzer.service
[Unit]
Description=Phidget Concrete Analyzer Service
After=network.target phidget-app.service auto-timezone.service

[Service]
User=root
WorkingDirectory=/usr/userapps/PhidgetProject/AppData
ExecStart=/usr/bin/python3 /usr/userapps/PhidgetProject/AppData/analyzer_service.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Sync Service
cat << 'EOF' > /etc/systemd/system/phidget-sync.service
[Unit]
Description=Phidget Data Sync Worker
After=network.target phidget-app.service

[Service]
User=root
WorkingDirectory=/usr/userapps/PhidgetProject/AppData
ExecStart=/usr/bin/python3 /usr/userapps/PhidgetProject/AppData/sync_worker.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

chmod +x /usr/userapps/PhidgetProject/AppData/*.py 2>/dev/null || true
chmod +x /usr/userapps/PhidgetProject/Web/*.py 2>/dev/null || true

timedatectl set-ntp true

systemctl daemon-reload
systemctl enable --now auto-timezone.service
systemctl enable --now phidget-updater.timer
systemctl enable --now phidget-web.service
systemctl enable --now phidget-app.service
systemctl enable --now phidget-analyzer.service
systemctl enable --now phidget-sync.service 2>/dev/null || true

echo "================================================================================"
echo "✅ Vollinstallation und Konfiguration erfolgreich abgeschlossen!"
echo "================================================================================"