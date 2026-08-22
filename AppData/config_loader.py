#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json

class ConfigLoader:
    VERSION = "5.3.0"
        
    def __init__(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_dir = os.path.join(base_dir, "../config") if os.path.exists(os.path.join(base_dir, "../config")) else os.path.join(base_dir, "config")

        self.main_config_path = os.path.join(self.config_dir, "config.json")
        self.secrets_path = os.path.join(self.config_dir, "secrets.json")
        self.channel_mapping_path = os.path.join(self.config_dir, "channel_mapping.json")
        self.device_mapping_path = os.path.join(self.config_dir, "device_mapping.json")

        self.main_config = self._load_json(self.main_config_path)
        self.secrets = self._load_json(self.secrets_path)
        self.channel_mapping = self._load_json(self.channel_mapping_path)
        self.device_mapping = self._load_json(self.device_mapping_path)

        # --- 1. DEVICE VARIABLEN ---
        self.device_name_technical = self.main_config.get("device_name", "ccssite01")
        self.device_name_friendly = self.device_mapping.get(self.device_name_technical, self.device_name_technical)

        # --- 2. PHIDGET & SYSTEM VARIABLEN ---
        self.phidget_serial = self.main_config.get("phidget_serial")
        self.interval_minutes = self.main_config.get("interval_minutes", 2)
        self.temp_delta_min = self.main_config.get("temp_delta_min", 0.6)
        self.temp_delta_max = self.main_config.get("temp_delta_max", 1.0)

        # --- 3. MQTT / THINGSBOARD LOGIN DATEN ---
        tb_cfg = self.secrets.get("thingsboard", {})
        self.mqtt_host = tb_cfg.get("host")
        self.mqtt_port = tb_cfg.get("port")
        self.mqtt_username = tb_cfg.get("username")
        self.mqtt_password = tb_cfg.get("password")

        # --- 4. NTFY CHANNELS ---
        notify_cfg = self.secrets.get("notify", {})
        self.ntfy_channel = notify_cfg.get("channel_name")
        
        admin_notify_cfg = self.secrets.get("admin_notify", {})
        self.admin_ntfy_channel = admin_notify_cfg.get("channel_name", self.ntfy_channel)

        # --- 5. TECHNISCHE KANALNAMEN & DYNAMISCHE PORT-ERKENNUNG ---
        self.technical_channels = []
        self.temperature_channels = []
        
        tc_channel_counter = 0

        for sensor in self.main_config.get("sensors", []):
            stype = sensor.get("sensor_type")
            t_key = sensor.get("telemetry_key", "Temp")

            if stype == "tc_4port":
                channels_per_module = sensor.get("channels_count", 4)
                for _ in range(channels_per_module):
                    col_name = f"{t_key}{tc_channel_counter}"
                    if col_name not in self.technical_channels:
                        self.technical_channels.append(col_name)
                        self.temperature_channels.append(col_name)
                    tc_channel_counter += 1
                        
            elif stype == "humidity_temp":
                if "ambient" not in self.technical_channels:
                    self.technical_channels.append("ambient")
                if "humidity" not in self.technical_channels:
                    self.technical_channels.append("humidity")
            elif t_key and t_key != "Unbelegt" and t_key not in self.technical_channels:
                self.technical_channels.append(t_key)
                if t_key.lower().startswith("temp"):
                    self.temperature_channels.append(t_key)

        # Fallback falls keine Thermocouple-Sensoren definiert sind
        if not self.temperature_channels:
            self.temperature_channels = ["Temp0", "Temp1", "Temp2", "Temp3"]
            for ch in self.temperature_channels:
                if ch not in self.technical_channels:
                    self.technical_channels.append(ch)

        if "ambient" not in self.technical_channels:
            self.technical_channels.append("ambient")
        if "humidity" not in self.technical_channels:
            self.technical_channels.append("humidity")

        # Mapping für Friendly Names
        self.channel_names_mapping = {}
        for tech_key in self.technical_channels:
            self.channel_names_mapping[tech_key] = self.channel_mapping.get(tech_key, tech_key)

    def _load_json(self, filepath):
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Fehler beim Laden von {filepath}: {e}")
        return {}

    def get_friendly_channel_name(self, technical_key):
        return self.channel_names_mapping.get(technical_key, technical_key)

    def get_temperature_channels(self):
        """Liefert dynamisch alle reinen Temperatur-Messkanäle (z.B. Temp0..Temp3 oder Temp0..Temp7)."""
        return sorted(self.temperature_channels)