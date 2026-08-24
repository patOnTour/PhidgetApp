#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import yaml
import sqlite3
import logging
import signal
import numpy as np

from Phidget22.Devices.TemperatureSensor import TemperatureSensor
from Phidget22.Devices.HumiditySensor import HumiditySensor

BASE_DIR = "/usr/userapps/PhidgetProject"
CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.yaml")
RAM_DB_PATH = "/tmp/telemetry.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [10Hz-Reader] %(message)s")
logger = logging.getLogger("Reader")

class Phidget10HzReader:
    def __init__(self):
        self.config = self._load_config()
        self.active_sensors = []
        self.sensor_map = []  # Tuples: (sensor_obj, sensor_type, channel_idx)
        self.running = True
        
        self._init_ram_db()
        signal.signal(signal.SIGTERM, self.shutdown)
        signal.signal(signal.SIGINT, self.shutdown)

    def _load_config(self):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _init_ram_db(self):
        with sqlite3.connect(RAM_DB_PATH, timeout=5.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS telemetry_buffer (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc REAL NOT NULL,
                    channel_idx INT NOT NULL,
                    temperature REAL NOT NULL,
                    synced INTEGER DEFAULT 0
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_synced_id ON telemetry_buffer(synced, id);")

    def setup_hardware(self):
        serial = self.config["device"]["phidget_serial"]
        
        for s_conf in self.config.get("sensors", []):
            port = s_conf.get("port")
            stype = s_conf.get("type")
            
            if stype == "humidity_temp":
                try:
                    hum = HumiditySensor()
                    hum.setIsLocal(True)
                    hum.setHubPort(port)
                    hum.setDeviceSerialNumber(serial)
                    hum.openWaitForAttachment(3000)
                    self.active_sensors.append(hum)
                    self.sensor_map.append((hum, "humidity", 101))

                    amb = TemperatureSensor()
                    amb.setIsLocal(True)
                    amb.setHubPort(port)
                    amb.setDeviceSerialNumber(serial)
                    amb.openWaitForAttachment(3000)
                    self.active_sensors.append(amb)
                    self.sensor_map.append((amb, "ambient", 100))
                except Exception as ex:
                    logger.error(f"Fehler bei Port {port} (Humidity/Ambient): {ex}")

            elif stype == "tc_4port":
                for ch in range(4):
                    try:
                        tc = TemperatureSensor()
                        tc.setIsLocal(True)
                        tc.setHubPort(port)
                        tc.setChannel(ch)
                        tc.setDeviceSerialNumber(serial)
                        tc.openWaitForAttachment(3000)
                        self.active_sensors.append(tc)
                        
                        channel_idx = len([s for s in self.sensor_map if s[1] == "tc"])
                        self.sensor_map.append((tc, "tc", channel_idx))
                    except Exception as ex:
                        logger.error(f"Fehler bei Port {port} Ch {ch} (Thermo): {ex}")

        logger.info(f"{len(self.active_sensors)} Phidget-Kanäle lokal gebunden.")

    def run_loop(self):
        logger.info("1Hz Sampling Loop (10Hz Oversampling) gestartet...")
        
        while self.running:
            sec_start = time.time()
            samples = {}
            
            # 10 Samples innerhalb von 1 Sekunde erfassen
            for _ in range(10):
                sample_start = time.time()
                for sensor, stype, ch_idx in self.sensor_map:
                    try:
                        val = sensor.getHumidity() if stype == "humidity" else sensor.getTemperature()
                        if val is not None:
                            samples.setdefault(ch_idx, []).append(float(val))
                    except Exception:
                        pass
                
                elapsed = time.time() - sample_start
                time.sleep(max(0.001, 0.1 - elapsed))

            # Exakte UTC-Sekunde bilden
            utc_now = int(time.time())
            db_records = []
            
            for ch_idx, vals in samples.items():
                if vals:
                    avg_val = round(float(np.mean(vals)), 2)
                    db_records.append((utc_now, ch_idx, avg_val, 0))

            if db_records:
                try:
                    with sqlite3.connect(RAM_DB_PATH, timeout=5.0) as conn:
                        conn.executemany(
                            "INSERT OR IGNORE INTO telemetry_buffer (timestamp_utc, channel_idx, temperature, synced) VALUES (?, ?, ?, ?)",
                            db_records
                        )
                except Exception as e:
                    logger.error(f"Fehler beim Schreiben in RAM-DB: {e}")

            # Auf die volle Sekunde auffüllen
            sec_elapsed = time.time() - sec_start
            time.sleep(max(0.01, 1.0 - sec_elapsed))

    def shutdown(self, signum, frame):
        logger.info("Schließe Phidget-Hardware...")
        self.running = False
        for s in self.active_sensors:
            try:
                s.close()
            except Exception:
                pass
        sys.exit(0)

if __name__ == "__main__":
    reader = Phidget10HzReader()
    reader.setup_hardware()
    reader.run_loop()