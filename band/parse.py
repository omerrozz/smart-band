"""Saatten inen aktivite dosyalarını çözer (biçim Gadgetbridge'den uyarlandı)."""
from __future__ import annotations

import struct


def file_id(data: bytes) -> dict:
    ts, tz, version, flags = struct.unpack_from("<IbBB", data)
    return {"ts": ts, "tz": tz, "version": version,
            "type": (flags >> 7) & 1, "subtype": (flags & 127) >> 2, "detail": flags & 3}


def kind(fid: dict) -> str:
    if fid["type"] == 0:
        base = {0: "daily", 3: "sleep_stages", 6: "manual", 8: "sleep"}.get(fid["subtype"], "activity")
    else:
        base = "workout"
    return f"{base}_{ {0: 'details', 1: 'summary', 2: 'gps'}.get(fid['detail'], 'x') }"


class _Groups:
    """Başlıktaki her yarım bayt bir alan grubunun var olup olmadığını söyler."""

    def __init__(self, header: bytes, data: bytes, pos: int):
        self.header, self.data, self.pos = header, data, pos
        self.group = -1

    def reset(self) -> None:
        self.group = -1

    def _nibble(self) -> int:
        b = self.header[self.group // 2]
        return (b >> 4) if self.group % 2 == 0 else (b & 0x0F)

    def next(self, bits: int) -> bool:
        self.group += 1
        if self.group >= len(self.header) * 2 or not self._nibble() & 8:
            return False
        size = bits // 8
        self.bits = bits
        self.value = int.from_bytes(self.data[self.pos : self.pos + size], "little")
        self.pos += size
        return True

    def has(self, idx: int) -> bool:
        return bool(self._nibble() & (1 << (2 - idx)))

    def get(self, idx: int, bits: int) -> int:
        shift = self.bits - idx - bits
        return (self.value >> shift) & ((1 << bits) - 1)


def daily_details(data: bytes) -> list[dict] | None:
    """Dakikada bir kayıt: adım, kalori, mesafe, nabız, SpO2, stres."""
    fid = file_id(data)
    header_size = {1: 4, 2: 4, 3: 5, 4: 6}.get(fid["version"])
    if header_size is None:
        return None
    end = len(data) - 4
    g = _Groups(data[8 : 8 + header_size], data, 8 + header_size)
    ts, out = fid["ts"], []
    while g.pos < end:
        g.reset()
        row = {"ts": ts, "steps": None, "kcal": None, "dist": None, "hr": None, "spo2": None, "stress": None,
               "energy": None, "light": None, "moment": None}
        extra = 0
        if g.next(16):
            if g.has(1):
                extra = g.get(1, 1)
            if g.has(2):
                row["steps"] = g.get(2, 14)
        if g.next(8) and g.has(1):
            row["kcal"] = g.get(2, 6)
        g.next(8)
        if g.next(16) and g.has(0):
            row["dist"] = g.get(0, 16)
        if g.next(8) and g.has(0):
            row["hr"] = g.get(0, 8) or None
        if g.next(8) and g.has(0):
            v = g.get(0, 8)
            row["energy"] = v if v <= 100 else None
        g.next(16)
        if fid["version"] >= 3:
            if g.next(8) and g.has(0):
                row["spo2"] = g.get(0, 8) or None
            if g.next(8) and g.has(0):
                v = g.get(0, 8)
                row["stress"] = v if 0 < v < 255 else None
        if extra:
            g.pos += 1
        if fid["version"] >= 4:  # ortam ışığı ve vücut hareketi: ham değerler, birimleri bilinmiyor
            if g.next(16) and g.has(0):
                row["light"] = g.get(0, 16)
            if g.next(16) and g.has(0):
                row["moment"] = g.get(0, 16)
        if g.pos > end:
            break
        out.append(row)
        ts += 60
    return out


_SUMMARY = [  # (ad, struct biçimi); None = ayrılmış alan
    ("steps", "I"), ("active_kcal", "H"), (None, "B"), ("hr_rest", "B"), ("hr_max", "B"), ("hr_max_ts", "I"),
    ("hr_min", "B"), ("hr_min_ts", "I"), ("hr_avg", "B"), ("stress_avg", "B"), ("stress_max", "B"),
    ("stress_min", "B"), ("standing", "3s"), ("kcal", "H"), ("recovery_hours", "H"), (None, "B"),
    ("spo2_max", "B"), ("spo2_max_ts", "I"), ("spo2_min", "B"), ("spo2_min_ts", "I"), ("spo2_avg", "B"),
    ("load_day", "H"), ("load_week", "H"), ("load_level", "B"), ("vitality_light", "B"),
    ("vitality_moderate", "B"), ("vitality_high", "B"), ("vitality", "H"), (None, "B"), (None, "B"),
    (None, "H"), (None, "H"),
]


def daily_summary(data: bytes) -> dict | None:
    fid = file_id(data)
    header_size, slots = {3: (3, 21), 4: (3, 21), 5: (4, 32)}.get(fid["version"], (None, None))
    if header_size is None:
        return None
    header, pos = data[8 : 8 + header_size], 8 + header_size
    out = {"ts": fid["ts"]}
    for i, (name, fmt) in enumerate(_SUMMARY[:slots]):
        size = struct.calcsize("<" + fmt)
        if pos + size > len(data) - 4:
            break
        (value,) = struct.unpack_from("<" + fmt, data, pos)
        pos += size
        valid = bool(header[i // 8] & (1 << (7 - i % 8)))
        if name and valid:
            if name == "standing":
                bits = int.from_bytes(value, "little")
                out["standing_hours"] = [h for h in range(24) if bits >> h & 1]
            else:
                out[name] = value
    return out


def parse(data: bytes):
    """(tür, çözülmüş veri | None). Çözülemeyen tür/sürüm None döner, ham dosya saklanır.

    Uyku, antrenman ve elle ölçüm dosyaları parse_more modülünde çözülür.
    """
    k = kind(file_id(data))
    if k == "daily_details":
        return k, daily_details(data)
    if k == "daily_summary":
        return k, daily_summary(data)
    import parse_more  # döngüsel içe aktarma olmasın diye burada
    return k, parse_more.parse(k, data)


if __name__ == "__main__":
    import json
    import sys
    from datetime import datetime

    for path in sys.argv[1:]:
        k, parsed = parse(open(path, "rb").read())
        print(path, "->", k)
        if isinstance(parsed, list):
            print(f"  {len(parsed)} dakika; ilk üçü ve sonuncusu:")
            for r in parsed[:3] + parsed[-1:]:
                print("  ", datetime.fromtimestamp(r["ts"]).strftime("%H:%M"), {a: b for a, b in r.items() if a != "ts" and b is not None})
        else:
            print("  ", json.dumps(parsed))
