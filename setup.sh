#!/usr/bin/env bash
# ==============================================================================
# @file: setup.sh
# @version: 7.2.0
# @date: 2026-08-25
# @description: All-in-One Installer & Updater fuer Phidget Edge-Boxen mit
#               integrierter SSH Deploy Key Erstellung und GitHub-Verbindungspruefung.
# @author: Patrick Staehli
# ==============================================================================
set -e

APP_DIR="/usr/userapps/PhidgetProject"
GIT_REPO_SSH="git@github.com:patOnTour/PhidgetApp.git"
GIT_REPO_HTTPS="https://github.com/patOnTour/PhidgetApp.git"

SSH_KEY_DIR="/root/.ssh"
SSH_KEY_PRIV="$SSH_KEY_DIR/id_ed25519"
SSH_KEY_PUB="$SSH_KEY_DIR/id_ed25519.pub"

DEFAULT_SERVER_URL="https://telemetry.concretum-setting.com/api/v1/telemetry/ingest"
FIXED_API_TOKEN="DeinGeheimerApiToken456!"
FIXED_NTFY_SERVER="https://ntfy.concretum-setting.com"
DEFAULT_LOCATION_CHANNEL="Concretum"
DEFAULT_ADMIN_CHANNEL="Admin"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}=====================================================${NC}"
echo -e "${BLUE}    PhidgetBox All-in-One Installer & Updater        ${NC}"
echo -e "${BLUE}=====================================================${NC}"

if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}Fehler: Bitte als root ausfuehren (sudo ./setup.sh)${NC}"
    exit 1
fi

# 1. Modus waehlen
echo ""
echo "Bitte Modus waehlen:"
echo " 1) Update durchfuehren (Bestehende Box aktualisieren, config.yaml bleibt erhalten)"
echo " 2) Neuinstallation (Frischen Koffer einrichten / config.yaml neu erstellen)"
read -rp "Auswahl [1/2]: " MODE_CHOICE

if [ "$MODE_CHOICE" == "1" ]; then
    MODE="UPDATE"
else
    MODE="INSTALL"
fi

echo -e "\n${YELLOW}--> Starte $MODE...${NC}"

# 2. Uhrzeit synchronisieren
echo -e "${YELLOW}--> Synchronisiere Systemzeit...${NC}"
date -s "$(curl -sI https://google.com | grep -i '^date:' | cut -d' ' -f3-)" 2>/dev/null || true

# 3. SSH-Schluessel pruefen & GitHub-Authentifizierung
echo -e "\n${YELLOW}--> Pruefe GitHub SSH-Authentifizierung...${NC}"
mkdir -p "$SSH_KEY_DIR"
chmod 700 "$SSH_KEY_DIR"

if [ ! -f "$SSH_KEY_PRIV" ]; then
    echo -e "${YELLOW}Kein SSH-Schluessel gefunden. Generiere neuen Ed25519-Key...${NC}"
    ssh-keygen -t ed25519 -C "phidget-box-$(hostname)" -N "" -f "$SSH_KEY_PRIV"
fi

# GitHub Host Key hinterlegen
ssh-keyscan -t rsa,ecdsa,ed25519 github.com >> "$SSH_KEY_DIR/known_hosts" 2>/dev/null || true

# Verbindungstest zu GitHub
SSH_AUTH_OK=false
SSH_TEST=$(ssh -o StrictHostKeyChecking=accept-new -T git@github.com 2>&1 || true)
if echo "$SSH_TEST" | grep -qi "successfully authenticated"; then
    echo -e "${GREEN}✔ GitHub SSH-Authentifizierung erfolgreich!${NC}"
    SSH_AUTH_OK=true
    GIT_REPO="$GIT_REPO_SSH"
else
    echo -e "${RED}⚠ GitHub verweigert den SSH-Zugriff (Permission denied).${NC}"
    echo -e "${YELLOW}------------------------------------------------------------${NC}"
    echo -e "Bitte folgenden Public Key auf GitHub als Deploy Key hinterlegen:"
    echo -e "URL: ${BLUE}https://github.com/patOnTour/PhidgetApp/settings/keys${NC}\n"
    cat "$SSH_KEY_PUB"
    echo -e "\n${YELLOW}------------------------------------------------------------${NC}"
    echo "Tipp: Haken bei 'Allow write access' setzen, falls von der Box gepusht werden soll."
    
    read -rp "Schluessel auf GitHub hinterlegt? [J/n]: " KEY_CONFIRMED
    KEY_CONFIRMED=${KEY_CONFIRMED:-J}
    
    if [[ "$KEY_CONFIRMED" =~ ^[jJyY]$ ]]; then
        SSH_TEST_AGAIN=$(ssh -o StrictHostKeyChecking=accept-new -T git@github.com 2>&1 || true)
        if echo "$SSH_TEST_AGAIN" | grep -qi "successfully authenticated"; then
            echo -e "${GREEN}✔ Verbindung erfolgreich bestaetigt!${NC}"
            GIT_REPO="$GIT_REPO_SSH"
        else
            echo -e "${YELLOW}SSH-Auth weiterhin inaktiv. Verwende HTTPS-Fallback...${NC}"
            GIT_REPO="$GIT_REPO_HTTPS"
        fi
    else
        echo -e "${YELLOW}Verwende HTTPS fuer den Klon-Vorgang...${NC}"
        GIT_REPO="$GIT_REPO_HTTPS"
    fi
fi

# 4. Dienste stoppen bei Update
if [ "$MODE" == "UPDATE" ]; then
    echo -e "${YELLOW}--> Stoppe aktive Phidget-Dienste...${NC}"
    systemctl stop phidget-reader.service phidget-sync.service phidget-web.service phidget-netmon.service phidget-updater.service 2>/dev/null || true
fi

# 5. Paket-Abhaengigkeiten installieren
echo -e "${YELLOW}--> Installiere Python-Pakete & System-Tools...${NC}"
apt-get update -y >/dev/null 2>&1 || true
apt-get install -y git python3-pip python3-yaml python3-numpy python3-requests python3-flask >/dev/null 2>&1 || true

PIP_FLAGS=""
if python3 -m pip install --help 2>&1 | grep -q -- '--break-system-packages'; then
    PIP_FLAGS="--break-system-packages"
fi
python3 -m pip install $PIP_FLAGS pyyaml numpy requests flask Phidget22 >/dev/null 2>&1 || true

# 6. Git Repository vorbereiten
mkdir -p /usr/userapps
if [ "$MODE" == "INSTALL" ] && [ -d "$APP_DIR" ]; then
    echo -e "${YELLOW}--> Entferne altes Verzeichnis fuer Neuinstallation...${NC}"
    rm -rf "$APP_DIR"
fi

if [ ! -d "$APP_DIR/.git" ]; then
    echo -e "${YELLOW}--> Klone Repository von GitHub ($GIT_REPO)...${NC}"
    git clone "$GIT_REPO" "$APP_DIR"
else
    echo -e "${YELLOW}--> Aktualisiere Git-Repository...${NC}"
    cd "$APP_DIR"
    git config --global --add safe.directory "$APP_DIR"
    git remote set-url origin "$GIT_REPO"
    git fetch origin main
    git reset --hard origin/main
fi

cd "$APP_DIR"
git config --global --add safe.directory "$APP_DIR"

# 7. Neuinstallations-Dialog & Hardware-Erkennung
if [ "$MODE" == "INSTALL" ]; then
    echo -e "\n${BLUE}--- Geraete-Identifikation ---${NC}"
    read -rp "Geraete-ID (z. B. ccssite01): " DEV_ID
    read -rp "Anzeigename (z. B. Baustellenkoffer 1): " DEV_NAME
    read -rp "Location Channel [Default: ${DEFAULT_LOCATION_CHANNEL}]: " LOC_CHANNEL
    LOC_CHANNEL=${LOC_CHANNEL:-$DEFAULT_LOCATION_CHANNEL}
    read -rp "Admin Channel [Default: ${DEFAULT_ADMIN_CHANNEL}]: " ADM_CHANNEL
    ADM_CHANNEL=${ADM_CHANNEL:-$DEFAULT_ADMIN_CHANNEL}

    echo -e "\n${YELLOW}--> Scanne Phidget VINT-Hardware via phidget22admin...${NC}"
    ADMIN_OUTPUT=$(phidget22admin -d -v 2>/dev/null || true)

    DETECTED_SERIAL=$(echo "$ADMIN_OUTPUT" | grep -oE '\([0-9]+\) 6-Port' | head -n1 | grep -oE '[0-9]+' || true)
    if [ -z "$DETECTED_SERIAL" ]; then
        DETECTED_SERIAL=$(python3 -c "
import sys
try:
    from Phidget22.Devices.Hub import Hub
    h = Hub()
    h.setIsLocal(True)
    h.openWaitForAttachment(2000)
    print(h.getDeviceSerialNumber())
    h.close()
except Exception:
    sys.exit(0)
" 2>/dev/null || true)
    fi

    if [ -n "$DETECTED_SERIAL" ]; then
        echo -e "${GREEN}✔ Phidget VINT-Hub erkannt! Seriennummer: ${DETECTED_SERIAL}${NC}"
    else
        echo -e "${RED}⚠ Kein VINT-Hub automatisch erkannt.${NC}"
        read -rp "Bitte Phidget Seriennummer manuell eingeben (z. B. 625458): " DETECTED_SERIAL
    fi

    declare -A PORT_MAP
    for p in {0..5}; do
        MATCH=$(echo "$ADMIN_OUTPUT" | grep -E "\(${DETECTED_SERIAL}/${p}\)" | head -n1 || true)
        if echo "$MATCH" | grep -qi "Humidity"; then
            PORT_MAP[$p]="humidity_temp"
        elif echo "$MATCH" | grep -qi "Thermocouple"; then
            PORT_MAP[$p]="tc_4port"
        fi
    done

    AUTO_PORTS_FOUND=${#PORT_MAP[@]}

    if [ "$AUTO_PORTS_FOUND" -ge 1 ]; then
        echo -e "${GREEN}✔ Folgende Port-Belegung wurde automatisch erkannt:${NC}"
        for p in "${!PORT_MAP[@]}"; do
            echo "   - Port $p: ${PORT_MAP[$p]}"
        done
        read -rp "Diese Port-Belegung uebernehmen? [J/n]: " ACCEPT_PORTS
        ACCEPT_PORTS=${ACCEPT_PORTS:-J}
    else
        ACCEPT_PORTS="n"
    fi

    if [[ ! "$ACCEPT_PORTS" =~ ^[jJyY]$ ]]; then
        echo -e "\n${YELLOW}--> Manuelle Port-Konfiguration (Ports 0 bis 5):${NC}"
        PORT_MAP=()
        for p in {0..5}; do
            echo -e "\nKonfiguration fuer ${BLUE}Port $p${NC}:"
            echo "  0) Keines / Nicht belegt"
            echo "  1) Humidity Phidget (humidity_temp)"
            echo "  2) Thermocouple Phidget (tc_4port)"
            read -rp "Auswahl fuer Port $p [0/1/2, Default: 0]: " P_CHOICE
            case "$P_CHOICE" in
                1) PORT_MAP[$p]="humidity_temp" ;;
                2) PORT_MAP[$p]="tc_4port" ;;
                *) ;;
            esac
        done
    fi

    echo -e "\n${YELLOW}--> Generiere config/config.yaml...${NC}"
    mkdir -p "$APP_DIR/config"

    cat << EOF_CFG > "$APP_DIR/config/config.yaml"
device:
  device_id: "${DEV_ID}"
  friendly_name: "${DEV_NAME}"
  phidget_serial: ${DETECTED_SERIAL}

server:
  ingest_url: "${DEFAULT_SERVER_URL}"
  api_token: "${FIXED_API_TOKEN}"
  batch_size: 200
  sync_interval_sec: 1.0

updater:
  check_interval_sec: 900

ntfy:
  server_url: "${FIXED_NTFY_SERVER}"
  location_channel: "${LOC_CHANNEL}"
  admin_channel: "${ADM_CHANNEL}"

network:
  ping_target: "1.1.1.1"
  check_interval_sec: 15
  hotspot_fallback_min: 5

sensors:
EOF_CFG

    for p in $(echo "${!PORT_MAP[@]}" | tr ' ' '\n' | sort -n); do
        stype="${PORT_MAP[$p]}"
        slabel="Umgebung"
        if [ "$stype" == "tc_4port" ]; then
            slabel="Temp"
        fi
        cat << EOF_SENS >> "$APP_DIR/config/config.yaml"
  - port: ${p}
    type: "${stype}"
    label: "${slabel}"
EOF_SENS
    done

    echo -e "${GREEN}✔ config.yaml erfolgreich erstellt.${NC}"
fi

# 8. Systemd-Dienste verlinken und aktivieren
echo -e "\n${YELLOW}--> Registriere und starte Systemd-Dienste...${NC}"
mkdir -p /etc/systemd/system
for sfile in "$APP_DIR/systemd/"*.service; do
    if [ -f "$sfile" ]; then
        sname=$(basename "$sfile")
        ln -sf "$sfile" "/etc/systemd/system/$sname"
        systemctl enable "$sname" >/dev/null 2>&1 || true
        echo "   ✔ Verlinkt: $sname"
    fi
done

systemctl daemon-reload
systemctl restart phidget-reader.service phidget-sync.service phidget-web.service phidget-netmon.service phidget-updater.service 2>/dev/null || true

echo -e "\n${GREEN}=====================================================${NC}"
echo -e "${GREEN}       SETUP / UPDATE ERFOLGREICH ABGESCHLOSSEN      ${NC}"
echo -e "${GREEN}=====================================================${NC}"
systemctl status phidget-updater.service --no-pager