#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: sync_worker.py
Beschreibung: Robuster Telemetrie-Sync-Worker fuer Phidget-Messkoffer.
              - 1-Hz-Abtastwerte blockweise an REST-API synchronisieren
              - ntfy-Lebenszeichen & Ampel-Status (Orange: Stack-Abbau, Gruen: Live)
              - Dynamisches Kanal-Mapping (temp0=0, temp1=1, ... ambient=100, humidity=101)
              - Verarbeitet Remote-Befehle (System Commands)
Version: 4.0.0
"""

import os
import sys
import time
import json
import socket
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [SyncWorker] %(message)s"
)
logger = logging.getLogger("SyncWorker")

cfg = ConfigLoader()
DEVICE_ID = cfg.device_name_technical
DB_PATH = os.path.join(current_dir, "telemetry_buffer.db")
CONFIG_PATH = cfg.main_config_path

# Standard-Konfigurationen
BATCH_SIZE = 100
SYNC_INTERVAL = 5.0          # Im Live-Betrieb alle 5 Sekunden senden
CATCHUP_INTERVAL = 0.5       # Beim Stack-Abbau zuegig senden
STACK_ALERT_THRESHOLD = 50   # Ab dieser Zeilenzahl gilt der Modus als "Stacking / Orange"

# Status-Speicher fuer Ampel-Meldungen
status_state = {
    "is_stacking": False,
    "last_notified_status": None,
    "startup_notified": False
}


def get_secrets():
    try:
        with open(cfg.secrets_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_ip_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def count_unsynced_records():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM telemetry WHERE COALESCE(synced, 0) = 0;")
        count = cur.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def send_admin_notification(title, message, priority="default", tags="information_source"):
    """Sendet System-Status an das Admin-Topic des eigenen ntfy-Servers."""
    secrets = get_secrets()
    server_url = secrets.get("ntfy", {}).get("server_url", "https://ntfy.concretum-setting.com").rstrip("/")
    admin_channel = secrets.get("admin_notify", {}).get("channel_name", "Admin")

    url = f"{server_url}/{admin_channel}"
    try:
        requests.post(
            url,
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": tags
            },
            timeout=8
        )
    except Exception as e:
        logger.debug(f"Admin-Notification fehlgeschlagen: {e}")


def resolve_channel_index(col_name: str) -> int:
    col_lower = col_name.lower().strip()
    if col_lower in ["ambient", "ambient_temp", "umgebung"]:
        return 100
    if col_lower in ["humidity", "luftfeuchtigkeit", "feuchte"]:
        return 101
    if col_lower.startswith("temp"):
        suffix = col_lower[4:]
        if suffix.isdigit():
            return int(suffix)
    return 200 + (abs(hash(col_lower)) % 100)


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
        if command == "restart_service":
            service_name = payload.get("service", "phidget-app.service")
            res = subprocess.run(["systemctl", "restart", service_name], capture_output=True, text=True, timeout=10)
            success = (res.returncode == 0)
        elif command == "reboot_system":
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
                success = True
    except Exception as e:
        logger.error(f"[RemoteCommand] Fehler: {e}")
        success = False

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE system_commands SET executed = ? WHERE id = ?", (1 if success else 2, cmd_id))
        conn.commit()
        conn.close()
    except Exception:
        pass


def process_pending_commands():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, command, payload FROM system_commands WHERE executed = 0 ORDER BY id ASC LIMIT 5")
        rows = cur.fetchall()
        conn.close()
        for r in rows:
            execute_system_command(r["id"], r["command"], r["payload"])
    except Exception:
        pass


def sync_batch():
    global status_state
    secrets = get_secrets()
    api_cfg = secrets.get("api", {})
    endpoint = api_cfg.get("ingest_url", "https://telemetry.concretum-setting.com/api/v1/telemetry/ingest")
    api_token = api_cfg.get("token", "")

    unsynced_total = count_unsynced_records()

    # 1. Ampel-Status prüfen & Admin-Pushes absetzen
    if not status_state["startup_notified"]:
        status_state["startup_notified"] = True
        ip_addr = get_ip_address()
        if unsynced_total > STACK_ALERT_THRESHOLD:
            status_state["is_stacking"] = True
            status_state["last_notified_status"] = "ORANGE"
            est_min = round((unsynced_total / BATCH_SIZE * CATCHUP_INTERVAL) / 60, 1)
            send_admin_notification(
                title=f"🟠 Stack-Abbau: {DEVICE_ID}",
                message=f"Box gestartet (IP: {ip_addr}).\n{unsynced_total} Zeilen im Puffer (~{est_min} Min. Aufholzeit).",
                priority="high",
                tags="large_orange_diamond,hourglass_flowing_sand"
            )
        else:
            status_state["is_stacking"] = False
            status_state["last_notified_status"] = "GREEN"
            send_admin_notification(
                title=f"🟢 Live: {DEVICE_ID}",
                message=f"Box gestartet (IP: {ip_addr}).\nPuffer leer, Live-Stream aktiv.",
                priority="default",
                tags="green_circle,rocket"
            )
    else:
        # Statuswechsel während des Betriebs (Stack abgebaut -> Wechsel auf Grün)
        if status_state["is_stacking"] and unsynced_total <= STACK_ALERT_THRESHOLD:
            status_state["is_stacking"] = False
            status_state["last_notified_status"] = "GREEN"
            send_admin_notification(
                title=f"🟢 Synchronisiert: {DEVICE_ID}",
                message=f"Alle Pufferdaten vollständig abgearbeitet.\nSystem sendet nun im Live-Modus.",
                priority="default",
                tags="green_circle,white_check_mark"
            )

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
        return 0

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
        return 0

    payload = {
        "device_id": DEVICE_ID,
        "records": records
    }

    try:
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "X-Pending-Count": str(unsynced_total)
        }
        res = requests.post(endpoint, json=payload, headers=headers, timeout=10.0)

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
            logger.info(f"Sync OK ({DEVICE_ID}): {len(synced_ids)} Zeilen ({len(records)} Messpunkte) uebertragen. Rest-Puffer: {max(0, unsynced_total - len(synced_ids))}")
        else:
            logger.warning(f"API meldet Status {res.status_code}: {res.text}")

    except requests.exceptions.RequestException as e:
        logger.debug(f"Sync offline: {e}")
    finally:
        conn.close()

    return unsynced_total


if __name__ == "__main__":
    logger.info(f"Phidget Telemetry Sync-Worker gestartet fuer: {DEVICE_ID}")
    TelemetryDB(DB_PATH)
    
    while True:
        try:
            pending = sync_batch()
            process_pending_commands()
            # Wenn noch mehr als 1 Batch im Stack liegt -> sofort mit minimaler Pause weiterleeren
            if pending > BATCH_SIZE:
                time.sleep(CATCHUP_INTERVAL)
            else:
                time.sleep(SYNC_INTERVAL)
        except Exception as e:
            logger.error(f"Unerwarteter Fehler im Sync-Loop: {e}")
            time.sleep(SYNC_INTERVAL)