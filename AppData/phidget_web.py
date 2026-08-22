#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import yaml
import sqlite3
import logging
import threading
from flask import Flask, jsonify, render_template_string

BASE_DIR = "/usr/userapps/PhidgetProject"
CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.yaml")
RAM_DB_PATH = "/tmp/telemetry.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [WebGUI] %(message)s")
logger = logging.getLogger("WebGUI")

app = Flask(__name__)

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def get_latest_telemetry():
    telemetry = {}
    pending_count = 0
    try:
        conn = sqlite3.connect(RAM_DB_PATH, timeout=2.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Pufferstand
        cursor.execute("SELECT COUNT(*) AS cnt FROM telemetry_buffer WHERE synced = 0;")
        pending_count = cursor.fetchone()["cnt"]

        # Neueste Werte je Kanal
        cursor.execute("""
            SELECT channel_idx, temperature, timestamp_utc 
            FROM telemetry_buffer 
            WHERE id IN (SELECT MAX(id) FROM telemetry_buffer GROUP BY channel_idx);
        """)
        rows = cursor.fetchall()
        for r in rows:
            telemetry[r["channel_idx"]] = {
                "temp": r["temperature"],
                "ts": r["timestamp_utc"]
            }
        conn.close()
    except Exception as e:
        logger.error(f"DB-Abfragefehler im WebGUI: {e}")

    return telemetry, pending_count

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ cfg.device.friendly_name }} - Live Status</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #121212; color: #e0e0e0; margin: 0; padding: 20px; }
        .card { background: #1e1e1e; border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 4px 10px rgba(0,0,0,0.5); }
        h1 { margin-top: 0; color: #4caf50; font-size: 24px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 15px; }
        .sensor-box { background: #2a2a2a; border-radius: 8px; padding: 15px; text-align: center; border-left: 4px solid #4caf50; }
        .sensor-val { font-size: 28px; font-weight: bold; color: #fff; margin: 10px 0 0 0; }
        .sensor-label { font-size: 14px; color: #aaa; text-transform: uppercase; }
        .badge { display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; background: #333; color: #fff; }
        .badge-pending { background: #ff9800; color: #000; }
        .badge-ok { background: #4caf50; color: #fff; }
    </style>
    <script>
        async function updateData() {
            try {
                const res = await fetch('/api/live');
                const data = await res.json();
                document.getElementById('pending-count').innerText = data.pending_count;
                
                for (const [ch, val] of Object.entries(data.telemetry)) {
                    const el = document.getElementById('ch-' + ch);
                    if (el) el.innerText = val.temp.toFixed(1) + ' °C';
                }
            } catch (e) {}
        }
        setInterval(updateData, 1000);
    </script>
</head>
<body>
    <div class="card">
        <h1>{{ cfg.device.friendly_name }} ({{ cfg.device.device_id }})</h1>
        <p>Phidget Serial: <strong>{{ cfg.device.phidget_serial }}</strong></p>
        <p>Pufferstand (Unsynced): <span id="pending-count" class="badge badge-pending">{{ pending_count }}</span> Datensätze</p>
    </div>

    <div class="card">
        <h2>Live Messwerte (1 Hz)</h2>
        <div class="grid">
            {% for ch, label in channel_labels.items() %}
            <div class="sensor-box">
                <div class="sensor-label">{{ label }}</div>
                <div class="sensor-val" id="ch-{{ ch }}">
                    {% if ch in telemetry %}{{ "%.1f"|format(telemetry[ch].temp) }} °C{% else %}--.- °C{% endif %}
                </div>
            </div>
            {% endfor %}
        </div>
    </div>
</body>
</html>
"""

@app.route("/")
def index():
    cfg = load_config()
    telemetry, pending_count = get_latest_telemetry()
    
    channel_labels = {100: "Umgebung", 101: "Luftfeuchte"}
    for idx in range(8):
        channel_labels[idx] = f"Temp {idx+1}"

    return render_template_string(
        HTML_TEMPLATE, 
        cfg=cfg, 
        telemetry=telemetry, 
        pending_count=pending_count,
        channel_labels=channel_labels
    )

@app.route("/api/live")
def api_live():
    telemetry, pending_count = get_latest_telemetry()
    return jsonify({
        "telemetry": telemetry,
        "pending_count": pending_count,
        "timestamp_utc": time.time()
    })

# Optionale LCD1100 Integration (falls ein LCD angeschlossen ist)
def lcd_worker():
    cfg = load_config()
    serial = cfg["device"]["phidget_serial"]
    lcd_port = None

    for s in cfg.get("sensors", []):
        if s.get("type") == "lcd1100":
            lcd_port = s.get("port", 2)
            break

    if lcd_port is None:
        logger.info("Kein LCD in config.yaml definiert. LCD-Worker übersprungen.")
        return

    try:
        from Phidget22.Devices.LCD import LCD
        lcd = LCD()
        lcd.setIsLocal(True)
        lcd.setHubPort(lcd_port)
        lcd.setDeviceSerialNumber(serial)
        lcd.openWaitForAttachment(3000)
        logger.info(f"LCD1100 an Port {lcd_port} erfolgreich verbunden.")

        while True:
            telemetry, pending = get_latest_telemetry()
            
            # Zeile 1
            t0 = telemetry.get(0, {}).get("temp", "--.-")
            t1 = telemetry.get(1, {}).get("temp", "--.-")
            line1 = f"T1:{t0}C T2:{t1}C" if isinstance(t0, float) else "Phidget Ready"
            
            # Zeile 2
            line2 = f"Buf: {pending} recs"
            
            lcd.writeText(0, 0, line1[:16].ljust(16))
            lcd.writeText(1, 0, line2[:16].ljust(16))
            lcd.flush()
            time.sleep(1.0)
    except Exception as e:
        logger.warning(f"LCD-Worker nicht gestartet/unterbrochen: {e}")

if __name__ == "__main__":
    t = threading.Thread(target=lcd_worker, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8081, debug=False)
