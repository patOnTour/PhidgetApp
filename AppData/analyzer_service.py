#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: analyzer_service.py
Beschreibung: Dedizierter Dienst fuer Regressionsberechnung, Triggerevaluierung, 
              WebGUI-Status-Synchronisation und automatischen 30-Minuten-Export.
Version: 2.1.0 (6h-Fallback Support, P0/P1 Fixes & Server-konforme Notifications)
"""

import time
import os
import sqlite3
import logging
import subprocess
import traceback
import pandas as pd
from datetime import datetime, timedelta

from config_loader import ConfigLoader
from advanced_analyzer import ConcreteSettingAnalyzer
import notifier

base_dir = os.path.dirname(os.path.abspath(__file__))
log_dir = os.path.join(base_dir, "logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "analyzer_service.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [AnalyzerService] %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("AnalyzerService")


class AnalyzerService:
    VERSION = "2.1.0"

    def __init__(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.db_path = os.path.join(base_dir, "telemetry_buffer.db")
        self.config_path = os.path.join(base_dir, '../config/config.json')
        self.exporter_script = os.path.join(base_dir, "exporter.py")
        self.analyzer = ConcreteSettingAnalyzer(db_path=self.db_path)

    def _get_db_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute('PRAGMA journal_mode=WAL;')
        return conn

    def check_manual_exports(self, channels):
        """1. Prueft force_export flags via advanced_analyzer und loest den Exporter aus."""
        for channel in channels:
            try:
                is_force_export = self.analyzer.check_and_clear_export_flag(channel)
                if is_force_export:
                    friendly_name = self.analyzer.cfg.get_friendly_channel_name(channel)
                    logger.info(f"[Manual Export] Manueller EXPORT fuer {channel} ({friendly_name}) via exporter.py ausgeloest!")
                    self.run_exporter(channel, export_type="MANUAL")
            except Exception as e:
                logger.error(f"Fehler bei manueller Export-Pruefung fuer {channel}: {e}")

    def check_scheduled_exports(self):
        """2. Prueft faellige 30-Minuten Auto-Exporte und fuehrt exporter.py aus."""
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            now_dt = datetime.now().astimezone()
            now_iso = now_dt.strftime('%Y-%m-%d %H:%M:%S')

            # Faellige Exporte suchen
            cursor.execute("""
                SELECT channel, target_export_time, export_attempts 
                FROM setting_state 
                WHERE trigger_fired = 1 
                  AND (export_status = 'PENDING' OR export_status IS NULL)
                  AND target_export_time IS NOT NULL 
                  AND target_export_time <= ?
            """, (now_iso,))
            
            due_jobs = cursor.fetchall()

            for ch, target_time, attempts in due_jobs:
                logger.info(f"[Auto Export Scheduler] Faelliger 30-Minuten-Export fuer Kanal {ch} erkannt (Zielzeit war: {target_time}).")
                
                # Atomar als RUNNING markieren
                cursor.execute("""
                    UPDATE setting_state 
                    SET export_status = 'RUNNING', export_attempts = export_attempts + 1 
                    WHERE LOWER(channel) = ?
                """, (ch.lower(),))
                conn.commit()

                # Exporter ausfuehren
                success = self.run_exporter(ch, export_type="AUTOMATIC_30MIN")

                if success:
                    # 1. setting_state aktualisieren (trigger_fired = 2 fuer abgeschlossen)
                    cursor.execute("""
                        UPDATE setting_state 
                        SET export_status = 'FINISHED', 
                            trigger_fired = 2, 
                            exported_at = ? 
                        WHERE LOWER(channel) = ?
                    """, (datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S'), ch.lower()))
                    
                    # 2. Kanal vollstaendig auf STOPPED setzen
                    cursor.execute("""
                        UPDATE channel_control 
                        SET status = 'STOPPED', started_at = NULL, force_export = 0, updated_at = ? 
                        WHERE LOWER(channel) = ?
                    """, (datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S'), ch.lower()))
                    
                    logger.info(f"[Auto Export Scheduler] Export fuer Kanal {ch} erfolgreich abgeschlossen und Kanal auf STOPPED gesetzt.")
                else:
                    cursor.execute("""
                        UPDATE setting_state 
                        SET export_status = 'PENDING' 
                        WHERE LOWER(channel) = ?
                    """, (ch.lower(),))
                
                conn.commit()

            conn.close()
        except Exception as e:
            logger.error(f"Fehler im Auto-Export-Scheduler: {e}")

    def run_exporter(self, channel, export_type="AUTO"):
        """Startet den CLI-Exporter als entkoppelten Subprocess."""
        try:
            logger.info(f"[Exporter Execution] Starte exporter.py fuer Kanal {channel} (Typ: {export_type})...")
            cmd = ["python3", self.exporter_script, "--channel", str(channel)]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = proc.communicate(timeout=60)

            if proc.returncode == 0:
                logger.info(f"[Exporter Execution] Exporter fuer {channel} erfolgreich beendet.")
                return True
            else:
                logger.error(f"[Exporter Execution] Exporter Fehler ({proc.returncode}): {stderr}")
                return False
        except subprocess.TimeoutExpired:
            logger.error(f"[Exporter Execution] Timeout (>60s) beim Export von Kanal {channel}.")
            return False
        except Exception as e:
            logger.error(f"[Exporter Execution] Unerwarteter Fehler beim Exporter-Aufruf: {e}")
            return False

    def process_channel_analysis(self, channel):
        """3. Mathematische Regressions- & Trigger-Analyse via ConcreteSettingAnalyzer."""
        try:
            status = self.analyzer.get_channel_status(channel)
            if status not in ["RUNN", "RUNNING"]:
                return

            conn_start = self._get_db_connection()
            cursor_start = conn_start.cursor()
            cursor_start.execute("SELECT started_at FROM channel_control WHERE LOWER(channel) = LOWER(?)", (channel,))
            row_start = cursor_start.fetchone()
            started_at_str = row_start[0] if row_start and row_start[0] else None
            
            cursor_start.execute("SELECT trigger_fired FROM setting_state WHERE LOWER(channel) = LOWER(?)", (channel,))
            row_trig = cursor_start.fetchone()
            conn_start.close()

            already_fired = row_trig[0] if row_trig else 0
            if already_fired != 0:
                return

            # Startzeit in Sekunden ermitteln fuer den 6h-Fallback
            start_time_sec = None
            if started_at_str:
                try:
                    clean_str = str(started_at_str).replace('T', ' ')[:19]
                    dt_start = datetime.strptime(clean_str, '%Y-%m-%d %H:%M:%S')
                    start_time_sec = dt_start.timestamp()
                except Exception:
                    start_time_sec = None

            times_sec, temps, ambs, parsed_times = self.analyzer.load_data_from_db(channel)
            if temps is None or len(temps) < 6:
                return

            df_history = pd.DataFrame({'Timestamp': parsed_times, channel: temps})

            # Vollstaendige Triggerevaluierung aus advanced_analyzer
            trigger_type, t_ab, temp_ab, rot_val, slope_val = self.analyzer.evaluate_triggers(
                channel, df_history, start_time_sec=start_time_sec
            )
            
            if trigger_type:
                now_dt = datetime.now().astimezone()
                target_dt = now_dt + timedelta(minutes=30)
                
                t_ab_str = str(t_ab)
                target_str = target_dt.strftime('%Y-%m-%d %H:%M:%S')
                now_iso = now_dt.isoformat()

                logger.info(f"🔥 [TRIGGER ERKANNT] Kanal {channel}! Typ: {trigger_type}, Abbindebeginn: {t_ab_str}, Temp: {temp_ab:.1f}°C.")

                # Atomare Aktualisierung von setting_state UND channel_control
                conn = self._get_db_connection()
                cursor = conn.cursor()
                
                cursor.execute('''
                    INSERT INTO setting_state 
                        (channel, probe_inserted, trigger_fired, trigger_time, target_export_time, export_status)
                    VALUES (?, 1, 1, ?, ?, 'PENDING')
                    ON CONFLICT(channel) DO UPDATE SET 
                        trigger_fired = 1, 
                        trigger_time = excluded.trigger_time, 
                        target_export_time = excluded.target_export_time,
                        export_status = 'PENDING'
                ''', (channel, t_ab_str, target_str))

                cursor.execute('''
                    UPDATE channel_control 
                    SET status = 'TRIGGERED', updated_at = ? 
                    WHERE LOWER(channel) = LOWER(?)
                ''', (now_iso, channel))

                conn.commit()
                conn.close()

                # Alarmierung via Notifier an ntfy senden
                notifier.send_setting_alarm_notification(
                    channel=channel,
                    trigger_type=trigger_type,
                    t_ab_str=t_ab_str,
                    temp_ab=temp_ab
                )

        except Exception as e:
            logger.error(f"Fehler bei Analyse von Kanal {channel}: {e}\n{traceback.format_exc()}")

    def run(self):
        logger.info(f"Starte Analyse-Dienst (v{self.VERSION})...")
        
        while True:
            try:
                time.sleep(10)
                
                self.analyzer.cfg = ConfigLoader()
                channels = self.analyzer.cfg.get_temperature_channels()

                # 1. Manuelle Exporte verarbeiten
                self.check_manual_exports(channels)

                # 2. Faellige 30-Minuten Auto-Exporte verarbeiten
                self.check_scheduled_exports()

                # 3. Triggerevaluierung fuer alle aktiven Kanaele
                for channel in channels:
                    self.process_channel_analysis(channel)

            except Exception as e:
                logger.error(f"Fehler in Analyse-Schleife: {e}\n{traceback.format_exc()}")


if __name__ == "__main__":
    AnalyzerService().run()