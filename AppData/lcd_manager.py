#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: lcd_manager.py
Beschreibung: Live-Anzeige fuer Phidget LCD1100_1 an VINT-Port 2.
Version: 1.9.0 (Differentielles Rendern & Kein clear() zur Controller-Entlastung)
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from config_loader import ConfigLoader
from Phidget22.Devices.LCD import LCD, LCDFont
from Phidget22.Phidget import PhidgetException

logger = logging.getLogger("LCDManager")


class PhidgetLCDController:
    def __init__(self, port=2, phidget_serial=None, db_path=None):
        self.port = port
        self.phidget_serial = phidget_serial
        self.cfg = ConfigLoader()
        self.lcd = None
        self.last_reconnect_attempt = 0

        # Zeilen-Cache, um redundante writeText/flush-Befehle zu verhindern
        self.last_rendered_lines = {}

        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.db_path = db_path if db_path else os.path.join(base_dir, "telemetry_buffer.db")

        self.mapping_file_path = os.path.join(base_dir, "..", "config", "channel_mapping.json")
        if not os.path.exists(self.mapping_file_path):
            self.mapping_file_path = os.path.join(base_dir, "config", "channel_mapping.json")

        self.channel_mapping = {}
        self.last_mapping_mtime = 0
        self.cached_channel_states = {}
        self.last_state_check = 0

        self._reload_channel_mapping(force=True)
        self._init_lcd()

    def _get_serial(self):
        if self.phidget_serial:
            return int(self.phidget_serial)
        serial = getattr(self.cfg, "phidget_serial", None)
        if not serial and hasattr(self.cfg, "main_config"):
            serial = self.cfg.main_config.get("phidget_serial")
        return int(serial) if serial else None

    def _init_lcd(self):
        try:
            if self.lcd:
                try:
                    self.lcd.close()
                except Exception:
                    pass
                self.lcd = None

            serial = self._get_serial()
            lcd_inst = LCD()

            if serial and serial != 0:
                lcd_inst.setDeviceSerialNumber(serial)
            
            lcd_inst.setHubPort(self.port)
            lcd_inst.setIsHubPortDevice(False)
            lcd_inst.setChannel(0)

            lcd_inst.openWaitForAttachment(5000)

            lcd_inst.setBacklight(0.80)
            lcd_inst.setContrast(0.55)
            lcd_inst.clear()

            self.lcd = lcd_inst
            self.last_rendered_lines = {}  # Cache bei Init/Reconnect leeren
            logger.info(f"[LCD] LCD1100 an Hub {serial}, Port {self.port} erfolgreich initialisiert.")
            return True
        except Exception as e:
            logger.error(f"[LCD] Fehler bei Display-Initialisierung: {e}")
            if self.lcd:
                try:
                    self.lcd.close()
                except Exception:
                    pass
            self.lcd = None
            return False

    def _reload_channel_mapping(self, force=False):
        try:
            if os.path.exists(self.mapping_file_path):
                mtime = os.path.getmtime(self.mapping_file_path)
                if force or mtime > self.last_mapping_mtime:
                    with open(self.mapping_file_path, "r", encoding="utf-8") as f:
                        self.channel_mapping = json.load(f)
                    self.last_mapping_mtime = mtime
        except Exception as e:
            logger.error(f"[LCD] Fehler beim Reload von channel_mapping.json: {e}")

    def get_friendly_name(self, ch_key):
        self._reload_channel_mapping()
        for k, v in self.channel_mapping.items():
            if k.lower() == ch_key.lower():
                return v
        return ch_key

    def get_configured_channels(self):
        return self.cfg.get_temperature_channels()

    def fetch_channel_states_from_db(self):
        now = time.time()
        if now - self.last_state_check < 2.0 and self.cached_channel_states:
            return self.cached_channel_states

        states = {}
        try:
            conn = sqlite3.connect(self.db_path, timeout=0.5)
            cursor = conn.cursor()
            cursor.execute("SELECT channel, status FROM channel_control")
            rows = cursor.fetchall()
            conn.close()

            configured = self.get_configured_channels()
            for ch, st in rows:
                st_clean = st.upper() if st else "STOPPED"
                for conf_ch in configured:
                    if ch.lower() == conf_ch.lower():
                        states[conf_ch] = st_clean
            self.cached_channel_states = states
            self.last_state_check = now
        except Exception:
            pass
        return self.cached_channel_states

    def check_wifi_status(self):
        try:
            with open("/sys/class/net/wlan0/operstate", "r") as f:
                if f.read().strip() == "up":
                    return "OK"
        except Exception:
            pass
        return "--"

    def update_live_fast(self, live_temps, channel_states=None, mqtt_connected=False, **kwargs):
        now_ts = time.time()

        # 1. Attachment Check & Reconnect
        is_attached = False
        if self.lcd:
            try:
                is_attached = self.lcd.getAttached()
            except Exception:
                is_attached = False
                self.lcd = None

        if not is_attached:
            if now_ts - self.last_reconnect_attempt > 5.0:
                self.last_reconnect_attempt = now_ts
                logger.warning("[LCD] Display nicht bereit. Versuche Reconnect...")
                if not self._init_lcd():
                    return
            else:
                return

        # 2. Status & Netzdaten holen
        channel_states = self.fetch_channel_states_from_db()
        wifi_st = self.check_wifi_status()
        mqtt_st = "OK" if mqtt_connected else "--"
        now_str = datetime.now().astimezone().strftime("%H:%M:%S")

        dirty = False  # Markiert, ob ueberhaupt Daten an das LCD gesendet werden muessen

        try:
            # Header-Zeile mit fester Laenge von 21 Zeichen (Font 5x8 auf 128px Breite)
            header_text = f"{now_str} W:{wifi_st:<2} M:{mqtt_st:<2}"
            header_text = f"{header_text:<21}"

            if self.last_rendered_lines.get(0) != header_text:
                self.lcd.writeText(LCDFont.FONT_5x8, 0, 0, header_text)
                self.last_rendered_lines[0] = header_text
                dirty = True

            channels = self.get_configured_channels()

            # Bis zu 4 Sensorzeilen differentiell rendern (Font 6x12)
            for idx, ch_key in enumerate(channels[:4]):
                line_idx = idx + 1
                friendly_name = self.get_friendly_name(ch_key)
                short_name = (friendly_name[:6]) if len(friendly_name) > 6 else friendly_name
                st = channel_states.get(ch_key, "STOPPED")

                val = live_temps.get(ch_key)
                if val is None:
                    val = live_temps.get(ch_key.lower())
                if val is None:
                    val = live_temps.get(ch_key.capitalize())

                val_str = f"{val:.1f}C" if isinstance(val, (int, float)) else "--.-C"

                if st in ["RUNN", "RUNNING"]:
                    icon, tag = ">", "RUN"
                elif st == "TRIGGERED":
                    icon, tag = "!", "TRIG"
                else:
                    icon, tag = "-", "OFF"

                y_pos = line_idx * 12
                # Feste Zeichenbreite (21 Zeichen), ueberschreibt vorherigen Text sauber ohne clear()
                line_text = f"{icon}{short_name:<6} {val_str:>6} [{tag}]"
                line_text = f"{line_text:<21}"

                if self.last_rendered_lines.get(line_idx) != line_text:
                    self.lcd.writeText(LCDFont.FONT_6x12, 0, y_pos, line_text)
                    self.last_rendered_lines[line_idx] = line_text
                    dirty = True

            # Nur flashen, wenn wirklich eine Aenderung stattfand
            if dirty:
                self.lcd.flush()

        except Exception as e:
            logger.error(f"[LCD] Kommunikationsfehler beim Schreiben auf Display: {e}")
            try:
                if self.lcd:
                    self.lcd.close()
            except Exception:
                pass
            self.lcd = None
            self.last_rendered_lines = {}

    def close(self):
        if self.lcd:
            try:
                self.lcd.setBacklight(0.0)
                self.lcd.close()
            except Exception:
                pass
            self.lcd = None
            self.last_rendered_lines = {}