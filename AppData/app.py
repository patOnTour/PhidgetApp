#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: app.py
Beschreibung: 1-Hz-Logger fuer Phidget-Sensoren basierend auf config.json mit SQLite-Puffer.
Version: 3.4.1 (Clean Dynamic Mapping)
"""

import time
import os
import sys
import sqlite3
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timezone

try:
    from Phidget22.Phidget import Phidget
    from Phidget22.Devices.TemperatureSensor import TemperatureSensor
    from Phidget22.Devices.HumiditySensor import HumiditySensor
except ImportError:
    pass

from config_loader import ConfigLoader
from telemetry_db import TelemetryDB

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
        self.config = self.config_loader.main_config
        self.device_id = self.config.get("device_name", "ccssite01")
        self.db = TelemetryDB()
        self.sensors = []  # Liste: (sensor_obj, sensor_type, db_column_key)
        self.running = True
        self.memory_buffer = []
        self.last_db_flush = time.time()
        self.init_hardware()

    def init_hardware(self):
        phidget_serial = self.config.get("phidget_serial", -1)
        logger.info(f"Initialisiere Phidgets fuer Device: {self.device_id} (Hub-Serial: {phidget_serial})")
        
        tc_count = 0
        for s_conf in self.config.get("sensors", []):
            port = s_conf.get("port")
            stype = s_conf.get("sensor_type")

            if stype == "tc_4port":
                for ch in range(4):
                    try:
                        ch_key = f"temp{tc_count}"
                        sensor = TemperatureSensor()
                        sensor.setHubPort(port)
                        sensor.setChannel(ch)
                        sensor.setIsHubPortDevice(False)
                        if phidget_serial > 0:
                            sensor.setDeviceSerialNumber(phidget_serial)
                        sensor.openWaitForAttachment(3000)
                        sensor.setDataInterval(1000)
                        self.sensors.append((sensor, "temp", ch_key))
                        logger.info(f"Sensor gebunden: Port {port}, Ch {ch} -> Spalte '{ch_key}'")
                        tc_count += 1
                    except Exception as e:
                        logger.warning(f"Konnte TC-Sensor Port {port} Ch {ch} nicht binden: {e}")

            elif stype == "humidity_temp":
                # 1. Umgebungstemperatur an Port 5 -> Spalte 'ambient'
                try:
                    sensor_amb = TemperatureSensor()
                    sensor_amb.setHubPort(port)
                    sensor_amb.setIsHubPortDevice(False)
                    if phidget_serial > 0:
                        sensor_amb.setDeviceSerialNumber(phidget_serial)
                    sensor_amb.openWaitForAttachment(3000)
                    sensor_amb.setDataInterval(1000)
                    self.sensors.append((sensor_amb, "temp", "ambient"))
                    logger.info(f"Ambient-Temperatur gebunden: Port {port} -> Spalte 'ambient'")
                except Exception as e:
                    logger.warning(f"Konnte Ambient-Sensor an Port {port} nicht binden: {e}")

                # 2. Relative Luftfeuchtigkeit an Port 5 -> Spalte 'humidity'
                try:
                    sensor_hum = HumiditySensor()
                    sensor_hum.setHubPort(port)
                    sensor_hum.setIsHubPortDevice(False)
                    if phidget_serial > 0:
                        sensor_hum.setDeviceSerialNumber(phidget_serial)
                    sensor_hum.openWaitForAttachment(3000)
                    sensor_hum.setDataInterval(1000)
                    self.sensors.append((sensor_hum, "humidity", "humidity"))
                    logger.info(f"Luftfeuchte-Sensor gebunden: Port {port} -> Spalte 'humidity'")
                except Exception as e:
                    logger.warning(f"Konnte Luftfeuchte-Sensor an Port {port} nicht binden: {e}")

    def read_all(self):
        now_utc = datetime.now(timezone.utc)
        timestamp_str = now_utc.strftime('%Y-%m-%d %H:%M:%S')
        row = {"timestamp": timestamp_str}
        
        for sensor, stype, key in self.sensors:
            try:
                if stype == "humidity":
                    val = sensor.getHumidity()
                    row[key] = round(val, 2) if (0.0 <= val <= 100.0) else None
                else:
                    val = sensor.getTemperature()
                    row[key] = round(val, 3) if (-40.0 <= val <= 125.0) else None
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
            cursor.execute("PRAGMA table_info(telemetry);")
            columns = [col[1].lower() for col in cursor.fetchall()]
            
            for entry in self.memory_buffer:
                entry_keys = [k.lower() for k in entry.keys() if k.lower() in columns]
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
        logger.info("Mess-Schleife gestartet.")
        while self.running:
            loop_start = time.time()
            try:
                data_point = self.read_all()
                self.memory_buffer.append(data_point)
                
                if time.time() - self.last_db_flush >= 5.0 or len(self.memory_buffer) >= 5:
                    self.flush_to_sqlite()
                    
            except Exception as e:
                logger.error(f"Fehler im Messloop: {e}")

            elapsed = time.time() - loop_start
            sleep_time = max(0.05, 1.0 - elapsed)
            time.sleep(sleep_time)


if __name__ == "__main__":
    app = PhidgetLoggerApp()
    app.run()