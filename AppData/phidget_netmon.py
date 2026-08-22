#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import yaml
import socket
import logging
import subprocess
import requests

BASE_DIR = "/usr/userapps/PhidgetProject"
CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.yaml")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [NetMon] %(message)s")
logger = logging.getLogger("NetMon")

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def get_ip_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "Keine IP (Offline)"

def check_ping(target):
    try:
        res = subprocess.run(["ping", "-c", "1", "-W", "3", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    except Exception:
        return False

def send_ntfy_boot():
    cfg = load_config()
    ntfy_url = cfg.get("ntfy", {}).get("server_url", "https://ntfy.sh")
    channel = cfg.get("ntfy", {}).get("admin_channel", "Admin")
    device_name = cfg.get("device", {}).get("friendly_name", "PhidgetBox")
    
    ip = get_ip_address()
    full_url = f"{ntfy_url.rstrip('/')}/{channel}"

    try:
        requests.post(
            full_url,
            data=f"🚀 {device_name} erfolgreich gestartet!\nIP: {ip}\nStatus: Online".encode("utf-8"),
            headers={
                "Title": f"Boot Up: {device_name}",
                "Tags": "rocket,computer"
            },
            timeout=5.0
        )
        logger.info(f"ntfy-Bootbenachrichtigung gesendet an {channel}")
    except Exception as e:
        logger.warning(f"ntfy-Bootbenachrichtigung fehlgeschlagen: {e}")

def reset_wifi_interface():
    logger.warning("WLAN-Verbindung unterbrochen! Starte NetworkManager/wpa_supplicant neu...")
    try:
        subprocess.run(["systemctl", "restart", "wpa_supplicant"], check=False)
        subprocess.run(["ifconfig", "wlan0", "down"], check=False)
        time.sleep(2)
        subprocess.run(["ifconfig", "wlan0", "up"], check=False)
    except Exception as e:
        logger.error(f"Fehler beim WLAN-Reset: {e}")

def netmon_loop():
    cfg = load_config()
    ping_target = cfg.get("network", {}).get("ping_target", "1.1.1.1")
    check_interval = cfg.get("network", {}).get("check_interval_sec", 15)
    fallback_min = cfg.get("network", {}).get("hotspot_fallback_min", 5)

    send_ntfy_boot()

    offline_seconds = 0
    hotspot_active = False

    logger.info(f"NetMon Überwachung gestartet (Ping-Ziel: {ping_target})")

    while True:
        is_online = check_ping(ping_target)

        if is_online:
            if offline_seconds > 0:
                logger.info("Internetverbindung wiederhergestellt!")
            offline_seconds = 0
            if hotspot_active:
                logger.info("Deaktiviere Notfall-Hotspot...")
                subprocess.run(["systemctl", "stop", "hostapd"], check=False)
                hotspot_active = False
        else:
            offline_seconds += check_interval
            logger.warning(f"Ping fehlgeschlagen. Offline seit {offline_seconds}s")

            # Stufe 1: Sanfter WLAN-Reset nach 45s
            if offline_seconds == 45:
                reset_wifi_interface()

            # Stufe 2: Notfall-Hotspot aktivieren nach N Minuten
            if offline_seconds >= (fallback_min * 60) and not hotspot_active:
                logger.error(f"Seit {fallback_min} Min offline. Starte Notfall-Hotspot (hostapd)...")
                subprocess.run(["systemctl", "start", "hostapd"], check=False)
                hotspot_active = True

        time.sleep(check_interval)

if __name__ == "__main__":
    netmon_loop()
