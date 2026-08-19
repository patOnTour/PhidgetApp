#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: notifier.py
Beschreibung: Verwaltet den ntfy.sh Versand für Beton-Messungen, CSV/Plot-Anhänge,
              Turnaround-Events sowie geschützte Admin-Meldungen unter Berücksichtigung
              der Server-Listener Triggerwörter.
Version: v3.3-prod (Exakte Server-Kompatibilität für TRIGGER/ALARM/FERTIG)
"""

import os
import requests
import logging
import subprocess
from datetime import datetime
from config_loader import ConfigLoader

logger = logging.getLogger("Notifier")
cfg = ConfigLoader()


def get_ntfy_url(admin=False):
    """Liest den ntfy-Kanal direkt und ohne stille Fallbacks aus der secrets.json."""
    secrets = cfg.secrets
    
    if admin:
        channel = secrets.get("admin_notify", {}).get("channel_name") or secrets.get("notify", {}).get("channel_name")
    else:
        channel = secrets.get("notify", {}).get("channel_name")
        
    if channel:
        return f"https://ntfy.sh/{channel.strip()}"
    return ""


def send_push_notification(title, message, tags="", priority="default", attachment_file=None, attachment_name=None, admin=False):
    """Sendet Push-Benachrichtigungen an den ntfy.sh Kanal (inkl. Datei-/Plot-Anhang)."""
    url = get_ntfy_url(admin=admin)
    if not url:
        logger.error("Kein ntfy-Kanal in secrets.json definiert.")
        return False
        
    try:
        headers = {
            "Title": title,
            "Tags": str(tags),
            "Priority": str(priority)
        }
        
        if attachment_file and os.path.exists(attachment_file):
            filename = attachment_name or os.path.basename(attachment_file)
            headers["X-Filename"] = filename
            headers["Message"] = message
            
            with open(attachment_file, "rb") as file_handle:
                file_data = file_handle.read()
                
            response = requests.post(
                url, 
                data=file_data, 
                headers=headers, 
                timeout=20
            )
        else:
            response = requests.post(
                url, 
                data=message.encode('utf-8'), 
                headers=headers, 
                timeout=20
            )

        logger.info(f"Push gesendet an {'Admin' if admin else 'Haupt'}-Kanal: {title}")
        return response.status_code == 200

    except Exception as e:
        logger.error(f"Fehler beim Senden der Notification: {e}")
        return False


def send_turnaround_notification(channel, temp, timestamp, admin=False):
    """
    Sendet eine reine Info-Push bei Wendepunkt (Auskühlung beendet).
    WICHTIG: Enthält kein 'ALARM'/'TRIGGER'/'FERTIG', um Fehltrigger am Server zu vermeiden.
    """
    friendly_name = cfg.get_friendly_channel_name(channel)
    device_name = cfg.device_name_friendly

    title = f"ℹ️ Info: Temperaturanstieg [{friendly_name}]"
    message = (
        f"Kanal: {friendly_name} (`{channel}`)\n"
        f"Temperaturanstieg/Wendepunkt um {timestamp} erkannt (T = {temp:.1f} °C).\n"
        f"Überwachung auf Abbindebeginn läuft..."
    )
    return send_push_notification(
        title=title,
        message=message,
        tags="chart_with_upwards_trend,thermometer",
        priority="default",
        admin=admin
    )


def send_setting_alarm_notification(channel, trigger_type, t_ab_str, temp_ab, admin=False):
    """
    Sendet den harten Abbinde-Alarm.
    Triggert das Server-Backend zuverlässig über 'ALARM' / 'TRIGGER'.
    """
    friendly_name = cfg.get_friendly_channel_name(channel)
    device_name = cfg.device_name_friendly

    title = f"🔥 Beton-Alarm: Abbindebeginn [{friendly_name}]!"
    message = (
        f"• Gerät: {device_name}\n"
        f"• Kanal: {friendly_name} (`{channel}`): TRIGGERED\n"
        f"• Abbindebeginn um {t_ab_str} erreicht ({trigger_type}).\n"
        f"• Temperatur: {temp_ab:.1f} °C\n"
        f"• Nachlaufzeit: 30 Min bis Auto-Export gestartet."
    )
    return send_push_notification(
        title=title,
        message=message,
        tags="fire,rotating_light",
        priority="high",
        admin=admin
    )


def send_completion_notification(channel, t_ab_str, admin=False):
    """
    Sendet die Abschlussmeldung nach 30-Minuten-Nachlauf.
    Triggert das Server-Backend via 'FERTIG' zur Archivierung.
    """
    friendly_name = cfg.get_friendly_channel_name(channel)
    device_name = cfg.device_name_friendly

    title = f"✅ Beton-Abschlussbericht: FERTIG [{friendly_name}]"
    message = (
        f"• Gerät: {device_name}\n"
        f"• Kanal: {friendly_name} (`{channel}`): FINISHED / STOPPED\n"
        f"• Abbindepunkt: {t_ab_str}\n"
        f"• 30 min Nachlauf abgeschlossen. Messung gestoppt."
    )
    return send_push_notification(
        title=title,
        message=message,
        tags="bar_chart,white_check_mark",
        priority="default",
        admin=admin
    )


def send_startup_notification(ip_address, device_name):
    """Sendet eine Push-Benachrichtigung beim Systemstart auf den Admin-Kanal mit Interaktions-Buttons."""
    admin_channel = cfg.admin_ntfy_channel
    if not admin_channel:
        logger.warning("[Notifier] Kein Admin-ntfy-Kanal konfiguriert.")
        return

    pretty_name = cfg.device_name_friendly
    web_gui_url = f"http://{ip_address}:8081"
    
    ntp_ok = False
    timezone_str = "UTC"
    try:
        res_sync = subprocess.run(["timedatectl", "show", "--property=NTPSynchronized"], capture_output=True, text=True, timeout=3)
        if "NTPSynchronized=yes" in res_sync.stdout:
            ntp_ok = True
            
        res_tz = subprocess.run(["timedatectl", "show", "--property=Timezone"], capture_output=True, text=True, timeout=3)
        if "Timezone=" in res_tz.stdout:
            timezone_str = res_tz.stdout.strip().split("=", 1)[1]
    except Exception as e:
        logger.error(f"[Notifier] Fehler beim Auslesen der Zeitzone: {e}")

    sync_status_str = "🟢 OK (Synchron)" if ntp_ok else "⚠️ NEIN (Gepuffert)"
    current_time_str = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')

    message = (
        f"🚀 Mess-Service auf Baustellenkoffer gestartet!\n"
        f"• Gerät: {pretty_name} ({cfg.device_name_technical})\n"
        f"• IP: {ip_address}\n"
        f"• WebGUI: {web_gui_url}\n"
        f"• Zeitzone: {timezone_str}\n"
        f"• Zeitsync: {sync_status_str}\n"
        f"• Systemzeit: {current_time_str}"
    )

    payload = {
        "topic": admin_channel,
        "title": f"🚀 Start-up [{pretty_name}]",
        "message": message,
        "tags": ["rocket", "construction"],
        "actions": [
            {
                "action": "http",
                "label": "📊 Status",
                "url": f"https://ntfy.sh/{admin_channel}",
                "method": "POST",
                "body": "status"
            },
            {
                "action": "view",
                "label": "🌐 WebGUI",
                "url": web_gui_url
            }
        ]
    }

    try:
        requests.post("https://ntfy.sh", json=payload, timeout=10)
        logger.info(f"[Notifier] Startup-Notification erfolgreich an Admin-Kanal '{admin_channel}' gesendet.")
    except Exception as e:
        logger.error(f"[Notifier] Fehler beim Senden der Startup-Notification: {e}")