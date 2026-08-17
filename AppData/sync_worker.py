#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import sqlite3
import logging
from datetime import datetime, timezone
import requests

# AppData- und Projekt-Pfade für Modul-Imports sicherstellen
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.abspath(os.path.join(current_dir, ".."))
for p in [current_dir, parent_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

# ConfigLoader initialisieren
try:
    from config_loader import ConfigLoader
    cfg = ConfigLoader()
    DEVICE_ID = cfg.device_name_technical
except Exception as e:
    cfg = None
    DEVICE_ID = "ccssite01"
    logging.warning(f"ConfigLoader konnte nicht geladen werden, Fallback auf '{DEVICE_ID}': {e}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

DB_PATH = "/usr/userapps/PhidgetProject/AppData/telemetry_buffer.db"
NAS_ENDPOINT = "https://telemetry.concretum-setting.com/api/v1/telemetry/ingest"
API_TOKEN = "DeinGeheimerApiToken456!"
BATCH_SIZE = 100
SYNC_INTERVAL = 30

CHANNEL_MAP = {
    "ambient": 100,
    "humidity": 101,
    "display": 102
}

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Stellt Tabellen, dynamische Sensoren-Spalten und Indizes idempotent sicher."""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    cursor = conn.cursor()

    # 1. Basis-Tabelle anlegen, falls sie gar nicht existiert
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            synced INTEGER DEFAULT 0
        );
    """)

    # 2. Bestehende Spalten auslesen
    cursor.execute("PRAGMA table_info(telemetry)")
    existing_cols = [col[1] for col in cursor.fetchall()]

    # Erwartete Kanäle aus ConfigLoader oder Fallback (bis zu 8 Kanäle)
    expected_channels = (
        cfg.technical_channels if cfg else [f"Temp{i}" for i in range(8)] + ["ambient", "humidity"]
    )

    # Spalten einzeln prüfen und nur hinzufügen, wenn sie noch nicht existieren
    for ch in expected_channels:
        if ch not in existing_cols:
            try:
                cursor.execute(f"ALTER TABLE telemetry ADD COLUMN {ch} REAL;")
                logging.info(f"Spalte '{ch}' zur SQLite-Tabelle 'telemetry' hinzugefügt.")
            except sqlite3.OperationalError as e:
                logging.debug(f"Spalte {ch} konnte nicht hinzugefügt werden: {e}")

    if "synced" not in existing_cols:
        try:
            cursor.execute("ALTER TABLE telemetry ADD COLUMN synced INTEGER DEFAULT 0;")
            logging.info("Spalte 'synced' zur Tabelle 'telemetry' hinzugefügt.")
        except sqlite3.OperationalError:
            pass

    # 3. Index für schnellen Sync sicherstellen
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_synced_id ON telemetry(synced, id);")

    # 4. Befehlstabelle sicherstellen
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            command TEXT NOT NULL,
            payload TEXT,
            executed INTEGER DEFAULT 0
        );
    """)

    conn.commit()
    conn.close()

def sync_batch():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(telemetry)")
    cols = [col["name"] for col in cursor.fetchall()]
    value_cols = [c for c in cols if c not in ("id", "timestamp", "synced")]

    cursor.execute(f"""
        SELECT * FROM telemetry 
        WHERE COALESCE(synced, 0) = 0 
        ORDER BY id ASC 
        LIMIT ?
    """, (BATCH_SIZE,))
    rows = cursor.fetchall()

    if not rows:
        conn.close()
        return

    records_dict = {}
    synced_ids = []

    for row in rows:
        row_id = row["id"]
        ts_val = row["timestamp"]
        
        if isinstance(ts_val, (int, float)):
            iso_ts = datetime.fromtimestamp(ts_val, tz=timezone.utc).isoformat()
        else:
            iso_ts = datetime.now(timezone.utc).isoformat()

        synced_ids.append(row_id)

        for col in value_cols:
            val = row[col]
            if val is not None:
                col_lower = col.lower()
                
                # Thermocouple Kanäle dynamisch (Temp0 .. Temp7 etc.)
                if col_lower.startswith("temp") and col_lower.replace("temp", "").isdigit():
                    ch_idx = int(col_lower.replace("temp", ""))
                elif col_lower in CHANNEL_MAP:
                    ch_idx = CHANNEL_MAP[col_lower]
                else:
                    ch_idx = 200 + abs(hash(col_lower)) % 100

                # Friendly Name aus ConfigLoader oder Fallback
                friendly_name = (
                    cfg.get_friendly_channel_name(col) if cfg else col
                )

                records_dict[(iso_ts, ch_idx)] = {
                    "timestamp": iso_ts,
                    "channel": ch_idx,
                    "temperature": float(val),
                    "job_id": friendly_name
                }

    records = list(records_dict.values())

    if not records:
        placeholders = ",".join("?" for _ in synced_ids)
        cursor.execute(f"UPDATE telemetry SET synced = 1 WHERE id IN ({placeholders})", synced_ids)
        conn.commit()
        conn.close()
        return

    payload = {
        "device_id": DEVICE_ID,
        "records": records
    }

    try:
        headers = {"Authorization": f"Bearer {API_TOKEN}"}
        res = requests.post(NAS_ENDPOINT, json=payload, headers=headers, timeout=15.0)

        if res.status_code == 200:
            placeholders = ",".join("?" for _ in synced_ids)
            cursor.execute(f"UPDATE telemetry SET synced = 1 WHERE id IN ({placeholders})", synced_ids)

            data = res.json()
            if "commands" in data and data["commands"]:
                for cmd in data["commands"]:
                    cursor.execute("""
                        INSERT INTO system_commands (received_at, command, payload, executed)
                        VALUES (datetime('now'), ?, ?, 0)
                    """, (cmd.get("command"), json.dumps(cmd.get("payload", {}))))

            conn.commit()
            logging.info(f"Sync erfolgreich ({DEVICE_ID}): {len(synced_ids)} Zeilen ({len(records)} Messpunkte) übertragen.")
        else:
            logging.warning(f"NAS API meldet Fehler {res.status_code}: {res.text}")

    except requests.exceptions.RequestException as e:
        logging.warning(f"Sync fehlgeschlagen (Netzwerkfehler): {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    logging.info(f"Phidget Telemetry Sync-Worker gestartet für Device: {DEVICE_ID}")
    init_db()
    while True:
        try:
            sync_batch()
        except Exception as e:
            logging.error(f"Unerwarteter Fehler im Sync-Loop: {e}")
        time.sleep(SYNC_INTERVAL)