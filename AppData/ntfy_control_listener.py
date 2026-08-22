#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: ntfy_control_listener.py
Beschreibung: Lauscht auf Steuerbefehle (START, EXPORT, RESET) via self-hosted ntfy.
"""

import json
import os
import time
import logging
import requests
import threading

CONFIG_DIR = "/usr/userapps/PhidgetProject/config"
SECRETS_FILE = os.path.join(CONFIG_DIR, "secrets.json")

def load_secrets():
    if os.path.exists(SECRETS_FILE):
        try:
            with open(SECRETS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def start_ntfy_listener(command_callback):
    def listener_thread():
        secrets = load_secrets()
        server_url = secrets.get("ntfy", {}).get("server_url", "https://ntfy.concretum-setting.com")
        channel = secrets.get("notify", {}).get("channel_name", "Concretum")
        stream_url = f"{server_url.rstrip('/')}/{channel}/json"

        logging.info(f"[NtfyControlListener] Starte interaktiven Stream-Listener auf: {stream_url}")

        while True:
            try:
                with requests.get(stream_url, stream=True, timeout=60) as resp:
                    if resp.status_code == 200:
                        for line in resp.iter_lines():
                            if line:
                                try:
                                    data = json.loads(line.decode("utf-8"))
                                    if data.get("event") == "message":
                                        msg = data.get("message", "").strip().upper()
                                        if msg in ["START", "EXPORT", "RESET"]:
                                            logging.info(f"[NtfyControlListener] Befehl empfangen: {msg}")
                                            command_callback(msg)
                                except Exception as e:
                                    logging.error(f"[NtfyControlListener] Parse-Fehler: {e}")
                    else:
                        logging.warning(f"[NtfyControlListener] HTTP {resp.status_code}. Wiederhole in 10s...")
            except Exception as e:
                logging.warning(f"[NtfyControlListener] Verbindungsabbruch ({e}). Wiederhole in 10s...")
            
            time.sleep(10)

    t = threading.Thread(target=listener_thread, daemon=True)
    t.start()
