#!/usr/bin/env python3
# -*- coding: utf-8 -*-

VERSION = "1.3.0"

import json
import os
import time
import subprocess
import requests
from datetime import datetime

REBOOT_LIMIT_SECONDS = 300  # 5 Minuten

PATHS_FILE = '/usr/userapps/PhidgetProject/config/paths.json'
with open(PATHS_FILE, 'r') as f:
    PATHS = json.load(f)

def get_device_name():
    """Liest den Gerätenamen aus der config.json. Fallback ist 'Messeinheit'."""
    try:
        config_path = PATHS.get('config_files', {}).get('main_config', '/usr/userapps/PhidgetProject/config/config.json')
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                data = json.load(f)
                name = data.get('device_name')
                if name:
                    return str(name).strip()
    except Exception:
        pass
    return "Messeinheit"

def get_admin_url_from_secrets():
    """Liest den ntfy-Kanal für Admin-/Netzwerk-Meldungen ausschließlich aus der secrets.json."""
    try:
        secrets_path = PATHS.get('config_files', {}).get('secrets', '/usr/userapps/PhidgetProject/config/secrets.json')
        if os.path.exists(secrets_path):
            with open(secrets_path, 'r') as f:
                data = json.load(f)
                channel = data.get('admin_notify', {}).get('channel_name') or data.get('notify', {}).get('channel_name')
                if channel:
                    return f"https://ntfy.sh/{channel.strip()}"
    except Exception:
        pass
    return None

DEVICE_NAME = get_device_name()
NTFY_URL = get_admin_url_from_secrets()
STATE_FILE = "/tmp/network_down_since.txt"
PENDING_ALERT_FILE = os.path.join(PATHS['app_data_dir'], "pending_reboot_alert.txt")

def check_internet():
    res = subprocess.run(["ping", "-c", "1", "-W", "3", "8.8.8.8"], capture_output=True)
    return res.returncode == 0

def main():
    now = time.time()
    
    if check_internet():
        if os.path.exists(STATE_FILE):
            try:
                os.remove(STATE_FILE)
            except Exception:
                pass
        return

    if not os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "w") as f:
                f.write(str(now))
        except Exception:
            pass
        return

    try:
        with open(STATE_FILE, "r") as f:
            down_since = float(f.read().strip())
    except Exception:
        down_since = now

    elapsed = now - down_since

    if elapsed >= REBOOT_LIMIT_SECONDS:
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        push_msg = (
            f"🚨 KRITISCH: Keine Netzwerkverbindung seit über 5 Minuten!\n"
            f"• Gerät: {DEVICE_NAME}\n"
            f"• Aktion: Automatischer System-Reboot wird ausgeführt.\n"
            f"• Zeit: {now_str}"
        )
        
        if NTFY_URL:
            try:
                requests.post(
                    NTFY_URL,
                    data=push_msg.encode('utf-8'),
                    headers={"Title": f"Reboot: {DEVICE_NAME} (Netzwerk)", "Priority": "urgent", "Tags": "rotating_light,skull"},
                    timeout=3
                )
            except Exception:
                pass
        
        try:
            with open(PENDING_ALERT_FILE, "w") as pf:
                pf.write(push_msg)
        except Exception:
            pass
        
        try:
            os.remove(STATE_FILE)
        except Exception:
            pass

        time.sleep(2)
        os.system("sudo reboot")

if __name__ == "__main__":
    main()