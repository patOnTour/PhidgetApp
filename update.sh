#!/usr/bin/env bash
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "❌ Fehler: Skript muss als root ausgefuehrt werden!"
  exit 1
fi

REPO_DIR="/usr/userapps/PhidgetProject"
CONFIG_DIR="$REPO_DIR/config"
SECRETS_FILE="$CONFIG_DIR/secrets.json"

echo "=== 1. Ueberfluessige Legacy-Dienste deaktivieren & entfernen ==="
if systemctl is-active --quiet phidget-analyzer.service 2>/dev/null; then
    systemctl stop phidget-analyzer.service
fi
systemctl disable phidget-analyzer.service 2>/dev/null || true
rm -f /etc/systemd/system/phidget-analyzer.service

echo "=== 2. APT-Quellen bereinigen & System-Pakete nachinstallieren ==="
# Backports & tote Mirrors aus ALLEN Quellen restlos entfernen
sed -i -E '/(bullseye-backports|backports|ftp\.debian\.org)/d' /etc/apt/sources.list 2>/dev/null || true
sed -i -E '/(bullseye-backports|backports|ftp\.debian\.org)/d' /etc/apt/sources.list.d/*.list 2>/dev/null || true
rm -f /etc/apt/sources.list.d/*backports*.list /etc/apt/sources.list.d/multistrap-phidgets.list 2>/dev/null || true

# Saubere Basisquellen anlegen, falls Datei fehlt oder leer ist
if [ ! -f /etc/apt/sources.list ] || [ ! -s /etc/apt/sources.list ]; then
cat << 'EOF_APT' > /etc/apt/sources.list
deb http://deb.debian.org/debian bullseye main contrib non-free
deb http://security.debian.org/debian-security bullseye-security main contrib non-free
deb http://deb.debian.org/debian bullseye-updates main contrib non-free
EOF_APT
fi

while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
    echo "Warte auf Freigabe der Paketverwaltung..."
    sleep 2
done

apt-get update -y
apt-get install -y --fix-missing hostapd dnsmasq iptables wireless-tools curl

echo "=== 3. secrets.json aktualisieren & Standort zuweisen ==="
echo ""
echo "Waehle den ntfy-Standortkanal fuer dieses Geraet:"
echo "  [1] Concretum"
echo "  [2] EbiLab"
echo "  [3] Ebirec"
echo "  [4] Manuelle Eingabe"
read -rp "Auswahl [1-4] (Standard: 1): " LOCATION_CHOICE

case "$LOCATION_CHOICE" in
    2)
        SELECTED_CHANNEL="EbiLab"
        ;;
    3)
        SELECTED_CHANNEL="Ebirec"
        ;;
    4)
        read -rp "Bitte Kanalnamen eingeben: " SELECTED_CHANNEL
        ;;
    *)
        SELECTED_CHANNEL="Concretum"
        ;;
esac

echo "-> Gewaehlter Kanal: $SELECTED_CHANNEL"
mkdir -p "$CONFIG_DIR"

python3 - << PYEOF
import json, os

p = "$SECRETS_FILE"
selected_channel = "$SELECTED_CHANNEL"

d = {}
if os.path.exists(p):
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}

# 1. Eigener ntfy Server
if "ntfy" not in d:
    d["ntfy"] = {}
d["ntfy"]["server_url"] = "https://ntfy.concretum-setting.com"

# 2. Hauptkanal nach Standort
d["notify"] = {"channel_name": selected_channel}

# 3. Admin-Kanal fest auf "Admin"
d["admin_notify"] = {"channel_name": "Admin"}

# 4. ThingsBoard Defaults behalten oder setzen
if "thingsboard" not in d:
    d["thingsboard"] = {
        "host": "mqtt.thingsboard.cloud",
        "port": 1883,
        "username": "40wvw4o5z9hiua30e340",
        "password": "aenma0lxe1ufe5cqa41a"
    }

# 5. Telemetry API Ingest Sektion
if "api" not in d:
    d["api"] = {
        "ingest_url": "https://telemetry.concretum-setting.com/api/v1/telemetry/ingest",
        "token": "DeinGeheimerApiToken456!"
    }

with open(p, "w", encoding="utf-8") as f:
    json.dump(d, f, indent=4, ensure_ascii=False)

print(f"-> secrets.json aktualisiert: Server=https://ntfy.concretum-setting.com, Channel={selected_channel}, Admin=Admin")
PYEOF

echo "=== 4. Hotspot Manager einrichten ==="
cat << 'HTEOF' > /root/phidget-hotspot.sh
#!/usr/bin/env bash
sleep 10
if ping -c 1 -W 3 1.1.1.1 >/dev/null 2>&1; then
    echo "Internet vorhanden. Kein Hotspot noetig."
    systemctl stop hostapd dnsmasq 2>/dev/null || true
    exit 0
fi

echo "Kein Internet verfuegbar. Aktiviere Notfall-Hotspot..."
DEVICE_NAME=$(cat /usr/userapps/PhidgetProject/config/config.json 2>/dev/null | grep -oP '(?<="device_name": ")[^"]*' || echo "Box")

cat << CONF > /etc/hostapd/hostapd.conf
interface=wlan0
driver=nl80211
ssid=Phidget-$DEVICE_NAME
hw_mode=g
channel=7
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=concretum1234
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
CONF

cat << DCONF > /etc/dnsmasq.d/hotspot.conf
interface=wlan0
dhcp-range=192.168.4.2,192.168.4.50,255.255.255.0,24h
DCONF

ip addr flush dev wlan0
ip addr add 192.168.4.1/24 dev wlan0

systemctl restart dnsmasq
systemctl restart hostapd
HTEOF

chmod +x /root/phidget-hotspot.sh

cat << 'SEOF' > /etc/systemd/system/phidget-hotspot.service
[Unit]
Description=Phidget Offline Hotspot Manager
After=network.target

[Service]
Type=oneshot
ExecStart=/root/phidget-hotspot.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SEOF

echo "=== 5. Systemd-Dienste aktualisieren ==="
cat << 'EOF_APP' > /etc/systemd/system/phidget-app.service
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
EOF_APP

cat << 'EOF_SYNC' > /etc/systemd/system/phidget-sync.service
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
EOF_SYNC

cat << 'EOF_WEB' > /etc/systemd/system/phidget-web.service
[Unit]
Description=Phidget Web Management GUI
After=network.target

[Service]
User=root
WorkingDirectory=/usr/userapps/PhidgetProject/Web
ExecStart=/usr/bin/python3 /usr/userapps/PhidgetProject/Web/web_app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF_WEB

systemctl daemon-reload
systemctl enable phidget-hotspot.service
systemctl restart phidget-app.service phidget-sync.service phidget-web.service

echo "=== ✅ Update erfolgreich abgeschlossen! ==="
