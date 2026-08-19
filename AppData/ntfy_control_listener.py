#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: ntfy_control_listener.py
Beschreibung: Empfaengt Ntfy-Befehle (Text & JSON), steuert dynamische Menues
              und synchronisiert Zustaende fuer das Server-Dashboard.
Version: 5.1.0 (Server-Parser-Kompatibilitaet & Vereinheitlichte Status-Strings)
"""

import os
import sys
import json
import sqlite3
import time
import requests
import subprocess
from datetime import datetime
import socket

current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)
from config_loader import ConfigLoader
from telemetry_db import TelemetryDB

VERSION = "5.1.0"

cfg = ConfigLoader()
DB_PATH = os.path.join(current_dir, "telemetry_buffer.db")
db = TelemetryDB(DB_PATH)


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_channel_data():
    channel_values = {}
    channel_controls = {}
    ambient_temp = None
    
    try:
        if os.path.exists(DB_PATH):
            conn = sqlite3.connect(DB_PATH, timeout=5.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Telemetrie lesen
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='telemetry';")
            if cursor.fetchone():
                cursor.execute("PRAGMA table_info(telemetry)")
                columns = [row['name'] for row in cursor.fetchall()]
                
                for tech_key in cfg.technical_channels:
                    matched_col = next((col for col in columns if col.lower() == tech_key.lower()), None)
                    if matched_col:
                        cursor.execute(f"SELECT {matched_col} FROM telemetry WHERE {matched_col} IS NOT NULL ORDER BY timestamp DESC LIMIT 1")
                        row = cursor.fetchone()
                        if row and row[0] is not None:
                            channel_values[tech_key] = row[0]
                
                if 'ambient' in [col.lower() for col in columns]:
                    cursor.execute("SELECT ambient FROM telemetry WHERE ambient IS NOT NULL ORDER BY timestamp DESC LIMIT 1")
                    row_amb = cursor.fetchone()
                    if row_amb and row_amb[0] is not None:
                        ambient_temp = row_amb[0]

            # Kanal-Status lesen
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='channel_control';")
            if cursor.fetchone():
                cursor.execute("PRAGMA table_info(channel_control)")
                cols = [col['name'] for col in cursor.fetchall()]
                name_col = 'name' if 'name' in cols else ('channel' if 'channel' in cols else cols[1])
                status_col = 'status' if 'status' in cols else 'state'
                
                cursor.execute(f"SELECT {name_col}, {status_col}, force_export FROM channel_control")
                for r in cursor.fetchall():
                    ch_name = r[name_col]
                    st = (r[status_col] or 'STOPPED').upper()
                    fx = r['force_export'] if 'force_export' in r.keys() else 0
                    channel_controls[ch_name] = "EXPORT" if fx == 1 else st
                    
            conn.close()
    except Exception as e:
        print(f"[NtfyControlListener] DB-Lesefehler: {e}", flush=True)
        
    return channel_values, channel_controls, ambient_temp


def send_menu_response(title, message, actions, tags=None):
    """Standardisierter Helper zum Senden interaktiver ntfy-Nachrichten."""
    ntfy_channel = cfg.ntfy_channel
    payload = {
        "topic": ntfy_channel,
        "title": title,
        "message": message,
        "markdown": True,
        "actions": actions
    }
    if tags:
        payload["tags"] = tags
        
    try:
        requests.post("https://ntfy.sh", json=payload, timeout=10)
    except Exception as e:
        print(f"[NtfyControlListener] Fehler beim Senden des Menue-Payloads: {e}", flush=True)


def handle_sys_info():
    """Sendet Detail-Informationen ueber System, Netzwerk und Zeitzone."""
    local_ip = get_local_ip()
    web_gui_url = f"http://{local_ip}:8081"
    pretty_name = cfg.device_name_friendly
    tech_name = cfg.device_name_technical
    
    ntp_ok = False
    timezone_str = "UTC"
    try:
        res_sync = subprocess.run(["timedatectl", "show", "--property=NTPSynchronized"], capture_output=True, text=True, timeout=3)
        if "NTPSynchronized=yes" in res_sync.stdout:
            ntp_ok = True
        res_tz = subprocess.run(["timedatectl", "show", "--property=Timezone"], capture_output=True, text=True, timeout=3)
        if "Timezone=" in res_tz.stdout:
            timezone_str = res_tz.stdout.strip().split("=", 1)[1]
    except Exception:
        pass

    sync_str = "🟢 OK (Synchron)" if ntp_ok else "⚠️ NEIN (Gepuffert)"
    now_str = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')

    msg = (
        f"**Geraet:** {pretty_name} (`{tech_name}`)\n"
        f"**IP-Adresse:** {local_ip}\n"
        f"**WebGUI:** {web_gui_url}\n"
        f"**Zeitzone:** {timezone_str}\n"
        f"**Zeitsync:** {sync_str}\n"
        f"**Systemzeit:** {now_str}"
    )

    actions = [
        {
            "action": "view",
            "label": "🌐 WebGUI",
            "url": web_gui_url
        },
        {
            "action": "http",
            "label": "⏰ Uhrzeit Sync",
            "url": f"https://ntfy.sh/{cfg.ntfy_channel}",
            "method": "POST",
            "body": "sync_time"
        },
        {
            "action": "http",
            "label": "📊 Status",
            "url": f"https://ntfy.sh/{cfg.ntfy_channel}",
            "method": "POST",
            "body": "status"
        }
    ]

    send_menu_response(f"ℹ️ SYSTEM-INFO [{pretty_name}]", msg, actions, tags=["information_source"])


def handle_status_overview():
    """Zeigt den Haupt-Status. Formatiert fuer Regex-Parsing auf dem Server."""
    values, controls, ambient = get_channel_data()
    pretty_name = cfg.device_name_friendly
    
    lines = []
    for tech_key in cfg.get_temperature_channels():
        friendly_name = cfg.get_friendly_channel_name(tech_key)
        val = values.get(tech_key, 'N/A')
        val_str = f"{val:.1f} °C" if isinstance(val, (int, float)) else "N/A"
        ctrl = controls.get(tech_key, 'STOPPED')
        
        if ctrl == 'TRIGGERED':
            status_tag = "TRIGGERED"
        elif ctrl in ['RUNN', 'RUNNING']:
            status_tag = "RUNNING"
        else:
            status_tag = "STOPPED"
            
        lines.append(f"• **{friendly_name}** (`{tech_key}`): {status_tag} | **{val_str}**")

    amb_str = f"{ambient:.1f} °C" if ambient is not None else "N/A"
    now_str = datetime.now().astimezone().strftime('%H:%M Uhr')
    
    msg = "\n".join(lines) + f"\n\n*Umgebung:* {amb_str} | *Stand:* {now_str}"

    actions = [
        {
            "action": "http",
            "label": "ℹ️ Info",
            "url": f"https://ntfy.sh/{cfg.ntfy_channel}",
            "method": "POST",
            "body": json.dumps({"cmd": "sys_info"})
        },
        {
            "action": "http",
            "label": "🎛️ Kanaele",
            "url": f"https://ntfy.sh/{cfg.ntfy_channel}",
            "method": "POST",
            "body": json.dumps({"cmd": "list_channels", "page": 1})
        }
    ]

    send_menu_response(f"📊 STATUS [{pretty_name}]", msg, actions, tags=["bar_chart"])


def handle_list_channels(page=1):
    """Listet Temperaturkanaele auf max. 3 Buttons auf (Paginierung)."""
    channels = cfg.get_temperature_channels()
    total_channels = len(channels)
    pretty_name = cfg.device_name_friendly
    
    items_per_page = 2
    total_pages = (total_channels + items_per_page - 1) // items_per_page
    if page > total_pages:
        page = 1

    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_channels = channels[start_idx:end_idx]

    actions = []
    for ch in page_channels:
        friendly = cfg.get_friendly_channel_name(ch)
        actions.append({
            "action": "http",
            "label": friendly[:12],
            "url": f"https://ntfy.sh/{cfg.ntfy_channel}",
            "method": "POST",
            "body": json.dumps({"cmd": "channel_view", "channel": ch})
        })

    if total_pages > 1:
        next_page = (page % total_pages) + 1
        actions.append({
            "action": "http",
            "label": f"Weiter ▶ ({next_page}/{total_pages})",
            "url": f"https://ntfy.sh/{cfg.ntfy_channel}",
            "method": "POST",
            "body": json.dumps({"cmd": "list_channels", "page": next_page})
        })

    msg = f"Waehle einen Kanal zur Detailansicht und Steuerung (Seite {page}/{total_pages}):"
    send_menu_response(f"🎛️ KANAL-AUSWAHL [{pretty_name}]", msg, actions, tags=["control_knobs"])


def handle_channel_view(tech_key):
    """Zeigt Live-Details eines spezifischen Kanals mit Steuerungs-Buttons."""
    values, controls, _ = get_channel_data()
    friendly_name = cfg.get_friendly_channel_name(tech_key)
    pretty_name = cfg.device_name_friendly
    
    val = values.get(tech_key, 'N/A')
    val_str = f"{val:.1f} °C" if isinstance(val, (int, float)) else "N/A"
    ctrl = controls.get(tech_key, 'STOPPED')

    if ctrl == 'TRIGGERED':
        st_text = "TRIGGERED (Abbindebeginn erkannt)"
    elif ctrl in ['RUNN', 'RUNNING']:
        st_text = "RUNNING (Ueberwachung laeuft)"
    else:
        st_text = "STOPPED (Bereit / Inaktiv)"

    msg = (
        f"• **Kanal:** {friendly_name} (`{tech_key}`)\n"
        f"• **Status:** `{tech_key}`: {ctrl}\n"
        f"• **Temperatur:** {val_str}\n"
        f"• **Letztes Update:** {datetime.now().astimezone().strftime('%H:%M:%S Uhr')}"
    )

    actions = [
        {
            "action": "http",
            "label": "▶️ Start (RUNN)",
            "url": f"https://ntfy.sh/{cfg.ntfy_channel}",
            "method": "POST",
            "body": f"start:{tech_key}"
        },
        {
            "action": "http",
            "label": "🔄 Reset (STOP)",
            "url": f"https://ntfy.sh/{cfg.ntfy_channel}",
            "method": "POST",
            "body": f"reset:{tech_key}"
        },
        {
            "action": "http",
            "label": "📊 Export",
            "url": f"https://ntfy.sh/{cfg.ntfy_channel}",
            "method": "POST",
            "body": f"export:{tech_key}"
        }
    ]

    send_menu_response(f"📍 KANAL: {friendly_name} [{pretty_name}]", msg, actions, tags=["thermometer"])


def handle_command(message_text):
    try:
        raw_text = message_text.strip()
        lowered = raw_text.lower()
        
        ignore_markers = [
            "•", "▶️", "🔄", "🌐", "📊", "📍", "🎛️", "ℹ️", "🚀",
            "geraet:", "gerät:", "ip-adresse:", "webgui:", 
            "zeitzone:", "zeitsync:", "systemzeit:", "umgebung:", 
            "letztes update:", "waehle einen kanal", "wähle einen kanal", 
            "daten-export", "abschlussbericht"
        ]
        
        if any(marker in lowered for marker in ignore_markers):
            return False

        print(f"[NtfyControlListener] Verarbeite Input: '{raw_text}'", flush=True)

        # 1. JSON-Commands
        if raw_text.startswith("{") and raw_text.endswith("}"):
            try:
                cmd_data = json.loads(raw_text)
                cmd = cmd_data.get("cmd")
                
                if cmd == "sys_info":
                    handle_sys_info()
                    return True
                elif cmd == "status":
                    handle_status_overview()
                    return True
                elif cmd == "list_channels":
                    handle_list_channels(page=int(cmd_data.get("page", 1)))
                    return True
                elif cmd == "channel_view":
                    handle_channel_view(cmd_data.get("channel", "Temp0"))
                    return True
            except json.JSONDecodeError:
                pass

        # 2. Textbefehle
        text = raw_text.lower()

        if text in ["ping", "pong", "status", "hilfe", "help"]:
            handle_status_overview()
            return True

        elif text in ["info", "sysinfo", "sys_info"]:
            handle_sys_info()
            return True

        elif text.startswith("runn:") or text.startswith("start:"):
            target = text.split(":", 1)[1].strip()
            tech_key = target
            for tk in cfg.technical_channels:
                if target == tk.lower() or target == cfg.get_friendly_channel_name(tk).lower():
                    tech_key = tk
                    break
            db.start_channel(tech_key)
            handle_channel_view(tech_key)
            return True

        elif text.startswith("reset:") or text.startswith("stop:") or text.startswith("stopp:"):
            target = text.split(":", 1)[1].strip()
            tech_key = target
            for tk in cfg.technical_channels:
                if target == tk.lower() or target == cfg.get_friendly_channel_name(tk).lower():
                    tech_key = tk
                    break
            db.reset_channel(tech_key)
            handle_channel_view(tech_key)
            return True

        elif text.startswith("export:"):
            target = text.split(":", 1)[1].strip()
            tech_key = target
            for tk in cfg.technical_channels:
                if target == tk.lower() or target == cfg.get_friendly_channel_name(tk).lower():
                    tech_key = tk
                    break
            conn = sqlite3.connect(DB_PATH, timeout=5.0)
            cursor = conn.cursor()
            now_str = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute("UPDATE channel_control SET force_export = 1, updated_at = ? WHERE LOWER(channel) = LOWER(?)", (now_str, tech_key))
            conn.commit()
            conn.close()
            
            msg_ack = f"📊 Export fuer Kanal **{cfg.get_friendly_channel_name(tech_key)}** (`{tech_key}`) wird gestartet..."
            send_menu_response(f"Export Ausgeloest [{cfg.device_name_friendly}]", msg_ack, [], tags=["outbox_tray"])
            return True

        elif text in ["sync_time", "timesync", "synctime"]:
            try:
                subprocess.run(["systemctl", "restart", "systemd-timesyncd"], check=False)
                time.sleep(2)
                res = subprocess.run(["timedatectl", "show", "--property=NTPSynchronized"], capture_output=True, text=True, timeout=3)
                is_ok = "NTPSynchronized=yes" in res.stdout
                now_str = datetime.now().astimezone().strftime('%H:%M:%S Uhr')
                msg = f"🟢 Zeitsync erfolgreich ({now_str})" if is_ok else f"⚠️ Sync fehlgeschlagen/offline ({now_str})"
                send_menu_response(f"Uhrzeit-Sync [{cfg.device_name_friendly}]", msg, [], tags=["clock1"])
            except Exception as e:
                print(f"[NtfyControlListener] Sync-Fehler: {e}", flush=True)
            return True

    except Exception as e:
        print(f"[NtfyControlListener] Fehler in handle_command: {e}", flush=True)
    return False


def listen_ntfy():
    channel = cfg.ntfy_channel
    if not channel:
        print("[NtfyControlListener] FEHLER: Kein ntfy_channel in secrets.json gefunden!", flush=True)
        return
        
    url = f"https://ntfy.sh/{channel}/json"
    print(f"[NtfyControlListener] Starte interaktiven Stream-Listener auf Kanal: {channel}", flush=True)
    
    while True:
        try:
            with requests.get(url, stream=True, timeout=45) as response:
                if response.status_code == 200:
                    for line in response.iter_lines(chunk_size=1, decode_unicode=True):
                        if line:
                            try:
                                data = json.loads(line)
                                if data.get("event") == "message":
                                    msg = data.get("message", "")
                                    if msg:
                                        handle_command(msg)
                            except json.JSONDecodeError:
                                pass
                            except Exception as inner_e:
                                print(f"[NtfyControlListener] Zeilen-Fehler: {inner_e}", flush=True)
                        else:
                            time.sleep(0.1)
                else:
                    time.sleep(10)
        except requests.exceptions.Timeout:
            time.sleep(2)
        except Exception as e:
            print(f"[NtfyControlListener] Stream-Verbindungsfehler: {e}. Neustart in 10s...", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    listen_ntfy()