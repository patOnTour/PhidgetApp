#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: telemetry_db.py
Beschreibung: Kapselt alle SQLite-Datenbankoperationen fuer Telemetrie, Queue, Export-Status und Housekeeping.
Version: 1.3.0 (Dynamische Spalten & P0-Export-Erweiterung)
"""

import sqlite3
import json
import time
import logging
from datetime import datetime
from config_loader import ConfigLoader

logger = logging.getLogger("TelemetryDB")

class TelemetryDB:
    def __init__(self, db_path):
        self.db_path = db_path
        self.cfg = ConfigLoader()
        self.init_database()

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute('PRAGMA journal_mode=WAL;')
        return conn

    def init_database(self):
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # 1. Tabellen mit Basisschema anlegen
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS setting_state (
                    channel TEXT PRIMARY KEY,
                    probe_inserted INTEGER DEFAULT 1,
                    trigger_fired INTEGER DEFAULT 0,
                    trigger_time TEXT,
                    target_export_time TEXT,
                    timestamp TEXT,
                    cooldown_until TEXT,
                    export_status TEXT DEFAULT 'PENDING',
                    exported_at TEXT,
                    export_attempts INTEGER DEFAULT 0,
                    export_error TEXT
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS channel_control (
                    channel TEXT PRIMARY KEY,
                    status TEXT DEFAULT 'STOP',
                    force_export INTEGER DEFAULT 0,
                    updated_at TEXT,
                    started_at TEXT
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS telemetry_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    payload TEXT
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    ambient REAL,
                    humidity REAL
                )
            ''')

            conn.commit()

            # 2. Spaltenmigration für setting_state durchführen
            cursor.execute("PRAGMA table_info(setting_state)")
            existing_ss_cols = [row[1].lower() for row in cursor.fetchall()]

            ss_migrations = {
                "export_status": "TEXT DEFAULT 'PENDING'",
                "exported_at": "TEXT",
                "export_attempts": "INTEGER DEFAULT 0",
                "export_error": "TEXT"
            }

            for col_name, col_type in ss_migrations.items():
                if col_name.lower() not in existing_ss_cols:
                    cursor.execute(f"ALTER TABLE setting_state ADD COLUMN {col_name} {col_type}")
                    logger.info(f"[DB Migration] Spalte '{col_name}' zu 'setting_state' hinzugefügt.")

            # --- HIER: Migration für telemetry_queue ---
            cursor.execute("PRAGMA table_info(telemetry_queue)")
            existing_tq_cols = [row[1].lower() for row in cursor.fetchall()]
            if "retry_count" not in existing_tq_cols:
                cursor.execute("ALTER TABLE telemetry_queue ADD COLUMN retry_count INTEGER DEFAULT 0")
                logger.info("[DB Migration] Spalte 'retry_count' zu 'telemetry_queue' hinzugefügt.")
            # -------------------------------------------

            # 3. Dynamische Spaltenmigration für telemetry ausführen (z. B. Temp0..Temp7)
            cursor.execute("PRAGMA table_info(telemetry)")
            existing_telem_cols = [row[1].lower() for row in cursor.fetchall()]

            for ch_key in self.cfg.technical_channels:
                ch_clean = ch_key.lower()
                if ch_clean not in existing_telem_cols:
                    cursor.execute(f"ALTER TABLE telemetry ADD COLUMN {ch_clean} REAL")
                    logger.info(f"[DB Migration] Dynamische Spalte '{ch_clean}' zu 'telemetry' hinzugefügt.")

            # 4. Kanäle in channel_control & setting_state vorinitialisieren
            now_iso = datetime.now().isoformat()
            for ch_key in self.cfg.get_temperature_channels():
                cursor.execute(
                    "INSERT OR IGNORE INTO channel_control (channel, status, updated_at) VALUES (?, 'STOP', ?)",
                    (ch_key, now_iso)
                )
                cursor.execute(
                    "INSERT OR IGNORE INTO setting_state (channel, probe_inserted, trigger_fired) VALUES (?, 1, 0)",
                    (ch_key,)
                )

            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Fehler bei DB-Initialisierung: {e}")

    def insert_telemetry(self, timestamp, telemetry_data):
        try:
            db_data = {"timestamp": timestamp, "ambient": 20.0, "humidity": 50.0}
            
            # Keys kleingeschrieben für SQLite-Matching vorbereiten
            for k, v in telemetry_data.items():
                db_data[k.lower()] = v

            conn = self._get_connection()
            cursor = conn.cursor()
            
            # Nur Spalten einfügen, die in der Datenbank auch existieren
            cursor.execute("PRAGMA table_info(telemetry)")
            valid_cols = [row[1].lower() for row in cursor.fetchall()]
            
            filtered_data = {k: v for k, v in db_data.items() if k.lower() in valid_cols}

            cols = ",".join(filtered_data.keys())
            placeholders = ",".join(["?"] * len(filtered_data))
            
            cursor.execute(f"INSERT INTO telemetry ({cols}) VALUES ({placeholders})", list(filtered_data.values()))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Fehler beim Speichern der Telemetrie: {e}")

    def buffer_telemetry_to_queue(self, timestamp, payload_dict):
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            payload_str = json.dumps(payload_dict, ensure_ascii=False)
            cursor.execute("INSERT INTO telemetry_queue (timestamp, payload) VALUES (?, ?)", (timestamp, payload_str))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Fehler beim Puffern in telemetry_queue: {e}")

    def get_channel_states(self):
        """Liest den aktuellen Status aller Kanäle aus SQLite für das LCD Display."""
        states = {}
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT channel, status FROM channel_control")
            rows = cursor.fetchall()
            conn.close()
            for ch, st in rows:
                states[ch] = st.upper() if st else "STOPPED"
        except Exception as e:
            logger.error(f"Fehler beim Lesen der Kanal-Zustände: {e}")
        return states

    def run_housekeeping(self, max_age_hours=24):
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cutoff = time.time() - (max_age_hours * 3600)
            cursor.execute("DELETE FROM telemetry WHERE timestamp < ?", (cutoff,))
            deleted = cursor.rowcount
            conn.commit()
            conn.close()
            if deleted > 0:
                logger.info(f"[Housekeeping] {deleted} alte Telemetrie-Zeilen (>24h) aus DB gelöscht.")
        except Exception as e:
            logger.error(f"Fehler bei Housekeeping: {e}")

    def reset_channel(self, channel):
        """Setzt einen Kanal vollständig zurück (status = 'RESET', started_at = NULL)."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            ch_clean = str(channel).strip().lower()

            cursor.execute("PRAGMA table_info(channel_control)")
            cols = [row[1].lower() for row in cursor.fetchall()]

            updates = ["status = 'RESET'", "started_at = NULL"]
            if 'force_export' in cols:
                updates.append("force_export = 0")

            query = f"UPDATE channel_control SET {', '.join(updates)} WHERE LOWER(channel) = ?"
            cursor.execute(query, (ch_clean,))

            cursor.execute("""
                UPDATE setting_state 
                SET trigger_fired = 0, 
                    trigger_time = NULL, 
                    target_export_time = NULL, 
                    export_status = 'PENDING', 
                    exported_at = NULL, 
                    export_attempts = 0, 
                    export_error = NULL, 
                    cooldown_until = NULL 
                WHERE LOWER(channel) = ?
            """, (ch_clean,))

            conn.commit()
            conn.close()
            logger.info(f"Kanal {channel} erfolgreich auf RESET zurückgesetzt.")
            return True
        except Exception as e:
            logger.error(f"Fehler beim Reset von Kanal {channel}: {e}")
            return False

    def start_channel(self, channel):
        """Schaltet einen Kanal scharf (status = 'RUNN', started_at = NOW())."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            ch_clean = str(channel).strip().lower()
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            cursor.execute("PRAGMA table_info(channel_control)")
            cols = [row[1].lower() for row in cursor.fetchall()]

            if 'force_export' in cols:
                cursor.execute(
                    "UPDATE channel_control SET status = 'RUNN', started_at = ?, force_export = 0 WHERE LOWER(channel) = ?",
                    (now_str, ch_clean)
                )
            else:
                cursor.execute(
                    "UPDATE channel_control SET status = 'RUNN', started_at = ? WHERE LOWER(channel) = ?",
                    (now_str, ch_clean)
                )

            cursor.execute("""
                UPDATE setting_state 
                SET trigger_fired = 0, 
                    trigger_time = NULL, 
                    target_export_time = NULL, 
                    export_status = 'PENDING' 
                WHERE LOWER(channel) = ?
            """, (ch_clean,))

            conn.commit()
            conn.close()
            logger.info(f"Kanal {channel} gestartet (RUNN) mit started_at={now_str}.")
            return True
        except Exception as e:
            logger.error(f"Fehler beim Starten von Kanal {channel}: {e}")
            return False