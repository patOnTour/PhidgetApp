#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: notifier.py
Beschreibung: Versendet formatierte ntfy-Push-Benachrichtigungen (Startup, Fehler, Warnungen).
"""

import json
import os
import requests
import datetime

CONFIG_DIR = "/usr/userapps/PhidgetProject/config"
SECRETS_FILE = os.path.join(CONFIG_DIR, "secrets.json")
DEVICE_MAPPING_FILE = os.path.join(CONFIG_DIR, "device_mapping.json")

def load_json(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def get_config():
    secrets = load_json(SECRETS_FILE)
    server_url = secrets.get("ntfy", {}).get("server_url", "https://ntfy.concretum-setting.com")
    location_channel = secrets.get("notify", {}).get("channel_name", "Concretum")
    admin_channel = secrets.get("admin_notify", {}).get("channel_name", "Admin")
    return server_url, location_channel, admin_channel

def get_friendly_name(device_name):
    mapping = load_json(DEVICE_MAPPING_FILE)
    return mapping.get(device_name, device_name)

def send_startup_notification(ip, device_name, tz_name="Europe/Zurich", ntp_synced=True):
    server_url, location_channel, admin_channel = get_config()
    friendly_name = get_friendly_name(device_name)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    sync_status = "OK (Synchron)" if ntp_synced else "Nicht synchronisiert"
    
    msg = (
        f"• Geraet: {friendly_name} ({device_name})\n"
        f"• IP: {ip}\n"
        f"• WebGUI: {ip}:8081\n"
        f"• Zeitzone: {tz_name}\n"
        f"• Zeitsync: {sync_status}\n"
        f"• Systemzeit: {now_str}"
    )
    
    headers = {
        "Title": f"Start-up BEREIT [{friendly_name}]",
        "Priority": "default",
        "Tags": "rocket,white_check_mark",
        "Actions": f"view, STATUS, http://{ip}:8081, clear=true; view, WEBGUI, http://{ip}:8081, clear=true"
    }
    
    success = True
    # Immer an Admin und an den Standort-Kanal senden
    for channel in set([admin_channel, location_channel]):
        if not channel:
            continue
        try:
            url = f"{server_url.rstrip('/')}/{channel}"
            resp = requests.post(url, data=msg.encode("utf-8"), headers=headers, timeout=5)
            if resp.status_code != 200:
                success = False
        except Exception:
            success = False
            
    return success
