#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sqlite3
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import io
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timedelta
from config_loader import ConfigLoader
import notifier

log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(log_dir, exist_ok=True)

analyzer_logger = logging.getLogger("AnalyzerAudit")
analyzer_logger.setLevel(logging.INFO)

if not analyzer_logger.handlers:
    log_path = os.path.join(log_dir, "analyzer_audit.log")
    handler = TimedRotatingFileHandler(log_path, when="midnight", interval=1, backupCount=30, encoding="utf-8")
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    analyzer_logger.addHandler(handler)

class ConcreteSettingAnalyzer:
    VERSION = "5.3.0"

    def __init__(self, db_path=None, ema_span=30, reg_window=15):
        self.cfg = ConfigLoader()
        self.db_path = db_path if db_path is not None else os.path.join(os.path.dirname(os.path.abspath(__file__)), "telemetry_buffer.db")
        self.ema_span = ema_span     # 30 Messpunkte = 10 Min bei 20s Takt
        self.reg_window = reg_window # 15 Messpunkte = 5 Min bei 20s Takt
        self.version = self.VERSION
        self.turnaround_armed = {}      # Merker: 10 Min Absinken erkannt
        self.turnaround_triggered = {}  # Merker: Wendepunkt bereits gefeuert
        self._init_analysis_db()

    def _init_analysis_db(self):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute('PRAGMA journal_mode=WAL;')
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS analysis_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_eval REAL,
                    channel_name TEXT,
                    t_ab_time TEXT,
                    t_ab_temp REAL,
                    max_curvature_rate REAL,
                    status TEXT
                )
            ''')
            conn.commit()
            conn.close()
        except Exception as e:
            analyzer_logger.error(f"Fehler beim Initialisieren der Analyse-DB: {e}")

    def get_channel_status(self, channel):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM channel_control WHERE LOWER(channel) = LOWER(?)", (channel,))
            row = cursor.fetchone()
            conn.close()
            if row and row[0]:
                return row[0].upper()
        except Exception as e:
            analyzer_logger.error(f"Fehler beim Auslesen des Kanalstatus fuer {channel}: {e}")
        return "RUNNING"
    
    def check_and_clear_export_flag(self, channel):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT force_export FROM channel_control WHERE LOWER(channel) = LOWER(?)", (channel,))
            row = cursor.fetchone()
            if row and row[0] == 1:
                cursor.execute("UPDATE channel_control SET force_export = 0 WHERE LOWER(channel) = LOWER(?)", (channel,))
                conn.commit()
                conn.close()
                return True
            conn.close()
        except Exception as e:
            analyzer_logger.error(f"Fehler bei Export-Flag-Pruefung fuer {channel}: {e}")
        return False

    def get_manual_export_window(self, times_sec, temps, ambs, parsed_times, minutes=120):
        """Schneidet die Daten auf das gewuenschte Zeitfenster (Standard: 120 Minuten) zu."""
        if times_sec is None or len(times_sec) == 0:
            return times_sec, temps, ambs, parsed_times

        cutoff_sec = times_sec[-1] - (minutes * 60)
        idx_start = np.searchsorted(times_sec, cutoff_sec)

        return (
            times_sec[idx_start:],
            temps[idx_start:],
            ambs[idx_start:] if ambs is not None else None,
            parsed_times[idx_start:]
        )

    def is_turnaround_sent(self, channel_name):
        """Prueft in SQLite, ob der Wendepunkt fuer diesen Kanal bereits gemeldet wurde."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT info_turning_point_sent FROM setting_state WHERE LOWER(channel) = LOWER(?)",
                (str(channel_name).strip(),)
            )
            row = cursor.fetchone()
            conn.close()
            return bool(row and row[0] == 1)
        except Exception as e:
            analyzer_logger.error(f"Fehler beim Pruefen von info_turning_point_sent fuer {channel_name}: {e}")
            return False

    def mark_turnaround_sent(self, channel_name):
        """Markiert in SQLite den Wendepunkt als gesendet."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE setting_state SET info_turning_point_sent = 1 WHERE LOWER(channel) = LOWER(?)",
                (str(channel_name).strip(),)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            analyzer_logger.error(f"Fehler beim Setzen von info_turning_point_sent fuer {channel_name}: {e}")

    def check_turnaround(self, channel_name, times, raw_temps, friendly_name, latest_time):
        """
        Wendepunkt-Erkennung:
        1. Scharfschalten: Wenn ueber 10 Minuten (30 Punkte) die Tendenz negativ ist.
        2. Ausloesen: Sobald im Anschluss ueber ca. 3 Minuten (9 Punkte) die Steigung positiv ist.
        Prueft die Persistenz direkt ueber die Datenbank.
        """
        ch_key = str(channel_name).lower()
        if len(raw_temps) < 40 or self.is_turnaround_sent(channel_name):
            return

        smooth = pd.Series(raw_temps).rolling(window=9, min_periods=3).mean()

        falling_window = smooth.iloc[-39:-9]
        rising_window = smooth.iloc[-9:]

        if len(falling_window) >= 20:
            poly_fall = np.polyfit(np.arange(len(falling_window)), falling_window.values, 1)
            if poly_fall[0] < -0.001 and (falling_window.iloc[0] - falling_window.iloc[-1]) >= 0.10:
                self.turnaround_armed[ch_key] = True

        if self.turnaround_armed.get(ch_key, False) and len(rising_window) >= 6:
            poly_rise = np.polyfit(np.arange(len(rising_window)), rising_window.values, 1)
            reheating_delta = rising_window.iloc[-1] - rising_window.iloc[0]

            if poly_rise[0] > 0.002 and reheating_delta >= 0.05:
                min_temp = smooth.iloc[-39:].min()
                self.mark_turnaround_sent(channel_name)
                analyzer_logger.info(
                    f"[TURNAROUND EVENT] {friendly_name} ({channel_name}): "
                    f"Wendepunkt nach 10 Min Absinken erreicht! Tiefstwert: {min_temp:.2f} °C, "
                    f"3-Min-Anstieg: +{reheating_delta:.2f} °C ({latest_time})"
                )
                try:
                    notifier.send_turnaround_notification(
                        channel=channel_name,
                        temp=min_temp,
                        timestamp=latest_time
                    )
                except Exception as ex:
                    analyzer_logger.error(f"Fehler beim Senden der Turnaround-Push: {ex}")

    def evaluate_triggers(self, channel_name, df_history, start_time_sec=None):
        friendly_name = self.cfg.get_friendly_channel_name(channel_name)
        ch_key = str(channel_name).lower()
        
        if df_history is None or len(df_history) < 6:
            analyzer_logger.info(f"[{friendly_name}/{channel_name}] Nicht genuegend Messpunkte (< 6) fuer Analyse.")
            return None, None, None, 0.0, 0.0

        matched_col = next((c for c in df_history.columns if str(c).lower() == ch_key), None)
        if not matched_col:
            analyzer_logger.warning(f"[{friendly_name}/{channel_name}] Spalte nicht im DataFrame gefunden.")
            return None, None, None, 0.0, 0.0

        times = df_history['Timestamp']
        raw_temps = pd.Series(df_history[matched_col].values)

        if len(raw_temps) < 6:
            return None, None, None, 0.0, 0.0

        temps = raw_temps.ewm(span=self.ema_span, adjust=False).mean()

        latest_temp = raw_temps.iloc[-1]
        raw_t = times.iloc[-1] if hasattr(times, 'iloc') else times[-1]

        if isinstance(raw_t, (int, float)):
            latest_time = datetime.fromtimestamp(raw_t).strftime('%Y-%m-%d %H:%M:%S')
            current_sec = float(raw_t)
        elif isinstance(raw_t, str) and len(raw_t) >= 19:
            try:
                dt_utc = datetime.strptime(raw_t[:19], '%Y-%m-%d %H:%M:%S')
                latest_time = dt_utc.strftime('%Y-%m-%d %H:%M:%S')
                current_sec = dt_utc.timestamp()
            except Exception:
                latest_time = raw_t
                current_sec = 0.0
        else:
            latest_time = str(raw_t)
            current_sec = 0.0

        latest_slope = 0.0
        latest_rotation = 0.0

        # --- 1. Wendepunkt-Erkennung (10min Sink / 3min Steig) ---
        self.check_turnaround(channel_name, times, raw_temps, friendly_name, latest_time)

        # --- 2. Rotations-Trigger (Polyfit Schwellenwert 0.000002 unverändert) ---
        try:
            window_size = 30
            sub_times = times.iloc[-window_size:] if len(times) >= window_size else times
            sub_temps = temps.iloc[-window_size:] if len(temps) >= window_size else temps

            t_sec = (pd.to_datetime(sub_times) - pd.to_datetime(sub_times.iloc[0])).dt.total_seconds().values

            if len(t_sec) >= 6:
                poly = np.polyfit(t_sec, sub_temps.values, 2)
                latest_rotation = (2.0 * poly[0])
                latest_slope = 2.0 * poly[0] * t_sec[-1] + poly[1]

                if latest_rotation >= 0.000002:
                    analyzer_logger.info(f"[TRIGGER ALARM] {friendly_name} ({channel_name}): Rotations-Trigger ausgeloest! Rotation={latest_rotation:.9f} >= 0.000002 bei {latest_temp:.2f} Grad C ({latest_time})")
                    return "rotation_trigger", latest_time, latest_temp, latest_rotation, latest_slope
        except Exception as e:
            analyzer_logger.error(f"Fehler bei Rotationsberechnung fuer {channel_name}: {e}")

        # --- 3. Fallback-Kaskade (5x Delta >= 0.05 °C) ---
        last_6_raw = raw_temps.iloc[-6:].values
        deltas = np.diff(last_6_raw)
        
        if all(d >= 0.05 for d in deltas):
            analyzer_logger.info(f"[TRIGGER ALARM] {friendly_name} ({channel_name}): Fallback-Kaskade ausgeloest! 5x Delta >= 0.05 Grad C ({np.round(deltas, 2)}) bei {latest_temp:.2f} Grad C ({latest_time})")
            return "fallback_trigger", latest_time, latest_temp, latest_rotation, latest_slope

        # --- 4. 6h-Fallback: Analyse mittels Tangentenmethode bei Normalbeton ---
        if start_time_sec and (current_sec - start_time_sec) >= (6 * 3600):
            try:
                t_ab, temp_ab, _, _, _, _ = self.calculate_tangent_intersection(times.values, temps.values)
                if t_ab is not None:
                    analyzer_logger.info(f"[6H-FALLBACK TRIGGER] {friendly_name} ({channel_name}): Tangentenschnittpunkt nach 6h gefunden: {t_ab} bei {temp_ab:.2f} Grad C")
                    return "6h_tangent_fallback", t_ab, temp_ab, latest_rotation, latest_slope
            except Exception as e_6h:
                analyzer_logger.error(f"Fehler beim 6h-Fallback fuer {channel_name}: {e_6h}")

        analyzer_logger.info(f"[Audit] {friendly_name} ({channel_name}) | Punkte: {len(raw_temps)} | Temp: {latest_temp:.2f} Grad C | Rotation: {latest_rotation:.9f} | Deltas: {np.round(deltas, 2)}")

        return None, None, None, latest_rotation, latest_slope

    def calculate_tangent_intersection(self, times, temps):
        try:
            if len(temps) < 30:
                return None, None, None, None, None, None

            dt_objects = []
            sec_list = []
            for t in times:
                if isinstance(t, (int, float)):
                    dt_objects.append(datetime.fromtimestamp(t))
                    sec_list.append(float(t))
                else:
                    dt = datetime.strptime(str(t)[:19], '%Y-%m-%d %H:%M:%S')
                    dt_objects.append(dt)
                    sec_list.append(dt.timestamp())

            df = pd.DataFrame({'time': dt_objects, 'time_sec': sec_list, 'temp': temps})
            df = df.sort_values('time').reset_index(drop=True)
            
            df['temp_smooth'] = df['temp'].rolling(window=15, center=True, min_periods=1).mean()
            
            dt_seconds = df['time'].diff().dt.total_seconds().fillna(20.0)
            df['slope'] = df['temp_smooth'].diff() / dt_seconds
            
            skip_n = min(30, max(20, len(df) // 5))
            valid_slice = df.iloc[skip_n:]
            if valid_slice.empty:
                return None, None, None, None, None, None
            
            max_slope_idx = valid_slice['slope'].idxmax()
            t_max_time = df.loc[max_slope_idx, 'time']

            t1_start = t_max_time - timedelta(minutes=5)
            t1_end = t_max_time + timedelta(minutes=5)
            df_t1 = df[(df['time'] >= t1_start) & (df['time'] <= t1_end)]
            
            if len(df_t1) < 2:
                return None, None, None, None, None, None
                
            x1 = df_t1['time_sec']
            y1 = df_t1['temp_smooth']
            m1, b1 = np.polyfit(x1, y1, 1)

            pre_slice = df.iloc[max(5, skip_n // 2):max_slope_idx]
            if pre_slice.empty:
                pre_slice = df.iloc[:max_slope_idx]

            min_idx = pre_slice['temp_smooth'].idxmin() if not pre_slice.empty else 0
            t_min_time = df.loc[min_idx, 'time']
            
            t2_start = t_min_time - timedelta(minutes=10)
            t2_end = t_min_time + timedelta(minutes=10)
            df_t2 = df[(df['time'] >= t2_start) & (df['time'] <= t2_end)]
            
            if len(df_t2) < 2:
                df_t2 = df.iloc[max(0, min_idx - 5):min_idx + 5]
                
            x2 = df_t2['time_sec']
            y2 = df_t2['temp_smooth']
            m2, b2 = np.polyfit(x2, y2, 1)

            if abs(m1 - m2) < 1e-9:
                return None, None, None, None, None, None

            xs = (b2 - b1) / (m1 - m2)
            
            t_min_sec = df.loc[min_idx, 'time_sec']
            t_max_sec = df.loc[max_slope_idx, 'time_sec']

            if xs < (t_min_sec - 1800) or xs > t_max_sec:
                return None, None, None, None, None, None

            t_ab_dt = datetime.fromtimestamp(xs)
            temp_ab = m1 * xs + b1
            return t_ab_dt.strftime("%Y-%m-%d %H:%M:%S"), float(temp_ab), m1, b1, m2, b2

        except Exception as e:
            analyzer_logger.error(f"Fehler beim Tangentenschnittpunkt: {e}")
            return None, None, None, None, None, None

    def generate_setting_plot(self, times, temps, ambs, channel_name="Temp0"):
        friendly_channel = self.cfg.get_friendly_channel_name(channel_name)
        fig, ax = plt.subplots(figsize=(10, 5))
        
        parsed_dt = []
        sec_list = []
        for t in times:
            if isinstance(t, (int, float)):
                dt = datetime.fromtimestamp(t)
                parsed_dt.append(dt)
                sec_list.append(float(t))
            else:
                dt = datetime.strptime(str(t)[:19], '%Y-%m-%d %H:%M:%S')
                parsed_dt.append(dt)
                sec_list.append(dt.timestamp())
        
        ax.plot(parsed_dt, temps, label=f'Beton {friendly_channel}', color='#ff6600', linewidth=2)
        if ambs is not None and len(ambs) == len(parsed_dt):
            ax.plot(parsed_dt, ambs, label='Umgebung', color='#0056b3', linestyle='--', alpha=0.7)
        
        t_ab, temp_ab, m1, b1, m2, b2 = self.calculate_tangent_intersection(times, temps)
        
        if t_ab is not None and temp_ab is not None:
            t_ab_dt = datetime.strptime(str(t_ab)[:19], "%Y-%m-%d %H:%M:%S")
            
            ax.axvline(x=t_ab_dt, color='red', linestyle=':', label=f'Abbindebeginn: {t_ab_dt.strftime("%H:%M:%S")}')
            ax.scatter([t_ab_dt], [temp_ab], color='red', s=100, zorder=5)

            x_sec = np.array(sec_list)
            y_tangent1 = m1 * x_sec + b1
            y_tangent2 = m2 * x_sec + b2

            min_y, max_y = min(temps) - 1.0, max(temps) + 1.0
            ax.plot(parsed_dt, y_tangent1, color='#28a745', linestyle='--', alpha=0.7, label='Steigungstangente')
            ax.plot(parsed_dt, y_tangent2, color='#6c757d', linestyle='--', alpha=0.7, label='Ruhetangente')
            ax.set_ylim(min_y, max_y)

        ax.set_title(f'Abbindeverhalten - Kanal {friendly_channel} ({channel_name})', fontsize=12, fontweight='bold')
        ax.set_xlabel('Zeit', fontsize=10)
        ax.set_ylabel('Temperatur (Grad C)', fontsize=10)
        ax.grid(True, linestyle='--', alpha=0.6)
        ax.legend(loc='upper left')
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=150)
        buf.seek(0)
        plt.close(fig)
        return buf, t_ab, temp_ab

    def load_data_from_db(self, channel=None):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("SELECT started_at FROM channel_control WHERE LOWER(channel) = LOWER(?)", (channel,))
            res_start = cursor.fetchone()
            started_at_str = res_start[0] if res_start and res_start[0] else None

            cursor.execute("PRAGMA table_info(telemetry)")
            columns = [row[1] for row in cursor.fetchall()]
            
            matched_col = next((c for c in columns if c.lower() == str(channel).lower()), None) if channel else None
            
            if not matched_col:
                available_temp_cols = [c for c in columns if 'temp' in c.lower() and c.lower() != 'ambient']
                if available_temp_cols:
                    matched_col = available_temp_cols[0]
                else:
                    conn.close()
                    return None, None, None, None
            
            query = f'SELECT timestamp, "{matched_col}", ambient FROM telemetry ORDER BY timestamp ASC'
            df = pd.read_sql(query, conn)
            conn.close()
            
            if df.empty:
                return None, None, None, None
            
            if started_at_str:
                try:
                    if isinstance(started_at_str, str):
                        clean_str = started_at_str.replace('T', ' ')[:19]
                        dt = datetime.strptime(clean_str, '%Y-%m-%d %H:%M:%S')
                        dt_buffered = dt + timedelta(minutes=3)
                        start_epoch = dt_buffered.timestamp()
                    else:
                        start_epoch = float(started_at_str) + 180
                    df = df[df['timestamp'] >= start_epoch]
                except Exception as ex_dt:
                    analyzer_logger.error(f"Fehler beim Parsen von started_at ({started_at_str}): {ex_dt}")

            if df.empty:
                return None, None, None, None

            parsed_times = [datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S') for t in df['timestamp']]
            times_sec = df['timestamp'].values.astype(float)
            temps = df[matched_col].values.astype(float)
            ambs = df['ambient'].values.astype(float) if 'ambient' in df.columns else np.full(len(df), 20.0)
            
            return times_sec, temps, ambs, parsed_times
        except Exception as e:
            analyzer_logger.error(f"Fehler beim Laden aus der DB fuer Kanal {channel}: {e}")
            return None, None, None, None

    def generate_csv_data(self, times, temps, ambs, channel_name="Temp0"):
        friendly_channel = self.cfg.get_friendly_channel_name(channel_name)
        
        formatted_times = []
        for t in times:
            if isinstance(t, (int, float)):
                formatted_times.append(datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S'))
            else:
                formatted_times.append(str(t)[:19])
            
        df = pd.DataFrame({
            'Zeitstempel': formatted_times,
            f'Temperatur_{friendly_channel}_GradC': temps,
            'Umgebung_GradC': ambs if ambs is not None else 20.0
        })
        return df.to_csv(index=False, sep=';')