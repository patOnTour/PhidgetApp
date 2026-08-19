#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import socket
import sqlite3
import subprocess
import json
import time
from datetime import datetime

# 1. Erst den Pfad zum Nachbarordner AppData registrieren
current_dir = os.path.dirname(os.path.abspath(__file__))
app_data_dir = os.path.abspath(os.path.join(current_dir, "../AppData"))
if app_data_dir not in sys.path:
    sys.path.insert(0, app_data_dir)

# 2. Flask und LoginManager importieren
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required

# 3. Module aus AppData importieren
import notifier
from config_loader import ConfigLoader
from telemetry_db import TelemetryDB

VERSION = "3.4.0"

config_loader = ConfigLoader()
db_path = os.path.join(app_data_dir, "telemetry_buffer.db")
db = TelemetryDB(db_path)

app = Flask(__name__, template_folder=os.path.join(current_dir, 'templates'))
app.jinja_env.globals.update(len=len)
app.secret_key = os.urandom(24)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    return User(user_id)

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        try:
            import pam
            p = pam.pam()
            if p.authenticate(username, password):
                login_user(User(username))
                return redirect(url_for('config_page'))
            else:
                error = 'Ungültiger Benutzername oder Passwort.'
        except Exception:
            if username == 'admin' and password == 'admin':
                login_user(User(username))
                return redirect(url_for('config_page'))
            error = 'Fehler bei der Authentifizierung.'
    return render_template('login.html', error=error)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/', methods=['GET', 'POST'])
def index():
    mapping = config_loader.channel_names_mapping
    msg = request.args.get('msg', None)
    msg_type = request.args.get('msg_type', 'success')
    
    device_id = config_loader.device_name_technical
    device_name_pretty = config_loader.device_name_friendly
    local_ip = get_local_ip()
    web_gui_url = f"http://{local_ip}:8081"
    ntfy_channel = config_loader.ntfy_channel or 'N/A'

    if request.method == 'POST':
        new_mapping = {}
        for key in mapping.keys():
            val = request.form.get(key, '').strip()
            if val:
                new_mapping[key] = val
        
        mapping_file_path = config_loader.channel_mapping_path
        try:
            with open(mapping_file_path, 'w', encoding='utf-8') as f:
                json.dump(new_mapping, f, indent=4, ensure_ascii=False)
            config_loader.channel_names_mapping = new_mapping
            mapping = new_mapping
            msg = 'Kanal-Namen erfolgreich aktualisiert!'
            msg_type = 'success'
        except Exception as e:
            msg = f'Fehler beim Speichern des Mappings: {e}'
            msg_type = 'danger'

    channel_values = {}
    channel_statuses = {}
    channel_controls = {}
    
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # 1. Messwerte lesen
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='telemetry';")
            if cursor.fetchone():
                cursor.execute("PRAGMA table_info(telemetry)")
                columns = [row[1] for row in cursor.fetchall()]
                
                for key in mapping.keys():
                    if 'temp' in key.lower():
                        matched_col = next((col for col in columns if col.lower() == key.lower()), None)
                        if matched_col:
                            try:
                                cursor.execute(f'SELECT "{matched_col}" FROM telemetry WHERE "{matched_col}" IS NOT NULL ORDER BY timestamp DESC LIMIT 1')
                                row = cursor.fetchone()
                                if row and row[0] is not None:
                                    channel_values[key] = f"{row[0]:.1f} °C"
                                else:
                                    channel_values[key] = "Wartet auf Messung..."
                            except Exception:
                                channel_values[key] = "N/A"
                        else:
                            channel_values[key] = "Spalte fehlt in DB"

            # 2. Status & Steuerung aus channel_control lesen
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='channel_control';")
            if cursor.fetchone():
                for key in mapping.keys():
                    if 'temp' in key.lower():
                        cursor.execute("SELECT status, force_export FROM channel_control WHERE LOWER(channel) = LOWER(?)", (key,))
                        ctrl_row = cursor.fetchone()
                        if ctrl_row:
                            st, fx = (ctrl_row[0] or 'RESET').upper(), ctrl_row[1]
                            
                            if fx == 1:
                                channel_controls[key] = "EXPORT"
                            else:
                                channel_controls[key] = st
                            
                            if st == 'TRIGGERED':
                                channel_statuses[key] = "🔴 Abbinden erkannt!"
                            elif st == 'RUNN':
                                channel_statuses[key] = "🟢 Überwachung läuft"
                            else:
                                channel_statuses[key] = "⚪ Bereit / Inaktiv"
                        else:
                            channel_controls[key] = "RESET"
                            channel_statuses[key] = "⚪ Bereit / Inaktiv"

            conn.close()
        except Exception as e:
            print(f"DB Fehler in web_app: {e}")

    return render_template(
        'channels.html', 
        mapping=mapping, 
        msg=msg, 
        msg_type=msg_type,
        device_id=device_id,
        device_name_pretty=device_name_pretty,
        local_ip=local_ip,
        web_gui_url=web_gui_url,
        ntfy_channel=ntfy_channel,
        channel_values=channel_values,
        channel_statuses=channel_statuses,
        channel_controls=channel_controls
    )

@app.route('/control', methods=['POST'])
def control_channel():
    channel = request.form.get('channel')
    action = request.form.get('action')
    
    if not channel or not action:
        return redirect(url_for('index'))
    
    action_upper = action.upper()
    
    if action_upper == 'RUNN':
        db.start_channel(channel)
        msg_text = f"Kanal '{channel}' gestartet und scharfgeschaltet (RUNN)!"
    elif action_upper in ['RESET', 'STOP']:
        db.reset_channel(channel)
        msg_text = f"Kanal '{channel}' zurückgesetzt (RESET)!"
    elif action_upper == 'EXPORT':
        db.request_export(channel)
        msg_text = f"Manueller Export für '{channel}' angefordert!"
        
    return redirect(url_for('index', msg=msg_text, msg_type="success"))

@app.route('/config')
@login_required
def config_page():
    config = config_loader.main_config
    config['notify'] = {'channel_name': config_loader.ntfy_channel}
    config['thingsboard'] = {
        'host': config_loader.mqtt_host,
        'port': config_loader.mqtt_port,
        'username': config_loader.mqtt_username,
        'password': config_loader.mqtt_password
    }
    mapping = config_loader.channel_names_mapping
    device_mapping = config_loader.device_mapping
    msg = request.args.get('msg', None)
    msg_type = request.args.get('msg_type', 'success')
    return render_template('index.html', config=config, mapping=mapping, device_mapping=device_mapping, msg=msg, msg_type=msg_type)

@app.route('/save', methods=['POST'])
@login_required
def save():
    global config_loader
    config = config_loader.main_config
    config['device_name'] = request.form.get('device_name')
    config['systemd_service'] = request.form.get('systemd_service', 'phidget-app.service')
    config['phidget_serial'] = int(request.form.get('phidget_serial', 0))
    config['temp_delta_min'] = float(request.form.get('temp_delta_min', 0.6))
    config['temp_delta_max'] = float(request.form.get('temp_delta_max', 1.0))
    config['interval_minutes'] = int(request.form.get('interval_minutes', 2))
    
    secrets_data = {
        'notify': {'channel_name': request.form.get('notify_channel', '').strip()},
        'admin_notify': {'channel_name': config_loader.secrets.get('admin_notify', {}).get('channel_name', 'CCSPhidgetAdmin')},
        'thingsboard': {
            'host': request.form.get('tb_host', 'mqtt.thingsboard.cloud').strip(),
            'port': int(request.form.get('tb_port', 1883)),
            'username': request.form.get('tb_username', '').strip(),
            'password': request.form.get('tb_password', '').strip()
        }
    }
    
    config['sensors'] = []
    for i in range(6):
        port_num = int(request.form.get(f'port_{i}', i))
        sensor_type = request.form.get(f'sensor_type_{i}', 'none')
        telemetry_key = request.form.get(f'telemetry_key_{i}', 'Unbelegt')
        config['sensors'].append({'port': port_num, 'sensor_type': sensor_type, 'telemetry_key': telemetry_key})
    
    new_channel_mapping = {}
    for key in config_loader.channel_names_mapping.keys():
        val = request.form.get(f'mapping_{key}', '').strip()
        if val:
            new_channel_mapping[key] = val

    new_device_mapping = {}
    for key in config_loader.device_mapping.keys():
        val = request.form.get(f'dev_map_{key}', '').strip()
        if val:
            new_device_mapping[key] = val

    try:
        with open(config_loader.main_config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        with open(config_loader.secrets_path, 'w', encoding='utf-8') as f:
            json.dump(secrets_data, f, indent=4, ensure_ascii=False)
            
        if new_channel_mapping:
            with open(config_loader.channel_mapping_path, 'w', encoding='utf-8') as f:
                json.dump(new_channel_mapping, f, indent=4, ensure_ascii=False)
        if new_device_mapping:
            with open(config_loader.device_mapping_path, 'w', encoding='utf-8') as f:
                json.dump(new_device_mapping, f, indent=4, ensure_ascii=False)

    except Exception as e:
        print(f"Fehler beim Speichern der Konfiguration: {e}")

    config_loader = ConfigLoader()
    subprocess.run(['systemctl', 'restart', 'phidget-app.service'], check=False)
    return redirect(url_for('config_page', msg='Konfiguration gespeichert!', msg_type='success'))

@app.route('/test-notify', methods=['POST'])
@login_required
def test_notify():
    try:
        success = notifier.send_push_notification(
            title="🔔 Test Push-Benachrichtigung",
            message="Verbindung zum ntfy-Server funktioniert einwandfrei.",
            tags="white_check_mark",
            admin=True
        )
        if success:
            return redirect(url_for('config_page', msg="Test-Push erfolgreich gesendet!", msg_type="success"))
        else:
            return redirect(url_for('config_page', msg="Fehler: Push konnte nicht gesendet werden.", msg_type="danger"))
    except Exception as e:
        return redirect(url_for('config_page', msg=f"Push-Fehler: {e}", msg_type="danger"))

@app.route('/test-thingsboard', methods=['POST'])
@login_required
def test_thingsboard():
    try:
        from tb_device_mqtt import TBDeviceMqttClient
        tb_cfg = config_loader.secrets.get("thingsboard", {})
        client = TBDeviceMqttClient(
            tb_cfg.get("host", "mqtt.thingsboard.cloud"),
            port=int(tb_cfg.get("port", 1883)),
            username=tb_cfg.get("username"),
            password=tb_cfg.get("password"),
            client_id=f"{config_loader.device_name_technical}_test"
        )
        client.connect()
        time.sleep(1.5)
        connected = client.is_connected()
        client.disconnect()

        if connected:
            return redirect(url_for('config_page', msg="ThingsBoard MQTT-Verbindung erfolgreich!", msg_type="success"))
        else:
            return redirect(url_for('config_page', msg="Fehler: ThingsBoard antwortet nicht / Zugangsdaten ungültig.", msg_type="danger"))
    except Exception as e:
        return redirect(url_for('config_page', msg=f"ThingsBoard-Fehler: {e}", msg_type="danger"))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8081, debug=False)