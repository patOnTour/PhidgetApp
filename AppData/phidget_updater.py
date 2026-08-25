"""
@file: phidget_updater.py
@version: 1.2.0
@date: 2026-08-25
@description: Automatischer Git-Update-Daemon fuer Edge-Geraete. Behebt Git Exit-Code 128 durch automatische Registrierung des Safe-Directory und saubere Environment-Uebergabe.
@author: Patrick Staehli
"""

import os
import sys
import time
import glob
import logging
import subprocess
import requests
import yaml

BASE_DIR = "/usr/userapps/PhidgetProject"
CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.yaml")
MIGRATIONS_DIR = os.path.join(BASE_DIR, "migrations")
STATE_DIR = "/var/lib/phidget-updater"
LAST_COMMIT_FILE = os.path.join(STATE_DIR, "last_commit.txt")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [Updater] %(message)s")
logger = logging.getLogger("Updater")


def ensure_git_safe_directory():
    """Verhindert Git Exit-Code 128 (dubious ownership)."""
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", BASE_DIR],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"Fehler beim Laden von config.yaml: {e}")
    return {}


def run_cmd(cmd, cwd=BASE_DIR, check=False):
    env = os.environ.copy()
    env["HOME"] = "/root"
    res = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, env=env)
    if check and res.returncode != 0:
        raise RuntimeError(f"Befehl '{cmd}' fehlgeschlagen:\n{res.stderr.strip()}")
    return res.stdout.strip(), res.stderr.strip(), res.returncode


def send_ntfy_status(title, message, priority=3, tags=None):
    cfg = load_config()
    ntfy_url = cfg.get("ntfy", {}).get("server_url", "https://ntfy.concretum-setting.com")
    channel = cfg.get("ntfy", {}).get("admin_channel", "Admin")
    dev_name = cfg.get("device", {}).get("friendly_name", "PhidgetBox")

    url = f"{ntfy_url.rstrip('/')}/{channel}"
    headers = {
        "Priority": str(priority),
        "Title": f"{dev_name}: {title}".encode("utf-8")
    }
    if tags:
        headers["Tags"] = ",".join(tags)

    try:
        requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=5.0)
    except Exception as e:
        logger.warning(f"ntfy-Benachrichtigung fehlgeschlagen: {e}")


def is_device_busy_measuring():
    """Prueft via API-Server, ob aktuell eine Beton-Messung auf dem Geraet laeuft."""
    cfg = load_config()
    dev_id = cfg.get("device", {}).get("device_id")
    ingest_url = cfg.get("server", {}).get("ingest_url", "")
    token = cfg.get("server", {}).get("api_token", "")

    if not dev_id or not ingest_url or not token:
        logger.warning("Fehlende Konfigurationsdaten (device_id, ingest_url oder api_token). Pruefung uebersprungen.")
        return False

    base_api = ingest_url.split("/api/v1/")[0]
    status_url = f"{base_api}/api/v1/control/device-status/{dev_id}"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        res = requests.get(status_url, headers=headers, timeout=5.0)
        if res.status_code == 200:
            data = res.json()
            is_measuring = bool(data.get("is_measuring", False))
            active_cnt = data.get("active_channels", 0)
            if is_measuring:
                logger.info(f"Server meldet aktive Messung ({active_cnt} Kanal/Kanaele). Update wird pausiert.")
            return is_measuring
        else:
            logger.warning(f"Statuspruefung fehlgeschlagen (HTTP {res.status_code}): {res.text}")
    except Exception as ex:
        logger.warning(f"Server nicht erreichbar fuer Statuspruefung ({ex}).")

    return False


def get_current_head_commit():
    out, _, code = run_cmd("git rev-parse HEAD")
    return out if code == 0 else ""


def sync_systemd_services():
    """Verlinkt alle .service Dateien aus repo/systemd nach /etc/systemd/system/ und startet neu."""
    logger.info("Synchronisiere Systemd-Dienste...")
    service_files = glob.glob(os.path.join(BASE_DIR, "systemd", "*.service"))
    services_to_restart = []

    for s_path in service_files:
        s_name = os.path.basename(s_path)
        dest = os.path.join("/etc/systemd/system", s_name)

        if not os.path.islink(dest) or os.readlink(dest) != s_path:
            run_cmd(f"ln -sf {s_path} {dest}")
            logger.info(f"Systemd Service verlinkt: {s_name}")

        if s_name != "phidget-updater.service":
            services_to_restart.append(s_name)

    run_cmd("systemctl daemon-reload")

    for s_name in services_to_restart:
        run_cmd(f"systemctl enable {s_name}")
        run_cmd(f"systemctl restart {s_name}")
        logger.info(f"Dienst neu gestartet: {s_name}")


def run_pending_migrations():
    """Fuehrt alle python/sh-Dateien im migrations/ Ordner aus, falls vorhanden."""
    if not os.path.exists(MIGRATIONS_DIR):
        return

    mig_files = sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*")))
    for mig in mig_files:
        if not (mig.endswith(".py") or mig.endswith(".sh")):
            continue

        mig_name = os.path.basename(mig)
        marker = os.path.join(STATE_DIR, f".done_{mig_name}")
        if os.path.exists(marker):
            continue

        logger.info(f"Fuehre Migrationsskript aus: {mig_name}")
        try:
            if mig.endswith(".py"):
                run_cmd(f"/usr/bin/python3 {mig}", check=True)
            elif mig.endswith(".sh"):
                run_cmd(f"/bin/bash {mig}", check=True)

            with open(marker, "w") as f:
                f.write(str(time.time()))
            logger.info(f"Migration {mig_name} erfolgreich.")
        except Exception as ex:
            logger.error(f"Fehler bei Migration {mig_name}: {ex}")
            send_ntfy_status("Update-Fehler (Migration)", f"Fehler in {mig_name}: {ex}", priority=4, tags=["warning"])


def check_and_apply_update():
    os.makedirs(STATE_DIR, exist_ok=True)
    ensure_git_safe_directory()

    if is_device_busy_measuring():
        return

    old_commit = get_current_head_commit()

    _, _, code = run_cmd("git fetch origin main")
    if code != 0:
        logger.debug("Fetch fehlgeschlagen (offline oder kein Zugriff).")
        return

    remote_commit, _, _ = run_cmd("git rev-parse origin/main")
    if not remote_commit or remote_commit == old_commit:
        return

    logger.info(f"Neues Release gefunden! Alt: {old_commit[:7]} -> Neu: {remote_commit[:7]}")

    _, err, code = run_cmd("git reset --hard origin/main")
    if code != 0:
        logger.error(f"Git reset fehlgeschlagen: {err}")
        send_ntfy_status("Update fehlgeschlagen", f"Git reset Error:\n{err}", priority=4, tags=["x"])
        return

    req_file = os.path.join(BASE_DIR, "requirements.txt")
    if os.path.exists(req_file):
        logger.info("Installiere neue Python-Abhaengigkeiten...")
        run_cmd(f"/usr/bin/python3 -m pip install -r {req_file} --break-system-packages")

    run_pending_migrations()
    sync_systemd_services()

    with open(LAST_COMMIT_FILE, "w") as f:
        f.write(remote_commit)

    log_msg = f"System erfolgreich aktualisiert auf Commit {remote_commit[:7]}."
    logger.info(log_msg)
    send_ntfy_status("Update Erfolgreich", log_msg, priority=3, tags=["arrows_counterclockwise", "white_check_mark"])


def updater_loop():
    logger.info("Phidget-Updater Daemon gestartet.")
    ensure_git_safe_directory()
    while True:
        try:
            cfg = load_config()
            interval = cfg.get("updater", {}).get("check_interval_sec", 900)
            check_and_apply_update()
        except Exception as e:
            logger.error(f"Unerwarteter Fehler im Updater-Loop: {e}")
            interval = 900
        time.sleep(interval)


if __name__ == "__main__":
    updater_loop()