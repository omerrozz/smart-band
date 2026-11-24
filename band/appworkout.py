"""Uygulamadan başlatılan antrenmanın kaydedicisi.

Saatin kendi antrenman modu telefondan/bilgisayardan başlatılamıyor (kaynak protokolde böyle bir komut yok);
bu yüzden kayıt, saatin canlı akışından (8/47: günlük adım ve kalori sayaçları + nabız, yaklaşık 5 saniyede bir)
bilgisayarda tutulur. Saate hiçbir şey yazılmaz; GPS yoktur.

Kurallar
- Adım ve kalori: canlı akıştaki günlük sayaçların iki okuma arasındaki farkı toplanır (sayaç gece yarısı
  sıfırlanırsa o okuma yeni taban olur, fark 0 sayılır).
- Mesafe TAHMİNDİR: adım farkı × adım boyu. Adım boyu boydan türetilir: yürüyüşte boy × 0,415, koşuda
  boy × 0,65. Koşu ve koşu bandında bir okumanın ritmi RUN_CADENCE'in (130 adım/dk) altındaysa o adımlar
  yürüyüş boyuyla sayılır. Bisiklet, ip atlama ve serbest antrenmanda mesafe tahmin edilmez.
- Duraklatma: duraklatılmışken gelen okumalar sayılmaz (yalnızca nabız gösterilir); sürdürünce ilk okuma yeni
  taban olur, aradaki adım ve kalori eklenmez. Süre = bitiş − başlangıç − duraklatılan süre.
- Bağlantı kopması: oturum sürer, süre akmaya devam eder. İki okuma arası LINK_GAP'ten (60 sn) uzunsa aralık
  "gaps" listesine yazılır ve seriye nabzı boş bir satır eklenir. Saat aradaki adım ve kaloriyi saymaya devam
  ettiği için bağlantı dönünce bu fark toplamlara (ve mesafe tahminine) eklenir.
- SILENCE (30 dk) boyunca hiç okuma gelmezse antrenman son okumanın anında bitirilir. Üst sınır CAP (6 saat).
- Servis yeniden başlarsa oturum veritabanından geri yüklenir; sessizlik ya da üst sınır aşılmışsa bitirilir.
- 60 saniyeden kısa ya da hiç nabız okunmamış oturum kaydedilmez.
- Çakışma: saatin kendi antrenman kaydı her zaman önceliklidir (bkz. Store.drop_app_overlaps).

Telefon GPS'i (yalnızca konum kaynağı olan kurulumda, ör. Android uygulaması; koşu, yürüyüş, bisiklet)
- Oturum gps=True ile başlatılırsa on_fix() ile gelen noktalar süzülür: doğruluğu GPS_MAX_ACC'tan (30 m) kötü
  olan, önceki kabul edilen noktaya göre azami hızı (yaya 12 m/sn, bisiklet 25 m/sn) aşan nokta atılır;
  doğruluk ilk kez GPS_LOCK_ACC'a (20 m) inene dek hiçbir nokta kabul edilmez.
- Mesafe: kabul edilen noktaların son GPS_SMOOTH saniyelik ortalaması bir çapaya göre izlenir; ortalama konum
  çapadan GPS_STEP (6 m; doğruluk kötüyse 0,4 × doğruluk) uzaklaşınca aradaki haversine uzaklığı eklenir ve
  çapa oraya taşınır. Dururken GPS'in oynaması bu eşiğin altında kaldığı için metre eklemez; sağlayıcı hız
  bildiriyorsa ve hız GPS_STILL'in (0,3 m/sn) altındaysa çapa hiç taşınmaz.
- GPS sağlıklıyken (son nokta GPS_LOST = 20 sn içinde) adım tahmini eklenmez. GPS henüz bulunmadıysa ya da
  20 saniyeden uzun kesildiyse o aralığın mesafesi adımdan tahmin edilir ve aralık "gps_fallback"e yazılır.
- Duraklatmada noktalar sayılmaz; sürdürünce ilk nokta yeni çapadır, aradaki yol eklenmez.
- İz, saat kayıtlarındaki biçimde saklanır: {ts, lat, lon, accuracy, speed?, alt?, hr?}.
"""
from __future__ import annotations

import math
import threading
import time

import i18n

SPORTS = {  # spor: adım boyu türü (None = mesafe tahmin edilmez)
    "running": "run", "walking": "walk", "cycling": None, "treadmill": "run", "jump_rope": None, "freestyle": None,
}
STRIDE_WALK = 0.415   # adım boyu / boy, yürüyüş
STRIDE_RUN = 0.65     # adım boyu / boy, koşu
RUN_CADENCE = 130     # koşu sporlarında bu ritmin altındaki adımlar yürüyüş sayılır (adım/dk)
LINK_GAP = 60         # iki okuma arası bu kadar saniyeyi aşarsa bağlantı boşluğu sayılır
STALE = 20            # bu kadar saniye okuma gelmezse arayüzde "saatten veri gelmiyor" gösterilir
SILENCE = 30 * 60     # bu kadar süre hiç okuma gelmezse antrenman kendiliğinden biter
CAP = 6 * 3600        # en uzun antrenman
MIN_SECONDS = 60      # daha kısa oturum kaydedilmez
GOAL_NOTIFY_WINDOW = 120  # hedef bağlantı yokken dolduysa bildirim en geç bu kadar sonra gider
MAX_CADENCE = 300     # bunun üstündeki anlık ritim (ve o okumanın hızı) seriye yazılmaz
SMOOTH = 20           # anlık ritim ve hız bu kadar saniyelik pencereden hesaplanır
GOALS = {"time": (1, 360), "distance": (0.1, 100), "calories": (10, 5000)}  # dk, km, kcal
ZONE_KEYS = ("warm_up", "fat_burn", "aerobic", "anaerobic", "extreme")
ZONE_FROM = (.5, .6, .7, .8, .9)  # arayüzdeki ZN ile aynı sınırlar: azami nabzın yüzdesi
GPS_SPORTS = ("running", "walking", "cycling")  # telefon GPS'inin kullanıldığı sporlar
GPS_MAX_ACC = 30      # doğruluğu bundan kötü nokta atılır (m)
GPS_LOCK_ACC = 20     # ilk kabul için gereken doğruluk (m)
GPS_MAX_SPEED = 12.0  # yaya sporlarda iki nokta arası bundan hızlı sıçrama atılır (m/sn)
GPS_MAX_SPEED_BIKE = 25.0
GPS_LOST = 20         # son noktadan bu kadar saniye sonra GPS kesilmiş sayılır; mesafe adımdan sürer
GPS_SMOOTH = 5        # konum bu kadar saniyelik noktaların ortalamasıyla yumuşatılır
GPS_STEP = 6.0        # çapa, yumuşatılmış konum bu kadar uzaklaşınca taşınır (m); dururken oynama eklenmez
GPS_STILL = 0.3       # sağlayıcının bildirdiği hız bunun altındaysa durağan sayılır (m/sn)
GPS_PACE = 12         # anlık hız bu kadar saniyelik pencereden hesaplanır
GPS_SAVE = 5          # süren oturum bu kadar saniyede bir veritabanına yazılır (okuma gelmese de)


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """İki nokta arası uzaklık (m)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(a)))


def stride(height_cm: float, kind: str) -> float:
    """Tahmini adım boyu (m)."""
    return height_cm / 100 * (STRIDE_RUN if kind == "run" else STRIDE_WALK)


def zone_of(hr, top: int) -> int:
    """0–4 bölge sırası; Bölge 1'in altı −1. Arayüzdeki zoneOf ile aynı."""
    z = -1
    if hr:
        for k, x in enumerate(ZONE_FROM):
            if hr / top >= x:
                z = k
    return z


class Recorder:
    """store: Store; profile(): güncel profil; max_hr(): azami nabız. Zaman hep unix saniyesi (float)."""

    def __init__(self, store, profile, max_hr):
        self.store, self.profile, self.max_hr = store, profile, max_hr
        self.lock = threading.RLock()
        self.s: dict | None = None      # süren oturum (veritabanına da yazılır)
        self.samples: list[list] = []   # [ts, nabız, adım, kcal, mesafe, ritim, hız]
        self.fill_ts = None             # on_hr'ın yazdığı son satırın saniyesi
        self.live = (0.0, None, None)   # son canlı okumanın anı, ritmi ve hızı (saniyelik satırlara taşınır)
        self.seg = 0                    # kesintisiz bölümün ilk örneğinin sırası (ritim/hız penceresi için)
        self.hr = None
        self.track: list[dict] = []     # telefon GPS'inden kabul edilen noktalar (veritabanına da yazılır)
        self.recent: list[tuple] = []   # son GPS_SMOOTH saniyenin noktaları (ts, lat, lon)
        self.path: list[tuple] = []     # son GPS_PACE saniyenin yumuşatılmış konumları
        self.gps_error: str | None = None  # konum alınamıyorsa nedeni (izin yok, konum kapalı)
        self.gps_saved = 0.0

    # --- durum ---------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.s is not None

    def elapsed(self, now: float | None = None) -> int:
        s = self.s
        if not s:
            return 0
        now = s["paused_at"] if s["paused"] else (now or time.time())
        return max(0, int(now - s["start"] - s["paused_total"]))

    def goal_progress(self, now: float | None = None):
        s = self.s
        g = s and s["goal"]
        if not g:
            return None
        cur = {"time": self.elapsed(now) / 60, "distance": s["distance"] / 1000, "calories": s["calories"]}[g["type"]]
        return {"type": g["type"], "value": g["value"], "current": round(cur, 2),
                "progress": round(min(1.0, cur / g["value"]), 3), "reached": bool(s["goal_reached"])}

    def state(self, now: float | None = None) -> dict:
        with self.lock:
            s = self.s
            if not s:
                return {"active": False}
            now = now or time.time()
            est = SPORTS[s["sport"]] is not None
            el = self.elapsed(now)
            last = self.samples[-1] if self.samples else None
            fresh = last is not None and not s["paused"] and now - s["heard"] <= STALE
            g = s.get("gps")
            live = self._gps_live(s, now)
            dist = round(s["distance"]) if est or g else None
            out = {"active": True, "paused": s["paused"], "sport": s["sport"], "started": s["start"],
                   "elapsed": el, "hr": self.hr if now - s["heard"] <= STALE else None,
                   "steps": s["steps"], "calories": s["calories"], "distance": dist,
                   "distance_estimated": est and not live,
                   "speed": last[6] if fresh and est else None, "cadence": last[5] if fresh else None,
                   "pace": round(el / dist * 1000) if dist and dist >= 50 else None,
                   "goal": self.goal_progress(now), "zone": s["zone"], "stale": now - s["heard"] > STALE,
                   "heard": int(s["heard"]), "band_workout": bool(s.get("band_workout")),
                   "trace": [x[1] for x in self.samples[-90:]]}
            if g:  # telefon GPS'li oturum
                fix = g["last"]
                if live and not s["paused"]:
                    out["speed"] = g["speed"]
                what = ("paused" if s["paused"] else "unavailable" if self.gps_error else "ok" if live and fix
                        else "searching" if live or not g["locked"] else "lost")
                out.update(distance_source="gps" if live else "steps" if est else None, track_len=len(self.track),
                           gps={"state": what, "accuracy": round(fix[3]) if fix else None, "error": self.gps_error,
                                "lat": fix[1] if fix else None, "lon": fix[2] if fix else None})
            return out

    def _own_stride(self, sport: str) -> float | None:
        try:
            return self.store.own_stride(sport)
        except Exception:
            return None

    # --- komutlar ------------------------------------------------------
    def start(self, sport: str, goal: dict | None, zone: int | None, now: float | None = None,
              gps: bool = False) -> None:
        """Geçersiz istekte ValueError (mesaj kullanıcıya gösterilir). gps: telefonun konumu kullanılacak."""
        with self.lock:
            if self.s:
                raise ValueError(i18n.t("Zaten süren bir antrenman var. Önce onu bitir."))
            if sport not in SPORTS:
                raise ValueError(i18n.t("Bilinmeyen spor türü."))
            gps = bool(gps) and sport in GPS_SPORTS
            if goal is not None:
                try:
                    kind, value = goal["type"], float(goal["value"])
                    lo, hi = GOALS[kind]
                except (KeyError, TypeError, ValueError):
                    raise ValueError(i18n.t("Hedef türü ya da değeri geçersiz.")) from None
                if kind == "distance" and SPORTS[sport] is None and not gps:
                    raise ValueError(i18n.t("Bu sporda mesafe tahmin edilemediği için mesafe hedefi konamaz."))
                if not lo <= value <= hi:
                    raise ValueError({"time": i18n.t("Süre hedefi 1–360 dakika arasında olmalı."),
                                      "distance": i18n.t("Mesafe hedefi 0,1–100 km arasında olmalı."),
                                      "calories": i18n.t("Kalori hedefi 10–5.000 kcal arasında olmalı.")}[kind])
                goal = {"type": kind, "value": value}
            if zone is not None and zone not in (1, 2, 3, 4, 5):
                raise ValueError(i18n.t("Bölge 1 ile 5 arasında olmalı."))
            now = now or time.time()
            self.s = {"start": int(now), "sport": sport, "goal": goal, "zone": zone, "paused": False,
                      "paused_at": None, "paused_total": 0.0, "pauses": [], "gaps": [], "heard": now,
                      "steps": 0, "calories": 0, "distance": 0.0, "goal_reached": None, "goal_notified": False,
                      "height": self.profile()["height"], "band_workout": False,
                      "stride_own": self._own_stride(sport),
                      "last": None}   # last: son sayılan okuma {t, steps, calories}; fark bunun üstünden alınır
            if gps:
                # m: GPS'ten gelen metre; steps_m: GPS yokken adımdan eklenen; pending: son noktadan beri adım
                # tahmini (GPS sağlıklı kalırsa atılır, kesilirse eklenir); last: son nokta [ts, lat, lon, doğruluk]
                self.s["gps"] = {"locked": False, "last": None, "anchor": None, "wait": None, "speed": None,
                                 "m": 0.0, "steps_m": 0.0, "pending": 0.0, "fallback": [], "rejected": 0}
            self.samples, self.seg, self.hr = [], 0, None
            self.track, self.recent, self.path, self.gps_error = [], [], [], None
            self.store.app_session_save(self.s, reset=True)

    def pause(self, now: float | None = None) -> None:
        with self.lock:
            s = self.s
            if not s or s["paused"]:
                return
            self._gps_close(s)
            s.update(paused=True, paused_at=now or time.time(), last=None)
            self.store.app_session_save(s)

    def resume(self, now: float | None = None) -> None:
        with self.lock:
            s = self.s
            if not s or not s["paused"]:
                return
            now = now or time.time()
            s["pauses"].append([int(s["paused_at"]), int(now)])
            s["paused_total"] += now - s["paused_at"]
            # sessizlik sayacı sürdürünce yeniden başlar; ilk okuma yeni taban olur
            s.update(paused=False, paused_at=None, heard=max(s["heard"], now), last=None)
            self.seg = len(self.samples)
            if s.get("gps"):  # ilk nokta yeni çapa olur; duraklatılan yol eklenmez
                s["gps"].update(last=None, anchor=None, wait=now, speed=None, pending=0.0)
                s["gps"].pop("pending_from", None)
                self.recent, self.path = [], []
            self.store.app_session_save(s)

    def set_zone(self, zone: int | None) -> None:
        with self.lock:
            if self.s:
                self.s["zone"] = zone
                self.store.app_session_save(self.s)

    def note_band_workout(self) -> None:
        """Saatte de bir antrenman başladı: kayıt sürer, bitişte saatin kaydı öncelikli olur."""
        with self.lock:
            if self.s and not self.s.get("band_workout"):
                self.s["band_workout"] = True
                self.store.app_session_save(self.s)

    def discard(self) -> None:
        with self.lock:
            self.s, self.samples, self.hr = None, [], None
            self.track, self.recent, self.path, self.gps_error = [], [], [], None
            self.store.app_session_clear()

    # --- telefon GPS'i --------------------------------------------------
    def _gps_live(self, s: dict, now: float) -> bool:
        """GPS mesafe kaynağı olarak kullanılabilir mi (bulundu ve son nokta taze)."""
        g = s.get("gps")
        if not g or not g["locked"]:
            return False
        if s["paused"]:
            return True
        return now - max(g["last"][0] if g["last"] else 0, g["wait"] or 0) <= GPS_LOST

    def _flush_steps(self, s: dict, t0: float, now: float) -> None:
        """GPS'siz geçen aralığın adım tahminini mesafeye ekler ve aralığı işaretler."""
        g = s["gps"]
        if g["pending"] <= 0:
            return
        t0 = g.pop("pending_from", None) or t0   # aralık, adımların birikmeye başladığı andan sayılır
        s["distance"] += g["pending"]
        g["steps_m"] += g["pending"]
        g["pending"] = 0.0
        fb = g["fallback"]
        if fb and t0 - fb[-1][1] <= 10:
            fb[-1][1] = int(now)
        else:
            fb.append([int(t0), int(now)])

    def _add_distance(self, s: dict, metres: float, t0: float, now: float) -> None:
        """Adımdan tahmin edilen metre: GPS'li oturumda yalnızca GPS sağlıklı değilken sayılır."""
        g = s.get("gps")
        if not g:
            s["distance"] += metres
            return
        if g["pending"] <= 0:
            g["pending_from"] = t0
        g["pending"] += metres
        if not self._gps_live(s, now):
            self._flush_steps(s, t0, now)

    def _gps_close(self, s: dict) -> None:
        """Duraklatma ya da bitişte: çapadan son yumuşatılmış konuma kalan parça eklenir."""
        g = s.get("gps")
        if not g or not g["anchor"] or not self.path:
            return
        d = haversine(g["anchor"][0], g["anchor"][1], self.path[-1][1], self.path[-1][2])
        if d >= GPS_STEP / 2:
            s["distance"] += d
            g["m"] += d
        g["anchor"] = None

    def on_fix(self, ts: float, lat: float, lon: float, accuracy: float | None, speed: float | None = None,
               alt: float | None = None) -> bool:
        """Telefonun konum sağlayıcısından bir nokta. Kabul edilip ize eklendiyse True."""
        with self.lock:
            s = self.s
            g = s and s.get("gps")
            if not g or s["paused"]:
                return False
            self.gps_error = None
            last = g["last"]
            if last is not None and ts <= last[0]:
                return False  # eski ya da yinelenen nokta
            bad = accuracy is None or accuracy > (GPS_MAX_ACC if g["locked"] else GPS_LOCK_ACC)
            gap = last is None or ts - last[0] > GPS_LOST   # ilk nokta, duraklatma sonrası ya da kesinti sonrası
            if not bad and not gap:
                top = GPS_MAX_SPEED_BIKE if s["sport"] == "cycling" else GPS_MAX_SPEED
                bad = haversine(last[1], last[2], lat, lon) / max(1.0, ts - last[0]) > top
            if bad:
                g["rejected"] += 1
                return False
            if gap:
                if last is not None:   # kesinti: aradaki yol adımdan sayılır, GPS'ten eklenmez
                    self._flush_steps(s, last[0], ts)
                    try:   # tanı: kesintiler bağlantı günlüğünde görünsün (konum bilgisi yazılmaz)
                        self.store.log_event("gps", i18n.t("{sec} sn kesintiden sonra konum geldi; o ana dek {n} nokta süzüldü",
                                                           sec=int(ts - last[0]), n=g["rejected"]))
                    except Exception:
                        pass
                self.recent, self.path = [], []
                g.update(anchor=None, speed=None)
            g.update(locked=True, last=[ts, lat, lon, accuracy], wait=None, pending=0.0)
            g.pop("pending_from", None)
            self.recent = [x for x in self.recent if ts - x[0] < GPS_SMOOTH] + [(ts, lat, lon)]
            la = sum(x[1] for x in self.recent) / len(self.recent)
            lo = sum(x[2] for x in self.recent) / len(self.recent)
            if g["anchor"] is None:
                g["anchor"] = [la, lo]
            else:
                d = haversine(g["anchor"][0], g["anchor"][1], la, lo)
                # sağlayıcı hız veriyorsa (Doppler) ve duruyor diyorsa konumdaki kayma yol sayılmaz
                moving = speed is None or speed >= GPS_STILL
                if moving and d >= max(GPS_STEP, 0.4 * accuracy):
                    s["distance"] += d
                    g["m"] += d
                    g["anchor"] = [la, lo]
            self.path = [x for x in self.path if ts - x[0] <= GPS_PACE] + [(ts, la, lo)]
            if ts - self.path[0][0] >= 4:
                g["speed"] = round(haversine(self.path[0][1], self.path[0][2], la, lo) / (ts - self.path[0][0]), 2)
            if not self.track or int(ts) > self.track[-1]["ts"]:
                point = {"ts": int(ts), "lat": round(lat, 6), "lon": round(lon, 6), "accuracy": round(accuracy, 1)}
                if speed is not None:
                    point["speed"] = round(speed, 2)
                if alt is not None:
                    point["alt"] = round(alt, 1)
                if self.hr and ts - s["heard"] <= STALE:
                    point["hr"] = self.hr
                self.track.append(point)
                self.store.app_track_add(point)
            self._goal_check(ts)
            if ts - self.gps_saved >= GPS_SAVE:
                self.gps_saved = ts
                self.store.app_session_save(s)
            return True

    def gps_problem(self, text: str | None) -> None:
        """Konum alınamıyor (izin yok, konum kapalı…); None: sorun kalktı. Mesafe adımdan sürer."""
        with self.lock:
            self.gps_error = text

    # --- okumalar ------------------------------------------------------
    def on_reading(self, steps: int, calories: int, hr, now: float | None = None) -> None:
        """Canlı akıştan bir okuma (günlük adım ve kalori sayaçları, nabız ya da None)."""
        with self.lock:
            s = self.s
            if not s:
                return
            now = now or time.time()
            s["heard"], self.hr = now, hr
            if s["paused"]:
                return
            ts, last = int(now), s["last"]
            dup = bool(self.samples and ts <= self.samples[-1][0])
            if dup and self.samples[-1][0] != self.fill_ts:
                return  # aynı saniyede ikinci okuma
            # dup: bu saniyenin satırını saniyelik nabız (on_hr) zaten yazdı; toplamlar işlenir, ikinci satır yazılmaz
            cadence = speed = None
            if last is not None:
                dt = now - last["t"]
                d_steps, d_kcal = steps - last["steps"], calories - last["calories"]
                if d_steps < 0 or d_kcal < 0:   # günlük sayaç sıfırlandı (gece yarısı)
                    d_steps = d_kcal = 0
                gap = dt > LINK_GAP
                if gap:
                    s["gaps"].append([int(last["t"]), ts])
                    mark = [int(last["t"]) + 1, None, s["steps"], s["calories"], round(s["distance"], 1), None, None]
                    self.samples.append(mark)
                    self.store.app_sample_add(mark)
                    self.seg = len(self.samples)
                kind = SPORTS[s["sport"]]
                rate = d_steps / dt * 60 if dt > 0 else 0
                if kind and d_steps:
                    use = "walk" if kind == "run" and rate < RUN_CADENCE else kind
                    # kullanıcının ölçülmüş adım boyu varsa o kullanılır (yavaş kısımları da içeren ortalama)
                    per = s.get("stride_own") or stride(s["height"], use)
                    self._add_distance(s, d_steps * per, last["t"], now)
                s["steps"] += d_steps
                s["calories"] += d_kcal
                if not gap:  # ritim ve hız kısa bir pencereden: 5 saniyelik okumalar tek tek çok oynak
                    ref = next((x for x in self.samples[self.seg:] if ts - x[0] <= SMOOTH), None)
                    if ref and ts > ref[0]:
                        span = ts - ref[0]
                        cadence = round((s["steps"] - ref[2]) / span * 60)
                        if kind:
                            speed = round((s["distance"] - ref[4]) / span, 2)
                        if cadence > MAX_CADENCE:  # sayaç toplu güncellendi: anlık değer anlamsız, toplamlar doğru
                            cadence = speed = None
                    if s.get("gps") and self._gps_live(s, now) and s["gps"]["speed"] is not None:
                        speed = s["gps"]["speed"]   # GPS varken hız oradan
            s["last"] = {"t": now, "steps": steps, "calories": calories}
            self.live = (now, cadence, speed)
            if not dup:
                row = [ts, hr, s["steps"], s["calories"], round(s["distance"], 1), cadence, speed]
                self.samples.append(row)
                self.store.app_sample_add(row)
            self._goal_check(now)
            self.store.app_session_save(s)

    def on_hr(self, hr, now: float | None = None) -> None:
        """Saatin antrenman ekranı açıkken saniyede bir yolladığı nabız (8/50): kayıt saniyelik olur.

        Adım ve kalori toplamları yaklaşık 5 saniyede bir gelen canlı akıştan (on_reading) sayılmaya devam eder;
        bu satırlar o anki toplamları taşır. Mesafe ve hız GPS varken her saniye tazedir. 22 Kasım 2026'da eklendi.
        """
        with self.lock:
            s = self.s
            if not s or not hr:
                return
            now = now or time.time()
            s["heard"], self.hr = now, hr
            ts = int(now)
            if s["paused"] or s["last"] is None or (self.samples and ts <= self.samples[-1][0]):
                return   # ilk satırı ve tabanı on_reading kurar
            at, cadence, speed = self.live
            if now - at > 12:
                cadence = speed = None
            if s.get("gps") and self._gps_live(s, now) and s["gps"]["speed"] is not None:
                speed = s["gps"]["speed"]
            row = [ts, hr, s["steps"], s["calories"], round(s["distance"], 1), cadence, speed]
            self.samples.append(row)
            self.fill_ts = ts
            self.store.app_sample_add(row)

    def _goal_check(self, now: float) -> None:
        s = self.s
        if s["goal"] and not s["goal_reached"] and not s["paused"]:
            g = self.goal_progress(now)
            if g["current"] >= g["value"]:
                s["goal_reached"] = int(now)

    def tick(self, now: float | None = None, connected: bool = True):
        """Saniyede bir çağrılır. Dönen değer: None | ("goal", metin) | ("finished", sonuç)."""
        with self.lock:
            s = self.s
            if not s:
                return None
            now = now or time.time()
            if now - s["start"] >= CAP:
                return "finished", self.finish(min(now, s["start"] + CAP), auto="cap")
            if now - s["heard"] >= SILENCE:
                return "finished", self.finish(None, auto="silence")
            was = s["goal_reached"]
            self._goal_check(now)
            if s["goal_reached"] and not was:
                self.store.app_session_save(s)
            if s["goal_reached"] and not s["goal_notified"]:
                if now - s["goal_reached"] > GOAL_NOTIFY_WINDOW:
                    s["goal_notified"] = True   # geç kaldı: bayat bildirim gönderilmez
                    self.store.app_session_save(s)
                elif connected:
                    s["goal_notified"] = True
                    self.store.app_session_save(s)
                    g = s["goal"]
                    v = i18n.num(g["value"])
                    km = (i18n.t("{v} km tamamlandı.", v=v) if self._gps_live(s, now)
                          else i18n.t("Tahmini {v} km tamamlandı.", v=v))
                    return "goal", {"time": i18n.t("{v} dakika doldu.", v=v), "distance": km,
                                    "calories": i18n.t("{v} kcal yakıldı.", v=v)}[g["type"]]
            return None

    # --- bitiş ---------------------------------------------------------
    def finish(self, now: float | None = None, auto: str | None = None) -> dict:
        """Oturumu kapatır. {"saved": bool, "id": başlangıç | None, "message": metin, "auto": neden | None}.

        now=None: son okumanın anında biter (sessizlik ya da yeniden başlatma sonrası).
        """
        with self.lock:
            s = self.s
            if not s:
                return {"saved": False, "id": None, "message": i18n.t("Süren bir antrenman yok."), "auto": auto}
            real = [x for x in self.samples if x[1]]
            if now is None:
                now = max(self.samples[-1][0] if self.samples else s["start"], s["paused_at"] or 0)
            if s.get("gps") and not s["paused"]:
                self._gps_close(s)
                if not self._gps_live(s, now):
                    self._flush_steps(s, now, now)
            if s["paused"]:
                s["pauses"].append([int(s["paused_at"]), int(max(now, s["paused_at"]))])
                s["paused_total"] += max(0.0, now - s["paused_at"])
                s.update(paused=False, paused_at=None)
            end = int(now)
            duration = max(0, int(now - s["start"] - s["paused_total"]))
            out = {"saved": False, "id": None, "auto": auto}
            if duration < MIN_SECONDS:
                out["message"] = i18n.t("Antrenman 1 dakikadan kısa sürdü, kaydedilmedi.")
            elif not real:
                out["message"] = i18n.t("Hiç nabız okunamadı, antrenman kaydedilmedi. Saat menzilde ve bilekte miydi?")
            else:
                summary, series = self._build(s, end, duration, real)
                if summary.get("distance_source") == "gps":   # telefon GPS'inin izi, saat kayıtlarındaki biçimde
                    clash = self.store.save_app_workout(summary, series, self.track)
                else:
                    clash = self.store.save_app_workout(summary, series)
                if clash is None:
                    out.update(saved=True, id=s["start"], message=i18n.t("Antrenman kaydedildi."))
                else:
                    out.update(id=clash, message=i18n.t("Bu aralıkta saatin kendi antrenman kaydı var; uygulama kaydı ayrıca "
                                                        "saklanmadı."))
            self.discard()
            return out

    def _build(self, s: dict, end: int, duration: int, real: list) -> tuple[dict, dict]:
        top = self.max_hr()
        zones = dict.fromkeys(ZONE_KEYS, 0)
        prev = None
        for ts, hr, *_ in self.samples:
            if hr:
                dt = ts - prev if prev is not None and 0 < ts - prev <= 15 else 5  # okuma aralığı; boşluklar sayılmaz
                z = zone_of(hr, top)
                if z >= 0:
                    zones[ZONE_KEYS[z]] += dt
            prev = ts if hr else None
        hrs = [x[1] for x in real]
        est = SPORTS[s["sport"]] is not None
        g = s.get("gps")
        by_gps = bool(g and g["m"] >= 1 and len(self.track) > 1)   # GPS'ten mesafe ve iz var
        has_dist = est or by_gps
        summary = {"start": s["start"], "end": end, "sport": s["sport"], "duration": duration,
                   "calories": s["calories"], "steps": s["steps"], "hr_avg": round(sum(hrs) / len(hrs)),
                   "hr_max": max(hrs), "hr_min": min(hrs), "hr_zones": zones,
                   "source": "app", "distance_estimated": est, "paused": int(s["paused_total"]), "gaps": s["gaps"]}
        if g:
            # mesafenin çoğu GPS'tense "tahmini" sayılmaz; adımdan tamamlanan aralıklar ayrıca yazılır
            mostly = by_gps and g["m"] >= 0.5 * s["distance"]
            summary.update(distance_estimated=est and not mostly, distance_source="gps" if by_gps else "steps",
                           distance_gps=round(g["m"]), distance_steps=round(g["steps_m"]),
                           gps_fallback=g["fallback"], gps_rejected=g["rejected"])
        if s["sport"] != "cycling" and s["steps"]:
            summary["cadence_avg"] = round(s["steps"] / duration * 60)
        if has_dist and s["distance"] >= 1:
            dist = round(s["distance"])
            summary.update(distance=dist, pace_avg=round(duration / dist * 1000), speed_avg=round(dist / duration, 3))
            if s["steps"]:
                summary["stride_avg"] = round(s["distance"] / s["steps"], 2)
        if s["goal"]:
            summary["goal"] = dict(s["goal"], reached=bool(s["goal_reached"]))
        if s["zone"]:
            summary["coach_zone"] = s["zone"]
        rows = self.samples
        series = {"ts": [x[0] for x in rows], "hr": [x[1] or None for x in rows]}
        if has_dist:
            series["speed"] = [x[6] for x in rows]
            series["distance"] = [x[4] for x in rows]
        if s["sport"] != "cycling":
            series["cadence"] = [x[5] for x in rows]
        series["steps"] = [x[2] for x in rows]
        series["kcal"] = [x[3] for x in rows]
        return summary, series

    # --- yeniden başlatma ------------------------------------------------
    def restore(self, now: float | None = None):
        """Servis açılırken: yarım kalmış oturumu geri yükler; çok eskiyse bitirir. Bitirdiyse sonucu döndürür."""
        with self.lock:
            saved = self.store.app_session_load()
            if not saved:
                return None
            self.s, self.samples = saved
            self.seg, self.hr = len(self.samples), None
            self.recent, self.path, self.gps_error = [], [], None
            self.track = self.store.app_track_load() if self.s.get("gps") else []
            if self.s.get("gps"):   # yeniden başlatma arası bir kesintidir: ilk nokta yeni çapa olur
                self.s["gps"].update(anchor=None, speed=None)
            now = now or time.time()
            if now - self.s["start"] >= CAP or now - self.s["heard"] >= SILENCE:
                return self.finish(None, auto="restart")
            return None
