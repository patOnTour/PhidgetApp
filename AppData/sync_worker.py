#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: sync_worker.py
Beschreibung: Robuster Hintergrund-Synchronisationsdienst fuer Telemetriedaten zum NAS/Server.
              - 0-basiertes Index-Mapping (temp0=0 ... temp7=7, ambient=100, humidity=101)
              - Verarbeitet Remote-Befehle aus der Ingest-Response (start_channel, stop_channel, export_channel)
              - 5s-Sync-Intervall fuer Heartbeat und latenzarme Fernsteuerung
Version: 2.4.0 (Remote Channel Control via Ingest API Response)
"""

import os
import sys
import time
import json
import sqlite3
import logging
import subprocess
from datetime import datetime, timezone
import requests

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.abspath(os.path.join(current_dir, ".."))
for p in [current_dir, parent_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

from config_loader import ConfigLoader
from telemetry_db import TelemetryDB

cfg = ConfigLoader()
DEVICE_ID = cfg.device_name_technical
DB_PATH = os.path.join(current_dir, "telemetry_buffer.db")
db = TelemetryDB(DB_PATH)

NAS_ENDPOINT = "https://telemetry.concretum-setting.com/api/v1/telemetry/ingest"
API_TOKEN = "DeinGeheimerApiToken456!"
BATCH_SIZE = 100
SYNC_INTERVAL = 5.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [SyncWorker] %(message)s"
)
logger = logging.getLogger("SyncWorker")


def resolve_channel_index(col_name: str) -> int:
    col = col_name.lower().strip()
    
    if col in ["ambient", "ambient_temp", "umgebung"]:
        return 100
    if col in ["humidity", "luftfeuchtigkeit", "feuchte"]:
        return 101

    if col.startswith("temp"):
        suffix = col[4:]
        if suffix.isdigit():
            return int(suffix)

    return 200 + (abs(hash(col)) % 100)


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def execute_system_command(cmd_id: int, command: str, payload_raw: str):
    logger.info(f"[RemoteCommand] Fuehre Befehl #{cmd_id} aus: '{command}'")
    payload = {}
    if payload_raw:
        try:
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
        except Exception:
            payload = {}

    success = False
    try:
        if command == "start_channel":
            channel = payload.get("channel", "Temp0")
            success = db.start_channel(channel)
            logger.info(f"[RemoteCommand] Kanal {channel} gestartet: {success}")

        elif command in ["stop_channel", "reset_channel"]:
            channel = payload.get("channel", "Temp0")
            success = db.reset_channel(channel)
            logger.info(f"[RemoteCommand] Kanal {channel} zurueckgesetzt: {success}")

        elif command == "export_channel":
            channel = payload.get("channel", "Temp0")
            success = db.request_export(channel)
            logger.info(f"[RemoteCommand] Export fuer Kanal {channel} angefordert: {success}")

        elif command == "restart_service":
            service_name = payload.get("service", "phidget-app.service")
            res = subprocess.run(["systemctl", "restart", service_name], capture_output=True, text=True, timeout=10)
            success = (res.returncode == 0)

        elif command == "reboot_system":
            logger.warning("[RemoteCommand] System-Reboot via Ingest-API angefordert!")
            subprocess.Popen(["sleep", "3", "&&", "reboot"], shell=True)
            success = True

        elif command == "resync_range":
            start_ts = payload.get("start_ts")
            end_ts = payload.get("end_ts")
            if start_ts and end_ts:
                conn = get_db()
                cur = conn.cursor()
                cur.execute("UPDATE telemetry SET synced = 0 WHERE timestamp >= ? AND timestamp <= ?", (start_ts, end_ts))
                conn.commit()
                conn.close()
                logger.info(f"[RemoteCommand] Telemetriedaten von {start_ts} bis {end_ts} fuer Resync markiert.")
                success = True

        elif command == "custom_sql":
            query = payload.get("query")
            if query and not query.strip().lower().startswith("drop"):
                conn = get_db()
                cur = conn.cursor()
                cur.execute(query)
                conn.commit()
                conn.close()
                success = True

        else:
            logger.warning(f"[RemoteCommand] Unbekannter Befehl: {command}")

    except Exception as e:
        logger.error(f"[RemoteCommand] Fehler bei Ausfuehrung von #{cmd_id} ({command}): {e}")
        success = False

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE system_commands SET executed = ? WHERE id = ?", (1 if success else 2, cmd_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Fehler beim Aktualisieren von system_commands: {e}")


def process_pending_commands():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, command, payload FROM system_commands WHERE executed = 0 ORDER BY id ASC LIMIT 5")
        rows = cur.fetchall()
        conn.close()

        for r in rows:
            execute_system_command(r["id"], r["command"], r["payload"])
    except Exception as e:
        logger.error(f"Fehler beim Auslesen anstehender System-Befehle: {e}")


def sync_batch():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(telemetry)")
    cols = [col["name"].lower() for col in cursor.fetchall()]
    value_cols = [c for c in cols if c not in ("id", "timestamp", "synced")]

    cursor.execute("""
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
                ch_idx = resolve_channel_index(col)
                records_dict[(iso_ts, ch_idx)] = {
                    "timestamp": iso_ts,
                    "channel": ch_idx,
                    "temperature": float(val),
                    "job_id": col
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
        res = requests.post(NAS_ENDPOINT, json=payload, headers=headers, timeout=12.0)

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
            logger.info(f"Sync erfolgreich ({DEVICE_ID}): {len(synced_ids)} Zeilen ({len(records)} Messpunkte) uebertragen.")
        else:
            logger.warning(f"NAS API meldet Status {res.status_code}: {res.text}")

    except requests.exceptions.RequestException as e:
        logger.debug(f"Sync aktuell nicht erreichbar (Offline): {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    logger.info(f"Phidget Telemetry Sync-Worker gestartet fuer Device: {DEVICE_ID}")
    
    while True:
        try:
            sync_batch()
            process_pending_commands()
        except Exception as e:
            logger.error(f"Unerwarteter Fehler im Sync-Loop: {e}")
        time.sleep(SYNC_INTERVAL)