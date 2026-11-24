"""Komut denemesi: alarmları oku, saati titret, bildirim gönder."""
import json, sys, time
from datetime import datetime
from pathlib import Path
from client import Band, xiaomi_pb2

cfg = json.loads((Path(__file__).parent / "config.json").read_text())

def drain(band, secs, label):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        cmd = band.read(end - time.monotonic())
        if cmd is not None and not (cmd.type == 8 and cmd.subtype == 47):
            print(f"[{label}] <-", str(cmd).replace("\n", " ")[:400])

with Band(cfg["address"], cfg["auth_key"], cfg["channel"]) as band:
    band.send(17, 0)
    drain(band, 3, "alarmlar")

    c = xiaomi_pb2.Command(type=2, subtype=18); c.system.findDevice = 0
    band._send(c); print("saati bul: başlat gönderildi")
    drain(band, 4, "bul")
    c = xiaomi_pb2.Command(type=2, subtype=18); c.system.findDevice = 1
    band._send(c); print("saati bul: durdur gönderildi")
    drain(band, 2, "bul")

    c = xiaomi_pb2.Command(type=7, subtype=0)
    n = c.notification.notification2.notification3
    n.package = "pc.bileklik"; n.appName = "Bileklik"; n.title = "PC'den merhaba"
    n.unknown4 = ""; n.body = "Bu bildirim bilgisayardaki uygulamadan geldi."
    n.timestamp = datetime.now().strftime("%Y%m%dT%H%M%S"); n.id = int(time.time()) & 0x7FFFFFFF
    band._send(c); print("bildirim gönderildi")
    drain(band, 5, "bildirim")
