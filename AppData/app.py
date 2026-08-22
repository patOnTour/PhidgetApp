#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: app.py
Beschreibung: Logger fuer Phidget-Sensoren mit dynamischer Drosselung 
              (1Hz aktiv / 20s inaktiv), Startup-Push mit echter IP und SQLite-Puffer.
Version: 4.1.0
"""

import time
import os
import sys
import socket
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
import notifier

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Logs")
os.makedirs(LOG_DIR, exist_ok=True)
log_file = os.path.join(LOG_DIR, "phidget_app.log")

logger = logging.getLogger("PhidgetApp")
logger.setLevel(logging.INFO)
handler = TimedRotatingFileHandler(log_file, when="midnight", interval=1, backupCount=7)
handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(handler)
logger.addHandler(logging.StreamHandler(sys.stdout))


def get_local_ip():
    """Ermittelt die tatsaechlich genutzte IP des Geraets (LAN oder WLAN)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


class PhidgetLoggerApp:
    def __init__(self):
        self.config_loader = ConfigLoader()
        self.config = self.config_loader.main_config
        self.device_id = self.config.get("device_name", "ccssite01")
        self.db = TelemetryDB()
        self.sensors = []
        self.running = True
        self.memory_buffer = []
        self.last_db_flush = time.time()
        self.last_state_check = 0
        self.has_active_channels = False
        
        self.init_hardware()
        
        # Startup-Notification mit echter IP senden
        real_ip = get_local_ip()
        logger.info(f"System bereit. Sende Startup-Push mit IP: {real_ip}")
        notifier.send_startup_notification(real_ip, self.device_id)

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

    def check_active_state(self):
        """Prueft alle 3 Sekunden, ob mindestens ein Kanal aktiv ueberwacht wird."""
        now = time.time()
        if now - self.last_state_check < 3.0:
            return self.has_active_channels
        
        self.last_state_check = now
        try:
            conn = sqlite3.connect(self.db.db_path, timeout=1.0)
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM channel_control")
            rows = cursor.fetchall()
            conn.close()
            
            self.has_active_channels = any(
                row[0] and row[0].upper() in ["RUNN", "RUNNING", "TRIGGERED"] 
                for row in rows
            )
        except Exception:
            self.has_active_channels = True
            
        return self.has_active_channels

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
        logger.info("Mess-Schleife gestartet (mit adaptiver Drosselung).")
        while self.running:
            loop_start = time.time()
            try:
                is_active = self.check_active_state()
                data_point = self.read_all()
                self.memory_buffer.append(data_point)
                
                flush_interval = 5.0 if is_active else 1.0
                if (time.time() - self.last_db_flush >= flush_interval) or len(self.memory_buffer) >= 5:
                    self.flush_to_sqlite()
                    
            except Exception as e:
                logger.error(f"Fehler im Messloop: {e}")

            elapsed = time.time() - loop_start
            target_delay = 1.0 if self.has_active_channels else 20.0
            sleep_time = max(0.05, target_delay - elapsed)
            time.sleep(sleep_time)


if __name__ == "__main__":
    app = PhidgetLoggerApp()
    app.run()
