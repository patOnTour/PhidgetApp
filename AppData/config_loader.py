#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json

class ConfigLoader:
    VERSION = "2.0.0"
        
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

        self.device_name_technical = self.main_config.get("device_name", "ccssite01")
        self.device_name_friendly = self.device_mapping.get(self.device_name_technical, self.device_name_technical)

        self.phidget_serial = self.main_config.get("phidget_serial")
        self.interval_minutes = self.main_config.get("interval_minutes", 2)
        self.temp_delta_min = self.main_config.get("temp_delta_min", 0.6)
        self.temp_delta_max = self.main_config.get("temp_delta_max", 1.0)

        # MQTT / Thingsboard
        tb_cfg = self.secrets.get("thingsboard", {})
        self.mqtt_host = tb_cfg.get("host")
        self.mqtt_port = tb_cfg.get("port")
        self.mqtt_username = tb_cfg.get("username")
        self.mqtt_password = tb_cfg.get("password")

        # NTFY
        notify_cfg = self.secrets.get("notify", {})
        self.ntfy_channel = notify_cfg.get("channel_name")
        admin_notify_cfg = self.secrets.get("admin_notify", {})
        self.admin_ntfy_channel = admin_notify_cfg.get("channel_name", self.ntfy_channel)

        # --- DETERMINISTISCHE KANAL-GENERIERUNG ---
        self.technical_channels = []
        self.temperature_channels = []
        self.channel_to_id_map = {}
        
        tc_idx = 0

        for sensor in self.main_config.get("sensors", []):
            stype = sensor.get("sensor_type", "").lower()
            if stype == "none" or stype == "unbelegt":
                continue

            if stype == "tc_4port":
                ch_count = sensor.get("channels_count", 4)
                for _ in range(ch_count):
                    key = f"temp{tc_idx}"
                    self.technical_channels.append(key)
                    self.temperature_channels.append(key)
                    self.channel_to_id_map[key] = tc_idx
                    tc_idx += 1

            elif stype == "humidity_temp":
                if "ambient" not in self.technical_channels:
                    self.technical_channels.append("ambient")
                    self.channel_to_id_map["ambient"] = 100
                if "humidity" not in self.technical_channels:
                    self.technical_channels.append("humidity")
                    self.channel_to_id_map["humidity"] = 101

            elif "temp" in stype:
                key = f"temp{tc_idx}"
                self.technical_channels.append(key)
                self.temperature_channels.append(key)
                self.channel_to_id_map[key] = tc_idx
                tc_idx += 1

        # Fallback falls keine TC-Sensoren definiert sind
        if not self.temperature_channels:
            for i in range(4):
                key = f"temp{i}"
                self.technical_channels.append(key)
                self.temperature_channels.append(key)
                self.channel_to_id_map[key] = i

        if "ambient" not in self.technical_channels:
            self.technical_channels.append("ambient")
            self.channel_to_id_map["ambient"] = 100
        if "humidity" not in self.technical_channels:
            self.technical_channels.append("humidity")
            self.channel_to_id_map["humidity"] = 101

        # Friendly Names Mapping
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
        return self.channel_names_mapping.get(technical_key.lower(), technical_key)

    def get_temperature_channels(self):
        return list(self.temperature_channels)

    def get_channel_id_map(self):
        return dict(self.channel_to_id_map)