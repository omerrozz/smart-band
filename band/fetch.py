"""Saatteki aktivite dosyalarını ham olarak band/raw/ altına indirir (saatten silmez)."""
import json
import struct
from datetime import datetime
from pathlib import Path

from client import Band

HERE = Path(__file__).parent
RAW = HERE / "raw"
RAW.mkdir(exist_ok=True)
cfg = json.loads((HERE / "config.json").read_text())


def describe(fid: bytes) -> str:
    ts, tz, version, flags = struct.unpack("<IbBB", fid)
    return (f"{datetime.fromtimestamp(ts):%Y%m%dT%H%M%S}_t{(flags >> 7) & 1}"
            f"_s{(flags & 127) >> 2:02d}_d{flags & 3}_v{version}")


with Band(cfg["address"], cfg["auth_key"], cfg["channel"]) as band:
    ids = []
    for today in (True, False):
        found = band.list_files(today)
        print("bugün" if today else "geçmiş", len(found), "dosya")
        ids += [f for f in found if f not in ids]
    for fid in sorted(ids):
        name = describe(fid)
        path = RAW / f"{name}.bin"
        if path.exists():
            continue
        data = band.fetch_file(fid)
        print(name, len(data) if data else "ALINAMADI", flush=True)
        if data:
            path.write_bytes(data)
