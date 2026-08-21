#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: sync_worker.py
Beschreibung: Batch-Sync der 1-Hz-Telemetriedaten zur Synology REST-API.
Version: 3.2.1 (Fixed Timestamp Syntax & UTC Normalization)
"""

import time
import os
import json
import sqlite3
import logging
import requests
from datetime import datetime, timezone
from config_loader import ConfigLoader
from telemetry_db import TelemetryDB

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

config_loader = ConfigLoader()
config = config_loader.main_config
secrets = config_loader.secrets

DEVICE_ID = config.get("device_name", "ccssite01")
API_URL = secrets.get("api", {}).get("ingest_url", "https://telemetry.concretum-setting.com/api/v1/telemetry/ingest")
API_TOKEN = secrets.get("api", {}).get("token", "")
SYNC_INTERVAL = 5  # Alle 5 Sekunden senden
BATCH_SIZE = 50

db = TelemetryDB()

def get_channel_number(col_name):
    c = col_name.lower().strip()
    if 'ambient' in c or 'umgebung' in c:
        return 100
    elif 'humidity' in c or 'feucht' in c:
        return 101
    elif 'temp' in c:
        digits = ''.join(filter(str.isdigit, c))
        return int(digits) if digits else 0
    return None

def sync_batch():
    conn = sqlite3.connect(db.db_path, timeout=10.0)
    cursor = conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.execute("PRAGMA table_info(telemetry);")
        all_cols = [c[1].lower() for c in cursor.fetchall() if c[1].lower() not in ['id', 'synced']]

        cols_select = ", ".join(f'"{col}"' for col in all_cols)
        cursor.execute(f"""
            SELECT id, {cols_select}
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
            synced_ids.append(row_id)
            
            row_data = dict(zip(all_cols, r[1:]))
            ts_raw = row_data.get('timestamp')

            # Zeitstempel sauber als UTC auflösen
            if isinstance(ts_raw, (int, float)):
                ts_formatted = datetime.fromtimestamp(ts_raw, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            elif isinstance(ts_raw, str):
                try:
                    dt_obj = datetime.strptime(ts_raw[:19], '%Y-%m-%d %H:%M:%S')
                    ts_formatted = dt_obj.strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    ts_formatted = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            else:
                ts_formatted = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

            for col_name, val in row_data.items():
                if col_name == 'timestamp' or val is None:
                    continue
                
                ch_num = get_channel_number(col_name)
                if ch_num is not None:
                    try:
                        records.append({
                            "timestamp": ts_formatted,
                            "channel": ch_num,
                            "temperature": round(float(val), 2)
                        })
                    except (ValueError, TypeError):
                        pass

        if not records:
            # Falls nur leere Messpunkte vorlagen, trotzdem als gesynct markieren
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
        
        if res.status_code in [200, 201]:
            placeholders = ",".join("?" for _ in synced_ids)
            cursor.execute(f"UPDATE telemetry SET synced = 1 WHERE id IN ({placeholders})", synced_ids)
            conn.commit()
            logging.info(f"Sync OK ({DEVICE_ID}): {len(synced_ids)} Datensätze gepuffert / {len(records)} Messpunkte an API gesendet.")
        else:
            logging.warning(f"Server meldet Status {res.status_code}: {res.text}")

    except requests.exceptions.RequestException as e:
        logging.debug(f"Sync wartet (Offline/Netzwerkfehler): {e}")
    except Exception as e:
        logging.error(f"Fehler im sync_batch: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    logging.info(f"1-Hz Sync-Worker gestartet fuer {DEVICE_ID} an {API_URL}")
    while True:
        try:
            sync_batch()
        except Exception as e:
            logging.error(f"Unerwarteter Fehler: {e}")
        time.sleep(SYNC_INTERVAL)