#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: app.py
Beschreibung: Robuste, dynamische Haupt-Messschleife fuer Phidget-Messeinheiten.
              - Kontinuierliche Telemetrie (Standby: 5.0s, Messung: 1.0s)
              - Dynamische Sensor- und Kanalerkennung via ConfigLoader
              - Nicht-blockierender Betrieb & lokales Logging in SQLite
Version: 4.0.0 (Dynamic Channel Mapping & Continuous Telemetry Heartbeat)
"""

import os
import sys
import time
import socket
import logging
from datetime import datetime

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.abspath(os.path.join(current_dir, ".."))
for p in [current_dir, parent_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

from config_loader import ConfigLoader
from telemetry_db import TelemetryDB
from phidget_reader import PhidgetReader
from lcd_manager import PhidgetLCDController
import notifier
import ntfy_control_listener

LOG_PATH = os.path.join(current_dir, "logs", "app.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [App] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("App")


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def handle_remote_command(cmd, db: TelemetryDB, config: ConfigLoader):
    logger.info(f"Remote-Steuerbefehl empfangen: {cmd}")
    channels = config.get_temperature_channels()
    
    if cmd == "START":
        for ch in channels:
            db.start_channel(ch)
    elif cmd in ["RESET", "STOP"]:
        for ch in channels:
            db.reset_channel(ch)
    elif cmd == "EXPORT":
        for ch in channels:
            db.request_export(ch)


def main():
    cfg = ConfigLoader()
    db = TelemetryDB(os.path.join(current_dir, "telemetry_buffer.db"))

    device_name = cfg.device_name_technical
    local_ip = get_local_ip()
    logger.info(f"Initialisiere Phidget-App fuer {device_name} ({cfg.device_name_friendly}) an IP: {local_ip}")

    # 1. Benachrichtigung & Remote Listener starten
    try:
        notifier.send_startup_notification(local_ip, device_name)
    except Exception as e:
        logger.warning(f"Startup-Benachrichtigung fehlgeschlagen: {e}")

    try:
        ntfy_control_listener.start_ntfy_listener(lambda cmd: handle_remote_command(cmd, db, cfg))
    except Exception as e:
        logger.warning(f"ntfy-Listener konnte nicht gestartet werden: {e}")

    # 2. Hardware initialisieren
    reader = PhidgetReader(cfg.main_config, cfg.phidget_serial)
    try:
        reader.setup_sensors()
    except Exception as e:
        logger.error(f"Fehler bei Phidget-Sensorinitialisierung: {e}")

    # Optionales LCD initialisieren (Port aus Sensor-Config ermitteln)
    lcd_port = None
    for s in cfg.main_config.get("sensors", []):
        if s.get("sensor_type") == "lcd1100":
            lcd_port = s.get("port", 2)
            break

    lcd_ctrl = None
    if lcd_port is not None:
        try:
            lcd_ctrl = PhidgetLCDController(port=lcd_port, phidget_serial=cfg.phidget_serial, db_path=db.db_path)
        except Exception as e:
            logger.warning(f"LCD Controller konnte nicht gestartet werden: {e}")

    logger.info("Haupt-Messschleife gestartet.")
    last_housekeeping = time.time()

    while True:
        cycle_start = time.time()

        try:
            # 1. Konfiguration & Kanalzustände frisch prüfen
            cfg = ConfigLoader()
            channel_states = db.get_channel_states()
            
            # Prüfen, ob mindestens ein Temperaturkanal auf RUNN / RUNNING / TRIGGERED steht
            active_channels = [
                ch for ch, st in channel_states.items() 
                if st in ["RUNN", "RUNNING", "TRIGGERED"]
            ]
            is_measuring = len(active_channels) > 0

            # 2. Dynamische Abtastrate definieren
            cycle_target_duration = 1.0 if is_measuring else 5.0

            # 3. Sensordaten abfragen (IMMER alle Kanäle erfassen)
            telemetry_data = {}
            live_temps = {}

            for sensor_obj, stype, key in reader.sensor_map:
                try:
                    if stype == "humidity":
                        val = sensor_obj.getHumidity()
                        if val is not None:
                            telemetry_data["humidity"] = round(float(val), 2)
                    elif stype == "ambient":
                        val = sensor_obj.getTemperature()
                        if val is not None:
                            telemetry_data["ambient"] = round(float(val), 2)
                    elif stype == "tc":
                        val = sensor_obj.getTemperature()
                        if val is not None:
                            val_float = round(float(val), 2)
                            live_temps[key] = val_float
                            # Thermoelement-Werte IMMER erfassen (fuer Sonden-Erkennung auf NAS)
                            telemetry_data[key.lower()] = val_float
                except Exception:
                    if stype == "tc":
                        telemetry_data[key.lower()] = None

            # 4. Datensatz in SQLite schreiben (löst Ingest-Heartbeat über sync_worker aus)
            now_epoch = time.time()
            db.insert_telemetry(now_epoch, telemetry_data)

            # 5. LCD Display aktualisieren (falls vorhanden)
            if lcd_ctrl:
                try:
                    lcd_ctrl.update_live_fast(live_temps, channel_states=channel_states)
                except Exception as e:
                    logger.debug(f"LCD Frame Update Fehler: {e}")

            # 6. Tägliches Housekeeping alle 6 Stunden
            if now_epoch - last_housekeeping > 21600:
                db.run_housekeeping(max_age_hours=48)
                last_housekeeping = now_epoch

        except Exception as e:
            logger.error(f"Unerwarteter Fehler im Messzyklus: {e}")

        # Präzises Schlafen bis zum nächsten Zyklus
        elapsed = time.time() - cycle_start
        sleep_time = max(0.1, cycle_target_duration - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()