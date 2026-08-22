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
    batch_size = cfg["server"].get("batch_size", 10)
    sync_interval = cfg["server"].get("sync_interval_sec", 5.0)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    logger.info(f"SyncWorker gestartet für Device: {device_id}")

    while True:
        try:
            conn = sqlite3.connect(RAM_DB_PATH, timeout=5.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Pufferstand ermitteln
            cursor.execute("SELECT COUNT(*) AS cnt FROM telemetry_buffer WHERE synced = 0;")
            pending_count = cursor.fetchone()["cnt"]

            # Ältestes Paket auslesen
            cursor.execute("""
                SELECT id, timestamp_utc, channel_idx, temperature 
                FROM telemetry_buffer 
                WHERE synced = 0 
                ORDER BY id ASC 
                LIMIT ?;
            """, (batch_size,))
            rows = cursor.fetchall()

            if rows:
                records = []
                ids_to_delete = []

                for r in rows:
                    ids_to_delete.append(r["id"])
                    dt_iso = datetime.fromtimestamp(r["timestamp_utc"], tz=timezone.utc).isoformat()
                    records.append({
                        "timestamp": dt_iso,
                        "channel": r["channel_idx"],
                        "temperature": float(r["temperature"]),
                        "job_id": f"temp{r['channel_idx']}" if r["channel_idx"] < 100 else ("ambient" if r["channel_idx"] == 100 else "humidity")
                    })

                payload = {
                    "device_id": device_id,
                    "records": records
                }

                batch_headers = dict(headers)
                batch_headers["X-Pending-Count"] = str(pending_count)

                res = requests.post(ingest_url, json=payload, headers=batch_headers, timeout=8.0)

                if res.status_code == 200:
                    placeholders = ",".join("?" for _ in ids_to_delete)
                    cursor.execute(f"DELETE FROM telemetry_buffer WHERE id IN ({placeholders});", ids_to_delete)
                    conn.commit()
                    logger.info(f"Paket gesendet ({len(records)} Werte). Restpuffer: {pending_count - len(records)}")

            conn.close()

        except requests.exceptions.RequestException:
            logger.debug("Server nicht erreichbar (Offline-Puffer aktiv).")
        except Exception as e:
            logger.error(f"Fehler im Sync-Worker: {e}")

        time.sleep(sync_interval)

if __name__ == "__main__":
    sync_loop()
