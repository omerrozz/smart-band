"""Sentetik aktivite dosyaları üretir (uyku, koşu, koşu bandı, elle ölçüm).

Elimizde bu türlerin gerçek örneği olmadığı için çözücüler bu dosyalarla sınanır. Kodlama,
parse_more.py'nin okuduğu (Gadgetbridge'den alınan) biçimin tersidir: 7 bayt dosya kimliği,
1 dolgu baytı, gövde, sonda CRC32 (LE) — client.py'nin indirdiği dosyalarla aynı zarf.

Kullanım:  python testfiles.py <klasör> [--all]
  --all: eski biçimli uyku evreleri dosyasını (öğle uykusu) da yazar.
Dosya adları Store.save_file ile aynı kalıptadır; zamanlar 2026-11-19/20 yerel saatindedir.
"""
from __future__ import annotations

import math
import random
import struct
import sys
import zlib
from datetime import datetime
from pathlib import Path

import parse
import workout_fields

TZ = 12        # UTC+3, 15 dakikalık dilim
MAX_HR = 192   # bölge sınırları için örnek azami nabız
ZONE_NAMES = ("warm_up", "fat_burn", "aerobic", "anaerobic", "extreme")


def local(y, mo, d, h, mi, s=0) -> int:
    return int(datetime(y, mo, d, h, mi, s).timestamp())


def wrap(ts: int, version: int, ftype: int, subtype: int, detail: int, body: bytes) -> bytes:
    """Dosya kimliği + dolgu + gövde + CRC32."""
    fid = struct.pack("<IbBB", ts, TZ, version, ftype << 7 | subtype << 2 | detail)
    data = fid + b"\x00" + body
    return data + struct.pack("<I", zlib.crc32(data))


# --- uyku (alt tür 8, sürüm 5) --------------------------------------------
STAGE_CODES = {"awake": 0, "light": 1, "deep": 2, "rem": 3}
NIGHT_BED = local(2026, 11, 19, 23, 25)
NIGHT_INTO_BED = local(2026, 11, 19, 22, 58)
# 5 döngü, 2 kısa uyanma; dakikalar
NIGHT_STAGES = [
    ("light", 18), ("deep", 42), ("light", 22), ("rem", 14),
    ("light", 20), ("deep", 38), ("light", 20), ("rem", 20),
    ("awake", 4),
    ("light", 22), ("deep", 25), ("light", 24), ("rem", 27),
    ("light", 24), ("deep", 14), ("light", 26), ("rem", 32),
    ("awake", 6),
    ("light", 18), ("deep", 8), ("light", 23), ("rem", 35),
]
NIGHT_MINUTES = sum(m for _, m in NIGHT_STAGES)
NIGHT_WAKE = NIGHT_BED + NIGHT_MINUTES * 60
NIGHT_SCORE = 86
_STAGE_HR = {"deep": 50, "light": 55, "rem": 61, "awake": 70}
_STAGE_RSA = {"deep": 45, "light": 34, "rem": 22, "awake": 14}  # solunuma bağlı RR salınımı (ms)


def _stage_at(minute: float) -> str:
    t = 0
    for name, m in NIGHT_STAGES:
        t += m
        if minute < t:
            return name
    return NIGHT_STAGES[-1][0]


def _packet(ptype: int, ts: int, body: bytes = b"", flags: int | None = None) -> bytes:
    """Uyku paketi: sihirli sayı, başlık uzunluğu (17), u64 LE zaman, eşlik, tür, u16 BE veri uzunluğu, veri.

    Verisiz paketlerde (2, 3, 9, 12, 13, 14, 18) uzunluk alanı bayrak taşır: flags ile verilir.
    """
    length = len(body) if flags is None else flags
    return b"\xfb\xfa\xfc\xff" + struct.pack("<BqBB", 17, ts, 0, ptype) + struct.pack(">H", length) + body


def night_rr(rng: random.Random) -> list[tuple[int, list[int]]]:
    """Gecenin RR aralıkları: [(ilk atımın zamanı ms, [aralık, ...] 10 ms'ye yuvarlanmış)], uyanmalarda kesilir."""
    segments, cur, t = [], None, NIGHT_BED * 1000.0
    end = NIGHT_WAKE * 1000.0
    k = 0
    while t < end:
        stage = _stage_at((t / 1000 - NIGHT_BED) / 60)
        if stage == "awake":  # uyanıkken bilek hareketli, RR kaydı yok
            cur = None
            t += 1000
            continue
        if cur is None:
            cur = (int(t), [])
            segments.append(cur)
        base = 60000 / _STAGE_HR[stage] + 25 * math.sin(t / 1000 / 540)
        rr = base + _STAGE_RSA[stage] * math.sin(2 * math.pi * t / 1000 / 4.3) + rng.gauss(0, 9)
        k += 1
        if k % 1700 == 0:      # ektopik atım: erken atım + telafi duraklaması
            pair = [0.62 * rr, 1.38 * rr]
        elif k % 4100 == 0:    # kaçan atım: ölçüm aralığının dışında
            pair = [2.05 * rr]
        else:
            pair = [rr]
        for v in pair:
            q = max(1, min(255, round(v / 10)))
            cur[1].append(q * 10)
            t += q * 10
    return segments


def sleep_file_v5() -> tuple[bytes, dict]:
    """Gadgetbridge'in v5 için tarif ettiği düzende bir gece (RR aralıkları ve 17 no'lu evre paketiyle).

    Gerçek örneği yok; RR/RMSSD ve evre paketi çözümünü sınamak için durur. generate() bunu yazmaz.
    """
    rng = random.Random(1119)
    hr = bytes(max(40, min(110, round(_STAGE_HR[_stage_at(m)] + 3 * math.sin(m / 17) + rng.gauss(0, 1.5))))
               for m in range(NIGHT_MINUTES))
    spo2 = bytes(max(90, min(99, round(96.5 + 1.2 * math.sin(i / 9) + rng.gauss(0, 0.6))))
                 for i in range(NIGHT_MINUTES // 5))
    snore_vals = [round(max(0.0, 30 + 12 * math.sin(i / 3) + rng.gauss(0, 3)), 1) for i in range(24)]
    snore = struct.pack(f"<{len(snore_vals)}f", *snore_vals)

    body = bytes([0xFF, 0xF0])                                    # 12 alanın geçerlilik bitleri
    body += struct.pack("<Bii", 0, NIGHT_BED, NIGHT_WAKE)         # isAwake, yatış (uykuya dalış), kalkış
    body += bytes([NIGHT_SCORE])
    body += struct.pack("<IBI", 4242, 0, NIGHT_WAKE - NIGHT_INTO_BED)  # 9 bayt doğrulanmamış üst veri
    body += struct.pack("<ii", NIGHT_INTO_BED, NIGHT_WAKE)
    body += struct.pack("<hhI", 60, len(hr), NIGHT_BED) + hr
    body += struct.pack("<hhI", 300, len(spo2), NIGHT_BED) + spo2
    body += struct.pack("<hhI", 60, len(snore_vals), NIGHT_BED + 95 * 60) + snore

    body += _packet(2, NIGHT_BED)                                 # uykuya dalış (verisiz)
    segments = night_rr(rng)
    for start_ms, rrs in segments:                                # RR: 10 dakikalık paketler, ±2 s saat kayması
        t, chunk, chunk_start = start_ms, [], start_ms
        first = True
        for v in rrs:
            chunk.append(v // 10)
            t += v
            if t - chunk_start >= 600_000:
                body += _packet(1, chunk_start + (0 if first else rng.randint(-2000, 2000)), bytes(chunk))
                chunk, chunk_start, first = [], t, False
        if chunk:
            body += _packet(1, chunk_start + (0 if first else rng.randint(-2000, 2000)), bytes(chunk))
    breathing = 0
    for w in range(NIGHT_MINUTES // 10):                          # 0x0a: pencere başı (ns) + kayıt başına saniye
        start_s = NIGHT_BED + w * 600
        recs = b""
        for i in range(10):
            off = i * 60 + 30
            stage = _stage_at((start_s + off - NIGHT_BED) / 60)
            rate = 0 if (w * 10 + i) % 37 == 0 else round({"deep": 13, "light": 14.5, "rem": 16, "awake": 17}[stage]
                                                              + rng.gauss(0, 0.7))
            breathing += 1 if rate else 0
            recs += bytes([off & 0xFF, rate, rng.randint(40, 90), off >> 8 << 4])
        body += _packet(0x0A, start_s * 1_000_000_000, recs)
    body += _packet(17, NIGHT_BED, b"".join(struct.pack(">H", STAGE_CODES[n] << 12 | m) for n, m in NIGHT_STAGES))
    tot = {n: sum(m for s, m in NIGHT_STAGES if s == n) for n in STAGE_CODES}
    asleep = NIGHT_MINUTES - tot["awake"]
    body += _packet(16, NIGHT_WAKE, struct.pack(">BHHHHHBB", 8 << 4 | 2, asleep, tot["awake"], tot["light"],
                                                tot["rem"], tot["deep"], 0x05, 0))
    body += _packet(3, NIGHT_WAKE)                                # uyanış (verisiz)
    body += _packet(0x65, NIGHT_WAKE, b"\x01\x02\x07\x04")        # işaret paketi

    truth = {"bed_time": NIGHT_BED, "wake_time": NIGHT_WAKE, "into_bed_time": NIGHT_INTO_BED, "score": NIGHT_SCORE,
             "duration": asleep * 60, **{n: m * 60 for n, m in tot.items()}, "stages": len(NIGHT_STAGES),
             "hr": list(hr), "spo2": list(spo2), "snore": snore_vals, "rr_segments": [s[1] for s in segments],
             "breathing": breathing, "wake_count": 2}
    return wrap(NIGHT_BED, 5, 0, 8, 1, body), truth


# Gerçek Band 9 Pro dosyasının (sürüm 4) düzeni: evre paketi boş, uyku bölümleri olay çiftleriyle gelir.
# (evre, dakika): "light" = saatin bildirdiği uyku bölümü, "deep" = bölümler arası uzun boşluk,
# "awake" = 5 dakikadan kısa boşluk (saat bunu 16 no'lu özetinde bölüme katar).
REAL_STAGES = [
    ("light", 38), ("awake", 1), ("light", 46), ("deep", 12), ("light", 27), ("deep", 11), ("light", 22),
    ("deep", 16), ("light", 31), ("awake", 4), ("light", 40), ("deep", 21), ("light", 33), ("deep", 14),
    ("light", 28), ("deep", 25), ("light", 52), ("deep", 11), ("light", 53),
]
REAL_MINUTES = sum(m for _, m in REAL_STAGES)
REAL_WAKE = NIGHT_BED + REAL_MINUTES * 60
STALE_BED = local(2026, 11, 18, 23, 23)  # 17 no'lu boş paket önceki gecenin yatış zamanını taşır


def sleep_file() -> tuple[bytes, dict]:
    """Gerçek dosyanın düzeninde bir gece (alt tür 8, sürüm 4): (dosya baytları, beklenen değerler).

    Başlık 0xD8: puan baytı var ama geçersiz (bit 2 kapalı), nabız (bit 3) ve SpO2 (bit 4) dizileri var.
    Paketler: 101 işaret, 0 ivme (150 bayt), 10 ve 11 olay kayıtları, 9/12/13/14/18 bayraklı verisiz
    paketler, her uyanışta 2 + 3 (ms) + birikimli 16 (13 baytlık kayıtlar) + boş 17. RR paketi (1) yoktur.
    """
    rng = random.Random(1121)
    edges, t = [], NIGHT_BED
    for name, m in REAL_STAGES:
        edges.append((name, t, t + m * 60))
        t += m * 60
    stage_at = lambda ts: next((n for n, a, b in edges if a <= ts < b), "light")
    hr = bytearray(max(45, min(110, round({"light": 62, "deep": 57, "awake": 66}[stage_at(NIGHT_BED + m * 60)]
                                          + 3 * math.sin(m / 19) + rng.gauss(0, 1.5)))) for m in range(REAL_MINUTES))
    for m in (5, 6, 7, 140, 141, 300):
        hr[m] = 0                                                  # ölçülemeyen dakikalar
    spo2 = bytearray(REAL_MINUTES)
    for m in range(8, REAL_MINUTES, 10):
        spo2[m] = max(90, min(99, round(96.5 + rng.gauss(0, 1.1))))  # 10 dakikada bir ölçüm, arası 0

    body = bytes([0xD8]) + struct.pack("<Bii", 0, NIGHT_BED, REAL_WAKE) + b"\x00"
    body += struct.pack("<hhI", 60, len(hr), NIGHT_BED) + bytes(hr)
    body += struct.pack("<hhI", 60, len(spo2), NIGHT_BED) + bytes(spo2)

    segments = [(a, b) for n, a, b in edges if n == "light"]
    records, events, seg_i = [], 0, 0   # 16 no'lu paketin birikimli kayıtları: [dakika, önceki boşluk dk]
    ns = lambda s, frac=0.47: int((s + frac) * 1_000_000_000)
    start = NIGHT_BED - 16
    body += _packet(101, ns(start), b"\x01\x02\x03\x15")
    w = 0
    while start + w * 600 < REAL_WAKE:
        w0 = start + w * 600
        body += _packet(0, ns(w0), bytes(rng.choice((0xAA, 0xAA, 0xAA, 0xAA, 0xA9, 0x2A)) for _ in range(150)))
        if w == 0:
            body += _packet(12, ns(w0), flags=1) + _packet(13, ns(w0), flags=2) + _packet(18, ns(w0), flags=2)
        offs = sorted(rng.sample(range(600), rng.choice((1, 1, 2, 3, 4, 7))))
        body += _packet(10, ns(w0), b"".join(bytes([o & 0xFF, rng.randint(5, 12), rng.randint(15, 140), o >> 8 << 4])
                                             for o in offs))
        body += _packet(11, ns(w0), b"".join(bytes([o & 0xFF]) + rng.randbytes(4) + b"\x00\x41\x03" for o in offs))
        events += len(offs)
        body += _packet(14, ns(w0 + 70), flags=3) + _packet(14, ns(w0 + 370), flags=1)
        w += 1
        while seg_i < len(segments) and segments[seg_i][1] <= start + w * 600:   # biten uyku bölümü
            a, b = segments[seg_i]
            gap = (a - segments[seg_i - 1][1]) // 60 if seg_i else 0
            if seg_i and gap < 5:
                records[-1][0] += gap + (b - a) // 60      # kısa boşluk bölüme katılır
            else:
                records.append([(b - a) // 60, gap])
            body += _packet(2, a * 1000, flags=0) + _packet(3, b * 1000, flags=5)
            body += _packet(16, NIGHT_BED, b"".join(struct.pack(">BHHHHHBB", (i + 1) << 4, r[0], 0, 0, 0, 0, 1, r[1])
                                                    for i, r in enumerate(records)))
            body += _packet(17, STALE_BED, b"")
            seg_i += 1
    tot = {n: sum(m for s, m in REAL_STAGES if s == n) * 60 for n in ("light", "deep", "awake")}
    truth = {"bed_time": NIGHT_BED, "wake_time": REAL_WAKE, "into_bed_time": None, "score": None,
             "duration": tot["light"] + tot["deep"], **tot, "rem": 0, "wake_count": 2,
             "stages": [{"ts": a, "end": b, "stage": n} for n, a, b in edges], "segments": len(segments),
             "records": len(records), "hr": list(hr), "spo2": list(spo2), "events": events, "windows": w}
    return wrap(NIGHT_BED, 4, 0, 8, 1, body), truth


NAP_BED = local(2026, 11, 19, 14, 5)
NAP_STAGES = [(0, 3), (12, 2), (27, 3), (36, 5)]  # (dakika, Gadgetbridge evre kodu: 3 hafif, 2 derin, 5 uyanık)
NAP_WAKE = NAP_BED + 38 * 60


def sleep_stages_file() -> bytes:
    """Eski biçimli uyku evreleri dosyası (alt tür 3, sürüm 2): 38 dakikalık öğle uykusu."""
    body = bytes(7) + struct.pack("<hii", 36, NAP_BED, NAP_WAKE) + bytes(3)
    body += struct.pack("<hhhh", 15, 21, 0, 2) + b"\x00"   # derin, hafif, REM, uyanık (dakika)
    for minute, phase in NAP_STAGES:
        body += struct.pack("<iB", NAP_BED + minute * 60, phase)
    return wrap(NAP_BED, 2, 0, 3, 0, body)


# --- elle ölçümler (alt tür 6, sürüm 2) --------------------------------------
MANUAL = [
    (local(2026, 11, 19, 9, 12), "hr", 72), (local(2026, 11, 19, 14, 30), "spo2", 97),
    (local(2026, 11, 19, 21, 40), "stress", 34), (local(2026, 11, 20, 7, 35), "hr", 61),
    (local(2026, 11, 20, 7, 36), "spo2", 98),
]
_MANUAL_CODES = {"hr": 1, "spo2": 2, "stress": 3, "temperature": 4}


def manual_file(samples=MANUAL, extra: bytes = b"") -> bytes:
    """Kayıt: u32 zaman, bayt (uzunluk << 4 | ölçüm türü), değer."""
    body = b"".join(struct.pack("<iBB", ts, 1 << 4 | _MANUAL_CODES[kind], value) for ts, kind, value in samples)
    return wrap(local(2026, 11, 19, 0, 0), 2, 0, 6, 0, body + extra)


# --- antrenman ----------------------------------------------------------------
def _diffuse(values: list[float]) -> list[int]:
    """Kesirli akışı, toplamı koruyarak tam sayılara böler."""
    out, acc = [], 0.0
    for v in values:
        acc += v
        n = int(acc)
        out.append(n)
        acc -= n
    return out


def simulate(seconds: int, distance: float, profile, hr_start: int, seed: int) -> list[dict]:
    """Saniyelik koşu verisi. profile(t) -> göreli hız; toplam mesafe `distance` metreye ölçeklenir."""
    rng = random.Random(seed)
    rel = [profile(t) for t in range(seconds)]
    scale = distance / sum(rel)
    speed = [r * scale for r in rel]
    dm = _diffuse([v * 10 for v in speed])
    cadence = [round(150 + (v - 2.2) * 25 + rng.gauss(0, 1.5)) for v in speed]
    steps = _diffuse([c / 60 for c in cadence])
    hr, out, total_dm = float(hr_start), [], 0
    kcal = []
    for t in range(seconds):
        target = 100 + (speed[t] - 1.5) * 48 + 8 * t / seconds
        hr += (target - hr) / 30 + rng.gauss(0, 0.25)
        kcal.append(0.04 + max(0.0, hr - 80) * 0.0021)
        before = total_dm
        total_dm += dm[t]
        out.append({"hr": max(60, min(MAX_HR - 2, round(hr))), "dm": dm[t], "steps": steps[t], "cadence": cadence[t],
                    "stride": round(speed[t] / (cadence[t] / 60) * 100), "pace": round(1000 / speed[t]),
                    "speed": speed[t], "km": before // 10000 != total_dm // 10000})
    for r, k in zip(out, _diffuse(kcal)):
        r["kcal"] = k
    return out


def zones(hrs: list[int]) -> dict:
    out = dict.fromkeys(ZONE_NAMES, 0)
    for h in hrs:
        pct = h / MAX_HR
        if pct >= 0.5:
            out[ZONE_NAMES[min(4, int((pct - 0.5) * 10))]] += 1
    return out


def summary_file(subtype: int, version: int, start: int, values: dict) -> bytes:
    """workout_fields tablosuna göre özet dosyası. values: Java anahtarı -> dosyadaki ham değer."""
    _, tbl = workout_fields.table(subtype, version)
    body = b"\xff" * tbl.header_size
    fmt = {"int": "<i", "short": "<H", "byte": "<B", "float": "<f"}
    for kind, key, _unit, arg in tbl.entries:
        if kind == "unknown":
            body += bytes(arg)
        elif kind != "unknown_if":
            body += struct.pack(fmt[kind], values.get(key, 0))
    return wrap(start, version, 1, subtype, 1, body)


def _summary_values(recs: list[dict], start: int, end: int, extra: dict) -> dict:
    hrs = [r["hr"] for r in recs]
    dist = sum(r["dm"] for r in recs) / 10
    steps = sum(r["steps"] for r in recs)
    z = zones(hrs)
    kcal = sum(r["kcal"] for r in recs)
    return {
        "TIME_START": start, "TIME_END": end, "ACTIVE_SECONDS": len(recs), "DISTANCE_METERS": round(dist),
        "CALORIES_BURNT": kcal, "CALORIES_TOTAL": kcal + round(len(recs) * 0.021),
        "PACE_AVG_SECONDS_KM": round(len(recs) / dist * 1000), "PACE_MAX": min(r["pace"] for r in recs),
        "PACE_MIN": max(r["pace"] for r in recs), "SPEED_AVG": dist / len(recs) * 3.6,
        "SPEED_MAX": max(r["speed"] for r in recs) * 3.6, "STEPS": steps,
        "STEP_LENGTH_AVG": round(dist / steps * 100), "STEP_RATE_AVG": round(steps / len(recs) * 60),
        "STEP_RATE_MAX": max(r["cadence"] for r in recs), "CADENCE_AVG": round(steps / len(recs) * 60),
        "CADENCE_MAX": max(r["cadence"] for r in recs), "HR_AVG": round(sum(hrs) / len(hrs)), "HR_MAX": max(hrs),
        "HR_MIN": min(hrs), "HR_ZONE_WARM_UP": z["warm_up"], "HR_ZONE_FAT_BURN": z["fat_burn"],
        "HR_ZONE_AEROBIC": z["aerobic"], "HR_ZONE_ANAEROBIC": z["anaerobic"], "HR_ZONE_EXTREME": z["extreme"],
        **extra,
    }


RUN_START = local(2026, 11, 19, 18, 5)
RUN_SECONDS = 3000
RUN_DISTANCE = 8000.0
RUN_PAUSE_AT, RUN_PAUSE = 1500, 40   # 25. dakikada 40 saniyelik duraklama
RUN_CENTER = (39.9200, 32.8500)      # (enlem, boylam)
RUN_LAPS = 2


def _run_profile(t: int) -> float:
    if t < 300:
        return 0.82 + 0.1 * t / 300                      # ısınma
    if t >= 2700:
        return 0.80                                      # soğuma
    if 1200 <= t < 2400:
        return 1.2 if (t - 1200) // 180 % 2 == 0 else 0.9  # 3 dakikalık tempo aralıkları
    return 1.0


def _loop(n: int = 3600) -> tuple[list[tuple[float, float]], list[float]]:
    """Kapalı parkur: hafif dalgalı çember. (noktalar metre cinsinden, birikimli uzunluk)"""
    pts = []
    for i in range(n + 1):
        a = 2 * math.pi * i / n
        r = 1 + 0.08 * math.sin(3 * a)
        pts.append((r * math.cos(a), r * math.sin(a)))
    cum = [0.0]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0))
    k = (RUN_DISTANCE / RUN_LAPS) / cum[-1]
    return [(x * k, y * k) for x, y in pts], [c * k for c in cum]


def run_files() -> tuple[dict, dict]:
    """Açık hava koşusu (alt tür 0x16; özet v6, ayrıntı v8, GPS v3): ({tür: bayt}, beklenen değerler)."""
    recs = simulate(RUN_SECONDS, RUN_DISTANCE, _run_profile, 86, 1905)
    end = RUN_START + RUN_SECONDS + RUN_PAUSE
    for i, r in enumerate(recs):
        r["ts"] = RUN_START + i + (RUN_PAUSE if i >= RUN_PAUSE_AT else 0)
    values = _summary_values(recs, RUN_START, end, {
        "XIAOMI_WORKOUT_TYPE": 1, "TRAINING_EFFECT_AEROBIC": 3.8, "TRAINING_EFFECT_ANAEROBIC": 1.6,
        "MAXIMUM_OXYGEN_UPTAKE": 46, "RECOVERY_TIME": 38, "WORKOUT_LOAD": 142, "VITALITY_GAIN": 14,
        "AVG_GROUND_CONTACT_TIME": 262, "MIN_GROUND_CONTACT_TIME": 231, "PROJECTED_TIME_5KM": 1620,
        "PROJECTED_TIME_10KM": 3390, "TIME_GOAL": 0, "DISTANCE_GOAL": 8000,
    })
    summary = summary_file(0x16, 6, RUN_START, values)

    # ayrıntı v8: 7 baytlık bit eşlemi (13 grubun hepsi var), bölüm başlığı 27 bayt (kayıt sayısı +4, zaman +8)
    body = bytes.fromhex("fffffffffffff0")
    for seg in (recs[:RUN_PAUSE_AT], recs[RUN_PAUSE_AT:]):
        body += struct.pack("<iii", 0, len(seg), seg[0]["ts"]) + bytes(15)
        for r in seg:
            body += bytes([r["kcal"] << 4 | r["steps"], r["hr"], 0x80 if r["km"] else 0, r["dm"], r["stride"]])
            body += bytes(4) + bytes(1) + bytes([r["cadence"]]) + struct.pack("<H", r["pace"]) + bytes(8)
    details = wrap(RUN_START, 8, 1, 0x16, 0, body)

    # GPS v3: saniyede bir nokta; boylam enlemden önce
    pts, cum = _loop()
    lat0, lon0 = RUN_CENTER
    m_lat = 111_320.0
    m_lon = 111_320.0 * math.cos(math.radians(lat0))
    body, d, j = b"\x00", 0.0, 0
    for r in recs:
        d += r["dm"] / 10
        s = d % cum[-1]
        while cum[j + 1] < s:
            j += 1
        if cum[j] > s:
            j = 0
            while cum[j + 1] < s:
                j += 1
        f = (s - cum[j]) / (cum[j + 1] - cum[j])
        x = pts[j][0] + f * (pts[j + 1][0] - pts[j][0])
        y = pts[j][1] + f * (pts[j + 1][1] - pts[j][1])
        alt = 42 + 9 * math.sin(2 * math.pi * s / cum[-1])
        body += struct.pack("<ifffHff", r["ts"], lon0 + x / m_lon, lat0 + y / m_lat, 3.0,
                            round(r["speed"] * 10) << 4, alt, 0.9)
    gps = wrap(RUN_START, 3, 1, 0x16, 2, body)

    truth = {"start": RUN_START, "end": end, "duration": RUN_SECONDS, "distance": round(sum(r["dm"] for r in recs) / 10),
             "hr": [r["hr"] for r in recs], "steps": sum(r["steps"] for r in recs), "values": values,
             "kcal": sum(r["kcal"] for r in recs), "zones": zones([r["hr"] for r in recs]), "points": len(recs)}
    return {"workout_summary": (summary, 6), "workout_details": (details, 8), "workout_gps": (gps, 3)}, truth


MILL_START = local(2026, 11, 20, 8, 40)
MILL_SECONDS = 1500
MILL_DISTANCE = 3600.0


def treadmill_files() -> tuple[dict, dict]:
    """Koşu bandı (alt tür 3; özet v11, ayrıntı v6), GPS yok."""
    recs = simulate(MILL_SECONDS, MILL_DISTANCE, lambda t: 0.85 if t < 180 or t >= 1380 else 1.0, 78, 2008)
    values = _summary_values(recs, MILL_START, MILL_START + MILL_SECONDS, {
        "TRAINING_EFFECT_AEROBIC": 2.9, "TRAINING_EFFECT_ANAEROBIC": 0.4, "MAXIMUM_OXYGEN_UPTAKE": 46,
        "V02MAX_LEVEL": 3, "RECOVERY_TIME": 17, "WORKOUT_LOAD": 61, "VITALITY_GAIN": 9, "DISTANCE_GOAL": 3500,
        "AVG_GROUND_CONTACT_TIME": 271, "MIN_GROUND_CONTACT_TIME": 244,
    })
    summary = summary_file(0x03, 11, MILL_START, values)
    # ayrıntı v6: imza FF FF 8B FF; bölüm başlığı: kayıt sayısı, zaman, evre (0x7f), mesafe (m); 12 baytlık kayıt
    body = bytes.fromhex("ffff8bff") + struct.pack("<iiBi", len(recs), MILL_START, 0x7F, values["DISTANCE_METERS"])
    for r in recs:
        body += bytes([r["steps"], r["hr"], r["dm"], r["stride"]]) + bytes(5) + bytes([r["cadence"]])
        body += struct.pack("<H", r["pace"])
    details = wrap(MILL_START, 6, 1, 0x03, 0, body)
    truth = {"start": MILL_START, "duration": MILL_SECONDS, "distance": values["DISTANCE_METERS"],
             "hr": [r["hr"] for r in recs], "steps": values["STEPS"], "values": values}
    return {"workout_summary": (summary, 11), "workout_details": (details, 6)}, truth


def generate(out: Path, everything: bool = False) -> list[Path]:
    """Dosyaları yazar; yazılan yolları döndürür."""
    out.mkdir(parents=True, exist_ok=True)
    files = [sleep_file()[0], manual_file()]
    files += [f for f, _ in run_files()[0].values()] + [f for f, _ in treadmill_files()[0].values()]
    if everything:
        files.append(sleep_stages_file())
    paths = []
    for data in files:
        info = parse.file_id(data)
        path = out / f"{datetime.fromtimestamp(info['ts']):%Y%m%dT%H%M%S}_{parse.kind(info)}_v{info['version']}.bin"
        path.write_bytes(data)
        paths.append(path)
    return paths


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--all"]
    if len(args) != 1:
        sys.exit(__doc__)
    for p in generate(Path(args[0]), "--all" in sys.argv):
        print(p, p.stat().st_size, "bayt")
