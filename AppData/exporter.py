#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modul: exporter.py
Beschreibung: Eigenstaendiger CLI-Exporter fuer Beton-Temperaturkanaele.
Generiert Plots, CSV-Exports und versendet Push-Meldungen via ntfy.
Version: 1.2.0 (Server-konforme FERTIG-Notification & STOPPED-Status)
"""

import sys
import os
import sqlite3
import argparse
import logging
from datetime import datetime

current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

from config_loader import ConfigLoader
from advanced_analyzer import ConcreteSettingAnalyzer
import notifier

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [Exporter] %(message)s'
)
logger = logging.getLogger("Exporter")

def execute_export(channel_name, db_path=None):
    cfg = ConfigLoader()
    analyzer = ConcreteSettingAnalyzer(db_path=db_path)
    friendly_name = cfg.get_friendly_channel_name(channel_name)

    logger.info(f"Starte Export-Erstellung fuer Kanal: {channel_name} ({friendly_name})...")

    # 1. Daten aus SQLite laden
    times_sec, temps, ambs, parsed_times = analyzer.load_data_from_db(channel_name)

    if temps is None or len(temps) == 0:
        logger.warning(f"Keine Messdaten fuer Kanal {channel_name} ab started_at vorhanden.")
        notifier.send_push_notification(
            title=f"Export fehlgeschlagen: {friendly_name}",
            message=f"Keine Messdaten ab Start fuer Kanal {channel_name} vorhanden.",
            tags="warning"
        )
        return False

    latest_temp = temps[-1]

    # 2. Plot & Tangentenschnittpunkt berechnen
    plot_buf, t_ab, temp_ab = analyzer.generate_setting_plot(
        times=parsed_times,
        temps=temps,
        ambs=ambs,
        channel_name=channel_name
    )

    ab_info = f"Abbindebeginn: {t_ab} ({temp_ab:.1f} Grad C)" if t_ab else "Abbindebeginn: Nicht ermittelt"

    # 3. CSV-Daten generieren
    csv_string = analyzer.generate_csv_data(
        times=parsed_times,
        temps=temps,
        ambs=ambs,
        channel_name=channel_name
    )

    # 4. Dateien temporaer auf Disk schreiben
    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    plot_filename = f"Graph_{friendly_name}_{date_str}.png"
    csv_filename = f"Daten_{friendly_name}_{date_str}.csv"
    
    plot_path = os.path.join(current_dir, plot_filename)
    csv_path = os.path.join(current_dir, csv_filename)

    try:
        with open(plot_path, "wb") as f:
            f.write(plot_buf.getvalue())
        with open(csv_path, "w", encoding="utf-8-sig") as f:
            f.write(csv_string)
    except Exception as e:
        logger.error(f"Fehler beim Speichern der Export-Dateien: {e}")
        return False

    message_text = f"Kanal: {friendly_name} (`{channel_name}`) | Punkte: {len(temps)} | Temp: {latest_temp:.1f} Grad C | {ab_info}"

    # 5. Push 1: Grafik-Anhang versenden
    success_img = notifier.send_push_notification(
        title=f"Daten-Export (Graph): {friendly_name}",
        message=message_text,
        tags="chart_with_upwards_trend",
        attachment_file=plot_path,
        attachment_name=plot_filename
    )

    # 6. Push 2: CSV-Anhang versenden
    success_csv = notifier.send_push_notification(
        title=f"Daten-Export (CSV): {friendly_name}",
        message=f"CSV-Messdaten fuer {friendly_name} (`{channel_name}`)",
        tags="file_folder",
        attachment_file=csv_path,
        attachment_name=csv_filename
    )

    # 7. Push 3: FERTIG-Abschlussbericht fuer Server-Archivierung
    t_ab_str = str(t_ab) if t_ab else "Manuell exportiert"
    notifier.send_completion_notification(
        channel=channel_name,
        t_ab_str=t_ab_str
    )

    # 8. Temporaere Dateien aufraeumen
    for p in [plot_path, csv_path]:
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    # 9. Kanal nach Export auf STOPPED & FINISHED setzen
    try:
        conn = sqlite3.connect(analyzer.db_path)
        cursor = conn.cursor()
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("""
            UPDATE channel_control 
            SET status = 'STOPPED', started_at = NULL, force_export = 0, updated_at = ? 
            WHERE LOWER(channel) = LOWER(?)
        """, (now_str, channel_name))

        cursor.execute("""
            UPDATE setting_state 
            SET export_status = 'FINISHED', trigger_fired = 2, exported_at = ? 
            WHERE LOWER(channel) = LOWER(?)
        """, (now_str, channel_name))

        conn.commit()
        conn.close()
        logger.info(f"Kanal {channel_name} nach Export erfolgreich auf STOPPED/FINISHED gesetzt.")
    except Exception as e:
        logger.error(f"Fehler beim Zuruecksetzen des Kanalstatus: {e}")

    logger.info(f"Export fuer {friendly_name} abgeschlossen (Graph: {success_img}, CSV: {success_csv}).")
    return success_img and success_csv


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Einzelexport fuer Beton-Temperaturkanaele")
    parser.add_argument("--channel", required=True, help="Technischer Kanalname (z.B. Temp0, Temp1)")
    args = parser.parse_args()

    execute_export(args.channel)