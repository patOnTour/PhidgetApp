#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: app.py
Beschreibung: Schlanker Hauptdienst fuer Phidget-Messeinheiten.
Abtastung, dynamische Drosselung (STOPPED: 20s / RUNNING: 1s), ntfy-Integration.
"""

import time
import os
import json
import logging
import socket
import threading
import sqlite3
import datetime
from Phidget22.Devices.TemperatureSensor import TemperatureSensor
from Phidget22.Devices.HumiditySensor import HumiditySensor
from Phidget22.PhidgetException import PhidgetException
import notifier
import ntfy_control_listener

CONFIG_DIR = "/usr/userapps/PhidgetProject/config"
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DB_PATH = "/usr/userapps/PhidgetProject/AppData/telemetry_buffer.db"
LOG_PATH = "/usr/userapps/PhidgetProject/AppData/logs/app.log"

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

app_state = {
    "status": "STOPPED",
    "device_name": "ccssite01",
    "serial": 0,
    "sensors": []
}

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            device_name TEXT,
            status TEXT,
            data JSON,
            synced INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                app_state["device_name"] = cfg.get("device_name", "ccssite01")
                app_state["serial"] = cfg.get("phidget_serial", 0)
                app_state["sensors"] = cfg.get("sensors", [])
        except Exception as e:
            logging.error(f"Fehler beim Laden der Config: {e}")

def handle_remote_command(cmd):
    logging.info(f"Steuerbefehl ausgefuehrt: {cmd}")
    if cmd == "START":
        app_state["status"] = "RUNNING"
    elif cmd in ["EXPORT", "RESET"]:
        app_state["status"] = "STOPPED"

def main():
    load_config()
    init_db()
    
    ip = get_local_ip()
    logging.info(f"Starte Phidget-App fuer Geraet: {app_state['device_name']} (IP: {ip})")
    
    # Startup-Push an Admin & Standort senden
    notifier.send_startup_notification(ip, app_state["device_name"])
    
    # ntfy Listener starten
    ntfy_control_listener.start_ntfy_listener(handle_remote_command)
    
    logging.info("Haupt-Messschleife gestartet.")
    while True:
        try:
            # Dynamische Drosselung: RUNNING = 1s, STOPPED = 20s
            sleep_duration = 1.0 if app_state["status"] == "RUNNING" else 20.0
            
            # (Messwert-Erfassung und Pufferung)
            time.sleep(sleep_duration)
        except Exception as e:
            logging.error(f"Fehler im Messzyklus: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
