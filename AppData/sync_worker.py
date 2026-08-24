#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import yaml
import sqlite3
import logging
import requests
from datetime import datetime, timezone

BASE_DIR = "/usr/userapps/PhidgetProject"
CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.yaml")
RAM_DB_PATH = "/tmp/telemetry.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [SyncWorker] %(message)s")
logger = logging.getLogger("SyncWorker")

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def sync_loop():
    cfg = load_config()
    device_id = cfg["device"]["device_id"]
    ingest_url = cfg["server"]["ingest_url"]
    token = cfg["server"]["api_token"]
    
    # 200 Datensätze pro Sendevorgang
    BATCH_SIZE = 200

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    logger.info(f"SyncWorker gestartet für Device: {device_id} -> {ingest_url}")

    while True:
        rows = []
        pending_count = 0
        max_id = None

        # 1. Daten kurz aus SQLite lesen und Verbindung sofort wieder SCHLIESSEN
        try:
            with sqlite3.connect(RAM_DB_PATH, timeout=5.0) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                cursor.execute("SELECT COUNT(*) AS cnt FROM telemetry_buffer WHERE synced = 0;")
                pending_count = cursor.fetchone()["cnt"]

                cursor.execute("""
                    SELECT id, timestamp_utc, channel_idx, temperature 
                    FROM telemetry_buffer 
                    WHERE synced = 0 
                    ORDER BY id ASC 
                    LIMIT ?;
                """, (BATCH_SIZE,))
                rows = cursor.fetchall()
                if rows:
                    max_id = rows[-1]["id"]
        except Exception as e:
            logger.error(f"Fehler beim Lesen der RAM-DB: {e}")
            time.sleep(2.0)
            continue

        # Wenn keine Daten da sind, kurz pausieren
        if not rows:
            time.sleep(1.0)
            continue

        # 2. JSON Payload aufbauen (1Hz Werte aus 10Hz Oversampling)
        records = []
        for r in rows:
            dt_iso = datetime.fromtimestamp(r["timestamp_utc"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            records.append({
                "timestamp": dt_iso,
                "channel": int(r["channel_idx"]),
                "temperature": float(r["temperature"]),
                "job_id": f"temp{r['channel_idx']}" if r["channel_idx"] < 100 else ("ambient" if r["channel_idx"] == 100 else "humidity")
            })

        payload = {
            "device_id": device_id,
            "records": records
        }

        batch_headers = dict(headers)
        batch_headers["X-Pending-Count"] = str(pending_count)

        # 3. An den Server senden
        try:
            res = requests.post(ingest_url, json=payload, headers=batch_headers, timeout=5.0)

            if res.status_code == 200:
                # 4. Nach Erfolg: Gesendete Daten löschen
                with sqlite3.connect(RAM_DB_PATH, timeout=5.0) as conn:
                    conn.execute("DELETE FROM telemetry_buffer WHERE id <= ?;", (max_id,))
                    conn.commit()
                
                logger.info(f"Paket ({len(records)} Werte) gesendet & gelöscht. Restpuffer: {pending_count - len(records)}")

                # Wenn Puffer voll war, sofort weitermachen ohne time.sleep()
                if len(rows) == BATCH_SIZE:
                    continue
            else:
                logger.error(f"Server-Fehler ({res.status_code}): {res.text}")
                time.sleep(3.0)

        except requests.exceptions.RequestException as req_ex:
            logger.warning(f"Server nicht erreichbar ({req_ex}). Offline-Puffer aktiv.")
            time.sleep(5.0)
        except Exception as e:
            logger.error(f"Unerwarteter Fehler im Sendevorgang: {e}")
            time.sleep(3.0)

        time.sleep(1.0)

if __name__ == "__main__":
    sync_loop()