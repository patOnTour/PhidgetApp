# EDGE ARCHITECTURE & DEPENDENCIES (Phidget SBC4 / Baustellenkoffer)

## 0 Global
- **User auf den Esge Geräten:** auf den Edge Geräten bin ich als root angemeldet und brauche kein sudo in der bash Zeile

## 1. Übersicht & Dienste
Die Koffer-Boxen (SBC4 Linux) erfassen autonom Temperatur- und Feuchtigkeitsdaten, puffern diese lokal und senden sie gebündelt an den zentralen Ingest-Server.

| Dienst / Skript | Verantwortung | Systemd Service |
| :--- | :--- | :--- |
| `phidget_reader.py` | 10Hz Oversampling, Median-/EMA-Filterung, 20s-Taktung in SQLite-Puffer | `phidget-reader.service` |
| `sync_worker.py` | Puffer-Übertragung via HTTP-POST an `/api/v1/telemetry/ingest` | `phidget-sync.service` |
| `phidget_netmon.py` | Überwachung Internet/WLAN, Hotspot-Fallback, Boot-Benachrichtigung | `phidget-netmon.service` |
| `phidget_web.py` | Lokale Web-Statusansicht (Port 8081) und optionales LCD1100-Display | `phidget-web.service` |
| `phidget_updater.py`| Git-basierter Auto-Update-Daemon mit Messungs-Sperrprüfung | `phidget-updater.service` |

## 2. Daten-Persistenz & Pufferung
- **SQLite-Pufferpfad:** `/usr/userapps/PhidgetProject/data/telemetry.db` (nicht mehr flüchtig unter `/tmp/`).
- **Puffer-Tabelle:** `telemetry_buffer(id, timestamp_utc, channel_idx, temperature, synced)`
- **Offline-Garantie:** Bei Stromausfall oder Netzunterbruch bleiben alle Datensätze auf dem Flash-Speicher erhalten. Nach Reconnect überträgt `sync_worker.py` Pakete sequenziell mit `X-Pending-Count`.

## 3. Boot- & Netzwerk-Logik
- **Boot-Erkennung:** `phidget_netmon.py` prüft `/proc/uptime` (< 180s) und ein Flag `/tmp/.boot_ntfy_sent`. Dienst-Neustarts durch den Updater lösen keine Fehlalarme mehr aus.
- **WLAN-Resilienz:** Bei Verbindungsverlust schaltet NetMon nach konfigurierter Zeit auf den Notfall-Hotspot (`hostapd`) um.