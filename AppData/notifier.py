#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import base64
import requests
import logging
import subprocess
from datetime import datetime
from config_loader import ConfigLoader

logger = logging.getLogger("Notifier")
cfg = ConfigLoader()


def get_ntfy_base_url():
    secrets = cfg.secrets
    return secrets.get("ntfy", {}).get("server_url", "https://ntfy.concretum-setting.com").rstrip("/")


def get_ntfy_url(admin=False):
    secrets = cfg.secrets
    base_url = get_ntfy_base_url()
    channel = secrets.get("admin_notify", {}).get("channel_name") if admin else secrets.get("notify", {}).get("channel_name")
    return f"{base_url}/{channel.strip()}" if channel else ""


def encode_rfc2047(text):
    if not text:
        return ""
    if all(ord(c) < 128 for c in text):
        return text
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"=?utf-8?B?{encoded}?="


def send_push_notification(title, message, tags="", priority="default", attachment_file=None, attachment_name=None, admin=False):
    url = get_ntfy_url(admin=admin)
    if not url:
        return False
    try:
        headers = {
            "Title": encode_rfc2047(title),
            "Tags": str(tags),
            "Priority": str(priority)
        }
        if attachment_file and os.path.exists(attachment_file):
            headers["X-Filename"] = attachment_name or os.path.basename(attachment_file)
            headers["Message"] = encode_rfc2047(message)
            with open(attachment_file, "rb") as f:
                res = requests.post(url, data=f.read(), headers=headers, timeout=20)
        else:
            headers["Content-Type"] = "text/plain; charset=utf-8"
            res = requests.post(url, data=message.encode('utf-8'), headers=headers, timeout=20)
        return res.status_code == 200
    except Exception as e:
        logger.error(f"Push-Fehler: {e}")
        return False


def send_turnaround_notification(channel, temp, timestamp, admin=False):
    friendly_name = cfg.get_friendly_channel_name(channel)
    return send_push_notification(
        title=f"Info: Temperaturanstieg [{friendly_name}]",
        message=f"Kanal: {friendly_name} (`{channel}`)\nWendepunkt um {timestamp} (T = {temp:.1f} °C).",
        tags="chart_with_upwards_trend,thermometer",
        admin=admin
    )


def send_setting_alarm_notification(channel, trigger_type, t_ab_str, temp_ab, admin=False):
    friendly_name = cfg.get_friendly_channel_name(channel)
    return send_push_notification(
        title=f"Beton-Alarm: Abbindebeginn [{friendly_name}]!",
        message=f"• Geraet: {cfg.device_name_friendly}\n• Kanal: {friendly_name} (`{channel}`): TRIGGERED\n• Abbindebeginn: {t_ab_str} ({trigger_type})\n• Temp: {temp_ab:.1f} °C\n• 30 Min Nachlauf gestartet.",
        tags="fire,rotating_light",
        priority="high",
        admin=admin
    )


def send_completion_notification(channel, t_ab_str, admin=False):
    friendly_name = cfg.get_friendly_channel_name(channel)
    return send_push_notification(
        title=f"Beton-Abschlussbericht: FERTIG [{friendly_name}]",
        message=f"• Geraet: {cfg.device_name_friendly}\n• Kanal: {friendly_name} (`{channel}`): STOPPED\n• Abbindepunkt: {t_ab_str}\n• Messung abgeschlossen.",
        tags="bar_chart,white_check_mark",
        admin=admin
    )


def send_startup_notification(ip_address, device_name):
    admin_channel = cfg.admin_ntfy_channel or "Admin"
    base_url = get_ntfy_base_url()
    url = f"{base_url}/{admin_channel}"
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
    except Exception:
        pass

    sync_status_str = "OK (Synchron)" if ntp_ok else "NEIN (Gepuffert)"
    current_time_str = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')

    message = (
        f"• Geraet: {pretty_name} ({device_name})\n"
        f"• IP: {ip_address}\n"
        f"• WebGUI: {web_gui_url}\n"
        f"• Zeitzone: {timezone_str}\n"
        f"• Zeitsync: {sync_status_str}\n"
        f"• Systemzeit: {current_time_str}"
    )

    actions = [
        f"http, Status, {base_url}/{admin_channel}, body=status",
        f"view, WebGUI, {web_gui_url}"
    ]

    headers = {
        "Title": encode_rfc2047(f"Start-up BEREIT [{pretty_name}]"),
        "Tags": "rocket,white_check_mark",
        "Priority": "default",
        "Actions": "; ".join(actions),
        "Content-Type": "text/plain; charset=utf-8"
    }

    try:
        res = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
        return res.status_code == 200
    except Exception as e:
        logger.error(f"Fehler beim Startup-Push: {e}")
        return False
