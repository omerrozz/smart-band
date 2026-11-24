"""Bağlantı denemesi: kimlik doğrula, pil ve cihaz bilgisini oku, canlı veriyi dinle."""
import json
import sys
from pathlib import Path

from client import Band

cfg = json.loads((Path(__file__).parent / "config.json").read_text())
seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 15

with Band(cfg["address"], cfg["auth_key"], cfg["channel"], verbose=True) as band:
    print("KİMLİK DOĞRULAMA BAŞARILI")
    print("pil:", band.battery())
    print("cihaz:", band.device_info())
    band.realtime_start()
    import time
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        cmd = band.read(end - time.monotonic())
        if cmd is not None and cmd.type == 8:
            print("canlı:", str(cmd).replace("\n", " "))
    band.realtime_stop()
