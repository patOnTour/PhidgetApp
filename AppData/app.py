#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: app.py
Beschreibung: Schlanker 1-Hz-Messdatenlogger fuer Phidget-Sensoren mit SQLite-Pufferung.
Version: 3.1.0 (Edge-Decoupled, In-Memory-Batching)
"""

import time
import os
import sys
import json
import sqlite3
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime

# Phidget22 Bibliotheken
try:
    from Phidget22.Phidget import Phidget
    from Phidget22.Devices.TemperatureSensor import TemperatureSensor
    from Phidget22.Devices.HumiditySensor import HumiditySensor
except ImportError:
    pass

from config_loader import ConfigLoader
from telemetry_db import TelemetryDB

# Logging Setup (24h-Rotation)
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Logs")
os.makedirs(LOG_DIR, exist_ok=True)
log_file = os.path.join(LOG_DIR, "phidget_app.log")

logger = logging.getLogger("PhidgetApp")
logger.setLevel(logging.INFO)
handler = TimedRotatingFileHandler(log_file, when="midnight", interval=1, backupCount=7)
handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(handler)
logger.addHandler(logging.StreamHandler(sys.stdout))

class PhidgetLoggerApp:
    def __init__(self):
        self.config_loader = ConfigLoader()
        self.config = self.config_loader.load_config()
        self.device_id = self.config.get("device_name", "ccssite01")
        self.db = TelemetryDB()
        self.sensors = []
        self.running = True
        self.memory_buffer = []
        self.last_db_flush = time.time()
        self.init_hardware()

    def init_hardware(self):
        phidget_serial = self.config.get("phidget_serial", -1)
        logger.info(f"Initialisiere Phidgets fuer Device: {self.device_id} (Hub-Serial: {phidget_serial})")
        
        for s_conf in self.config.get("sensors", []):
            port = s_conf.get("port")
            stype = s_conf.get("sensor_type")
            tkey = s_conf.get("telemetry_key", "temp")

            if stype == "tc_4port":
                for ch in range(4):
                    try:
                        ch_key = f"{tkey}{len(self.sensors)}"
                        sensor = TemperatureSensor()
                        sensor.setHubPort(port)
                        sensor.setChannel(ch)
                        sensor.setIsHubPortDevice(False)
                        if phidget_serial > 0:
                            sensor.setDeviceSerialNumber(phidget_serial)
                        sensor.openWaitForAttachment(3000)
                        sensor.setDataInterval(1000)  # 1000ms = 1 Hz
                        self.sensors.append((sensor, ch_key))
                        logger.info(f"Sensor gebunden: Port {port}, Ch {ch} -> {ch_key}")
                    except Exception as e:
                        logger.warning(f"Konnte TC-Sensor Port {port} Ch {ch} nicht binden: {e}")

            elif stype == "tmp1101":
                try:
                    ch_key = f"{tkey}{len(self.sensors)}"
                    sensor = TemperatureSensor()
                    sensor.setHubPort(port)
                    sensor.setIsHubPortDevice(False)
                    if phidget_serial > 0:
                        sensor.setDeviceSerialNumber(phidget_serial)
                    sensor.openWaitForAttachment(3000)
                    sensor.setDataInterval(1000)
                    self.sensors.append((sensor, ch_key))
                except Exception as e:
                    logger.warning(f"Konnte TMP1101 Port {port} nicht binden: {e}")

    def read_all(self):
        now_dt = datetime.now()
        timestamp_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')
        row = {"timestamp": timestamp_str}
        
        for sensor, key in self.sensors:
            try:
                temp = sensor.getTemperature()
                # Physische Plausibilitaet
                row[key] = round(temp, 3) if (-40.0 <= temp <= 125.0) else None
            except Exception:
                row[key] = None
        return row

    def flush_to_sqlite(self):
        if not self.memory_buffer:
            return
        
        conn = sqlite3.connect(self.db.db_path, timeout=10.0)
        cursor = conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode = WAL;")
            
            # Hole Spalten der Tabelle
            cursor.execute("PRAGMA table_info(telemetry);")
            columns = [col[1] for col in cursor.fetchall()]
            
            for entry in self.memory_buffer:
                entry_keys = [k for k in entry.keys() if k in columns]
                placeholders = ", ".join(["?"] * len(entry_keys))
                cols_str = ", ".join(entry_keys)
                vals = [entry[k] for k in entry_keys]
                
                cursor.execute(f"INSERT INTO telemetry ({cols_str}, synced) VALUES ({placeholders}, 0)", vals)
            
            conn.commit()
            self.memory_buffer.clear()
            self.last_db_flush = time.time()
        except Exception as e:
            logger.error(f"Fehler beim SQLite-Flush: {e}")
        finally:
            conn.close()

    def run(self):
        logger.info("🚀 1-Hz Mess-Schleife gestartet.")
        while self.running:
            loop_start = time.time()
            try:
                data_point = self.read_all()
                self.memory_buffer.append(data_point)
                
                # Alle 5 Sekunden gesammelt in SQLite flashen
                if time.time() - self.last_db_flush >= 5.0 or len(self.memory_buffer) >= 5:
                    self.flush_to_sqlite()
                    
            except Exception as e:
                logger.error(f"Fehler im 1-Hz Messloop: {e}")

            # Exakten 1-Sekunden-Takt halten
            elapsed = time.time() - loop_start
            sleep_time = max(0.05, 1.0 - elapsed)
            time.sleep(sleep_time)

if __name__ == "__main__":
    app = PhidgetLoggerApp()
    app.run()
