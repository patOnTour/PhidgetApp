#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: check_network.py
Beschreibung: Prueft Netzwerkverbindung. Schreibt Log-Warnung und verhindert Boot-Schleifen.
Version: 1.4.0 (Kein unkontrollierter Reboot im Offline-Betrieb)
"""

import os
import sys
import time
import subprocess
from datetime import datetime

current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)
from config_loader import ConfigLoader

cfg = ConfigLoader()
DEVICE_NAME = cfg.device_name_friendly
STATE_FILE = "/tmp/network_down_since.txt"


def check_internet():
    res = subprocess.run(["ping", "-c", "1", "-W", "3", "8.8.8.8"], capture_output=True)
    return res.returncode == 0


def main():
    now = time.time()
    
    if check_internet():
        if os.path.exists(STATE_FILE):
            try:
                os.remove(STATE_FILE)
            except Exception:
                pass
        return

    if not os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "w") as f:
                f.write(str(now))
        except Exception:
            pass
        return

    try:
        with open(STATE_FILE, "r") as f:
            down_since = float(f.read().strip())
    except Exception:
        down_since = now

    elapsed = now - down_since

    if elapsed >= 900:  # 15 Minuten
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Hinweis ({DEVICE_NAME}): Keine Internetverbindung seit {int(elapsed/60)} Minuten. Geraet misst lokal weiter.", flush=True)


if __name__ == "__main__":
    main()