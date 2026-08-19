#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: app.py
Beschreibung: Schlanker Hauptdienst fuer Phidget-Messeinheiten.
Abtastung, Puffern und Versenden von Telemetriedaten sowie Live-LCD-Anzeige.
Version: 8.1.2 (Stabiler LCD-Takt & Serial-Passing)
"""

import time
import os
import json
import logging
import threading
import socket
import subprocess
import traceback
from logging.handlers import TimedRotatingFileHandler

from config_loader import ConfigLoader
from advanced_analyzer import ConcreteSettingAnalyzer
from tb_device_mqtt import TBDeviceMqttClient
from phidget_reader import PhidgetReader
from telemetry_db import TelemetryDB
from lcd_manager import PhidgetLCDController
import notifier
import ntfy_control_listener

# 1. Konfiguration laden & Gerätenamen holen
cfg = ConfigLoader()
device_name = cfg.device_name_technical

# 2. Log-Ordner explizit auf ./logs/ setzen und automatisch erstellen
base_dir = os.path.dirname(os.path.abspath(__file__))
log_dir = os.path.join(base_dir, "logs")
os.makedirs(log_dir, exist_ok=True)

log_file = os.path.join(log_dir, f"{device_name}.log")
telemetry_log_file = os.path.join(log_dir, f"{device_name}_telemetry.log")


def setup_loggers(client_id):
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] [%(processName)s] %(message)s')
    
    # 1. Haupt-Logger
    main_log_file = os.path.join(log_dir, f"{client_id}.log")
    main_handler = TimedRotatingFileHandler(main_log_file, when="midnight", interval=1, backupCount=14, encoding="utf-8")
    main_handler.suffix = "%Y-%m-%d"
    main_handler.setFormatter(formatter)
    
    main_logger = logging.getLogger("MainApp")
    main_logger.setLevel(logging.INFO)
    main_logger.handlers = []
    main_logger.addHandler(main_handler)
    main_logger.addHandler(logging.StreamHandler())

    # 2. Telemetrie-Logger
    telem_log_file = os.path.join(log_dir, f"{client_id}_telemetry.log")
    telem_handler = TimedRotatingFileHandler(telem_log_file, when="midnight", interval=1, backupCount=14, encoding="utf-8")
    telem_handler.suffix = "%Y-%m-%d"
    telem_handler.setFormatter(formatter)
    
    telem_logger = logging.getLogger("TelemetryApp")
    telem_logger.setLevel(logging.INFO)
    telem_logger.handlers = []
    telem_logger.addHandler(telem_handler)

    return main_logger, telem_logger


class MQTTWorkerThread(threading.Thread):
    def __init__(self, app_instance):
        super().__init__(name="MQTTWorkerThread", daemon=True)
        self.app = app_instance
        self.running = True

    def run(self):
        logger.info("[MQTTWorker] Entkoppelter MQTT-Worker-Thread gestartet.")
        while self.running:
            try:
                if not self.app.tb_client.is_connected():
                    time.sleep(3)
                    continue

                import sqlite3
                conn = sqlite3.connect(self.app.db_path, timeout=5.0)
                cursor = conn.cursor()
                
                cursor.execute("SELECT id, timestamp, payload, COALESCE(retry_count, 0) FROM telemetry_queue ORDER BY id ASC LIMIT 20")
                rows = cursor.fetchall()

                if not rows:
                    conn.close()
                    time.sleep(2)
                    continue

                for row_id, ts_val, payload_str, retries in rows:
                    if not self.app.tb_client.is_connected():
                        break

                    try:
                        payload = json.loads(payload_str)
                        tb_payload = {
                            "ts": int(ts_val * 1000),
                            "values": payload
                        }
                        
                        publish_info = self.app.tb_client.send_telemetry(tb_payload)
                        ack_ok = False
                        
                        try:
                            if hasattr(publish_info, 'wait_for_publish'):
                                publish_info.wait_for_publish(timeout=5)
                                ack_ok = (publish_info.rc() == 0) if hasattr(publish_info, 'rc') else True
                            else:
                                ack_ok = bool(publish_info)
                        except Exception:
                            ack_ok = bool(publish_info)

                        if ack_ok:
                            cursor.execute("DELETE FROM telemetry_queue WHERE id = ?", (row_id,))
                            conn.commit()
                        else:
                            new_retries = retries + 1
                            if new_retries >= 5:
                                logger.error(f"[MQTTWorker] Puffer-ID {row_id} nach {new_retries} Versuchen verworfen (Poison Message).")
                                cursor.execute("DELETE FROM telemetry_queue WHERE id = ?", (row_id,))
                            else:
                                logger.warning(f"[MQTTWorker] PUBACK fehlgeschlagen fuer ID {row_id} (Versuch {new_retries}/5).")
                                cursor.execute("UPDATE telemetry_queue SET retry_count = ? WHERE id = ?", (new_retries, row_id))
                            conn.commit()
                            break

                    except Exception as ex:
                        logger.error(f"[MQTTWorker] Korrupte Payload bei ID {row_id} geloescht: {ex}")
                        cursor.execute("DELETE FROM telemetry_queue WHERE id = ?", (row_id,))
                        conn.commit()

                conn.close()
                time.sleep(1)
            except Exception as e:
                logger.error(f"[MQTTWorker] Fehler im Hintergrund-Thread: {e}\n{traceback.format_exc()}")
                time.sleep(3)


class LCDWorkerThread(threading.Thread):
    def __init__(self, app_instance):
        super().__init__(name="LCDWorkerThread", daemon=True)
        self.app = app_instance
        self.running = True

    def run(self):
        logger.info("[LCDWorker] Entkoppelter LCD-Thread gestartet.")
        while self.running:
            try:
                if self.app.lcd:
                    mqtt_ok = self.app.tb_client.is_connected() if self.app.tb_client else False
                    temps = getattr(self.app.reader, 'latest_temperatures', {})
                    if not temps:
                        temps = self.app.last_telemetry_values
                    
                    self.app.lcd.update_live_fast(
                        live_temps=temps,
                        mqtt_connected=mqtt_ok
                    )
            except Exception as e:
                logger.error(f"[LCDWorker] Fehler beim LCD-Update: {e}\n{traceback.format_exc()}")
            
            # Takt auf 2.0s zur Entlastung des VINT-Busses
            time.sleep(2.0)


class ConcreteApp:
    VERSION = "8.1.2"

    def __init__(self):
        self.config_path = os.path.join(base_dir, '../config/config.json')
        self.secrets_path = os.path.join(base_dir, '../config/secrets.json')
        
        self.config = self.load_json(self.config_path, {"device_name": "ccssite01", "phidget_serial": 625458, "sensors": []})
        self.secrets = self.load_json(self.secrets_path, {})
        
        self.device_name = self.config.get("device_name", "ccssite01")
        self.phidget_serial = self.config.get("phidget_serial", 625458)
        
        global logger, telem_logger
        logger, telem_logger = setup_loggers(self.device_name)
        
        self.analyzer = ConcreteSettingAnalyzer()
        self.db_path = self.analyzer.db_path
        
        self.last_telemetry_values = {}
        self.db = TelemetryDB(self.db_path)
        
        self.reader = PhidgetReader(self.config, self.phidget_serial)
        
        # LCD Controller temporaer deaktiviert:
        self.lcd = None
        #try:
        #    self.lcd = PhidgetLCDController(port=2, phidget_serial=self.phidget_serial, db_path=self.db_path)
        #except Exception as e:
        #    logger.error(f"[LCD] Initialisierung fehlgeschlagen: {e}")
        #    self.lcd = None

        self.tb_client = self.setup_tb_client()

    def load_json(self, path, fallback):
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return fallback

    def setup_tb_client(self):
        tb_conf = self.secrets.get("thingsboard", {})
        client = TBDeviceMqttClient(
            tb_conf.get("host", "mqtt.thingsboard.cloud"), 
            username=tb_conf.get("username"), 
            password=tb_conf.get("password"),
            client_id=self.device_name
        )
        try:
            client.connect()
            logger.info("MQTT Client mit Thingsboard verbunden.")
        except Exception as e:
            logger.warning(f"MQTT-Verbindung fehlgeschlagen: {e}")
        return client

    def auto_detect_timezone(self):
        try:
            import urllib.request
            req = urllib.request.Request("http://ip-api.com/json/?fields=timezone,status", headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
                if data.get("status") == "success":
                    detected_tz = data.get("timezone")
                    if detected_tz:
                        res_curr = subprocess.run(["timedatectl", "show", "--property=Timezone"], capture_output=True, text=True, timeout=3)
                        curr_tz = res_curr.stdout.strip().split("=", 1)[1] if "Timezone=" in res_curr.stdout else ""
                        
                        if curr_tz != detected_tz:
                            logger.info(f"[TimeManager] Neue Zeitzone via IP-Geolocation erkannt: {detected_tz} (alt: {curr_tz})")
                            subprocess.run(["timedatectl", "set-timezone", detected_tz], check=False)
                            
                            self.config["timezone"] = detected_tz
                            with open(self.config_path, 'w', encoding='utf-8') as f:
                                json.dump(self.config, f, indent=4)
                        else:
                            logger.info(f"[TimeManager] Zeitzone ist bereits korrekt: {detected_tz}")
                        return detected_tz
        except Exception as e:
            logger.warning(f"[TimeManager] Automatische IP-Zeitzonenerkennung fehlgeschlagen/offline: {e}")
            
        return self.config.get("timezone", "Europe/Zurich")
    
    def restore_and_check_time(self):
        tz = self.auto_detect_timezone()
        try:
            subprocess.run(["timedatectl", "set-timezone", tz], check=False)
        except Exception:
            pass

        time_file = os.path.join(base_dir, '../config/time_state.json')
        ntp_ok = False
        try:
            res = subprocess.run(["timedatectl", "show", "--property=NTPSynchronized"], capture_output=True, text=True, timeout=3)
            if "NTPSynchronized=yes" in res.stdout:
                ntp_ok = True
        except Exception:
            pass

        if not ntp_ok and os.path.exists(time_file):
            try:
                with open(time_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    last_ts = data.get("last_timestamp", 0)
                    
                if time.time() < last_ts:
                    from datetime import datetime
                    last_dt_str = datetime.fromtimestamp(last_ts).strftime('%Y-%m-%d %H:%M:%S')
                    logger.warning(f"[TimeManager] Systemzeit veraltet! Setze Uhrzeit auf letzten Stand: {last_dt_str}")
                    subprocess.run(["date", "-s", last_dt_str], check=False)
            except Exception as e:
                logger.error(f"[TimeManager] Fehler beim Wiederherstellen der Zeit: {e}")

        return ntp_ok

    def save_current_time_state(self):
        time_file = os.path.join(base_dir, '../config/time_state.json')
        try:
            from datetime import datetime
            with open(time_file, 'w', encoding='utf-8') as f:
                json.dump({"last_timestamp": time.time(), "updated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, f)
        except Exception as e:
            logger.error(f"[TimeManager] Fehler beim Speichern des Zeit-Status: {e}")

    def get_network_info(self):
        ip = "127.0.0.1"
        conn_type = "Unbekannt"
        ssid = "N/A"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass
        try:
            res = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, timeout=2)
            if res.returncode == 0 and res.stdout.strip():
                ssid = res.stdout.strip()
                conn_type = "WLAN"
            else:
                route_res = subprocess.run(["ip", "route", "get", "8.8.8.8"], capture_output=True, text=True, timeout=2)
                if "wlan" in route_res.stdout or "wlp" in route_res.stdout:
                    conn_type = "WLAN"
                elif "eth" in route_res.stdout or "en" in route_res.stdout:
                    conn_type = "LAN (Ethernet)"
                else:
                    conn_type = "Netzwerk aktiv"
        except Exception:
            conn_type = "Netzwerk aktiv"
        return ip, conn_type, ssid

    def run(self):
        logger.info(f"Starte Phidget-App (v{self.VERSION}) fuer Geraet: {self.device_name}")
        
        ntp_synced = self.restore_and_check_time()
        
        try:
            threading.Thread(target=ntfy_control_listener.listen_ntfy, daemon=True).start()
            logger.info("ntfy-Steuerungs-Listener gestartet.")
        except Exception as ex:
            logger.error(f"Fehler beim Starten des ntfy-Listeners: {ex}")

        mqtt_worker = MQTTWorkerThread(self)
        mqtt_worker.start()

        # LCD Worker temporaer auskommentieren:
        # lcd_worker = LCDWorkerThread(self)
        # lcd_worker.start()

        current_ip, connection_type, current_ssid = self.get_network_info()
        logger.info(f"IP: {current_ip} | Verbindung: {connection_type} | SSID: {current_ssid} | NTP-Sync: {ntp_synced}")
        try:
            notifier.send_startup_notification(current_ip, self.device_name)
            logger.info("Startup-Benachrichtigung erfolgreich via ntfy gesendet.")
        except Exception as ex:
            logger.error(f"Fehler beim Senden der Startup-Notification: {ex}")

        self.reader.setup_sensors()

        housekeeping_counter = 0
        time_state_counter = 0

        while True:
            loop_start = time.time()
            now_ts = loop_start
            
            try:
                housekeeping_counter += 1
                if housekeeping_counter >= 180:
                    self.db.run_housekeeping()
                    housekeeping_counter = 0

                # Zeitstatus nur alle 5 Minuten (15 x 20s) auf Disk schreiben
                time_state_counter += 1
                if time_state_counter >= 15:
                    self.save_current_time_state()
                    time_state_counter = 0

                telemetry_averages = self.reader.collect_oversampled_telemetry(duration_seconds=20, sample_interval=1.0)
                self.last_telemetry_values = telemetry_averages

                telemetry_values = {"device": self.device_name}
                telemetry_values.update(telemetry_averages)

                log_parts = []
                for k, v in telemetry_values.items():
                    if k == "device":
                        continue
                    friendly = self.analyzer.cfg.get_friendly_channel_name(k)
                    log_parts.append(f"{friendly} ({k}): {v:.1f} Grad C" if isinstance(v, (int, float)) else f"{friendly}: {v}")
                
                telem_logger.info(f"[Messung] {' | '.join(log_parts)}")

                self.db.insert_telemetry(now_ts, telemetry_averages)
                self.db.buffer_telemetry_to_queue(now_ts, telemetry_values)

            except Exception as e:
                logger.error(f"Fehler in Hauptschleife: {e}\n{traceback.format_exc()}")

            elapsed = time.time() - loop_start
            sleep_time = max(0.5, 20.0 - elapsed)
            time.sleep(sleep_time)

if __name__ == "__main__":
    ConcreteApp().run()