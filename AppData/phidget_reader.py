#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: phidget_reader.py
Beschreibung: Verwaltet die Anbindung, Initialisierung und Abtastung der Phidget-Sensoren.
Version: 1.3.0 (Live-Cache für 1Hz LCD-Update)
"""

import time
import logging
import numpy as np

from Phidget22.Devices.TemperatureSensor import TemperatureSensor
from Phidget22.Devices.HumiditySensor import HumiditySensor

logger = logging.getLogger("PhidgetReader")

class PhidgetReader:
    def __init__(self, config, phidget_serial):
        self.config = config
        self.phidget_serial = phidget_serial
        self.active_sensors = []
        self.sensor_map = []  # Direkte Zuordnung: (sensor_obj, type, telemetry_key)
        self.latest_temperatures = {}  # Live 1Hz Cache für LCD

    def setup_sensors(self):
        self.active_sensors = []
        self.sensor_map = []

        for s_conf in self.config.get("sensors", []):
            port = s_conf.get("port")
            stype = s_conf.get("sensor_type")
            t_key = s_conf.get("telemetry_key")
            
            if stype == "humidity_temp":
                try:
                    hum_sensor = HumiditySensor()
                    hum_sensor.setHubPort(port)
                    hum_sensor.setDeviceSerialNumber(self.phidget_serial)
                    hum_sensor.openWaitForAttachment(3000)
                    self.active_sensors.append(hum_sensor)
                    self.sensor_map.append((hum_sensor, "humidity", "humidity"))

                    amb_sensor = TemperatureSensor()
                    amb_sensor.setHubPort(port)
                    amb_sensor.setDeviceSerialNumber(self.phidget_serial)
                    amb_sensor.openWaitForAttachment(3000)
                    self.active_sensors.append(amb_sensor)
                    self.sensor_map.append((amb_sensor, "ambient", "ambient"))
                except Exception as ex:
                    logger.error(f"Fehler beim Initialisieren Luftfeuchte/Umgebung Port {port}: {ex}")

            elif stype == "tc_4port":
                for channel in range(4):
                    try:
                        temp_sensor = TemperatureSensor()
                        temp_sensor.setHubPort(port)
                        temp_sensor.setChannel(channel)
                        temp_sensor.setDeviceSerialNumber(self.phidget_serial)
                        temp_sensor.openWaitForAttachment(3000)
                        self.active_sensors.append(temp_sensor)
                        
                        tc_idx = len([s for s in self.sensor_map if s[1] == "tc"])
                        key = f"{t_key}{tc_idx}" if t_key else f"Temp{tc_idx}"
                        self.sensor_map.append((temp_sensor, "tc", key))
                    except Exception as ex:
                        logger.error(f"Fehler beim Initialisieren Thermo-Sensor Port {port} Kanal {channel}: {ex}")

        logger.info(f"{len(self.active_sensors)} Phidget-Sensoren erfolgreich initialisiert.")

    def collect_oversampled_telemetry(self, duration_seconds=20, sample_interval=1.0, sample_callback=None):
        samples = {}
        start_time = time.time()
        
        while (time.time() - start_time) < duration_seconds:
            sample_start = time.time()
            current_second_temps = {}
            
            for sensor, stype, key in self.sensor_map:
                try:
                    if stype == "humidity":
                        val = sensor.getHumidity()
                    else:
                        val = sensor.getTemperature()

                    if val is not None:
                        samples.setdefault(key, []).append(val)
                        current_second_temps[key] = val
                except Exception:
                    # Falls ein einzelner Sensor aussteigt, nicht crashen
                    pass

            if current_second_temps:
                self.latest_temperatures.update(current_second_temps)

            if sample_callback and current_second_temps:
                try:
                    sample_callback(current_second_temps)
                except Exception:
                    pass

            elapsed = time.time() - sample_start
            time.sleep(max(0.1, sample_interval - elapsed))

        telemetry_averages = {}
        for k, vals in samples.items():
            if vals:
                telemetry_averages[k] = round(float(np.mean(vals)), 2)

        return telemetry_averages
