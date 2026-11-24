"""Demo kipi: arayüzü saat olmadan denemek için uydurma verilerle çalışan servis.

Geçici bir veri klasörü açar, içine sahte bir config.json yazar, veritabanını bugünle biten
günlerin uydurma kayıtlarıyla doldurur (dakikalık ölçümler, uyku, antrenmanlar, kilo, yemek, su,
takvim) ve arayüzü sunar. Saate hiç bağlanmaz: band_loop başlatılmaz, onun yerine durum "bağlı bir
saat" gibi doldurulur ve küçük bir taklit döngüsü ayar anahtarlarını, alarmları ve "Canlı ölç"ü
yanıtlar. Bütün veriler uydurmadır; servis kapanınca klasör silinir.

Uyku ve antrenman kayıtları testfiles.py'nin ürettiği dosyalardan, gerçek çözücülerden geçerek
(Store.save_file) gelir; dakikalık kayıtlar ve diğer tablolar doğrudan yazılır.

Kullanım:  python demo.py [--port N] [--days N] [--lang tr|en]
"""
from __future__ import annotations

import argparse
import atexit
import json
import math
import os
import queue
import random
import shutil
import signal
import struct
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import testfiles

SEED = 2026
PROFILE = {"age": 22, "height": 178, "weight": 72, "sex": "m"}
GOALS = {"steps": 8000, "calories": 600, "water": 2500, "standing": 12, "moving": 30}
PARK = (39.93719, 32.85043)  # (enlem, boylam): Ankara'da Gençlik Parkı'nın havuzu; turlar çevresinde döner
LAP = 750.0                  # parktaki bir turun uzunluğu (m)
BATTERY = 78

# (kaç gün önce, saat, dakika, tür, süre sn, mesafe m, hız profili)
WORKOUTS = [
    (1, 18, 5, "run", 3000, 8000, "intervals"),
    (3, 19, 10, "walk", 2160, 3200, "steady"),
    (4, 7, 40, "treadmill", 1500, 3600, None),
    (6, 18, 20, "run", 1740, 5000, "steady"),
    (8, 17, 50, "walk", 2700, 4100, "steady"),
    (10, 18, 30, "run", 2400, 6500, "progressive"),
    (12, 8, 0, "treadmill", 1260, 3000, None),
]
# (kaç gün önce, tartıdan mı): kilo kayıtları
WEIGHINGS = [(13, True), (11, True), (9, True), (7, True), (6, False), (4, True), (2, True), (0, True)]
MEALS = {   # öğün: (günün dakikası, seçenekler); her seçenek [(yiyecek, porsiyon)]
    "kahvaltı": (40, [[("Haşlanmış yumurta", 2), ("Beyaz peynir", 1), ("Tam buğday ekmeği", 2), ("Çay", 2)],
                      [("Menemen", 1), ("Beyaz ekmek", 2), ("Çay", 2)],
                      [("Yulaf ezmesi", 1.5), ("Muz", 1), ("Süt", 1)]]),
    "öğle": (12 * 60 + 50, [[("Mercimek çorbası", 1), ("Tavuk göğsü ızgara", 1), ("Bulgur pilavı", 1), ("Ayran", 1)],
                            [("Kuru fasulye", 1), ("Pilav", 1), ("Cacık", 1)],
                            [("Tavuk dürüm", 1), ("Ayran", 1)]]),
    "ara": (16 * 60 + 10, [[("Türk kahvesi", 1), ("Badem", 1)], [("Elma", 1), ("Yoğurt", 1)], [("Filtre kahve", 1), ("Protein bar", 1)]]),
    "akşam": (19 * 60 + 40, [[("Köfte", 1), ("Makarna", 1), ("Çoban salata", 1)],
                             [("Balık ızgara", 1), ("Mevsim salata", 1), ("Beyaz ekmek", 1)],
                             [("Nohut yemeği", 1), ("Pilav", 1), ("Yoğurt", 1)]]),
}


def at(day: date, minute: float = 0) -> int:
    """O yerel günün verilen dakikasının Unix zamanı."""
    return int(datetime.combine(day, datetime.min.time()).timestamp() + minute * 60)


@contextmanager
def patched(module, **values):
    """Bir modülün sabitlerini geçici olarak değiştirir (testfiles'ın tarihlerini kaydırmak için)."""
    old = {k: getattr(module, k) for k in values}
    for k, v in values.items():
        setattr(module, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(module, k, v)


# --- kurulum --------------------------------------------------------------
# uydurma başlıkların İngilizcesi (--lang en); uygulamanın kendi metinleri i18n.py ve arayüzden gelir
DEMO_EN = {"Proje toplantısı": "Project meeting", "Kütüphane, 2. kat": "Library, 2nd floor", "Halı saha maçı": "Five-a-side match",
           "Diş kontrolü": "Dentist check-up", "Fatura ödeme": "Pay the bills", "Etkinlik": "Activity", "Nabız": "Heart rate",
           "Uyku": "Sleep", "Antrenman": "Workout", "Hava durumu": "Weather", "Müzik": "Music", "Stres": "Stress", "Ayarlar": "Settings"}
LANG = None   # None: dil seçilmemiş, arayüz tarayıcının diline göre karar verir


def tx(text: str) -> str:
    return DEMO_EN.get(text, text) if LANG == "en" else text


def prepare(port: int) -> Path:
    """Geçici veri klasörünü ve sahte ayar dosyasını yazar; server içe aktarılmadan ÖNCE çağrılır."""
    data = Path(tempfile.mkdtemp(prefix="bileklik-demo-"))
    atexit.register(shutil.rmtree, data, ignore_errors=True)
    (data / "config.json").write_text(json.dumps({
        "address": "00:00:00:00:00:00", "auth_key": "0" * 32, "channel": 5, "goals": GOALS, "profile": PROFILE,
        "weather": {"enabled": False, "city": "", "lat": None, "lon": None, "every": 60},
        "water": {"enabled": True, "every": 90, "from": "09:00", "to": "22:00"},
        "meal_reminder": {"enabled": True, "times": ["08:30", "13:00", "19:30"]},
        **({"lang": LANG} if LANG else {}),
    }, indent=2) + "\n")
    os.environ.update(BILEKLIK_DATA=str(data), BILEKLIK_PORT=str(port), BILEKLIK_BIND="127.0.0.1")
    return data


# --- antrenmanlar -----------------------------------------------------------
def speed_profile(name: str, seconds: int):
    """t. saniyedeki göreli hız."""
    def profile(t: int) -> float:
        if t < 240:
            return 0.80 + 0.15 * t / 240                       # ısınma
        if t >= seconds - 240:
            return 0.80                                        # soğuma
        if name == "intervals" and seconds * 0.4 <= t < seconds * 0.8:
            return 1.2 if (t - int(seconds * 0.4)) // 180 % 2 == 0 else 0.9   # 3 dakikalık tempo aralıkları
        if name == "progressive":
            return 0.92 + 0.2 * t / seconds
        return 1.0
    return profile


def park_loop(n: int = 720) -> tuple[list[tuple[float, float]], list[float]]:
    """Parkın içinde kapalı bir tur: metre cinsinden noktalar ve birikimli uzunluk (toplamı LAP)."""
    pts = []
    for i in range(n + 1):
        a = 2 * math.pi * i / n
        pts.append(((0.62 + 0.04 * math.cos(3 * a)) * math.cos(a), (1.0 + 0.05 * math.sin(2 * a)) * math.sin(a)))
    cum = [0.0]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0))
    k = LAP / cum[-1]
    return [(x * k, y * k) for x, y in pts], [c * k for c in cum]


def outdoor_files(sport: str, start: int, seconds: int, distance: float, profile: str, seed: int) -> list[bytes]:
    """GPS'li koşu ya da yürüyüş: özet, saniyelik ayrıntı ve iz dosyaları (testfiles.run_files ile aynı biçim)."""
    run = sport == "run"
    recs = testfiles.simulate(seconds, distance, speed_profile(profile, seconds), 88 if run else 82, seed)
    for i, r in enumerate(recs):
        r["ts"] = start + i
    extra = {"XIAOMI_WORKOUT_TYPE": 1 if run else 2, "MAXIMUM_OXYGEN_UPTAKE": 46, "DISTANCE_GOAL": 0,
             "TRAINING_EFFECT_AEROBIC": 3.1 + distance / 10000 if run else 1.4,
             "TRAINING_EFFECT_ANAEROBIC": 1.6 if profile == "intervals" else 0.6 if run else 0.0,
             "RECOVERY_TIME": round(distance / 250) if run else 6, "WORKOUT_LOAD": round(distance / 56) if run else 24,
             "VITALITY_GAIN": round(distance / 600) if run else 5}
    if run:
        extra.update(AVG_GROUND_CONTACT_TIME=262, MIN_GROUND_CONTACT_TIME=231, PROJECTED_TIME_5KM=1620,
                     PROJECTED_TIME_10KM=3390)
    values = testfiles._summary_values(recs, start, start + seconds, extra)
    summary = testfiles.summary_file(0x16, 6, start, values)

    body = bytes.fromhex("fffffffffffff0") + struct.pack("<iii", 0, len(recs), start) + bytes(15)
    for r in recs:
        body += bytes([r["kcal"] << 4 | r["steps"], r["hr"], 0x80 if r["km"] else 0, r["dm"], r["stride"]])
        body += bytes(5) + bytes([r["cadence"]]) + struct.pack("<H", r["pace"]) + bytes(8)
    details = testfiles.wrap(start, 8, 1, 0x16, 0, body)

    pts, cum = park_loop()
    m_lat, m_lon = 111_320.0, 111_320.0 * math.cos(math.radians(PARK[0]))
    body, d = b"\x00", 0.0
    for r in recs:
        d += r["dm"] / 10
        s = d % cum[-1]
        j = max(0, min(len(cum) - 2, next(i for i, c in enumerate(cum) if c > s) - 1))
        f = (s - cum[j]) / (cum[j + 1] - cum[j])
        x = pts[j][0] + f * (pts[j + 1][0] - pts[j][0])
        y = pts[j][1] + f * (pts[j + 1][1] - pts[j][1])
        body += struct.pack("<ifffHff", r["ts"], PARK[1] + x / m_lon, PARK[0] + y / m_lat, 3.0,
                            round(r["speed"] * 10) << 4, 852 + 4 * math.sin(2 * math.pi * s / cum[-1]), 0.9)
    return [summary, details, testfiles.wrap(start, 3, 1, 0x16, 2, body)]


def add_workouts(store, today: date, days: int, now: int) -> None:
    for k, (ago, hour, minute, sport, seconds, distance, profile) in enumerate(WORKOUTS):
        start = at(today - timedelta(days=ago), hour * 60 + minute)
        if ago >= days or start + seconds > now - 600:
            continue
        if sport == "treadmill":
            with patched(testfiles, MILL_START=start, MILL_SECONDS=seconds, MILL_DISTANCE=float(distance)):
                files = [f for f, _ in testfiles.treadmill_files()[0].values()]
        else:
            files = outdoor_files(sport, start, seconds, distance, profile, SEED + k)
        for data in files:
            store.save_file(data[:7], data)


# --- uyku -----------------------------------------------------------------
def add_night(store, bed: int, wake: int, rng: random.Random) -> None:
    """bed'de uykuya dalınıp wake'te uyanılan gece: testfiles'ın evre kalıbı bu süreye ölçeklenir."""
    total = (wake - bed) // 60
    stages = [[n, max(2, round(m * rng.uniform(0.8, 1.2)))] for n, m in testfiles.NIGHT_STAGES]
    k = total / sum(m for _, m in stages)
    stages = [[n, max(2, round(m * k))] for n, m in stages]
    stages[0][1] += total - sum(m for _, m in stages)          # yuvarlama farkı ilk hafif evreye
    shift = rng.randint(-2, 3)
    asleep = sum(m for n, m in stages if n != "awake")
    score = max(55, min(95, round(86 + (asleep - 460) / 6 + rng.gauss(0, 3))))
    with patched(testfiles, NIGHT_BED=bed, NIGHT_INTO_BED=bed - rng.randint(8, 35) * 60, NIGHT_WAKE=bed + total * 60,
                 NIGHT_MINUTES=total, NIGHT_STAGES=[tuple(s) for s in stages], NIGHT_SCORE=score,
                 _STAGE_HR={n: v + shift for n, v in testfiles._STAGE_HR.items()}):
        data = testfiles.sleep_file_v5()[0]
    store.save_file(data[:7], data)


# --- dakikalık kayıtlar ve günlük özet --------------------------------------
def plan_days(today: date, days: int, rng: random.Random) -> list[dict]:
    """Her gün için uyanma ve uykuya dalma dakikası ile hareketlilik katsayısı (ilk satır kayıt öncesi gün)."""
    out = []
    for i in range(days, -1, -1):
        day = today - timedelta(days=i)
        weekend = day.weekday() >= 5
        wake = 7 * 60 + rng.randint(-15, 25) + (75 if weekend else 0)
        for ago, hour, minute, *_ in WORKOUTS:      # sabah antrenmanından önce uyanılmış olmalı
            if ago == i:
                wake = min(wake, hour * 60 + minute - 35)
        out.append({"day": day, "weekend": weekend, "wake": wake, "onset": 23 * 60 + rng.randint(5, 50),
                    "factor": rng.uniform(0.4, 1.15) if i else 1.0})   # bugün ortalama bir gün
    return out


def walking_bouts(p: dict, rng: random.Random) -> dict[int, int]:
    """Günün yürüyüşleri: dakika -> adım. İşe gidiş geliş, öğle arası, akşam; her saat kısa bir kalkış."""
    f, steps = p["factor"], {}

    def bout(start: int, length: float, rate: int) -> None:
        for m in range(start, min(p["onset"] - 20, start + max(3, round(length * f)))):
            steps[m] = max(20, round(rate + rng.gauss(0, 6)))

    if p["weekend"]:
        bout(11 * 60 + rng.randint(0, 50), rng.randint(25, 45), 104)
        bout(16 * 60 + rng.randint(0, 60), rng.randint(15, 30), 100)
    else:
        bout(p["wake"] + rng.randint(50, 70), rng.randint(14, 20), 110)
        bout(12 * 60 + rng.randint(20, 50), rng.randint(8, 14), 100)
        bout(17 * 60 + rng.randint(20, 60), rng.randint(14, 22), 110)
    if rng.random() < 0.5:
        bout(20 * 60 + rng.randint(20, 70), rng.randint(10, 25), 102)
    for hour in range(p["wake"] // 60 + 1, 23):
        if rng.random() < 0.5 + 0.25 * f:
            start = hour * 60 + rng.randint(2, 50)
            for m in range(start, start + rng.randint(2, 4)):
                steps.setdefault(m, rng.randint(35, 80))
    return steps


def workout_minutes(store, day: date) -> dict[int, tuple]:
    """O günün antrenmanlarının dakikalık karşılığı: dakika -> (adım, kcal, metre, nabız)."""
    out, start = {}, at(day)
    for w in store.workouts():
        if not start <= w["start"] < start + 86400:
            continue
        s = store.workout(w["start"])["series"]
        groups: dict[int, list[int]] = {}
        for i, ts in enumerate(s["ts"]):
            groups.setdefault((ts - start) // 60, []).append(i)
        prev = 0
        for m, idx in groups.items():
            hr = [s["hr"][i] for i in idx if s["hr"][i]]
            out[m] = (s["steps"][idx[-1]] - (s["steps"][prev] if prev else 0),
                      round(w["calories"] * len(idx) / len(s["ts"])),
                      round(s["distance"][idx[-1]] - (s["distance"][prev] if prev else 0)),
                      round(sum(hr) / len(hr)) if hr else None)
            prev = idx[-1]
    return out


def add_day(store, p: dict, rng: random.Random, now: int, carry: dict) -> None:
    """Bir günün dakikalık satırları. Uykuda nabız yazılmaz: saat onu uyku dosyasına yazar (store._fill_sleep_hr)."""
    start, walks, sport = at(p["day"]), walking_bouts(p, rng), workout_minutes(store, p["day"])
    hr, drift, kcal_acc, energy = 62.0, 0.0, 0.0, carry.get("energy", 55.0)
    rows = []
    for m in range(1440):
        ts = start + m * 60
        if ts > now - 120:
            break
        if m < p["wake"] or m >= p["onset"]:
            energy = min(100.0, energy + 0.13)
            rows.append((ts, 0, 0, 0, None, None, None, round(energy), None, None))
            hr = 58.0
            continue
        if m in sport:
            steps, kcal, dist, beat = sport[m]
            hr = float(beat or hr)
            energy = max(5.0, energy - 0.45)
            rows.append((ts, steps, min(63, kcal), dist, beat, None, None, round(energy), None, None))
            continue
        steps = walks.get(m, rng.randint(4, 35) if rng.random() < 0.07 * p["factor"] + 0.02 else 0)
        drift = 0.97 * drift + rng.gauss(0, 0.9)
        hr += (64 + drift + 4 * math.sin((m - 600) / 1440 * 2 * math.pi) + 0.40 * min(steps, 125) - hr) * 0.45 + rng.gauss(0, 1.0)
        kcal_acc += steps * 0.045
        kcal = int(kcal_acc)
        kcal_acc -= kcal
        energy = max(5.0, energy - 0.042 - steps * 0.0012)
        calm = steps < 20
        stress = round(max(5, min(85, 26 + 16 * math.exp(-((m - 870) / 200) ** 2) + 2.5 * drift + rng.gauss(0, 4)))) if calm and m % 5 == 0 else None
        spo2 = max(94, min(99, round(97 + rng.gauss(0, 0.9)))) if calm and m % 10 == 5 else None
        rows.append((ts, steps, kcal, round(steps * 0.76), round(hr), spo2, stress, round(energy), None, None))
    carry["energy"] = energy
    with store.lock:
        store.db.executemany("INSERT OR REPLACE INTO minutes VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        store.db.commit()


def add_summary(store, p: dict, now: int, bmr: float, history: list[dict]) -> dict | None:
    """Saatin günlük özet dosyasındaki alanlar, dakikalık kayıttan hesaplanır."""
    day, start = p["day"], at(p["day"])
    m = store.minutes(day)
    hr = [(i, v) for i, v in enumerate(m["hr"]) if v]
    if not hr:
        return None
    steps = [v or 0 for v in m["steps"]]
    stress = [v for v in m["stress"] if v]
    spo2 = [v for v in m["spo2"] if v]
    lo, hi = min(hr, key=lambda x: x[1]), max(hr, key=lambda x: x[1])
    active = sum(v or 0 for v in m["kcal"])
    todays = [w for w in store.workouts() if start <= w["start"] < start + 86400]
    brisk = sum(1 for v in steps if v >= 90)
    gain = {"light": min(6, brisk // 8), "moderate": sum(w.get("vitality_gain", 0) for w in todays if w["sport"] != "running"),
            "high": sum(w.get("vitality_gain", 0) for w in todays if w["sport"] == "running"),
            "load": sum(w.get("training_load", 0) for w in todays),
            "recover": max([w["end"] + w.get("recovery_time", 0) for w in todays], default=0)}
    history.append(gain)
    week = history[-7:]
    end = min(now, start + 86400)
    summary = {
        "ts": start, "steps": sum(steps), "active_kcal": active, "hr_rest": store.rest_estimate(m["hr"]),
        "hr_max": hi[1], "hr_max_ts": start + hi[0] * 60, "hr_min": lo[1], "hr_min_ts": start + lo[0] * 60,
        "hr_avg": round(sum(v for _, v in hr) / len(hr)),
        "standing_hours": [h for h in range(24) if max(steps[h * 60:h * 60 + 60]) >= 35],
        "kcal": active + round(bmr * (end - start) / 86400),
        "recovery_hours": max(0, round((max(g["recover"] for g in history) - end) / 3600)),
        "load_day": gain["load"], "load_week": sum(g["load"] for g in week),
        "load_level": 1 + (sum(g["load"] for g in week) > 150) + (sum(g["load"] for g in week) > 350),
        "vitality_light": sum(g["light"] for g in week), "vitality_moderate": sum(g["moderate"] for g in week),
        "vitality_high": sum(g["high"] for g in week),
        "vitality": sum(g["light"] + g["moderate"] + g["high"] for g in week),
    }
    if stress:
        summary.update(stress_avg=round(sum(stress) / len(stress)), stress_max=max(stress), stress_min=min(stress))
    if spo2:
        summary.update(spo2_avg=round(sum(spo2) / len(spo2)), spo2_max=max(spo2), spo2_min=min(spo2))
    with store.lock:
        store.db.execute("INSERT OR REPLACE INTO daily VALUES (?,?)", (day.isoformat(), json.dumps(summary)))
        # anlık ölçüm tablosu (/api/day'in saatlik çubukları): 10 dakikada bir birikimli adım ve kalori
        rows, total, burned = [], 0, 0.0
        for i in range(1440):
            if m["steps"][i] is None:
                continue
            total += m["steps"][i]
            burned += (m["kcal"][i] or 0) + bmr / 1440
            if i % 10 == 9:
                ts = start + i * 60
                level = BATTERY + (now - ts) / 3600 * 0.23
                rows.append((ts, total, round(burned), m["hr"][i], round(level if level <= 100 else level - 62)))
        store.db.executemany("INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?)", rows)
        store.db.commit()
    return summary


# --- kilo, yemek, su, takvim, elle ölçüm --------------------------------------
def add_body(store, insights, plan: dict[date, dict], today: date, now: int, rng: random.Random) -> None:
    rows = []
    for ago, from_scale in WEIGHINGS:
        day = today - timedelta(days=ago)
        if day not in plan or (ts := at(day, plan[day]["wake"] + 12)) > now:
            continue
        kg = round(72.0 + 0.07 * ago + rng.gauss(0, 0.12), 2) if ago else 72.0
        rows.append((ts, kg))
        if from_scale:
            low = round(486 + rng.gauss(0, 5), 1)
            high = round(low * 0.894 + rng.gauss(0, 1.5), 1)
            shown = None
            if ago == 9:   # bir ölçümde tartının ekranındaki yağ oranı girilmiş: hesap ona göre ayarlanır
                shown = round(insights.fat_estimate(kg, PROFILE, low, high) - 1.1, 1)
            with store.lock:
                store.db.execute("INSERT OR REPLACE INTO scale VALUES (?,?,?,?,?,?)",
                                 (ts, kg, rng.randint(66, 76), low, high, shown))
    with store.lock:
        store.db.executemany("INSERT OR REPLACE INTO weights VALUES (?,?)", rows)
        store.db.commit()


def add_food(store, insights, plan: dict[date, dict], today: date, now: int, rng: random.Random) -> None:
    """Son bir haftanın öğünleri ve içilen su; bugünkülerden saati gelmemiş olanlar yazılmaz."""
    for ago in range(min(7, len(plan)) - 1, -1, -1):
        day = today - timedelta(days=ago)
        p = plan[day]
        for meal, (minute, options) in MEALS.items():
            ts = at(day, (p["wake"] + minute if meal == "kahvaltı" else minute) + rng.randint(-15, 15))
            if ts > now:
                continue
            for k, (name, qty) in enumerate(rng.choice(options)):
                insights.add_food(store, name, qty, ts + k * 60)
        glasses = [at(day, m + rng.randint(-20, 20)) for m in (510, 630, 780, 930, 1080, 1230, 1320)[:rng.randint(5, 7)]]
        with store.lock:
            store.db.executemany("INSERT INTO water (ts, ml) VALUES (?, 250)", [(ts,) for ts in glasses if ts <= now])
            store.db.commit()


def add_extras(store, today: date, now: int) -> None:
    for ago, minute, title, place, length in ((-1, 10 * 60, "Proje toplantısı", "Kütüphane, 2. kat", 60),
                                              (-3, 20 * 60, "Halı saha maçı", "", 60), (-5, 9 * 60 + 30, "Diş kontrolü", "", 45)):
        start = at(today - timedelta(days=ago), minute)
        store.calendar_save({"title": tx(title), "location": tx(place), "note": "", "start": start, "end": start + length * 60})
    manual = [(at(today, 9 * 60 + 12), "hr", 71), (at(today, 14 * 60 + 30), "spo2", 97), (at(today, 14 * 60 + 31), "stress", 34),
              (at(today - timedelta(days=1), 21 * 60 + 40), "hr", 64)]
    with store.lock:
        store.db.executemany("INSERT OR REPLACE INTO manual VALUES (?,?,?)", [r for r in manual if r[0] <= now])
        store.db.commit()


def fill(server, days: int) -> dict | None:
    """Veritabanını doldurur; bugünün özetini döndürür."""
    rng = random.Random(SEED)
    store, insights = server.store, server.insights
    now, today = int(time.time()), date.today()
    plan = plan_days(today, days, rng)
    add_workouts(store, today, days, now)
    for before, p in zip(plan, plan[1:]):          # her gece bir önceki akşam başlar, o sabah biter
        wake = at(p["day"], p["wake"])
        if wake <= now - 300:
            add_night(store, at(before["day"], before["onset"]), wake, rng)
    carry, history, summary = {}, [], None
    for p in plan[1:]:
        add_day(store, p, rng, now, carry)
        summary = add_summary(store, p, now, insights.bmr(PROFILE), history)
    by_day = {p["day"]: p for p in plan[1:]}
    add_body(store, insights, by_day, today, now, rng)
    add_food(store, insights, by_day, today, now, rng)
    add_extras(store, today, now)
    return summary if plan[-1]["day"] == today else None


# --- bağlı saat görüntüsü ---------------------------------------------------
def connect(server, summary: dict | None) -> None:
    """Durumu, saat bağlanıp ayarları okunmuş gibi doldurur."""
    pb, cfg, now = server.xiaomi_pb2, server.band_cfg, int(time.time())
    cmd = pb.Command(type=8, subtype=10)
    hr = cmd.health.heartRate
    hr.disabled, hr.interval, hr.alarmHighEnabled, hr.alarmHighThreshold = False, 1, True, 120
    hr.advancedMonitoring.enabled = True
    hr.heartRateAlarmLow.alarmLowEnabled, hr.heartRateAlarmLow.alarmLowThreshold = True, 40
    hr.breathingScore, hr.irregularRhythmAlert, hr.sleepRespiratoryRate = 1, 2, 1
    spo2 = cmd.health.spo2
    spo2.allDayCapable, spo2.mode, spo2.interval = 1, 2, 0
    spo2.alarmLow.alarmLowEnabled, spo2.alarmLow.alarmLowThreshold = True, 90
    cmd.health.standingReminder.enabled = True
    cmd.health.stress.allDayTracking = True
    cmd.health.stress.relaxReminder.enabled = False
    sleep = pb.Command(type=17, subtype=8).schedule.sleepMode
    sleep.enabled = True
    sleep.schedule.start.hour, sleep.schedule.start.minute = 23, 30
    sleep.schedule.end.hour, sleep.schedule.end.minute = 7, 0
    display = pb.Command(type=2, subtype=29).system.displayItems
    for code, name, off in (("activity", "Etkinlik", False), ("heart", "Nabız", False), ("sleep", "Uyku", False),
                            ("workout", "Antrenman", False), ("weather", "Hava durumu", False), ("music", "Müzik", True),
                            ("alarm", "Alarm", False), ("stress", "Stres", True), ("settings", "Ayarlar", False)):
        item = display.displayItem.add()
        item.code, item.name, item.disabled, item.isSettings = code, tx(name), off, int(code == "settings")
    cfg.update(hr=hr, spo2=spo2, standing=cmd.health.standingReminder, stress=cmd.health.stress, sleep=sleep,
               display=display, dnd=False)
    server.cal.update(sig=server.calendar_sig(server.calendar_due()), ts=now)   # takvim saate yazılmış sayılır
    soon = date.today() + timedelta(days=6)
    server.hub.update(
        link="connected", battery=BATTERY, charging=False, model="Demo Band", firmware="demo",
        steps=summary and summary["steps"], calories=summary and summary["kcal"], hr=None, updated=now, synced=now,
        alarms=[{"id": 1, "hour": 7, "minute": 0, "enabled": True, "repeat": 4, "flags": 31, "smart": True},
                {"id": 2, "hour": 9, "minute": 30, "enabled": False, "repeat": 0, "flags": 0, "smart": False}],
        max_alarms=10,
        reminders=[{"id": 1, "title": "Vitamin", "date": date.today().isoformat(), "time": "09:00", "repeat": 1, "flags": None},
                   {"id": 2, "title": tx("Fatura ödeme"), "date": soon.isoformat(), "time": "18:00", "repeat": 7, "flags": None}],
        max_reminders=50, display=server.bandmsg.display_state(display), settings=server.settings_state(),
        device={"worn": True, "charging": False, "asleep": False, "worn_since": None, "updated": now},
        coach=server.coach_state(), calendar=server.calendar_state(), weather=server.weather_state())


class DemoBand:
    """Saatin yerini tutar: komutlar hiçbir yere gitmez. Ayar, alarm ve ekran listesi komutları, saat
    onaylamış gibi duruma işlenir ki arayüzdeki anahtarlar çalışsın; gerisi (bildirim, saati bul,
    hatırlatıcı) sessizce yutulur."""

    def __init__(self, server):
        self.server = server

    def __getattr__(self, name):
        return lambda *args, **kwargs: None

    def _send(self, cmd) -> None:
        s, cfg, key = self.server, self.server.band_cfg, (cmd.type, cmd.subtype)
        field = {(8, 11): "heartRate", (8, 9): "spo2", (8, 13): "standingReminder", (8, 15): "stress"}.get(key)
        if field:
            cfg[{"heartRate": "hr", "standingReminder": "standing"}.get(field, field)] = getattr(cmd.health, field)
        elif key == (17, 9):
            cfg["sleep"] = cmd.schedule.sleepMode
        elif key == (2, 30):
            cfg["display"] = cmd.system.displayItems
            s.hub.update(display=s.bandmsg.display_state(cmd.system.displayItems))
        elif cmd.type == 17 and cmd.subtype in (1, 2, 4):
            alarms = [dict(a) for a in s.hub.snapshot()["alarms"]]
            if cmd.subtype == 4:
                alarms = [a for a in alarms if a["id"] not in cmd.schedule.deleteAlarm.id]
            else:
                new = cmd.subtype == 1
                d = cmd.schedule.createAlarm if new else cmd.schedule.editAlarm.alarmDetails
                aid = max([a["id"] for a in alarms], default=0) + 1 if new else cmd.schedule.editAlarm.id
                alarms = [a for a in alarms if a["id"] != aid] + [
                    {"id": aid, "hour": d.time.hour, "minute": d.time.minute, "enabled": d.enabled,
                     "repeat": d.repeatMode, "flags": d.repeatFlags, "smart": d.smart == 1}]
            s.hub.update(alarms=alarms)


def watch(server) -> None:
    """Saat döngüsünün taklidi: sıradaki işleri sahte saatle çalıştırır, "Şimdi eşitle"yi ve "Canlı ölç"ü yanıtlar."""
    hub, band, rng = server.hub, DemoBand(server), random.Random(SEED)
    live_from = live_until = 0.0
    while True:
        try:
            hub.jobs.get(timeout=1.0)(band)
        except queue.Empty:
            pass
        except Exception as exc:   # tek bir iş demoyu durdurmasın
            print(f"Demo: {exc!r}", flush=True)
        now = time.time()
        if hub.sync_now.is_set():
            hub.sync_now.clear()
            hub.update(synced=int(now))
        live = hub.snapshot().get("live")
        if not live:
            live_from = live_until = 0.0
        elif not live_until:                      # sensörün açılması: iki saniye sonra ilk değer
            live_from = live_from or now
            if now - live_from >= 2:
                live_until = now + server.LIVE_SECONDS
                hub.update(live={"state": "on", "until": int(live_until)})
        elif now >= live_until:
            server.livereq.update(req=False, until=0.0, first=False)
            hub.update(live=None, hr=None)
        else:
            beat = round(66 + 3 * math.sin(now / 7) + rng.gauss(0, 1))
            hub.update(hr=beat, updated=int(now), last_hr={"value": beat, "ts": int(now)})
            server.store.add_live(beat)


def main() -> None:
    ap = argparse.ArgumentParser(description="Arayüzü saat olmadan, uydurma verilerle açar.")
    ap.add_argument("--port", type=int, default=8400, help="arayüzün açılacağı kapı (varsayılan 8400)")
    ap.add_argument("--days", type=int, default=14, help="kaç günlük veri üretilsin (varsayılan 14)")
    ap.add_argument("--lang", choices=["tr", "en"], help="arayüz dili (verilmezse tarayıcının diline göre seçilir)")
    args = ap.parse_args()
    global LANG
    LANG = args.lang
    if args.port == 8377:
        sys.exit("8377 gerçek servisin kapısıdır; demo için başka bir kapı seç.")
    data = prepare(args.port)
    import server   # ayar ve veri klasörü ortam değişkenlerinden okunur; bu yüzden prepare'den sonra

    connect(server, fill(server, max(2, min(90, args.days))))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # kapanırken geçici klasör silinsin
    threading.Thread(target=watch, args=(server,), daemon=True).start()
    threading.Thread(target=server.appwk_loop, daemon=True).start()
    httpd = server.ThreadingHTTPServer((server.BIND, server.PORT), server.Handler)
    httpd.daemon_threads = True
    print(f"Demo arayüzü: http://127.0.0.1:{server.PORT}  (uydurma veri, geçici klasör: {data})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
