#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: sync_worker.py
Beschreibung: Batch-Sync der 1-Hz-Telemetriedaten zur Synology REST-API mit Zeitzonen-Konvertierung.
Version: 3.1.2
"""

import time
import os
import json
import sqlite3
import logging
from datetime import datetime, timezone
import requests
from config_loader import ConfigLoader
from telemetry_db import TelemetryDB

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

config_loader = ConfigLoader()
config = config_loader.main_config
secrets = config_loader.secrets

DEVICE_ID = config.get("device_name", "ccssite01")
API_URL = secrets.get("api", {}).get("ingest_url", "https://telemetry.concretum-setting.com/api/v1/telemetry/ingest")
API_TOKEN = secrets.get("api", {}).get("token", "")
SYNC_INTERVAL = 5
BATCH_SIZE = 50

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telemetry_buffer.db")
db = TelemetryDB(DB_PATH)

def format_to_utc_iso(raw_val):
    """Konvertiert alte Unix-Floats oder naive Strings sicher in ISO-8601 UTC."""
    if isinstance(raw_val, (int, float)):
        return datetime.fromtimestamp(float(raw_val), tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    
    val_str = str(raw_val).strip()
    # Pruefen auf String-Float wie '1787225812.52'
    if val_str.replace(".", "", 1).isdigit():
        return datetime.fromtimestamp(float(val_str), tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    
    # Pruefen auf alten SQL-String 'YYYY-MM-DD HH:MM:SS'
    if " " in val_str and "T" not in val_str:
        try:
            dt = datetime.strptime(val_str, '%Y-%m-%d %H:%M:%S')
            return dt.replace(tzinfo=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            pass

    # Bereits ISO-konform oder Fallback
    if "T" in val_str:
        return val_str if (val_str.endswith("Z") or "+" in val_str) else f"{val_str}Z"
    
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def sync_batch():
    conn = sqlite3.connect(db.db_path, timeout=10.0)
    cursor = conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.execute("PRAGMA table_info(telemetry);")
        all_cols = [c[1].lower() for c in cursor.fetchall()]
        
        selected_cols = [c for c in all_cols if "temp" in c or c in ["ambient", "humidity"]]

        cursor.execute(f"""
            SELECT id, timestamp, {', '.join(selected_cols)}
            FROM telemetry
            WHERE synced = 0
            ORDER BY id ASC
            LIMIT ?
        """, (BATCH_SIZE,))
        
        rows = cursor.fetchall()
        if not rows:
            return

        synced_ids = []
        records = []

        for r in rows:
            row_id = r[0]
            raw_ts = r[1]
            synced_ids.append(row_id)
            ts = format_to_utc_iso(raw_ts)

            for idx, col_name in enumerate(selected_cols):
                val = r[2 + idx]
                if val is not None:
                    if col_name == "ambient":
                        ch_num = 100
                    elif col_name == "humidity":
                        ch_num = 101
                    else:
                        try:
                            ch_num = int(''.join(filter(str.isdigit, col_name)))
                        except ValueError:
                            ch_num = idx
                    
                    records.append({
                        "timestamp": ts,
                        "channel": ch_num,
                        "temperature": float(val)
                    })

        if not records:
            placeholders = ",".join("?" for _ in synced_ids)
            cursor.execute(f"UPDATE telemetry SET synced = 1 WHERE id IN ({placeholders})", synced_ids)
            conn.commit()
            return

        payload = {
            "device_id": DEVICE_ID,
            "records": records
        }

        headers = {
            "Authorization": f"Bearer {API_TOKEN}",
            "Content-Type": "application/json"
        }

        res = requests.post(API_URL, json=payload, headers=headers, timeout=8)
        
        if res.status_code == 200:
            placeholders = ",".join("?" for _ in synced_ids)
            cursor.execute(f"UPDATE telemetry SET synced = 1 WHERE id IN ({placeholders})", synced_ids)
            conn.commit()
            logging.info(f"Sync OK ({DEVICE_ID}): {len(synced_ids)} Datensaetze ({len(records)} Messwerte) gesendet.")
        else:
            logging.warning(f"Server meldet Status {res.status_code}: {res.text}")

    except requests.exceptions.RequestException as e:
        logging.debug(f"Sync wartet (Offline/Netzwerkfehler): {e}")
    except Exception as e:
        logging.error(f"Fehler im sync_batch: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    logging.info(f"1-Hz Sync-Worker gestartet fuer {DEVICE_ID}")
    while True:
        try:
            sync_batch()
        except Exception as e:
            logging.error(f"Unerwarteter Fehler: {e}")
        time.sleep(SYNC_INTERVAL)