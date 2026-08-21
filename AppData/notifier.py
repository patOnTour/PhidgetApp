import os
import json
import sqlite3
import socket
import requests
from datetime import datetime

REPO_DIR = "/usr/userapps/PhidgetProject"
CONFIG_PATH = os.path.join(REPO_DIR, "config", "config.json")
SECRETS_PATH = os.path.join(REPO_DIR, "config", "secrets.json")
DB_PATH = os.path.join(REPO_DIR, "AppData", "telemetry_buffer.db")

def get_secrets():
    try:
        with open(SECRETS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def get_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def get_pending_sync_count():
    """Zählt ungesyncte Datensätze im lokalen Puffer."""
    if not os.path.exists(DB_PATH):
        return 0
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM telemetry WHERE synced = 0;")
        count = cur.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0

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

def send_startup_heartbeat():
    """Sendet sofortiges Lebenszeichen beim Hochfahren an den Admin-Kanal."""
    secrets = get_secrets()
    config = get_config()

    server_url = secrets.get("ntfy", {}).get("server_url", "https://ntfy.concretum-setting.com").rstrip("/")
    admin_channel = secrets.get("admin_notify", {}).get("channel_name", "Admin")
    device_name = config.get("device_name", socket.gethostname())
    
    pending_rows = get_pending_sync_count()
    ip_addr = get_ip_address()
    
    # Geschätzte Abbauzeit (Batch: 100 Zeilen alle ~5 Sek)
    est_minutes = round((pending_rows / 100 * 5) / 60, 1) if pending_rows > 0 else 0

    title = f"🟢 Online: {device_name}"
    msg = (
        f"Koffer ist gestartet und betriebsbereit.\n"
        f"• IP-Adresse: {ip_addr}\n"
        f"• Puffer-Rückstand: {pending_rows} Zeilen (~{est_minutes} Min. Sync-Dauer)\n"
        f"• Zeit (UTC): {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    url = f"{server_url}/{admin_channel}"
    try:
        requests.post(
            url,
            data=msg.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "rocket,green_circle"
            },
            timeout=8
        )
    except Exception as e:
        print(f"[Notifier] Konnte Startup-Heartbeat nicht senden: {e}")

if __name__ == "__main__":
    send_startup_heartbeat()