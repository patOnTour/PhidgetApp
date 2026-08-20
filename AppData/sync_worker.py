#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: sync_worker.py
Beschreibung: Batch-Sync der 1-Hz-Telemetriedaten zur Synology REST-API.
Version: 3.1.0
"""

import time
import os
import json
import sqlite3
import logging
import requests
from config_loader import ConfigLoader
from telemetry_db import TelemetryDB

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

config_loader = ConfigLoader()
config = config_loader.load_config()
secrets = config_loader.load_secrets()

DEVICE_ID = config.get("device_name", "ccssite01")
API_URL = secrets.get("api", {}).get("ingest_url", "https://telemetry.concretum-setting.com/api/v1/telemetry/ingest")
API_TOKEN = secrets.get("api", {}).get("token", "")
SYNC_INTERVAL = 5  # Sendet alle 5-10 Sekunden
BATCH_SIZE = 50     # Bis zu 50 Sekunden Daten pro Paket

db = TelemetryDB()

def sync_batch():
    conn = sqlite3.connect(db.db_path, timeout=10.0)
    cursor = conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.execute("PRAGMA table_info(telemetry);")
        all_cols = [c[1] for c in cursor.fetchall()]
        temp_cols = [c for c in all_cols if "temp" in c.lower()]

        cursor.execute(f"""
            SELECT id, timestamp, {', '.join(temp_cols)}
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
            ts = r[1]
            synced_ids.append(row_id)

            for idx, col_name in enumerate(temp_cols):
                temp_val = r[2 + idx]
                if temp_val is not None:
                    # ch_num extrahieren (z. B. temp0 -> 0)
                    try:
                        ch_num = int(''.join(filter(str.isdigit, col_name)))
                    except ValueError:
                        ch_num = idx
                    
                    records.append({
                        "timestamp": ts,
                        "channel": ch_num,
                        "temperature": float(temp_val)
                    })

        if not records:
            # Wenn nur None-Werte da waren, trotzdem als gesynct abhaken
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
            logging.info(f"Sync OK ({DEVICE_ID}): {len(synced_ids)}s gepuffert / {len(records)} Messwerte gesendet.")
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
