"""Mi Fitness kişisel veri dökümünü (CSV) band.db'ye aktarır.

Kullanım:
    python -I import_mifitness.py <döküm_klasörü> --db <band.db> [--dry-run] [--undo]

Kurallar:
  * Saatten gelen veri kazanır: bir anahtar (dakika, gün, gece, antrenman, ölçüm) tabloda zaten varsa
    o satıra dokunulmaz; döküm yalnız eksik olanı doldurur. Uyku ve antrenmanda zaman aralığı
    çakışan bir kayıt varsa da atlanır.
  * Eklenen her satır import_log tablosuna (source='mifitness', tablo, anahtar, satırın CRC'si) yazılır.
    --undo yalnız hâlâ aynı içerikte duran satırları siler; sonradan saatin üzerine yazdığı satır kalır.
  * İki kez çalıştırmak bir şey değiştirmez. --dry-run her şeyi hesaplar, sonra geri alır.
  * Döküm güvenilmeyen veridir: yalnız csv/json ile okunur, içindeki bağlantılar (GPX) açılmaz.
    Bu dosya uygulamanın öbür modüllerini içe aktarmaz; -I ile çalışır.

Eşleme (ayrıntı raporda):
  hlth_center_fitness_data      heart_rate/steps/calories/body_momentum/light_sensitivity_value -> minutes
                                single_heart_rate -> manual(hr); weight -> weights; sleep -> sleep;
                                resting_heart_rate/valid_stand/training_load/vitality -> daily
  hlth_center_aggregated_...    daily_report steps/calories/heart_rate -> daily; sleep.sleep_score -> sleep.score
  hlth_center_sport_record      -> workouts (yalnız özet; seri ve GPS dökümde yok)

Zaman: dökümdeki Time alanları gerçek unix saniyesidir; günlük kayıtların Time'ı yerel günün
tarihini UTC gece yarısı olarak taşır. Gün sınırları, uygulamanın geri kalanı gibi, sistemin yerel
saatine göre hesaplanır; dökümdeki timezone alanı (15 dakikalık dilim) sistemle uyuşmazsa uyarılır.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import zlib
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SOURCE = "mifitness"
MIN_TS = 1577836800  # 2020-01-01; saati ayarlanmamış bilekliğin 2000 yılı kayıtları atılır
SLEEP_STATES = {2: "deep", 3: "light", 4: "rem", 5: "awake"}  # parse_more.GB_STAGE_NAMES ile aynı kodlar
SPORT_TYPES = {1: "running", 2: "walking", 3: "treadmill", 6: "cycling", 7: "indoor_cycling", 8: "freestyle",
               9: "pool_swimming", 11: "elliptical", 13: "rowing", 14: "jump_rope", 16: "hiit"}
MINUTE_COLS = ("ts", "steps", "kcal", "dist", "hr", "spo2", "stress", "energy", "light", "moment")

csv.field_size_limit(1 << 30)


# --- okuma ------------------------------------------------------------------
def table_path(folder: Path, name: str) -> Path | None:
    found = sorted(folder.glob(f"*_MiFitness_{name}.csv"))
    return found[0] if found else None


def read_rows(folder: Path, name: str):
    """CSV satırlarını sözlük olarak verir; dosya yoksa ya da boşsa hiçbir şey vermez."""
    path = table_path(folder, name)
    if not path or path.stat().st_size == 0:
        return
    with open(path, newline="", encoding="utf-8-sig") as fh:
        yield from csv.DictReader(fh)


def _json(text: str):
    try:
        v = json.loads(text)
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, dict) else None


def _int(v) -> int | None:
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def local_day(ts: int) -> date:
    return date.fromtimestamp(ts)


def day_start(d: date) -> int:
    return int(datetime.combine(d, datetime.min.time()).timestamp())


def utc_label_day(ts: int) -> date:
    """Günlük kayıtların Time alanı: yerel günün tarihi, UTC gece yarısı olarak."""
    return datetime.fromtimestamp(ts, timezone.utc).date()


class Export:
    """Dökümün ayrıştırılmış hali; veritabanına dokunmaz."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.notes: list[str] = []
        self.skipped: Counter = Counter()
        self.tz_seen: Counter = Counter()
        self._fitness()
        self._aggregated()
        self._sport()
        for name, why in (("hlth_center_sport_track_data", "GPX yalnız uzak bağlantı olarak var; ağ kullanılmadı"),
                          ("user_fitness_data_records", "ikili nesne adları (seri/GPS dosyaları dökümde yok)")):
            n = sum(1 for _ in read_rows(folder, name))
            if n:
                self.skipped[f"{name}: {why}"] += n

    def _fitness(self) -> None:
        per_key: dict[str, dict[str, dict[int, dict]]] = defaultdict(lambda: defaultdict(dict))
        for r in read_rows(self.folder, "hlth_center_fitness_data"):
            t, v = _int(r.get("Time")), _json(r.get("Value"))
            if t is None or v is None:
                self.skipped["hlth_center_fitness_data: bozuk satır"] += 1
                continue
            if t < MIN_TS:
                self.skipped["hlth_center_fitness_data: 2020 öncesi zaman damgası (saat ayarsız)"] += 1
                continue
            per_key[r.get("Key", "")][r.get("Sid", "")][t] = v
        self.keys: dict[str, dict[int, dict]] = {}
        for key, by_sid in per_key.items():
            # aynı ana iki kaynak (bileklik + telefon) yazdıysa satırı çok olan kaynak kazanır
            merged: dict[int, dict] = {}
            dup = 0
            for sid in sorted(by_sid, key=lambda s: len(by_sid[s])):
                dup += sum(1 for t in by_sid[sid] if t in merged)
                merged.update(by_sid[sid])
            if dup:
                self.skipped[f"hlth_center_fitness_data/{key}: ikinci kaynaktan yinelenen dakika"] += dup
            self.keys[key] = merged
        for key in ("intensity", "vo2_max"):
            if self.keys.get(key):
                why = "karşılığı yok (günlük süre de aktarılmadı)" if key == "intensity" else "antrenman özetinde var"
                self.skipped[f"hlth_center_fitness_data/{key}: {why}"] += len(self.keys[key])

    def _aggregated(self) -> None:
        self.daily_report: dict[str, dict[date, dict]] = defaultdict(dict)
        for r in read_rows(self.folder, "hlth_center_aggregated_fitness_data"):
            t, v = _int(r.get("Time")), _json(r.get("Value"))
            if t is None or v is None or t < MIN_TS:
                continue
            if r.get("Tag") == "daily_report":
                self.daily_report[r.get("Key", "")][utc_label_day(t)] = v
            else:
                self.skipped[f"hlth_center_aggregated_fitness_data/{r.get('Tag')}:{r.get('Key')}: karşılığı yok"] += 1
        if self.daily_report.get("intensity"):
            self.skipped["hlth_center_aggregated_fitness_data/daily_report:intensity: karşılığı yok"] += len(
                self.daily_report["intensity"])

    def _sport(self) -> None:
        self.sports: list[dict] = []
        for r in read_rows(self.folder, "hlth_center_sport_record"):
            v = _json(r.get("Value"))
            if v is None or not _int(v.get("start_time") or r.get("Time")):
                self.skipped["hlth_center_sport_record: bozuk satır"] += 1
                continue
            v["_key"], v["_category"] = r.get("Key"), r.get("Category")
            v.setdefault("start_time", _int(r.get("Time")))
            self.sports.append(v)

    # --- dakikalar ------------------------------------------------------
    def minutes(self) -> dict[int, tuple]:
        k = self.keys
        hr, steps, cal = k.get("heart_rate", {}), k.get("steps", {}), k.get("calories", {})
        mom, light = k.get("body_momentum", {}), k.get("light_sensitivity_value", {})
        out = {}
        for ts in sorted(set(hr) | set(steps) | set(cal) | set(mom) | set(light)):
            if ts % 60:
                self.skipped["dakikaya oturmayan örnek"] += 1
                continue
            # bileklik her kaydettiği dakikaya hareket/ışık satırı yazar; o dakikada adım yoksa 0'dır
            # (saatten inen dosyada da böyle). Hareket satırı yoksa bilinmiyor (None) bırakılır.
            zero = 0 if ts in mom or ts in light else None
            st = steps.get(ts, {})
            bpm = _int(hr.get(ts, {}).get("bpm"))
            out[ts] = (ts, _int(st.get("steps")) if ts in steps else zero,
                       _int(cal[ts].get("calories")) if ts in cal else zero,
                       _int(st.get("distance")) if ts in steps else zero,
                       bpm if bpm and 0 < bpm < 255 else None, None, None, None,
                       _int(light[ts].get("light_sensitivity_value")) if ts in light else None,
                       _int(mom[ts].get("body_momentum")) if ts in mom else None)
        return out

    # --- günlük özet ----------------------------------------------------
    def daily(self, minutes: dict[int, tuple]) -> dict[str, dict]:
        k, rep = self.keys, self.daily_report
        by_day: dict[date, dict] = defaultdict(dict)
        for d, v in rep.get("steps", {}).items():
            if _int(v.get("steps")) is not None:
                by_day[d]["steps"] = int(v["steps"])
        for d, v in rep.get("calories", {}).items():
            if _int(v.get("calories")) is not None:
                by_day[d]["active_kcal"] = int(v["calories"])  # dakikalık "calories" toplamıyla aynı: etkin kcal
        for d, v in rep.get("heart_rate", {}).items():
            for src, dst in (("avg_rhr", "hr_rest"), ("max_hr", "hr_max"), ("min_hr", "hr_min"), ("avg_hr", "hr_avg")):
                if _int(v.get(src)):
                    by_day[d][dst] = int(v[src])
        for t, v in k.get("resting_heart_rate", {}).items():
            if _int(v.get("bpm")):
                by_day[utc_label_day(t)]["hr_rest"] = int(v["bpm"])
        stand: dict[date, set] = defaultdict(set)
        for t, v in k.get("valid_stand", {}).items():
            start = _int(v.get("start_time")) or t
            stand[local_day(start)].add(datetime.fromtimestamp(start).hour)
        for d, hours in stand.items():
            by_day[d]["standing_hours"] = sorted(hours)
        for t, v in k.get("training_load", {}).items():
            row = by_day[utc_label_day(t)]
            for src, dst in (("current_day_train_load", "load_day"), ("wtl_sum", "load_week"),
                             ("current_train_load_level", "load_level")):
                if _int(v.get(src)) is not None:
                    row[dst] = int(v[src])
        for t, v in k.get("vitality", {}).items():
            row = by_day[utc_label_day(t)]
            for src, dst in (("daily_low_intensity_vitality", "vitality_light"),
                             ("daily_medium_intensity_vitality", "vitality_moderate"),
                             ("daily_high_intensity_vitality", "vitality_high")):
                if _int(v.get(src)):
                    row[dst] = int(v[src])
            if _int(v.get("latest_accumulated_vitality")) is not None:
                row["vitality"] = int(v["latest_accumulated_vitality"])
        out = {}
        for d, row in sorted(by_day.items()):
            if not any(key in row for key in ("steps", "active_kcal", "hr_avg", "hr_rest")):
                continue
            start = day_start(d)
            # en yüksek/en düşük nabzın anı dökümde yok; dakikalık seride aynı değerin ilk görüldüğü dakika
            for key in ("hr_max", "hr_min"):
                if key in row:
                    hit = next((t for t in range(start, start + 86400, 60)
                                if t in minutes and minutes[t][4] == row[key]), None)
                    if hit:
                        row[key + "_ts"] = hit
            order = ("steps", "active_kcal", "hr_rest", "hr_max", "hr_max_ts", "hr_min", "hr_min_ts", "hr_avg",
                     "standing_hours", "load_day", "load_week", "load_level", "vitality_light",
                     "vitality_moderate", "vitality_high", "vitality")
            out[d.isoformat()] = {"ts": start, **{key: row[key] for key in order if key in row}, "source": SOURCE}
        return out

    # --- uyku -----------------------------------------------------------
    def sleeps(self, minutes: dict[int, tuple]) -> list[dict]:
        scores: dict[int, int] = {}
        for v in self.daily_report.get("sleep", {}).values():
            segs = [s for s in v.get("segment_details") or [] if isinstance(s, dict)]
            if segs and _int(v.get("sleep_score")):
                # puan güne aittir; günün en uzun parçasına yazılır
                best = max(segs, key=lambda s: _int(s.get("duration")) or 0)
                if _int(best.get("bedtime")):
                    scores[int(best["bedtime"])] = int(v["sleep_score"])
        out = []
        for t, v in sorted(self.keys.get("sleep", {}).items()):
            bed, wake = _int(v.get("bedtime")), _int(v.get("wake_up_time")) or t
            if not bed or not wake or wake <= bed or bed < MIN_TS:
                self.skipped["sleep: yatış/kalkış zamanı geçersiz"] += 1
                continue
            if v.get("timezone") is not None:
                self.tz_seen[(_int(v["timezone"]), int(datetime.fromtimestamp(wake).astimezone().utcoffset()
                                                            .total_seconds()) // 900)] += 1
            stages = []
            for it in v.get("items") or []:
                a, b = _int(it.get("start_time")), _int(it.get("end_time"))
                if a and b and b >= a:
                    stages.append({"ts": a, "end": b, "stage": SLEEP_STATES.get(_int(it.get("state")), "unknown")})
            stages.sort(key=lambda s: s["ts"])
            mins = lambda key: (_int(v.get(key)) or 0) * 60
            deep, light, rem, awake = (mins("sleep_deep_duration"), mins("sleep_light_duration"),
                                       mins("sleep_rem_duration"), mins("sleep_awake_duration"))
            duration = mins("duration") or deep + light + rem
            first = -(-bed // 60) * 60
            values = [(minutes[m][4] or 0) if m in minutes else 0 for m in range(first, wake, 60)]
            hr = {"start": first, "step": 60, "values": values} if any(values) else None
            row = {"bed_time": bed, "wake_time": wake, "into_bed_time": None, "is_awake": False,
                   "score": scores.get(bed), "duration": duration, "awake": awake, "light": light, "rem": rem,
                   "deep": deep, "sleep_index": None, "wake_count": _int(v.get("awake_count")), "stages": stages,
                   "hr": hr, "spo2": None, "snore": None, "breathing": [], "rr": [], "rmssd": None,
                   "complete": not v.get("is_uncomplete"), "version": _int(v.get("version")),
                   "source": SOURCE}
            for src, dst in (("avg_hr", "hr_avg"), ("min_hr", "hr_min"), ("max_hr", "hr_max")):
                if _int(v.get(src)):
                    row[dst] = int(v[src])
            out.append(row)
        return out

    # --- antrenman ------------------------------------------------------
    def workouts(self) -> list[dict]:
        out = []
        for v in self.sports:
            start = int(v["start_time"])
            if start < MIN_TS:
                continue
            if v.get("timezone") is not None:
                self.tz_seen[(_int(v["timezone"]), int(datetime.fromtimestamp(start).astimezone().utcoffset()
                                                            .total_seconds()) // 900)] += 1
            sport = SPORT_TYPES.get(_int(v.get("sport_type"))) or v.get("_category") or v.get("_key") or "unknown"
            s = {"start": start, "sport": sport, "sport_code": _int(v.get("proto_type")),
                 "version": _int(v.get("version")), "tz": _int(v.get("timezone"))}
            zones = {dst: _int(v.get(src)) or 0 for src, dst in (
                ("hrm_warm_up_duration", "warm_up"), ("hrm_fat_burning_duration", "fat_burn"),
                ("hrm_aerobic_duration", "aerobic"), ("hrm_anaerobic_duration", "anaerobic"),
                ("hrm_extreme_duration", "extreme"))}
            fields = (  # (dökümdeki ad, bizdeki ad, çarpan)
                ("end_time", "end", 1), ("duration", "duration", 1), ("distance", "distance", 1),
                ("calories", "calories", 1), ("total_cal", "calories_total", 1), ("avg_hrm", "hr_avg", 1),
                ("max_hrm", "hr_max", 1), ("min_hrm", "hr_min", 1), ("avg_pace", "pace_avg", 1),
                ("max_pace", "pace_max", 1), ("min_pace", "pace_min", 1), ("avg_speed", "speed_avg", 1 / 3.6),
                ("max_speed", "speed_max", 1 / 3.6), ("steps", "steps", 1), ("avg_cadence", "cadence_avg", 1),
                ("max_cadence", "cadence_max", 1), ("avg_stride", "stride_avg", 0.01),
                ("train_effect", "training_effect_aerobic", 1),
                ("anaerobic_train_effect", "training_effect_anaerobic", 1), ("vo2_max", "vo2max", 1),
                ("recover_time", "recovery_time", 3600), ("train_load", "training_load", 1),
                ("vitality", "vitality_gain", 1), ("avg_touchdown_duration", "ground_contact_time_avg_ms", 1),
                ("avg_vertical_amplitude", "vertical_oscillation_avg", 1),
                ("avg_vertical_stride_ratio", "vertical_ratio_avg", 1),
                ("rise_height", "elevation_gain", 1), ("fall_height", "elevation_loss", 1))
            for src, dst, scale in fields:
                x = v.get(src)
                if not isinstance(x, (int, float)) or isinstance(x, bool):
                    continue
                if x == 0 and dst not in ("training_load", "vitality_gain"):
                    continue  # saat dosyası çözücüsü de 0 değerli alanları atar
                x = x * scale if scale != 1 else x
                s[dst] = round(x, 3) if isinstance(x, float) else x
            if sum(zones.values()):
                s["hr_zones"] = zones
            if s.get("duration") and s.get("distance"):
                s.setdefault("pace_avg", round(s["duration"] / s["distance"] * 1000))
                s.setdefault("speed_avg", round(s["distance"] / s["duration"], 3))
            s["source"] = SOURCE
            out.append(s)
        return sorted(out, key=lambda s: s["start"])

    def manual(self) -> list[tuple]:
        out = []
        for t, v in sorted(self.keys.get("single_heart_rate", {}).items()):
            bpm = _int(v.get("bpm"))
            if bpm and 0 < bpm < 255:
                out.append((t, "hr", bpm))
        return out

    def weights(self) -> list[tuple]:
        out = []
        for t, v in sorted(self.keys.get("weight", {}).items()):
            kg = v.get("weight")
            if isinstance(kg, (int, float)) and 20 <= kg <= 400:
                out.append((t, float(kg)))
        return out


# --- veritabanı ---------------------------------------------------------------
SCHEMA = (  # store.py ve insights.py ile aynı tanımlar; yalnız eksikse oluşturulur
    "CREATE TABLE IF NOT EXISTS minutes (ts INTEGER PRIMARY KEY, steps INTEGER, kcal INTEGER,"
    " dist INTEGER, hr INTEGER, spo2 INTEGER, stress INTEGER)",
    "CREATE TABLE IF NOT EXISTS daily (day TEXT PRIMARY KEY, data TEXT)",
    "CREATE TABLE IF NOT EXISTS sleep (bed_time INTEGER PRIMARY KEY, wake_time INTEGER, day TEXT,"
    " duration INTEGER, deep INTEGER, light INTEGER, rem INTEGER, awake INTEGER, score INTEGER,"
    " rmssd REAL, data TEXT)",
    "CREATE INDEX IF NOT EXISTS sleep_day ON sleep (day)",
    "CREATE TABLE IF NOT EXISTS workouts (start INTEGER PRIMARY KEY, end INTEGER, sport TEXT,"
    " duration INTEGER, distance INTEGER, calories INTEGER, hr_avg INTEGER, hr_max INTEGER,"
    " summary TEXT, series TEXT, track TEXT)",
    "CREATE TABLE IF NOT EXISTS manual (ts INTEGER, type TEXT, value INTEGER, PRIMARY KEY (ts, type))",
    "CREATE TABLE IF NOT EXISTS weights (ts INTEGER PRIMARY KEY, kg REAL)",
    "CREATE TABLE IF NOT EXISTS import_log (source TEXT NOT NULL, tbl TEXT NOT NULL, key TEXT NOT NULL,"
    " crc INTEGER, imported INTEGER, PRIMARY KEY (source, tbl, key)) WITHOUT ROWID",
)
# tablo -> (anahtar sütunları, CRC'ye giren sütunlar)
TABLES = {
    "minutes": (("ts",), MINUTE_COLS),
    "daily": (("day",), ("day", "data")),
    "sleep": (("bed_time",), ("bed_time", "wake_time", "day", "duration", "deep", "light", "rem", "awake",
                              "score", "rmssd", "data")),
    "workouts": (("start",), ("start", "end", "sport", "duration", "distance", "calories", "hr_avg", "hr_max",
                              "summary", "series", "track")),
    "manual": (("ts", "type"), ("ts", "type", "value")),
    "weights": (("ts",), ("ts", "kg")),
}


def crc(row) -> int:
    return zlib.crc32(json.dumps(list(row), separators=(",", ":")).encode())


def ensure_schema(db: sqlite3.Connection) -> None:
    for stmt in SCHEMA:
        db.execute(stmt)
    have = {r[1] for r in db.execute("PRAGMA table_info(minutes)")}
    for col in ("energy", "light", "moment"):
        if col not in have:
            db.execute(f"ALTER TABLE minutes ADD COLUMN {col} INTEGER")


def _key_text(key: tuple) -> str:
    return "|".join(str(k) for k in key)


def insert_rows(db, table: str, rows: list[tuple], now: int) -> int:
    """Anahtarı tabloda olmayan satırları ekler ve günlüğe yazar. Eklenen satır sayısını döndürür."""
    keys, cols = TABLES[table]
    idx = [cols.index(k) for k in keys]
    if len(keys) == 1:
        existing = {r[0] for r in db.execute(f"SELECT {keys[0]} FROM {table}")}
        fresh = [r for r in rows if r[idx[0]] not in existing]
    else:
        existing = set(db.execute(f"SELECT {', '.join(keys)} FROM {table}"))
        fresh = [r for r in rows if tuple(r[i] for i in idx) not in existing]
    db.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", fresh)
    db.executemany("INSERT OR REPLACE INTO import_log VALUES (?,?,?,?,?)",
                   [(SOURCE, table, _key_text(tuple(r[i] for i in idx)), crc(r), now) for r in fresh])
    return len(fresh)


def overlaps(spans: list[tuple[int, int]], a: int, b: int) -> bool:
    return any(a < y and x < b for x, y in spans)


def run_import(db: sqlite3.Connection, ex: Export, now: int) -> dict:
    stats = {}
    minutes = ex.minutes()
    stats["minutes"] = (len(minutes), insert_rows(db, "minutes", list(minutes.values()), now))

    daily = ex.daily(minutes)
    stats["daily"] = (len(daily), insert_rows(
        db, "daily", [(d, json.dumps(v)) for d, v in daily.items()], now))

    # uyku: aynı geceye ait (aralığı çakışan) bir kayıt varsa saatinki kalır
    spans = [(a, b) for a, b in db.execute("SELECT bed_time, wake_time FROM sleep") if a and b]
    rows = []
    for s in ex.sleeps(minutes):
        if overlaps(spans, s["bed_time"], s["wake_time"]):
            continue
        spans.append((s["bed_time"], s["wake_time"]))
        rows.append((s["bed_time"], s["wake_time"], local_day(s["wake_time"]).isoformat(), s["duration"],
                     s["deep"], s["light"], s["rem"], s["awake"], s["score"], s["rmssd"], json.dumps(s)))
    total = len(ex.keys.get("sleep", {}))
    stats["sleep"] = (total, insert_rows(db, "sleep", rows, now))

    spans = [(a, b or a + 1) for a, b in db.execute("SELECT start, end FROM workouts")]
    rows = []
    for w in ex.workouts():
        end = w.get("end") or w["start"] + (w.get("duration") or 1)
        if overlaps(spans, w["start"], end):
            continue
        spans.append((w["start"], end))
        rows.append((w["start"], w.get("end"), w["sport"], w.get("duration"), w.get("distance"),
                     w.get("calories"), w.get("hr_avg"), w.get("hr_max"), json.dumps(w), None, None))
    stats["workouts"] = (len(ex.sports), insert_rows(db, "workouts", rows, now))

    manual = ex.manual()
    stats["manual"] = (len(manual), insert_rows(db, "manual", manual, now))
    weights = ex.weights()
    stats["weights"] = (len(weights), insert_rows(db, "weights", weights, now))
    return stats


def run_undo(db: sqlite3.Connection) -> dict:
    """Günlükteki satırları, içerikleri aktarıldığı gibi duruyorsa siler. {tablo: (silinen, bırakılan)}"""
    stats = {}
    for table, (keys, cols) in TABLES.items():
        logged = db.execute("SELECT key, crc FROM import_log WHERE source = ? AND tbl = ?",
                            (SOURCE, table)).fetchall()
        if not logged:
            continue
        idx = [cols.index(k) for k in keys]
        current = {_key_text(tuple(r[i] for i in idx)): crc(r)
                   for r in db.execute(f"SELECT {', '.join(cols)} FROM {table}")}
        gone = [k for k, c in logged if current.get(k) == c]
        kept = sum(1 for k, c in logged if k in current and current[k] != c)
        where = " AND ".join(f"{k} = ?" for k in keys)
        db.executemany(f"DELETE FROM {table} WHERE {where}", [tuple(k.split("|", len(keys) - 1)) for k in gone])
        db.execute("DELETE FROM import_log WHERE source = ? AND tbl = ?", (SOURCE, table))
        stats[table] = (len(gone), kept)
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Mi Fitness dökümünü band.db'ye aktarır (saat verisi korunur).")
    ap.add_argument("export_dir", type=Path)
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true", help="hesapla ve say, hiçbir şey yazma")
    ap.add_argument("--undo", action="store_true", help="bu aracın eklediği satırları sil")
    args = ap.parse_args(argv)
    if not args.db.is_file():
        print(f"veritabanı yok: {args.db}", file=sys.stderr)
        return 2
    if not args.undo and not args.export_dir.is_dir():
        print(f"döküm klasörü yok: {args.export_dir}", file=sys.stderr)
        return 2

    ex = None if args.undo else Export(args.export_dir)  # kilidi almadan önce her şeyi oku
    db = sqlite3.connect(args.db, timeout=60, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        ensure_schema(db)
        mode = " (dry-run, yazılmadı)" if args.dry_run else ""
        if args.undo:
            for table, (gone, kept) in run_undo(db).items():
                print(f"{table:9s} silindi {gone:7d}   sonradan değiştiği için bırakıldı {kept}")
            print("geri alma tamam" + mode)
        else:
            now = int(datetime.now().timestamp())
            stats = run_import(db, ex, now)
            for table, (seen, added) in stats.items():
                print(f"{table:9s} dökümde {seen:7d}   eklendi {added:7d}   zaten vardı/çakıştı {seen - added:7d}")
            for (theirs, ours), n in sorted(ex.tz_seen.items(), key=str):
                if theirs != ours:
                    print(f"UYARI: {n} kayıtta dökümün saat dilimi ({theirs}×15 dk) sistemle ({ours}×15 dk) "
                          "uyuşmuyor; gün sınırları sistem saatine göre hesaplandı")
            print("atlananlar:")
            for why, n in sorted(ex.skipped.items()):
                print(f"  {n:7d}  {why}")
            print("aktarma tamam" + mode)
        db.execute("ROLLBACK" if args.dry_run else "COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
