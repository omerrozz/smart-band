"""Kayıt katmanı: SQLite tabloları ve ham dosyaların işlenmesi.

server.py'den ayrıdır ki ayarları okumadan, saate bağlanmadan içe aktarılabilsin (testler için).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import i18n
import parse
import parse_more


# --- kayıt ------------------------------------------------------------
class Store:
    def __init__(self, path: Path, raw: Path):
        self.raw = raw  # ham dosyaların klasörü
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS samples ("
            "ts INTEGER PRIMARY KEY, steps INTEGER, calories INTEGER, hr INTEGER, battery INTEGER)"
        )
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS minutes (ts INTEGER PRIMARY KEY, steps INTEGER, kcal INTEGER,"
            " dist INTEGER, hr INTEGER, spo2 INTEGER, stress INTEGER);"
            "CREATE TABLE IF NOT EXISTS daily (day TEXT PRIMARY KEY, data TEXT);"
            "CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, kind TEXT, ts INTEGER, size INTEGER,"
            " fetched INTEGER, parsed INTEGER);"
            # uyku: gece başına bir satır; data = evreler, örnek dizileri, RR aralıkları (JSON)
            "CREATE TABLE IF NOT EXISTS sleep (bed_time INTEGER PRIMARY KEY, wake_time INTEGER, day TEXT,"
            " duration INTEGER, deep INTEGER, light INTEGER, rem INTEGER, awake INTEGER, score INTEGER,"
            " rmssd REAL, data TEXT);"
            "CREATE INDEX IF NOT EXISTS sleep_day ON sleep (day);"
            # antrenman: özet, saniyelik seri ve GPS izi ayrı dosyalardan gelir, başlangıç zamanı ortaktır
            "CREATE TABLE IF NOT EXISTS workouts (start INTEGER PRIMARY KEY, end INTEGER, sport TEXT,"
            " duration INTEGER, distance INTEGER, calories INTEGER, hr_avg INTEGER, hr_max INTEGER,"
            " summary TEXT, series TEXT, track TEXT);"
            "CREATE TABLE IF NOT EXISTS manual (ts INTEGER, type TEXT, value INTEGER, PRIMARY KEY (ts, type));"
            # bilekte olma durumu: saat her bildirdiğinde bir satır; worn NULL = bağlantı koptu, sonrası bilinmiyor
            "CREATE TABLE IF NOT EXISTS wear (ts INTEGER PRIMARY KEY, worn INTEGER);"
            # uygulamadan başlatılan, henüz bitmemiş antrenman: servis yeniden başlarsa buradan sürer
            "CREATE TABLE IF NOT EXISTS app_session (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT);"
            "CREATE TABLE IF NOT EXISTS app_samples (n INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT);"
            "CREATE TABLE IF NOT EXISTS app_track (n INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT);"
            # takvim: kullanıcının uygulamada girdiği etkinlikler; start ve end Unix saniyesi
            "CREATE TABLE IF NOT EXISTS calendar (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, location TEXT,"
            " note TEXT, start INTEGER, end INTEGER, all_day INTEGER);"
            # servis yeniden başlayınca hatırlanacak küçük değerler (son hava durumu, saate yazılan takvim)
            "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);"
        )
        self.db.commit()

    def file_size(self, fid: str):
        with self.lock:
            row = self.db.execute("SELECT size FROM files WHERE id = ?", (fid,)).fetchone()
        return row[0] if row else None

    def save_file(self, fid: bytes, data: bytes) -> str:
        """Ham dosyayı diske yazar, çözebildiğini tablolara işler. Türü döndürür."""
        info = parse.file_id(data)
        kind, parsed = parse.parse(data)
        name = f"{datetime.fromtimestamp(info['ts']):%Y%m%dT%H%M%S}_{kind}_v{info['version']}.bin"
        (self.raw / name).write_bytes(data)
        with self.lock:
            if kind == "daily_details" and parsed:
                self.db.executemany(
                    "INSERT OR REPLACE INTO minutes VALUES (:ts,:steps,:kcal,:dist,:hr,:spo2,:stress,:energy,:light,:moment)",
                    parsed)
            elif kind == "daily_summary" and parsed:
                day = date.fromtimestamp(parsed["ts"]).isoformat()
                self.db.execute("INSERT OR REPLACE INTO daily VALUES (?,?)", (day, json.dumps(parsed)))
            elif parsed is not None:
                self._ingest(kind, info, parsed)
            self.db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)",
                            (fid.hex(), kind, info["ts"], len(data), int(time.time()), int(parsed is not None)))
            self.db.commit()
        return kind

    def _ingest(self, kind: str, info: dict, parsed) -> None:
        """Uyku, antrenman ve elle ölçüm dosyalarını tablolara işler. Kilit çağıranda; commit etmez."""
        db = self.db
        if kind.startswith("sleep"):
            if not parsed.get("bed_time") or not parsed.get("wake_time"):
                return
            old = db.execute("SELECT wake_time, data FROM sleep WHERE bed_time = ?", (parsed["bed_time"],)).fetchone()
            if old:
                # aynı gecenin daha geç biten kaydı varsa ya da eldeki daha ayrıntılıysa dokunma
                if old[0] > parsed["wake_time"]:
                    return
                prev = json.loads(old[1])
                if old[0] == parsed["wake_time"] and prev.get("stages") and (
                        not parsed["stages"] or (parsed.get("stages_inferred") and not prev.get("stages_inferred"))):
                    return  # gerçek evreli kayıt, evresi tahmin edilmiş kayıtla ezilmez
            day = date.fromtimestamp(parsed["wake_time"]).isoformat()
            db.execute("INSERT OR REPLACE INTO sleep VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (parsed["bed_time"], parsed["wake_time"], day, parsed["duration"], parsed["deep"],
                        parsed["light"], parsed["rem"], parsed["awake"], parsed["score"], parsed["rmssd"],
                        json.dumps(dict(parsed, source=f"{kind} v{info['version']}"))))
        elif kind == "manual_details":
            db.executemany("INSERT OR REPLACE INTO manual VALUES (:ts,:type,:value)", parsed)
        elif kind.startswith("workout"):
            start = info["ts"]
            # aynı saniyede başlamış uygulama kaydı saatin dosyasıyla karışmasın
            row = db.execute("SELECT summary FROM workouts WHERE start = ?", (start,)).fetchone()
            if row and self._is_app(row[0]):
                db.execute("DELETE FROM workouts WHERE start = ?", (start,))
            db.execute("INSERT OR IGNORE INTO workouts (start) VALUES (?)", (start,))
            if kind == "workout_summary":
                db.execute("UPDATE workouts SET end=?, sport=?, duration=?, distance=?, calories=?, hr_avg=?,"
                           " hr_max=?, summary=? WHERE start=?",
                           (parsed.get("end"), parsed["sport"], parsed.get("duration"), parsed.get("distance"),
                            parsed.get("calories"), parsed.get("hr_avg"), parsed.get("hr_max"),
                            json.dumps(parsed), start))
                self._drop_app_overlaps()
            elif kind == "workout_details":
                db.execute("UPDATE workouts SET series=? WHERE start=?",
                           (json.dumps(parse_more.series(parsed)) if parsed else None, start))
            elif kind == "workout_gps":
                db.execute("UPDATE workouts SET track=? WHERE start=?",
                           (json.dumps(parsed) if parsed else None, start))

    def reparse(self) -> int:
        """Ham klasördeki uyku/antrenman/elle ölçüm dosyalarından henüz işlenmemiş olanları işler.

        Çözücü yazılmadan önce inmiş dosyalar için; files tablosunda parsed=1 olanlar atlanır.
        İşlenen dosya sayısını döndürür.
        """
        done = 0
        with self.lock:
            known = dict(self.db.execute("SELECT id, parsed FROM files"))
            for pattern in ("*_sleep_*.bin", "*_workout_*.bin", "*_manual_*.bin"):
                for f in sorted(self.raw.glob(pattern)):
                    data = f.read_bytes()
                    if len(data) < 13 or known.get(data[:7].hex()) == 1:
                        continue
                    info = parse.file_id(data)
                    kind, parsed = parse.parse(data)
                    if parsed is None:
                        continue
                    self._ingest(kind, info, parsed)
                    self.db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,1)",
                                    (data[:7].hex(), kind, info["ts"], len(data), int(f.stat().st_mtime)))
                    done += 1
            self.db.commit()
        return done

    # --- uyku ---------------------------------------------------------
    def sleep(self, day: date, rr: bool = False) -> dict | None:
        """O yerel günde BİTEN gece (birden çoksa en uzunu); yoksa None. Biçim için /api/sleep'e bak."""
        with self.lock:
            rows = self.db.execute("SELECT data FROM sleep WHERE day = ?", (day.isoformat(),)).fetchall()
        if not rows:
            return None
        nights = [json.loads(r[0]) for r in rows]
        d = max(nights, key=lambda n: (n["duration"] or 0, n["wake_time"] - n["bed_time"]))
        out = {"date": day.isoformat(), **d, "hrv": parse_more.hrv_series(d["rr"]), "naps": len(nights) - 1}
        if not rr:
            del out["rr"]
        return out

    # --- antrenman ----------------------------------------------------
    @staticmethod
    def _workout_row(start, summary, has_series, has_gps) -> dict:
        out = json.loads(summary) if summary else {"start": start, "sport": None}
        return {"id": start, **out, "has_series": bool(has_series), "has_gps": bool(has_gps)}

    def workouts(self) -> list[dict]:
        """Yeniden eskiye antrenman özetleri."""
        with self.lock:
            rows = self.db.execute("SELECT start, summary, series IS NOT NULL, track IS NOT NULL FROM workouts"
                                   " ORDER BY start DESC").fetchall()
        return [self._workout_row(*r) for r in rows]

    def workout(self, start: int) -> dict | None:
        """Özet + saniyelik seri + GPS izi (sütunlar halinde; nabız seriden eşlenir)."""
        with self.lock:
            row = self.db.execute("SELECT start, summary, series, track FROM workouts WHERE start = ?",
                                  (start,)).fetchone()
        if not row:
            return None
        series = json.loads(row[2]) if row[2] else None
        points = json.loads(row[3]) if row[3] else None
        out = self._workout_row(row[0], row[1], series, points)
        out["series"] = series
        out["track"] = None
        if points:
            hr = dict(zip(series["ts"], series["hr"])) if series else {}
            track = {"ts": [p["ts"] for p in points], "lat": [p["lat"] for p in points],
                     "lon": [p["lon"] for p in points]}
            for key in ("speed", "alt", "accuracy"):
                if any(key in p for p in points):
                    track[key] = [p.get(key) for p in points]
            if hr or any(p.get("hr") for p in points):  # telefon GPS'li kayıtta nabız noktanın içindedir
                track["hr"] = [hr.get(p["ts"]) or p.get("hr") for p in points]
            out["track"] = track
        return out

    # --- uygulamadan başlatılan antrenman -------------------------------
    APP_OVERLAP = 60  # saatin kaydıyla en az bu kadar saniye çakışan uygulama kaydı saklanmaz

    @staticmethod
    def _is_app(summary) -> bool:
        return bool(summary) and json.loads(summary).get("source") == "app"

    def _clash(self, start: int, end: int):
        """[start, end] aralığıyla en az APP_OVERLAP saniye çakışan, uygulamadan gelmeyen kaydın başlangıcı."""
        for b_start, b_end, b_dur, summary in self.db.execute(
                "SELECT start, end, duration, summary FROM workouts WHERE start < ? ORDER BY start DESC", (end,)):
            if summary is None or self._is_app(summary):
                continue  # özeti henüz inmemiş kayıt, özeti gelince yeniden bakılır
            b_end = b_end or b_start + (b_dur or 0)
            if min(end, b_end) - max(start, b_start) >= self.APP_OVERLAP:
                return b_start
        return None

    def _drop_app_overlaps(self) -> int:
        """Çakışma kuralı: saatin (ya da Mi Fitness'ın) kaydı her zaman kalır.

        Uygulamadan kaydedilmiş bir antrenman, uygulamadan gelmeyen bir kayıtla en az APP_OVERLAP (60) saniye
        çakışıyorsa silinir; çakışmıyorsa ikisi de kalır. Saatin özeti indiğinde, servis açılırken ve uygulama
        kaydı yazılmadan önce uygulanır. Kilit çağıranda; commit etmez. Silinen kayıt sayısını döndürür.
        """
        gone = 0
        for start, end, summary in self.db.execute("SELECT start, end, summary FROM workouts").fetchall():
            if self._is_app(summary) and self._clash(start, end or start) is not None:
                self.db.execute("DELETE FROM workouts WHERE start = ?", (start,))
                gone += 1
        return gone

    def drop_app_overlaps(self) -> int:
        with self.lock:
            gone = self._drop_app_overlaps()
            self.db.commit()
        return gone

    def save_app_workout(self, summary: dict, series: dict, track: list | None = None):
        """Uygulama kaydını yazar ve None döndürür; saatin kaydıyla çakışıyorsa yazmaz, o kaydın kimliğini döndürür.

        track: telefonun GPS'inden gelen iz (saat kayıtlarındaki biçimde nokta listesi) ya da None.
        """
        with self.lock:
            clash = self._clash(summary["start"], summary["end"])
            if clash is not None:
                return clash
            self.db.execute("INSERT OR REPLACE INTO workouts (start, end, sport, duration, distance, calories, hr_avg,"
                            " hr_max, summary, series, track) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (summary["start"], summary["end"], summary["sport"], summary["duration"],
                             summary.get("distance"), summary["calories"], summary["hr_avg"], summary["hr_max"],
                             json.dumps(summary), json.dumps(series), json.dumps(track) if track else None))
            self.db.commit()
        return None

    # --- takvim ve küçük değerler ---
    def calendar_list(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT id, title, location, note, start, end, all_day FROM calendar"
                                   " ORDER BY start, id").fetchall()
        return [{"id": r[0], "title": r[1], "location": r[2] or "", "note": r[3] or "", "start": r[4], "end": r[5],
                 "all_day": bool(r[6])} for r in rows]

    def calendar_save(self, e: dict) -> int | None:
        """Ekler (id None) ya da günceller; güncellenecek kayıt yoksa None döner."""
        row = (e["title"], e.get("location") or "", e.get("note") or "", int(e["start"]), int(e["end"]),
               int(bool(e.get("all_day"))))
        with self.lock:
            if e.get("id") is None:
                eid = self.db.execute("INSERT INTO calendar (title, location, note, start, end, all_day)"
                                      " VALUES (?,?,?,?,?,?)", row).lastrowid
            else:
                done = self.db.execute("UPDATE calendar SET title=?, location=?, note=?, start=?, end=?, all_day=?"
                                       " WHERE id=?", row + (int(e["id"]),)).rowcount
                eid = int(e["id"]) if done else None
            self.db.commit()
        return eid

    def calendar_delete(self, eid: int) -> bool:
        with self.lock:
            done = self.db.execute("DELETE FROM calendar WHERE id = ?", (int(eid),)).rowcount
            self.db.commit()
        return bool(done)

    def calendar_prune(self, now: int | None = None) -> int:
        """Bitmiş etkinlikleri siler; silinen sayısını döner."""
        with self.lock:
            done = self.db.execute("DELETE FROM calendar WHERE end <= ?", (int(now or time.time()),)).rowcount
            if done:
                self.db.commit()
        return done

    # --- olay günlüğü: bağlantı, eşitleme ve servis olayları (gece ne olduğunu sonradan görmek için) ---
    def log_event(self, kind: str, detail: str = "") -> None:
        try:
            with self.lock:
                self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, detail TEXT)")
                self.db.execute("INSERT INTO events (ts, kind, detail) VALUES (?,?,?)", (int(time.time()), kind, str(detail)[:200]))
                self.db.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - 3000")
                self.db.commit()
        except Exception:  # günlük hiçbir işi durdurmamalı
            pass

    def events(self, limit: int = 300) -> list[dict]:
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, detail TEXT)")
            rows = self.db.execute("SELECT ts, kind, detail FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": ts, "kind": kind, "detail": detail} for ts, kind, detail in rows]

    def kv_get(self, key: str, default=None):
        with self.lock:
            row = self.db.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def kv_set(self, key: str, value) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))
            self.db.commit()

    def app_session_save(self, session: dict, reset: bool = False) -> None:
        with self.lock:
            if reset:
                self.db.execute("DELETE FROM app_samples")
                self.db.execute("DELETE FROM app_track")
            self.db.execute("INSERT OR REPLACE INTO app_session VALUES (1, ?)", (json.dumps(session),))
            self.db.commit()

    def app_sample_add(self, row: list) -> None:
        """Commit etmez: her okumanın ardından gelen app_session_save ile birlikte yazılır."""
        with self.lock:
            self.db.execute("INSERT INTO app_samples (data) VALUES (?)", (json.dumps(row),))

    def app_session_load(self):
        """(oturum, örnekler) ya da None."""
        with self.lock:
            row = self.db.execute("SELECT data FROM app_session WHERE id = 1").fetchone()
            rows = self.db.execute("SELECT data FROM app_samples ORDER BY n").fetchall()
        return (json.loads(row[0]), [json.loads(r[0]) for r in rows]) if row else None

    def app_track_add(self, point: dict) -> None:
        """Süren antrenmanın GPS noktası. Commit etmez: ardından gelen app_session_save ile yazılır."""
        with self.lock:
            self.db.execute("INSERT INTO app_track (data) VALUES (?)", (json.dumps(point),))

    def app_track_load(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT data FROM app_track ORDER BY n").fetchall()
        return [json.loads(r[0]) for r in rows]

    def app_session_clear(self) -> None:
        with self.lock:
            self.db.execute("DELETE FROM app_session")
            self.db.execute("DELETE FROM app_samples")
            self.db.execute("DELETE FROM app_track")
            self.db.commit()

    def workout_gpx(self, start: int) -> str | None:
        """Antrenmanın GPX metni; GPS izi yoksa None."""
        with self.lock:
            row = self.db.execute("SELECT sport, series, track FROM workouts WHERE start = ?", (start,)).fetchone()
        if not row or not row[2]:
            return None
        points = json.loads(row[2])
        if not any(p["lat"] or p["lon"] for p in points):
            return None
        series = json.loads(row[1]) if row[1] else None
        hr = {t: h for t, h in zip(series["ts"], series["hr"]) if h} if series else {}
        name = f"{row[0] or 'workout'} {datetime.fromtimestamp(start):%Y-%m-%d %H:%M}"
        return parse_more.gpx(name, points, hr)

    # --- elle ölçümler --------------------------------------------------
    def manual(self, day: date) -> list[dict]:
        start = int(datetime.combine(day, datetime.min.time()).timestamp())
        end = int(datetime.combine(day + timedelta(days=1), datetime.min.time()).timestamp())
        with self.lock:
            rows = self.db.execute("SELECT ts, type, value FROM manual WHERE ts >= ? AND ts < ? ORDER BY ts",
                                   (start, end)).fetchall()
        return [{"ts": ts, "type": t, "value": v} for ts, t, v in rows]

    def add_wear(self, worn: bool | None, ts: int | None = None) -> None:
        """Saatin bildirdiği bilekte olma durumu. None: bağlantı koptu, bundan sonrası bilinmiyor."""
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO wear VALUES (?,?)",
                            (ts or int(time.time()), None if worn is None else int(worn)))
            self.db.commit()

    def off_wrist(self, day: date, now: int | None = None) -> list[list[int]]:
        """Saatin o gün bilekte olmadığını bildirdiği aralıklar, günün dakikası olarak [başlangıç, bitiş).

        Bir aralık "bilekte değil" bildirimiyle başlar, sonraki bildirimle (bileğe takıldı ya da bağlantı
        koptu) biter. Bağlantı yokken geçen süre hakkında hiçbir şey söylenmez.
        """
        start = int(datetime.combine(day, datetime.min.time()).timestamp())
        end = min(start + 86400, now or int(time.time()))
        with self.lock:
            before = self.db.execute("SELECT ts, worn FROM wear WHERE ts < ? ORDER BY ts DESC LIMIT 1",
                                     (start,)).fetchall()
            rows = before + self.db.execute("SELECT ts, worn FROM wear WHERE ts >= ? AND ts < ? ORDER BY ts",
                                            (start, start + 86400)).fetchall()
        out, since = [], None
        for ts, worn in rows:
            if since is not None and worn != 0:
                out.append([since, ts])
                since = None
            elif since is None and worn == 0:
                since = ts
        if since is not None:
            out.append([since, end])
        spans = [[max(0, (a - start) // 60), min(1440, -(-(b - start) // 60))] for a, b in out if b > start and a < end]
        return [s for s in spans if s[1] > s[0]]

    def minutes(self, day: date) -> dict:
        start = int(datetime.combine(day, datetime.min.time()).timestamp())
        cols = ("steps", "kcal", "dist", "hr", "spo2", "stress", "energy", "light", "moment")
        out = {c: [None] * 1440 for c in cols}
        with self.lock:
            rows = self.db.execute(
                f"SELECT ts, {', '.join(cols)} FROM minutes WHERE ts >= ? AND ts < ? ORDER BY ts", (start, start + 86400)
            ).fetchall()
            summary = self.db.execute("SELECT data FROM daily WHERE day = ?", (day.isoformat(),)).fetchone()
        for ts, *vals in rows:
            i = (ts - start) // 60
            for c, v in zip(cols, vals):
                out[c][i] = v
        # kullanıcının "Canlı ölç" ile aldığı okumalar, saatin o dakika için kaydı yoksa boşluğu doldurur;
        # hangi dakikaların canlı okumadan geldiği ayrıca bildirilir
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS live_hr (ts INTEGER PRIMARY KEY, hr INTEGER)")
            live = self.db.execute(
                "SELECT ts, hr FROM live_hr WHERE ts >= ? AND ts < ? ORDER BY ts", (start, start + 86400)
            ).fetchall()
        from_live = set()
        for ts, h in live:
            i = (ts - start) // 60
            if not out["hr"][i] or i in from_live:
                out["hr"][i] = h
                from_live.add(i)
        out["hr_live"] = sorted(from_live)
        out["hr_sleep"] = self._fill_sleep_hr(out["hr"], day, start)
        sm = self._mend_summary(json.loads(summary[0]) if summary else None, out["hr"])
        return {"date": day.isoformat(), **out, "summary": self._movement_kcal(sm, out, start)}

    def _fill_sleep_hr(self, hr: list, day: date, start: int) -> list[int]:
        """Uyku süresince saat dakikalık nabzı gün dosyasına değil uyku dosyasına yazar (22 Kasım 2026 gecesi görüldü:
        01:39–09:15 arası gün kaydı boş, uyku kaydında dakikalık nabız tam). Boş dakikalar uyku kaydından doldurulur;
        doldurulan dakikaların sırası döner. Gece yarısını aşan uyku için ertesi günde biten gece de bakılır."""
        filled = set()
        for d in (day, day + timedelta(days=1)):
            try:
                night = self.sleep(d)
            except Exception:
                night = None
            ser = (night or {}).get("hr") or {}
            step = ser.get("step") or 60
            for k, v in enumerate(ser.get("values") or []):
                i = (ser["start"] + k * step - start) // 60
                if 0 <= i < len(hr) and v and not hr[i]:
                    hr[i] = v
                    filled.add(i)
        return sorted(filled)

    def _movement_kcal(self, sm: dict | None, out: dict, start: int) -> dict | None:
        """Aktif kaloriyi yalnızca hareketten sayar.

        Saat, otururken yükselen nabzı da efor gibi kalorilendirir. Nabızdan enerji hesabı düşük yoğunlukta
        güvenilir değildir (flex nabız yöntemi: eşiğin altındaki dakikalar dinlenik harcamayla sayılır). Bu yüzden
        yalnızca adım atılan dakikaların kalorisi ile o günkü antrenmanların kalorisi toplanır; antrenman süresindeki
        dakikalar iki kez sayılmaz. Saatin kendi değeri active_band olarak kalır. Dakikalık kaydı olmayan günlerde
        (örn. Mi Fitness'tan gelenler) saatin değeri aynen bırakılır.
        """
        recorded = [i for i in range(1440) if out["kcal"][i] is not None]
        if sm is None or len(recorded) < 60:
            return sm
        with self.lock:
            rows = self.db.execute("SELECT start, summary FROM workouts WHERE start >= ? AND start < ?",
                                   (start, start + 86400)).fetchall()
        inside, extra = set(), 0
        for w_start, raw in rows:
            w = json.loads(raw) if raw else {}
            if not w.get("calories") or not w.get("duration"):
                continue
            a = (w_start - start) // 60
            inside.update(range(a, min(1440, a + int(w["duration"]) // 60 + 1)))
            extra += w["calories"]
        move = [i for i in recorded if out["steps"][i] and i not in inside]
        sm = dict(sm)
        sm.update(active_band=sm.get("active_kcal"), active_kcal=round(sum(out["kcal"][i] or 0 for i in move) + extra),
                  move_minutes=len(move) + len(inside))
        return sm

    @staticmethod
    def rest_estimate(hr: list) -> int | None:
        """Dinlenik nabız tahmini: günün en sakin 10 dakikasının ortalaması (en az 8 ölçümlü pencere)."""
        best = None
        for i in range(0, len(hr) - 9):
            w = [v for v in hr[i:i + 10] if v]
            if len(w) >= 8:
                m = sum(w) / len(w)
                best = m if best is None or m < best else best
        return round(best) if best is not None else None

    def _mend_summary(self, sm: dict | None, hr: list) -> dict | None:
        """Saat gün içinde yeniden başlarsa günlük özeti sıfırdan saymaya başlar: dinlenik nabız kaybolur, en düşük/
        en yüksek/ortalama yalnızca yeniden başlamadan sonrasını gösterir. Dakikalık kayıt daha genişse ondan tamamlanır."""
        vals = [v for v in hr if v]
        if sm is None or len(vals) < 30:
            return sm
        sm = dict(sm)
        if not sm.get("hr_rest"):
            est = self.rest_estimate(hr)
            if est:
                sm.update(hr_rest=est, hr_rest_est=True)
        if not sm.get("hr_min") or sm["hr_min"] > min(vals) + 3 or (sm.get("hr_max") or 0) < max(vals) - 3:
            sm.update(hr_min=min(min(vals), sm.get("hr_min") or 999), hr_max=max(max(vals), sm.get("hr_max") or 0),
                      hr_avg=round(sum(vals) / len(vals)), hr_from_minutes=True)
        return sm

    def upgrade(self) -> None:
        """Yeni sütunları ekler ve eldeki ham dosyaları yeniden çözer (bir kez)."""
        have = {r[1] for r in self.db.execute("PRAGMA table_info(minutes)")}
        new = [c for c in ("energy", "light", "moment") if c not in have]
        for c in new:
            self.db.execute(f"ALTER TABLE minutes ADD COLUMN {c} INTEGER")
        if new:
            for f in sorted(self.raw.glob("*_daily_details_*.bin")):
                rows = parse.daily_details(f.read_bytes())
                if rows:
                    self.db.executemany(
                        "UPDATE minutes SET energy=:energy, light=:light, moment=:moment WHERE ts=:ts", rows)
            self.db.commit()
        self.reparse()
        self.drop_app_overlaps()

    def csv(self, what: str) -> str:
        """Dışa aktarım: 'minutes' dakikalık ölçümler, 'days' günlük özetler."""
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        with self.lock:
            if what == "minutes":
                cols = ("steps", "kcal", "dist", "hr", "spo2", "stress", "energy", "light", "moment")
                w.writerow((i18n.t("zaman"),) + cols)
                for ts, *vals in self.db.execute(f"SELECT ts, {', '.join(cols)} FROM minutes ORDER BY ts"):
                    w.writerow([datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")] + ["" if v is None else v for v in vals])
            else:
                rows = [(d, json.loads(j)) for d, j in self.db.execute("SELECT day, data FROM daily ORDER BY day")]
                keys = sorted({k for _, j in rows for k in j if k not in ("ts", "standing_hours")})
                w.writerow([i18n.t("gun")] + keys + [i18n.t("ayakta_saat")])
                for d, j in rows:
                    w.writerow([d] + [j.get(k, "") for k in keys] + [len(j.get("standing_hours", []))])
        return buf.getvalue()

    def pending(self) -> list[dict]:
        """İndirilmiş ama henüz çözülemeyen dosyalar (uyku, antrenman)."""
        with self.lock:
            rows = self.db.execute("SELECT kind, ts, size FROM files WHERE parsed = 0 ORDER BY ts DESC").fetchall()
        return [{"kind": k, "ts": ts, "size": size} for k, ts, size in rows]

    def delete_app_workout(self, start: int) -> bool:
        """Yalnızca uygulamadan kaydedilmiş bir antrenmanı siler; saatten ya da Mi Fitness'tan gelen kayda dokunmaz."""
        with self.lock:
            row = self.db.execute("SELECT summary FROM workouts WHERE start = ?", (start,)).fetchone()
            if not row or not self._is_app(row[0]):
                return False
            self.db.execute("DELETE FROM workouts WHERE start = ?", (start,))
            self.db.commit()
            return True

    def own_stride(self, sport: str = "running") -> float | None:
        """Kullanıcının bu spordaki ölçülmüş adım boyu (m): mesafesi tahmin olmayan kayıtların ortancası."""
        vals = []
        for w in self.workouts():
            if w.get("sport") != sport or (w.get("distance_estimated") and not w.get("distance_measured")):
                continue
            steps, dist = w.get("steps"), w.get("distance")
            if isinstance(steps, (int, float)) and steps >= 1000 and dist and dist >= 1000 and 0.4 <= dist / steps <= 1.6:
                vals.append(dist / steps)
        if not vals:
            return None
        vals.sort()
        mid = len(vals) // 2
        return round(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2, 3)

    def set_workout_distance(self, start: int, metres: float) -> bool:
        """Uygulama kaydının mesafesini kullanıcının ölçtüğü değere çeker (ör. haritadan).

        GPS'ten gelen kısım korunur, adımdan tahmin edilen aralıklar ölçeklenir; seri, tempo ve adım boyu yenilenir.
        """
        with self.lock:
            row = self.db.execute("SELECT summary, series FROM workouts WHERE start = ?", (start,)).fetchone()
            if not row or not self._is_app(row[0]):
                return False
            summary = json.loads(row[0])
            series = json.loads(row[1]) if row[1] else None
            old = summary.get("distance")
            if not old or not 0.3 <= metres / old <= 3:
                return False
            gps, est = summary.get("distance_gps") or 0, summary.get("distance_steps")
            spans = summary.get("gps_fallback") or []
            partial = bool(est and spans and metres > gps)   # yalnızca adımdan sayılan aralıklar ölçeklenir
            ratio = (metres - gps) / est if partial else metres / old
            if series and series.get("distance"):
                ts, dist, speed = series["ts"], series["distance"], series.get("speed")
                out, total, prev = [], 0.0, 0.0
                for i, d in enumerate(dist):
                    if d is None:
                        out.append(None)
                        continue
                    k = ratio if not partial or any(a <= ts[i] <= b for a, b in spans) else 1.0
                    total += (d - prev) * k
                    prev = d
                    out.append(round(total, 1))
                    if speed and speed[i] is not None:
                        speed[i] = round(speed[i] * k, 2)
                fix = metres / total if total else 1.0     # yuvarlama ve sınır farkı: son değer ölçüme eşitlenir
                series["distance"] = [None if d is None else round(d * fix, 1) for d in out]
            dist = round(metres)
            summary.update(distance=dist, distance_measured=True, distance_before=summary.get("distance_before", old),
                           pace_avg=round(summary["duration"] / dist * 1000),
                           speed_avg=round(dist / summary["duration"], 3))
            if est:
                summary["distance_steps"] = max(0, dist - gps)
            if summary.get("steps"):
                summary["stride_avg"] = round(dist / summary["steps"], 2)
            self.db.execute("UPDATE workouts SET distance = ?, summary = ?, series = ? WHERE start = ?",
                            (dist, json.dumps(summary), json.dumps(series) if series else None, start))
            self.db.commit()
            return True

    def add_live(self, hr: int) -> None:
        """Kullanıcının başlattığı canlı ölçümden bir okuma."""
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS live_hr (ts INTEGER PRIMARY KEY, hr INTEGER)")
            self.db.execute("INSERT OR REPLACE INTO live_hr VALUES (?,?)", (int(time.time()), hr))
            self.db.commit()

    def add(self, steps, calories, hr, battery) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?)",
                (int(time.time()), steps, calories, hr, battery),
            )
            self.db.commit()

    def day(self, day: date) -> dict:
        start = int(datetime.combine(day, datetime.min.time()).timestamp())
        end = start + 86400
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, steps, hr FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end)
            ).fetchall()
        hours = [0] * 24
        heart = []
        first = None
        previous = None
        for ts, steps, hr in rows:
            if steps is not None:
                if previous is None:
                    first = {"ts": ts, "steps": steps}
                elif steps > previous:
                    hours[datetime.fromtimestamp(ts).hour] += steps - previous
                previous = steps if previous is None else max(previous, steps)
            if hr:
                heart.append([ts - start, hr])
        return {"date": day.isoformat(), "hours": hours, "first": first, "heart": heart}

    def days(self, count: int = 14) -> list[dict]:
        today = date.today()
        start = int(datetime.combine(today - timedelta(days=count - 1), datetime.min.time()).timestamp())
        with self.lock:
            rows = self.db.execute(
                "SELECT date(ts, 'unixepoch', 'localtime') d, MAX(steps), MAX(calories) "
                "FROM samples WHERE ts >= ? GROUP BY d",
                (start,),
            ).fetchall()
            summaries = dict(self.db.execute("SELECT day, data FROM daily").fetchall())
        found = {d: (s, c) for d, s, c in rows}
        out = []
        for i in range(count - 1, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            steps, calories = found.get(d, (None, None))
            row = {"date": d, "steps": steps, "calories": calories}
            if d in summaries:
                sm = json.loads(summaries[d])
                row.update(steps=max(sm.get("steps") or 0, steps or 0), calories=sm.get("kcal", calories),
                           active_kcal=sm.get("active_kcal"), hr_rest=sm.get("hr_rest"), hr_avg=sm.get("hr_avg"),
                           standing=len(sm.get("standing_hours", [])), vitality=sm.get("vitality"),
                           load_week=sm.get("load_week"))
                if not sm.get("hr_rest") and i <= 1:   # bugün ve dün: saatin özeti eksikse dakikalık kayıttan tahmin
                    fixed = self.minutes(today - timedelta(days=i))["summary"] or {}
                    if fixed.get("hr_rest"):
                        row.update(hr_rest=fixed["hr_rest"], hr_rest_est=True, hr_avg=fixed.get("hr_avg", row.get("hr_avg")))
            out.append(row)
        return out
