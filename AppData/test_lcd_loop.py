#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
from datetime import datetime
from Phidget22.Devices.LCD import LCD, LCDFont
from Phidget22.Phidget import PhidgetException

def run_isolated_test(port=1, serial=802547, interval=2.0):
    print(f"--- Starte isolierten LCD-Dauertest auf Port {port} (Intervall: {interval}s) ---")
    lcd = LCD()
    if serial:
        lcd.setDeviceSerialNumber(serial)
    lcd.setHubPort(port)
    lcd.setIsHubPortDevice(False)
    lcd.setChannel(0)

    try:
        lcd.openWaitForAttachment(5000)
        lcd.setBacklight(0.80)
        lcd.setContrast(0.55)
        print("LCD erfolgreich geoeffnet. Schreibe Test-Frames...")
    except Exception as e:
        print(f"Init fehlgeschlagen: {e}")
        return

    counter = 0
    start_time = time.time()

    try:
        while True:
            counter += 1
            now_str = datetime.now().strftime("%H:%M:%S")
            uptime = int(time.time() - start_time)
            
            # Header
            header = f"{now_str} Loop:{counter:<5} Up:{uptime}s"
            lcd.writeText(LCDFont.FONT_5x8, 0, 0, f"{header:<21}")
            
            # 4 Testzeilen
            for row in range(1, 5):
                text = f"Test Line {row} -> {counter * row}"
                lcd.writeText(LCDFont.FONT_6x12, 0, row * 12, f"{text:<21}")
            
            lcd.flush()
            print(f"[{now_str}] Frame #{counter} erfolgreich geflusht (Laufzeit: {uptime}s)")
            time.sleep(interval)

    except PhidgetException as pe:
        print(f"\n[ABBRUCH] PhidgetException aufgetreten nach {time.time() - start_time:.1f}s: {pe}")
    except KeyboardInterrupt:
        print("\nTest manuell beendet.")
    finally:
        try:
            lcd.close()
        except Exception:
            pass

if __name__ == "__main__":
    run_isolated_test()