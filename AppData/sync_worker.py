"""
@file: sync_worker.py
@version: 1.6.1
@date: 2026-08-29
@description: Sync-Worker mit dynamischer Git-Tag-Versionserkennung, SQLite-Pufferung und atomarem Chunk-Delete.
@author: Patrick Staehli
"""

import os
import time
import yaml
import sqlite3
import logging
import requests
import subprocess
from datetime import datetime, timezone

BASE_DIR = "/usr/userapps/PhidgetProject"
CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.yaml")
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "telemetry.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [SyncWorker] %(message)s")
logger = logging.getLogger("SyncWorker")


def get_git_version():
    """Liest den aktuellen Git-Tag oder Short-Commit aus dem Projektordner."""
    try:
        # Exakten Tag pruefen
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--exact-match"],
            cwd=BASE_DIR,
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()
        if tag:
            return tag
    except Exception:
        pass

    try:
        # Fallback: Short Commit Hash
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=BASE_DIR,
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()
        if commit:
            return f"rev-{commit}"
    except Exception:
        pass

    return "v1.6.1"


CLIENT_VERSION = get_git_version()


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"Fehler beim Laden von config.yaml: {e}")
    return {}


def sync_loop():
    global CLIENT_VERSION
    cfg = load_config()
    device_id = cfg.get("device", {}).get("device_id")
    ingest_url = cfg.get("server", {}).get("ingest_url")
    token = cfg.get("server", {}).get("api_token")
    
    if not device_id or not ingest_url or not token:
        logger.error("Fehlende Konfiguration (device_id, ingest_url oder api_token) in config.yaml!")
        time.sleep(5.0)
        return

    BATCH_SIZE = 200

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Client-Version": CLIENT_VERSION
    }

    logger.info(f"SyncWorker gestartet fuer Device: {device_id} (Version: {CLIENT_VERSION}) -> {ingest_url}")

    while True:
        if not os.path.exists(DB_PATH):
            time.sleep(1.0)
            continue

        rows = []
        pending_count = 0
        max_id = None

        try:
            with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
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
            logger.error(f"Fehler beim Lesen der persistenten SQLite-DB: {e}")
            time.sleep(2.0)
            continue

        if not rows:
            time.sleep(1.0)
            continue

        records = []
        for r in rows:
            dt_iso = datetime.fromtimestamp(r["timestamp_utc"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            ch_idx = int(r["channel_idx"])
            
            if ch_idx < 100:
                job_id = f"temp{ch_idx}"
            elif ch_idx == 100:
                job_id = "ambient"
            else:
                job_id = "humidity"

            records.append({
                "timestamp": dt_iso,
                "channel": ch_idx,
                "temperature": float(r["temperature"]),
                "job_id": job_id
            })

        payload = {
            "device_id": device_id,
            "records": records
        }

        batch_headers = dict(headers)
        batch_headers["X-Pending-Count"] = str(pending_count)

        try:
            res = requests.post(ingest_url, json=payload, headers=batch_headers, timeout=5.0)

            if res.status_code == 200:
                with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
                    conn.execute("DELETE FROM telemetry_buffer WHERE id <= ?;", (max_id,))
                    conn.commit()
                
                logger.info(f"Paket ({len(records)} Werte) gesendet. Restpuffer: {pending_count - len(records)}")

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