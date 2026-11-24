"""Bileklik servisi: saate bağlı kalır, verileri kaydeder, arayüzü sunar.

Çalıştırma:  ../.venv/bin/python server.py
"""
from __future__ import annotations

import json
import os
import queue
import secrets
import select
import socket
import sqlite3
import sys
import threading
import time
import zlib
from datetime import date, datetime, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import appworkout
import bandmsg
import foods
import i18n
import insights
import base64

import parse
import rpk
import scale
import weather
from client import Band, xiaomi_pb2
from store import Store

HERE = Path(__file__).parent
WEB = HERE / "web"
# Veri klasörü: BILEKLIK_DATA verilmişse ayar, veritabanı ve ham dosyalar orada durur (Android'de kod
# salt okunurdur); verilmemişse eskisi gibi kodun yanında.
DATA = Path(os.environ["BILEKLIK_DATA"]) if os.environ.get("BILEKLIK_DATA") else HERE
CONFIG_PATH = DATA / "config.json"
DB_PATH = DATA / "band.db"
RAW = DATA / "raw"
RAW.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("BILEKLIK_PORT") or 8377)
BIND = os.environ.get("BILEKLIK_BIND") or "0.0.0.0"  # Android'de 127.0.0.1: arayüz yalnızca telefonun içinden açılır
# Saatle konuşulacak taşıyıcıyı üreten işlev: (adres, kanal, zaman aşımı) -> soket benzeri nesne.
# None ise client.py bilgisayardaki gibi AF_BLUETOOTH soketi açar. Android uygulaması burayı doldurur.
transport_factory = None

# Kimse bakmıyorken kaç saniyede bir anlık ölçüm alınır; 0: alınmaz. Kapalı, çünkü 5 dakikada bir açılan canlı
# akış sürerken saatin kendi dakikalık kaydında büyük boşluklar oluştu (21 Kasım denemesi); arka planda saatin
# kaydı yeterli.
POLL_EVERY = int(os.environ.get("BILEKLIK_POLL", "0"))
POLL_WINDOW = 25      # bir ölçüm için canlı akışın en fazla açık kalacağı süre
BATTERY_EVERY = 600
SYNC_EVERY = 1800    # kimse bakmıyorken saatteki dosyalar kaç saniyede bir indirilir
SYNC_VIEWED = 300    # arayüz açıkken. Daha sık indirme saatin dakikalık kaydını bozuyor: 20 Kasım 2026'da
# dakikada bir eşitlemede dosyalar 2 kayıtla kapandı ve aralarda 1-3 dakikalık boşluklar oluştu;
# 5 dakikada bir eşitlemede kayıtlar kesintisizdi.
POLL_VIEWED = 60     # arayüz açıkken anlık ölçüm aralığı; kutudaki son ölçüm bir dakikadan eski kalmaz
# Not: saat her indirmeden sonra yeni bir günlük dosya başlatır; onay gönderilmediği için
# dosyalar saatte birikir. Seyrek eşitleme bu birikimi sınırlar.
RETRY_EVERY = 20

config = json.loads(CONFIG_PATH.read_text())
config.setdefault("goals", {"steps": 10000, "calories": 600})
config.setdefault("water", {"enabled": False, "every": 60, "from": "09:00", "to": "22:00"})
config.setdefault("profile", {"age": 22, "height": 178, "weight": 72, "sex": "m"})
config.setdefault("meal_reminder", {"enabled": False, "times": ["08:30", "13:00", "19:30"]})
config["goals"].setdefault("water", 2500)
LANGS = ("tr", "en")
if config.get("lang") not in LANGS:
    config.pop("lang", None)        # hiç seçilmemiş: arayüz sorar; servis o zamana dek Türkçe üretir
i18n.set_lang(config.get("lang") or "tr")
config["goals"].setdefault("standing", 12)  # saat
config["goals"].setdefault("moving", 30)    # dakika
config.setdefault("band_write", False)      # profil ya da hedef değişince saate de yazılsın mı
# hava durumu: konum dış servise (Open-Meteo) gittiği için kullanıcı açmadıkça kapalıdır
config.setdefault("weather", {})
for _k, _v in (("enabled", False), ("city", ""), ("lat", None), ("lon", None), ("every", 60)):
    config["weather"].setdefault(_k, _v)
meal_fired: set[str] = set()  # bugün gönderilen öğün hatırlatmaları
water_last = time.time()  # son su hatırlatmasının zamanı


def water_state() -> dict:
    """Ayarlar ve sıradaki hatırlatmanın zamanı."""
    w = dict(config["water"])
    nxt = datetime.fromtimestamp(water_last + w["every"] * 60)
    start = datetime.strptime(w["from"], "%H:%M").time()
    if nxt.strftime("%H:%M") < w["from"]:
        nxt = datetime.combine(nxt.date(), start)
    elif nxt.strftime("%H:%M") >= w["to"]:
        nxt = datetime.combine(nxt.date() + timedelta(days=1), start)
    w["next"] = int(nxt.timestamp()) if w["enabled"] else None
    return w
if "token" not in config:
    config["token"] = secrets.token_urlsafe(18)


def save_config() -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")


save_config()


# --- paylaşılan durum ---------------------------------------------------
class Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.listeners: set[queue.Queue] = set()
        self.state = {
            "link": "connecting",  # connecting | connected | away
            "steps": None, "calories": None, "hr": None,
            "battery": None, "charging": False,
            "model": None, "firmware": None,
            "updated": None,
            "synced": None,
            "alarms": [],
            "goals": dict(config["goals"]),
            "water": water_state(),
            "meal_reminder": config["meal_reminder"],
            "profile": dict(config["profile"], max_hr=insights.max_hr(config["profile"]), bmr=round(insights.bmr(config["profile"]))),
            "settings": {},
            "device": None,      # bilekte mi, şarjda mı, uykuda mı (saatin bildirdiği)
            "workout": dict(workout),
            "app_workout": {"active": False},
            "app_workout_last": None,  # son biten uygulama antrenmanının sonucu
            "reminders": [], "max_reminders": None,
            "display": [],
            "band_write": config["band_write"],
            "userinfo": None,    # profilin saate son yazılışı
            "weather": None, "calendar": None,  # servis açılırken doldurulur
            "host_hint": None,   # bağlantı art arda hemen kopuyorsa: saati başka bir uygulama tutuyor olabilir
            # bu kurulumun yetenekleri (Android uygulaması açar; bilgisayarda hepsi kapalı)
            "caps": {"gps": False, "notifications": False, "music": False},
            "phone_last": None,   # saate iletilen son telefon bildirimi: {"app", "at"}; metin tutulmaz
            "lang": config.get("lang"),   # "tr" | "en"; None: henüz seçilmedi (servis Türkçe üretir)
        }
        self.jobs: queue.Queue = queue.Queue()  # saat döngüsünde çalıştırılacak işler
        self.sync_now = threading.Event()

    def snapshot(self) -> dict:
        with self.lock:
            out = dict(self.state)
        out["app_workout"] = appwk.state()  # süre her okunuşta günceldir
        try:  # arayüz dosyası değişince açık sayfalar kendini yeniler
            out["ui"] = int((HERE / "web" / "index.html").stat().st_mtime)
        except OSError:
            pass
        return out

    def update(self, **changes) -> None:
        with self.lock:
            if all(self.state.get(k) == v for k, v in changes.items()):
                return
            before = {k: self.state.get(k) for k in ("link", "synced", "host_hint")}
            self.state.update(changes)
            after = {k: self.state.get(k) for k in before}
            battery = self.state.get("battery")
            data = dict(self.state)
            listeners = list(self.listeners)
        log = globals().get("store")
        if log is not None:  # olay günlüğü: bağlantı değişimi, eşitleme, başka uygulama uyarısı
            if after["link"] != before["link"]:
                log.log_event("link", f"{after['link']}" + (i18n.t(", pil %{battery}", battery=battery) if battery is not None and after["link"] == "connected" else ""))
            if after["synced"] != before["synced"] and after["synced"]:
                log.log_event("sync", i18n.t("pil %{battery}", battery=battery) if battery is not None else "")
            if after["host_hint"] and not before["host_hint"]:
                log.log_event("hint", i18n.t("saati başka bir uygulama tutuyor olabilir"))
        if appwk.active:  # başka bir alan değişse de sayfaya giden antrenman süresi güncel olsun
            data["app_workout"] = appwk.state()
        for q in listeners:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

    @property
    def viewers(self) -> int:
        return len(self.listeners)


band_cfg: dict = {}  # saatten okunan ayar mesajları; değiştirirken aynısı geri yollanır


def flag(v: int):
    """Saatin üç durumlu alanı: 0 desteklenmiyor, 1 açık, 2 kapalı."""
    return None if v == 0 else v == 1


def settings_state() -> dict:
    c, out = band_cfg, {}
    if "hr" in c:
        h = c["hr"]
        low = h.heartRateAlarmLow
        out["hr"] = {"on": not h.disabled, "alarm": h.alarmHighEnabled,
                     "threshold": h.alarmHighThreshold, "interval": h.interval,
                     "low": low.alarmLowThreshold if low.alarmLowEnabled else 0,
                     # None: saat bu seçeneği sunmuyor
                     "sleep": h.advancedMonitoring.enabled if h.HasField("advancedMonitoring") else None,
                     "irregular": flag(h.irregularRhythmAlert), "breathing": flag(h.breathingScore),
                     "resp": flag(h.sleepRespiratoryRate)}
    if "spo2" in c:
        low = c["spo2"].alarmLow
        out["spo2"] = {"mode": int(c["spo2"].mode), "low": low.alarmLowThreshold if low.alarmLowEnabled else 0,
                       # None: saat gün boyu ölçümü sunmuyor
                       "allday": int(c["spo2"].mode) == 2 if c["spo2"].allDayCapable else None}
    if "standing" in c:
        out["standing"] = {"on": c["standing"].enabled}
    if "stress" in c:
        out["stress"] = {"on": c["stress"].allDayTracking,
                         "relax": c["stress"].relaxReminder.enabled if c["stress"].HasField("relaxReminder") else None}
    if "sleep" in c:
        out["sleep_mode"] = bandmsg.sleep_mode_state(c["sleep"])
    if "goals" in c:
        out["band_goals"] = c["goals"]
    if "raise" in c:
        out["raise"] = {"mode": c["raise"].mode}
    if "dnd" in c:
        out["dnd"] = {"on": c["dnd"]}
    return out


def request_settings(band: Band) -> None:
    for t, st in ((8, 10), (8, 8), (8, 12), (8, 14), (2, 24), (17, 8)):
        band.send(t, st)


# /api/setting anahtarları: (band_cfg'deki mesaj, izinli değerler)
HR_FLAGS = {"hr_irregular": "irregularRhythmAlert", "hr_breathing": "breathingScore", "hr_resp": "sleepRespiratoryRate"}
SETTINGS = {
    "hr_on": ("hr", bool), "hr_alarm": ("hr", bool), "spo2_sleep": ("spo2", bool), "standing": ("standing", bool),
    "stress": ("stress", bool), "raise": ("raise", bool), "dnd": (None, bool),
    "hr_interval": ("hr", (0, 1, 10, 30)), "hr_high": ("hr", (100, 110, 120, 130, 140, 150)),
    "hr_low": ("hr", (0, 40, 45, 50)), "hr_sleep": ("hr", bool), **{k: ("hr", bool) for k in HR_FLAGS},
    "spo2_allday": ("spo2", bool), "spo2_low": ("spo2", (0, 80, 85, 90)), "stress_relax": ("stress", bool),
    "sleep_mode": ("sleep", bool), "sleep_from": ("sleep", "time"), "sleep_to": ("sleep", "time"),
}


def setting_value(key: str, raw):
    """İstekteki değeri ayarın türüne çevirir; uymuyorsa ValueError."""
    kind = SETTINGS[key][1]
    if kind is bool:
        return bool(raw)
    if kind == "time":
        return datetime.strptime(str(raw), "%H:%M").strftime("%H:%M")
    if isinstance(raw, bool) or int(raw) not in kind:
        raise ValueError
    return int(raw)


def apply_setting(band: Band, key: str, value) -> None:
    """Saatteki bir ayarı değiştirir: okunan mesajın tek alanı değişir, gerisi aynen geri gider."""
    cmd = None
    if key.startswith("hr_") and "hr" in band_cfg:
        cmd = xiaomi_pb2.Command(type=8, subtype=11)
        m = cmd.health.heartRate
        m.CopyFrom(band_cfg["hr"])
        if key == "hr_on":
            m.disabled = not value
        elif key == "hr_alarm":
            m.alarmHighEnabled = value
            m.alarmHighThreshold = m.alarmHighThreshold or 120
        elif key == "hr_interval":
            m.interval = value
        elif key == "hr_high":
            m.alarmHighThreshold = value
        elif key == "hr_low":  # 0 kapatır; eşik saatte kalır
            m.heartRateAlarmLow.alarmLowEnabled = value > 0
            if value:
                m.heartRateAlarmLow.alarmLowThreshold = value
        elif key == "hr_sleep":
            m.advancedMonitoring.enabled = value
        elif getattr(m, HR_FLAGS[key]) == 0:
            return  # saat bu seçeneği sunmuyor
        else:
            setattr(m, HR_FLAGS[key], 1 if value else 2)
    elif key.startswith("spo2_") and "spo2" in band_cfg:
        cmd = xiaomi_pb2.Command(type=8, subtype=9)
        m = cmd.health.spo2
        m.CopyFrom(band_cfg["spo2"])
        if key == "spo2_sleep":
            # Band 9 Pro "yalnızca uykuda" kipini kabul etmiyor (ayar kapalı kalıyor); tek anahtarı gün boyu
            # ölçümü açıp kapatıyor (aralık 0 bildiren saatler). 20 Kasım 2026'da saatte görüldü.
            plain = m.allDayCapable and not int(m.interval)
            m.mode = (2 if plain else max(1, int(m.mode))) if value else 0
        elif key == "spo2_allday":
            if not m.allDayCapable:
                return
            m.mode = 2 if value else 1
        else:  # spo2_low: 0 kapatır; eşik saatte kalır
            m.alarmLow.alarmLowEnabled = value > 0
            if value:
                m.alarmLow.alarmLowThreshold = value
    elif key == "stress_relax" and "stress" in band_cfg:
        cmd = xiaomi_pb2.Command(type=8, subtype=15)
        cmd.health.stress.CopyFrom(band_cfg["stress"])
        cmd.health.stress.relaxReminder.enabled = value
    elif key.startswith("sleep_") and "sleep" in band_cfg:
        field = {"sleep_mode": "on", "sleep_from": "start", "sleep_to": "end"}[key]
        cmd = bandmsg.sleep_mode(band_cfg["sleep"], **{field: value})
    elif key == "standing" and "standing" in band_cfg:
        cmd = xiaomi_pb2.Command(type=8, subtype=13)
        cmd.health.standingReminder.CopyFrom(band_cfg["standing"])
        cmd.health.standingReminder.enabled = value
    elif key == "stress" and "stress" in band_cfg:
        cmd = xiaomi_pb2.Command(type=8, subtype=15)
        cmd.health.stress.CopyFrom(band_cfg["stress"])
        cmd.health.stress.allDayTracking = value
    elif key == "raise" and "raise" in band_cfg:
        cmd = xiaomi_pb2.Command(type=2, subtype=25)
        cmd.system.raiseToWake.CopyFrom(band_cfg["raise"])
        cmd.system.raiseToWake.mode = 0 if value else 2
    elif key == "dnd":
        cmd = xiaomi_pb2.Command(type=2, subtype=23)
        cmd.system.dndStatus.status = 0 if value else 2
        band_cfg["dnd"] = value
    if cmd is None:
        return
    band._send(cmd)
    request_settings(band)
    hub.update(settings=settings_state())


def apply_alarm(band: Band, a: dict) -> None:
    def fill(d):
        d.time.hour, d.time.minute = a["hour"], a["minute"]
        d.repeatMode = a["repeat"]
        if a["repeat"] in (4, 5):
            d.repeatFlags = a.get("flags") or 31
        d.enabled = a["enabled"]
        d.smart = 1 if a.get("smart") else 2
    if a.get("delete"):
        cmd = xiaomi_pb2.Command(type=17, subtype=4)
        cmd.schedule.deleteAlarm.id.append(a["id"])
    elif a.get("id") is None:
        cmd = xiaomi_pb2.Command(type=17, subtype=1)
        fill(cmd.schedule.createAlarm)
    else:
        cmd = xiaomi_pb2.Command(type=17, subtype=2)
        cmd.schedule.editAlarm.id = a["id"]
        fill(cmd.schedule.editAlarm.alarmDetails)
    band._send(cmd)
    band.send(17, 0)


def apply_reminder(band: Band, r: dict) -> None:
    band._send(bandmsg.reminder(r))
    band.send(17, 14)


def write_user_info(band: Band) -> None:
    """Boy, kilo, doğum yılı, cinsiyet, azami nabız ve günlük hedefleri saate yazar (8/0)."""
    pr = config["profile"]
    band._send(bandmsg.user_info(pr, config["goals"], insights.max_hr(pr)))
    # bu saat 8/0 komutuna onay döndürmüyor ama değeri uyguluyor (20 Kasım 2026'da saatte doğrulandı)
    now = int(time.time())
    hub.update(userinfo={"sent": now, "written": now, "ok": True, "pending": False})


def auto_write() -> None:
    """Profil ya da hedef kaydedilince saate de yazar; saat bağlı değilse ilk bağlantıda yazar."""
    if hub.snapshot()["link"] == "connected":
        hub.jobs.put(write_user_info)
    else:
        user_info_pending.set()
        hub.update(userinfo={"sent": None, "written": None, "ok": None, "pending": True})


user_info_pending = threading.Event()


def apply_display(band: Band, order: list, off: set) -> None:
    if "display" not in band_cfg:
        return
    try:
        cmd = bandmsg.display_items(band_cfg["display"], order, off)
    except ValueError:  # liste bu arada saatte değişmiş
        band.send(2, 29)
        return
    band._send(cmd)
    band.send(2, 29)


# --- canlı antrenman ----------------------------------------------------
WORKOUT_QUIET = 60        # canlı değer yollayan antrenman bu kadar saniye susarsa bitmiş sayılır
WORKOUT_RESUME = 90       # yeniden bağlanınca antrenmanın sürdüğüne dair işaret bu sürede gelmeli
WORKOUT_MAX = 12 * 3600
workout = {"active": False, "paused": False, "sport": None, "started": None,
           "hr": None, "steps": None, "calories": None, "distance": None}
wk = {"heard": 0.0, "live": False, "base": None, "resumed": False}  # heard: son işaretin time.monotonic() değeri


def workout_begin(sport=None, ts=None) -> None:
    now = int(time.time())
    workout.update(active=True, paused=False, sport=sport, started=ts if ts and abs(ts - now) < 86400 else now,
                   hr=None, steps=None, calories=None, distance=None)
    wk.update(heard=time.monotonic(), live=False, base=None, resumed=False)


def workout_end() -> None:
    """Antrenmanı kapatır ve kaydı saatten indirmek için eşitleme ister."""
    if not workout["active"]:
        return
    workout.update(active=False, paused=False)
    hub.update(workout=dict(workout))
    hub.sync_now.set()


# --- uygulamadan başlatılan antrenmanın saatin ekranına yansıtılması -------------------------------
# Mi Fitness'ın GO düğmesi gibi: saate 8/26 ile "başlat" denir, saat antrenman ekranını açar ama
# değerleri kendi hesaplamaz; süre, nabız, kalori ve mesafe saniyede bir 8/49 ile gönderilir.
# (20 Kasım 2026'da Mi Fitness günlüğünden çıkarıldı ve saatte doğrulandı.) Bu kipte saat kendi
# antrenman dosyasını ve GNSS izini kaydetmez; kaydı uygulama tutar.
# Kodlar Mi Fitness günlüğünden: koşu 1, yürüyüş 2, koşu bandı 3 (üçü de 20 Kasım 2026'da doğrulandı); bisiklet 6
# başka kaynaktan, Mi Fitness konum bulamadığı için başlatılamadı ve doğrulanamadı.
BAND_SPORT = {"running": 1, "walking": 2, "treadmill": 3, "cycling": 6}
mirror = {"on": False, "sport": None, "grace": 0.0}  # grace: bitişten sonra saatten gelen son yankılar yok sayılır


def mirror_send(status: int) -> None:
    """status: 0 başlat, 1 duraklat, 2 devam, 3 bitir."""
    code = mirror["sport"]
    if code is None:
        return

    def job(band, code=code, status=status):
        cmd = xiaomi_pb2.Command(type=8, subtype=26)
        w = cmd.health.workoutStatusWatch
        w.timestamp = int(time.time())
        w.sportInfo.tzOffsetQuarterHours = int(datetime.now().astimezone().utcoffset().total_seconds() // 900)
        w.sport, w.status = code, status
        band._send(cmd)
    hub.jobs.put(job)


def mirror_start(sport: str) -> None:
    code = BAND_SPORT.get(sport)
    mirror.update(on=code is not None, sport=code)
    if code is not None:
        mirror_send(0)


def mirror_stop() -> None:
    if mirror["on"]:
        mirror_send(3)
        mirror["grace"] = time.time() + 20
    mirror.update(on=False, sport=None)


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def mirror_extra(st: dict) -> bytes:
    """8/49'un 5–9 numaralı alanları (AstroBox'ın PhoneSportDataV2A tanımından, 22 Kasım 2026).

    5 anlık tempo, 6 ortalama tempo (sn/km), 7 adım, 8 adım boyu (cm), 9 ritim (adım/dk). Etiketler kaynaktan
    doğrulandı; birimler çıkarımdır ve saatte henüz sınanmadı. Değeri olmayan alan hiç gönderilmez.
    """
    speed, steps, dist = st.get("speed"), st.get("steps") or 0, st.get("distance") or 0
    vals = {5: round(1000 / speed) if speed and speed >= 0.4 else None,
            6: st.get("pace"),
            7: steps or None,
            8: round(dist / steps * 100) if steps >= 20 and dist else None,
            9: st.get("cadence")}
    out = b""
    for tag, v in vals.items():
        if v and 0 < int(v) < 100000 and (tag not in (5, 6) or int(v) <= 3599):
            out += _varint(tag << 3) + _varint(int(v))
    return out


def mirror_stats() -> None:
    """Saatin antrenman ekranındaki sayılar: saniyede bir gönderilir."""
    if not (mirror["on"] and appwk.active) or hub.jobs.qsize() > 2 or hub.snapshot()["link"] != "connected":
        return
    st = appwk.state()
    if st.get("paused"):
        return

    def job(band, st=st):
        cmd = xiaomi_pb2.Command(type=8, subtype=49)
        w = cmd.health.workoutStatsPhone
        w.durationSeconds = int(st.get("elapsed") or 0)
        w.heartRate = int(st.get("hr") or 255)
        w.calories = int(st.get("calories") or 0)
        w.distance = int(st.get("distance") or 0)
        extra = mirror_extra(st)
        if extra:
            w.MergeFromString(extra)   # tanımda olmayan alanlar bilinmeyen alan olarak taşınır
        band._send(cmd)
    hub.jobs.put(job)


def mirror_event(ev: dict) -> None:
    """Yansıtma sürerken saatin kendi düğmesinden gelen duraklat / devam / bitir."""
    if ev["event"] == "pause":
        appwk.pause()
        appwk_coach(False)
    elif ev["event"] == "resume":
        appwk.resume()
        appwk_coach(True)
    elif ev["event"] == "finish":
        mirror.update(on=False, sport=None, grace=time.time() + 20)
        appwk_done(appwk.finish(time.time()))
        return
    appwk_push()


def workout_on_event(ev: dict) -> None:
    """Saatin başlat / duraklat / sürdür / bitir bildirimi."""
    if mirror["on"] and appwk.active:
        mirror_event(ev)
        return
    if time.time() < mirror["grace"]:
        return
    wk.update(heard=time.monotonic(), resumed=False)
    if ev["event"] == "finish":
        if workout["active"]:
            workout_end()
        else:  # başlangıcı görülmemiş antrenman da bitince kaydı indirilir
            hub.sync_now.set()
        return
    if ev["event"] == "start" or not workout["active"]:
        workout_begin(ev["sport"], ev["ts"] if ev["event"] == "start" else None)
        appwk.note_band_workout()
    if ev["sport"]:
        workout["sport"] = ev["sport"]
    workout["paused"] = ev["event"] == "pause"
    hub.update(workout=dict(workout))


def workout_on_live(stats: dict) -> None:
    """Saatin antrenman sırasında saniyede bir yolladığı değerler (antrenmanın başından sayılır)."""
    if mirror["on"] or time.time() < mirror["grace"]:
        if mirror["on"] and appwk.active:
            appwk.on_hr(stats.get("hr"))   # saatin saniyelik nabzı uygulama kaydına işlenir
        return  # yansıtılan antrenman uygulamanındır; saatin kendi antrenmanı sayılmaz
    if not workout["active"]:
        workout_begin()  # bağlanmadan önce başlamış antrenman: başlangıç saati bilinmiyor
        workout["started"] = None
        appwk.note_band_workout()
    wk.update(heard=time.monotonic(), live=True, resumed=False)
    workout.update(stats)
    hub.update(workout=dict(workout))


def workout_on_realtime(steps: int, calories: int, hr) -> None:
    """Saat canlı antrenman değeri yollamıyorsa günlük sayaçların antrenman başından beri farkı gösterilir."""
    if not workout["active"] or wk["live"]:
        return
    if wk["base"] is None:
        wk["base"] = (steps, calories)
    workout.update(hr=hr, steps=max(0, steps - wk["base"][0]), calories=max(0, calories - wk["base"][1]))
    hub.update(workout=dict(workout))


def workout_check(now: float) -> None:
    """Bitiş bildirimi kaçmış antrenmanı kapatır. now: time.monotonic()."""
    if not workout["active"]:
        return
    quiet = now - wk["heard"]
    if ((wk["live"] and not workout["paused"] and quiet > WORKOUT_QUIET) or (wk["resumed"] and quiet > WORKOUT_RESUME)
            or (workout["started"] and time.time() - workout["started"] > WORKOUT_MAX)):
        workout_end()


# --- cihaz durumu -------------------------------------------------------
device: dict = {}            # saatin son bildirdiği durum
wear_log = {"open": False}   # bu bağlantıda bilekte olma durumu kayda geçti mi


def device_changed(changes: dict) -> None:
    now = int(time.time())
    if "worn" in changes:
        before = device.get("worn")
        if before is not None and before != changes["worn"]:
            device["worn_since"] = now  # değişim bu bağlantıda görüldü
        if before != changes["worn"] or not wear_log["open"]:
            store.add_wear(changes["worn"], now)
            wear_log["open"] = True
    device.update(changes, updated=now)
    device.setdefault("worn_since", None)
    shown = {k: device.get(k) for k in ("worn", "charging", "asleep", "worn_since", "updated")}
    extra = {"charging": changes["charging"]} if "charging" in changes else {}
    if changes.get("battery") is not None:
        extra["battery"] = changes["battery"]
    hub.update(device=shown, **extra)


def device_lost() -> None:
    """Bağlantı koptu: bundan sonra saatin bilekte olup olmadığı bilinmiyor."""
    if wear_log["open"]:
        store.add_wear(None)
        wear_log["open"] = False
    device.clear()
    hub.update(device=None)


store = Store(DB_PATH, RAW)
store.upgrade()
store.log_event("start", i18n.t("servis başladı"))
insights.init(store)
store.add_wear(None)  # servis kapalıyken geçen süre bilinmiyor
# uygulamadan başlatılan antrenman (kurallar appworkout.py'nin başında)
appwk = appworkout.Recorder(store, lambda: config["profile"], lambda: insights.max_hr(config["profile"]))
appwk_last: dict = {}  # son biten antrenmanın sonucu (kendiliğinden bitenler arayüze bununla bildirilir)
appwk_restored = appwk.restore()  # yarım kalmış oturum: sürer ya da (çok eskiyse) bitirilip kaydedilir
hub = Hub()


# --- hava durumu --------------------------------------------------------
# Akış Gadgetbridge'in XiaomiWeatherService'indeki gibidir: saate önce konum listesi sorulur (10/5).
# Saat "desteklemiyorum" (durum 1) derse konum tek tek tanıtılır (10/7); liste tutuyorsa eksik konum
# eklenir (10/7), liste yazılır (10/6), veriler gider (10/0, 10/1, 10/2), sonra liste yeniden okunup
# bize ait olmayan konumlar silinir (10/8). Hava durumu kapalıyken bunların hiçbiri gönderilmez.
WEATHER_RETRY = 600   # alınamayınca en erken yeniden deneme (saniye)
WEATHER_WAIT = 10     # konum listesi sorusuna yanıt bu sürede gelmezse tek konumlu yol izlenir
wx = {"data": store.kv_get("weather"), "last_ok": store.kv_get("weather_sent"), "error": None, "tried": 0.0,
      "busy": False, "pending": False,          # pending: indirildi ama saate yazılmadı
      "multi": None, "on_band": set(), "asked": False, "want": 0.0}  # bu bağlantıda saatten öğrenilenler
wx_lock = threading.Lock()


def weather_data():
    """Seçili şehre ait son indirilen veri ya da None."""
    c, d = config["weather"], wx["data"]
    return d if d and (d.get("city"), d.get("lat"), d.get("lon")) == (c["city"], c["lat"], c["lon"]) else None


def weather_state() -> dict:
    c, d = config["weather"], weather_data()
    return {"enabled": c["enabled"], "city": c["city"], "lat": c["lat"], "lon": c["lon"], "every": c["every"],
            "last_ok": wx["last_ok"] if d else None, "temp": round(d["temp"]) if d else None,
            "text": weather.condition(d["code"]) if d else None, "error": wx["error"],
            "pending": bool(d and wx["pending"])}


def weather_fetch() -> str | None:
    """Tahmini indirir ve saate yazılmak üzere sıraya koyar. Ağa çıktığı için saat döngüsünde çağrılmaz.

    Hata iletisini ya da None döner. Dışarı yalnızca yuvarlanmış enlem ve boylam gider.
    """
    c = dict(config["weather"])
    if not c["enabled"] or c["lat"] is None or c["lon"] is None:
        return i18n.t("Hava durumu kapalı.")
    with wx_lock:
        if wx["busy"]:
            return None  # başka bir indirme sürüyor; sonucu o yazar
        wx["busy"] = True
    wx["tried"] = time.time()
    try:
        data = weather.forecast(c["lat"], c["lon"], c["city"])
    except weather.WeatherError as exc:
        wx["error"] = str(exc)
        hub.update(weather=weather_state())
        return str(exc)
    finally:
        wx["busy"] = False
    if dict(config["weather"]) != c:
        return None  # ayar bu arada değişti; bu sonuç kullanılmaz
    data.update(lat=c["lat"], lon=c["lon"], fetched=int(time.time()))
    wx.update(data=data, error=None, pending=True)
    store.kv_set("weather", data)
    if hub.snapshot()["link"] == "connected":
        hub.jobs.put(weather_push)
    hub.update(weather=weather_state())
    return None


def weather_send(band) -> None:
    """Konum kaydı ve üç veri komutu. Saatin konum listesi tutup tutmadığı bilinirken çağrılır."""
    d = weather_data()
    wx["want"] = 0.0
    if not (config["weather"]["enabled"] and d and wx["pending"]):
        return
    loc = (bandmsg.location_key(d["city"]), d["city"])
    if wx["multi"]:
        if loc not in wx["on_band"]:
            band._send(bandmsg.weather_location(d["city"]))
            wx["on_band"].add(loc)
        band._send(bandmsg.weather_locations([d["city"]]))
    else:
        band._send(bandmsg.weather_location(d["city"]))
    for build in (bandmsg.weather_current, bandmsg.weather_daily, bandmsg.weather_hourly):
        band._send(build(d))
    if wx["multi"]:
        band.send(10, 5)  # yanıtta saatte kalmış başka konumlar ayıklanır
    wx.update(pending=False, last_ok=int(time.time()))
    store.kv_set("weather_sent", wx["last_ok"])
    hub.update(weather=weather_state())


def weather_push(band) -> None:
    if not (config["weather"]["enabled"] and weather_data() and wx["pending"]):
        return
    if wx["multi"] is None:  # önce saatin konum listesi sorulur; yanıt gelince weather_send çalışır
        if not wx["want"]:
            wx["want"] = time.monotonic()
        if not wx["asked"]:
            wx["asked"] = True
            band.send(10, 5)
        return
    weather_send(band)


def weather_on_command(cmd) -> None:
    """Saatten gelen 10/x komutları. Hava durumu kapalıyken hiçbirine karşılık verilmez."""
    c = config["weather"]
    if not c["enabled"] or not c["city"]:
        return
    if cmd.subtype == 5:
        if cmd.status == 1:
            wx["multi"] = False
        elif cmd.status != 0:
            return
        else:
            wx["multi"] = True
            ours = (bandmsg.location_key(c["city"]), c["city"])
            found = bandmsg.weather_location_list(cmd.weather.locations)
            twice = sorted({x for x in found if found.count(x) > 1})
            stale = sorted({x for x in found if x != ours})
            for group in (twice, stale):
                if group:
                    hub.jobs.put(lambda band, group=group: band._send(bandmsg.weather_remove(group)))
            wx["on_band"] = {x for x in found if x == ours and found.count(x) == 1}
        if wx["want"]:
            hub.jobs.put(weather_send)
    elif cmd.subtype == 3 and cmd.status == 0:  # saat güncel veri istiyor (hava ekranı açıldı)
        if weather_data():
            wx["pending"] = True
            hub.jobs.put(weather_push)
    elif cmd.subtype in (1, 2) and cmd.status not in (0, 1):
        wx["error"] = i18n.t("Saat hava tahminini kabul etmedi (kod {code}).", code=cmd.status)
        hub.update(weather=weather_state())


def weather_tick(band) -> None:
    """Saat döngüsünün her turunda: süresi gelince yeniden indirir, bekleyen veriyi yazar."""
    c = config["weather"]
    if not c["enabled"] or c["lat"] is None:
        return
    if wx["want"] and time.monotonic() - wx["want"] > WEATHER_WAIT:
        wx["multi"] = False
        weather_send(band)
    now, d = time.time(), weather_data()
    if d and wx["pending"] and now - d.get("fetched", 0) < c["every"] * 60:
        weather_push(band)
    elif now - (wx["last_ok"] if d and wx["last_ok"] else 0) >= c["every"] * 60 and now - wx["tried"] >= WEATHER_RETRY \
            and not wx["busy"]:
        wx["tried"] = now
        threading.Thread(target=weather_fetch, daemon=True).start()


# --- takvim -------------------------------------------------------------
# Liste uygulamada tutulur; saate her seferinde önümüzdeki günlerin tamamı yazılır (12/1).
CALENDAR_KEEP = 200   # uygulamada saklanan en fazla etkinlik
CALENDAR_CHECK = 600  # pencereye yeni giren ya da biten etkinlik için denetim aralığı (saniye)
cal = store.kv_get("calendar_synced") or {"sig": None, "ts": None}  # saate en son yazılan liste


class Reject(ValueError):
    """İletisi kullanıcıya gösterilen doğrulama hatası."""


def calendar_due(now: int | None = None) -> list[dict]:
    return bandmsg.calendar_window(store.calendar_list(), int(now or time.time()))


def calendar_sig(due: list[dict]) -> list:
    return [[e["title"], e["note"], e["location"], e["start"], e["end"], bool(e["all_day"])] for e in due]


def calendar_state() -> dict:
    due = calendar_due()
    # hiç yazılmamış saat boş listeyle aynı sayılır: kullanıcı etkinlik girmeden saate bir şey yazılmaz
    return {"events": store.calendar_list(), "on_band": len(due), "max": bandmsg.CALENDAR_MAX,
            "days": bandmsg.CALENDAR_DAYS, "synced": cal["ts"], "pending": calendar_sig(due) != (cal["sig"] or [])}


def calendar_push(band) -> None:
    """Liste saattekinden farklıysa yazar; bitmiş etkinlikleri siler."""
    store.calendar_prune()
    due = calendar_due()
    sig = calendar_sig(due)
    if sig != (cal["sig"] or []):
        band._send(bandmsg.calendar_sync(due))
        cal.update(sig=sig, ts=int(time.time()))
        store.kv_set("calendar_synced", cal)
    hub.update(calendar=calendar_state())


def calendar_changed() -> bool:
    """Liste değişti: saat bağlıysa hemen yazılır. Sıraya alındıysa (saat bağlı değil) True döner."""
    store.calendar_prune()
    linked = hub.snapshot()["link"] == "connected"
    if linked:
        hub.jobs.put(calendar_push)
    hub.update(calendar=calendar_state())
    return not linked


def calendar_event(body: dict) -> dict:
    title = str(body.get("title") or "").strip()[:bandmsg.CALENDAR_TITLE]
    if not title:
        raise Reject(i18n.t("Başlık yaz."))
    try:
        day = date.fromisoformat(str(body.get("date")))
        all_day = bool(body.get("all_day"))
        if all_day:
            last = date.fromisoformat(str(body["end_date"])) if body.get("end_date") else day
            first, after = datetime.combine(day, datetime.min.time()), datetime.combine(last + timedelta(days=1), datetime.min.time())
        else:
            first = datetime.combine(day, datetime.strptime(str(body.get("time")), "%H:%M").time())
            if body.get("duration") not in (None, ""):
                after = first + timedelta(minutes=int(body["duration"]))
            else:
                last = date.fromisoformat(str(body["end_date"])) if body.get("end_date") else day
                after = datetime.combine(last, datetime.strptime(str(body.get("end_time")), "%H:%M").time())
        start, end = int(first.timestamp()), int(after.timestamp())
    except (ValueError, TypeError, OverflowError, OSError):
        raise Reject(i18n.t("Tarih ve saati seç.")) from None
    if end <= start:
        raise Reject(i18n.t("Bitiş başlangıçtan sonra olmalı."))
    if end - start > 31 * 86400:
        raise Reject(i18n.t("Bir etkinlik en çok 31 gün sürebilir."))
    if end <= time.time():
        raise Reject(i18n.t("Bitmiş bir etkinlik eklenemez."))
    return {"id": int(body["id"]) if body.get("id") is not None else None, "title": title,
            "location": str(body.get("location") or "").strip()[:bandmsg.CALENDAR_PLACE],
            "note": str(body.get("note") or "").strip()[:bandmsg.CALENDAR_NOTE],
            "start": start, "end": end, "all_day": all_day}


store.calendar_prune()
hub.state.update(weather=weather_state(), calendar=calendar_state())


# --- saat döngüsü -------------------------------------------------------
# Saat aynı anda tek uygulamayla konuşur. Başka bir uygulama (ör. telefonda Mi Fitness) saati tutuyorsa
# bağlantı kurulur ama birkaç saniyede kopar. Art arda QUICK_DROPS kez QUICK_DROP saniyeden kısa süren
# bağlantıdan sonra kullanıcı uyarılır ve yeniden deneme aralığı açılır (RETRY_EVERY, sonra RETRY_BACKOFF);
# bir bağlantı STABLE_AFTER saniye dayanınca her şey sıfırlanır.
QUICK_DROP = 5
QUICK_DROPS = 3
STABLE_AFTER = 30
RETRY_BACKOFF = (60, 120)
# Türkçe kaynak metinler; state'e yazılırken seçili dile çevrilir (dil değişince set_language yeniden çevirir)
HOST_HINT = i18n.mark("Saat bağlantısı birkaç saniyede kopuyor. Saati büyük olasılıkla başka bir uygulama (Mi Fitness) "
                      "tutuyor; o uygulamayı tümüyle kapatınca bağlantı kendiliğinden kurulur.")
PAUSE_HINT = i18n.mark("Saat bağlantısı duraklatıldı")


class LinkGuard:
    """Art arda hemen kopan bağlantıları sayar; bekleme süresini ve uyarıyı belirler."""

    def __init__(self):
        self.quick = 0   # art arda hemen kopan bağlantı sayısı
        self.level = 0   # 0: olağan aralık; 1..: RETRY_BACKOFF[level - 1]

    def stable(self) -> None:
        """Bağlantı STABLE_AFTER saniye dayandı."""
        self.quick = self.level = 0
        hub.update(host_hint=None)

    def ended(self, lasted: float | None) -> float:
        """Bağlantı bitti (lasted: kaç saniye sürdü; hiç kurulamadıysa None). Beklenecek süreyi döndürür."""
        if lasted is not None:
            if lasted >= STABLE_AFTER:
                self.stable()
            elif lasted < QUICK_DROP:
                self.quick += 1
                if self.quick >= QUICK_DROPS:
                    self.level = min(self.level + 1, len(RETRY_BACKOFF))
                    hub.update(host_hint=i18n.t(HOST_HINT))
            elif not self.level:
                self.quick = 0   # ne hemen koptu ne dayandı: sayım baştan
        return RETRY_BACKOFF[self.level - 1] if self.level else RETRY_EVERY


link_guard = LinkGuard()


def set_language(code: str) -> None:
    """Dili kaydeder; bundan sonra üretilen metinler ve state'te bekleyen metinler (hava, uyarı, spor adı) o dildedir."""
    config["lang"] = code
    save_config()
    i18n.set_lang(code)
    snap = hub.snapshot()
    workout["sport"] = i18n.switch(workout["sport"])
    hub.update(lang=code, weather=weather_state(), host_hint=i18n.switch(snap.get("host_hint")),
               workout=dict(snap["workout"], sport=i18n.switch(snap["workout"].get("sport"))))
retry_now = threading.Event()  # beklemeyi keser (ör. telefonda Bluetooth yeniden açılınca)


def rpk_job(action: str, data: bytes | None = None, package: str | None = None):
    """Saat uygulaması işi (oturum döngüsünde çalışır); sonucu bağlantı günlüğüne yazar."""
    def run(band):
        try:
            if action == "install":
                last = {"n": 0}

                def progress(n, total):
                    if n == total or n - last["n"] >= 4:
                        last["n"] = n
                        hub.update(rpk=f"{n}/{total}")
                r = rpk.install(band, data, progress)
                store.log_event("rpk", i18n.t("{package} gönderildi, saat yüklendiğini bildirdi", package=r["package"]) if r["installed"]
                                else i18n.t("{package} gönderildi, saatten yüklendi bildirimi gelmedi", package=r["package"]))
            elif action == "delete":
                store.log_event("rpk", i18n.t("{package} için silme isteği gönderildi", package=package) if rpk.delete(band, package)
                                else i18n.t("{package} saatte bulunamadı", package=package))
            apps = rpk.list_apps(band)
            store.log_event("rpk", i18n.t("saatteki uygulamalar: {apps}", apps=", ".join(f"{a['name']} ({a['id']})" for a in apps) or i18n.t("yok")))
            hub.update(rpk=None, rpk_apps=[{"id": a["id"], "name": a["name"]} for a in apps])
        except (TimeoutError, RuntimeError, ValueError, KeyError) as exc:
            store.log_event("rpk", i18n.t("başarısız: {error}", error=exc))
            hub.update(rpk=None)
    return run


probe = {"type": None, "until": 0.0}   # tanılama sorgusu: bu türden gelen yanıtlar bağlantı günlüğüne yazılır
pause = {"until": 0.0}   # saat bağlantısı bu ana kadar kurulmaz (tanılama: saat kendi halindeyken ne kaydediyor)


def pause_link(minutes: float) -> None:
    """Saat bağlantısını keser ve verilen süre yeniden kurmaz; 0 duraklatmayı bitirir."""
    pause["until"] = time.time() + max(0.0, minutes) * 60
    if minutes > 0:
        store.log_event("pause", i18n.t("saat bağlantısı {minutes} dakika duraklatıldı", minutes=round(minutes)))

        def drop(_band):
            raise ConnectionAbortedError("bağlantı duraklatıldı")
        hub.jobs.put(drop)
    else:
        retry_now.set()


def band_loop() -> None:
    while True:
        while time.time() < pause["until"]:
            hub.update(link="away", hr=None, host_hint=i18n.t(PAUSE_HINT))
            time.sleep(5)
        if hub.snapshot().get("host_hint") == i18n.t(PAUSE_HINT):
            hub.update(host_hint=None)
        band = Band(config["address"], config["auth_key"], config.get("channel", 5), transport=transport_factory,
                    app_device_id=config.get("app_device_id"))
        since = None
        stable = threading.Timer(STABLE_AFTER, link_guard.stable)
        stable.daemon = True
        try:
            hub.update(link="connecting")
            band.connect()
            since = time.monotonic()
            stable.start()
            hub.update(link="connected")
            print("Saate bağlandı", flush=True)
            session(band)
        except Exception as exc:  # bağlantı koptu ya da saat menzilde değil
            print(f"Saat bağlantısı yok: {exc!r}", flush=True)
            if since is None and band.sock is not None and isinstance(exc, ConnectionError):
                since = time.monotonic()  # kanal açıldı, saat el sıkışma bitmeden kapattı: bu da hemen kopmadır
        finally:
            stable.cancel()
            band.close()
        hub.update(link="away", hr=None)
        device_lost()
        retry_now.clear()
        retry_now.wait(link_guard.ended(None if since is None else time.monotonic() - since))


def last_fetch() -> float:
    with store.lock:
        return store.db.execute("SELECT MAX(fetched) FROM files").fetchone()[0] or 0


def sleep_fetch_ok() -> bool:
    """Uyku dosyası yalnızca kullanıcı uyanıp hareket edince indirilir.

    20-21 Kasım 2026 gecesi görüldü: uyku dosyası her indirildiğinde saat o ana kadarki uykuyu bir bölüm olarak
    kapatıyor (bölüm bitişleri indirme saatleriyle dakikası dakikasına aynıydı) ve sabah 07:46'daki indirmeden
    sonra geceyi bitmiş sayıp 09:45'e kadar süren uykuyu hiç kaydetmedi. Bu yüzden: son 20 dakikada en az 60
    adım atılmışsa (kalkmış, dolaşıyor) ya da öğleden sonraysa indirilir; ayrıca en çok yarım saatte bir.
    """
    now = time.time()
    if now - sleep_fetch_ok.last < 90:   # aynı eşitlemedeki öteki uyku dosyaları da inebilsin
        return True
    if now - sleep_fetch_ok.last < 1800:
        return False
    hour = datetime.now().hour
    with store.lock:
        steps = store.db.execute("SELECT COALESCE(SUM(steps), 0) FROM minutes WHERE ts >= ?", (int(now) - 1200,)).fetchone()[0]
    if steps >= 60 or 13 <= hour < 20:
        sleep_fetch_ok.last = now
        return True
    return False


sleep_fetch_ok.last = 0.0


def sync_files(band: Band) -> bool:
    """Saatteki dosyaları indirir ve veritabanına yazılanlar için saate onay gönderir (saat onları siler).

    21 Kasım 2026'ya kadar onay hiç gönderilmiyordu ("şimdilik silme"); saatte biriken dosyalar yüzünden saatin
    dakikalık kaydı yazmayı bıraktığı şüphesiyle kullanıcı onayı açtırdı. config.json'da "band_ack": false eski
    davranışa döner (dosyalar saatte kalır, Mi Fitness da alabilir).
    """
    ack = config.get("band_ack", True)
    acked = 0
    for today in (True, False):
        fids = band.list_files(today)
        print("Dosya listesi:", "bugün" if today else "geçmiş", len(fids), "dosya", flush=True)
        # Saat her indirmeden sonra yeni dosya açar; eskiler kapanır ve artık değişmez. Bu yüzden yalnızca
        # her türün en yeni dosyası (hâlâ yazılan) ile hiç inmemiş olanlar indirilir.
        newest: dict = {}
        for fid in fids:
            i = parse.file_id(fid)
            k = (i["type"], i["subtype"], i["detail"])
            newest[k] = max(newest.get(k, 0), i["ts"])
        for fid in fids:
            i = parse.file_id(fid)
            open_file = today and i["ts"] == newest[(i["type"], i["subtype"], i["detail"])]
            if not open_file and store.file_size(fid.hex()) is not None:
                if ack and acked < 60:   # daha önce inmiş, saatte bekleyen dosya: artık silinebilir
                    band.ack_file(fid)
                    acked += 1
                continue
            if parse.kind(i).startswith("sleep") and not sleep_fetch_ok():
                continue  # uyku sürerken dosyaya dokunulmaz (aşağıdaki açıklama)
            if livereq["req"] or not hub.jobs.empty():
                return False  # kullanıcı bir şey istedi; önce o yapılır, kalan dosyalar sonraki turda iner
            data = band.fetch_file(fid)
            if data:
                kind_name = store.save_file(fid, data)
                print("Dosya alındı:", kind_name, len(data), "bayt", flush=True)
                if not kind_name.startswith("daily"):
                    store.log_event("file", i18n.t("{kind}, {n} bayt", kind=kind_name, n=len(data)))
                if ack and store.file_size(fid.hex()) is not None:   # yalnızca gerçekten kaydedilen dosya
                    band.ack_file(fid)
                    acked += 1
    if acked:
        print("Saate onay gönderildi:", acked, "dosya", flush=True)
        store.log_event("ack", i18n.t("{n} dosya için saate onay gönderildi", n=acked))
    hub.update(synced=int(time.time()))
    return True


ZONES = {1: (.5, .6), 2: (.6, .7), 3: (.7, .8), 4: (.8, .9), 5: (.9, 1.0)}
COACH_GRACE = 10     # bölge dışında kaç saniye kalınca uyarılır
COACH_REPEAT = 45    # bölge dışında kalmaya devam ederse kaç saniyede bir yinelenir
COACH_MAX = 3 * 3600
coach = {"on": False, "zone": 2, "since": 0.0, "out_since": None, "last_alert": 0.0}


def appwk_push() -> None:
    hub.update(app_workout=appwk.state())


# --- telefonun konumu (yalnızca konum kaynağı olan kurulumda; bilgisayarda bunlar hiç çağrılmaz) ---
GPS_PUSH = 3   # konum geldikçe sayfalara en çok bu kadar saniyede bir durum yollanır
gps_pushed = {"at": 0.0, "state": None}


def location_enable(on: bool = True) -> None:
    """Konum kaynağı var: arayüz açık hava antrenmanında konumu ister, harita gösterir (state.caps.gps)."""
    hub.update(caps=dict(hub.snapshot()["caps"], gps=bool(on)))


def location_fix(ts: float, lat: float, lon: float, accuracy: float | None, speed: float | None = None,
                 alt: float | None = None) -> bool:
    """Konum kaynağından bir nokta (süzme ve mesafe appworkout.py'de). Kabul edildiyse True."""
    ok = appwk.on_fix(ts, lat, lon, accuracy, speed, alt)
    location_push()
    return ok


def location_problem(text: str | None) -> None:
    """Konum alınamıyor (izin yok, konum kapalı); None: sorun kalktı."""
    appwk.gps_problem(text)
    location_push(force=True)


def location_push(force: bool = False) -> None:
    if not appwk.active:
        return
    st = appwk.state()
    what = (st.get("gps") or {}).get("state")
    if force or what != gps_pushed["state"] or time.time() - gps_pushed["at"] >= GPS_PUSH:
        gps_pushed.update(at=time.time(), state=what)
        hub.update(app_workout=st)


def appwk_coach(on: bool) -> None:
    """Antrenmanın bölge uyarısını var olan bölge antrenmanı üzerinden açar ya da kapatır."""
    zone = (appwk.s or {}).get("zone")
    if on and zone:
        coach.update(on=True, zone=zone, since=time.time(), out_since=None, last_alert=0.0, app=True)
    elif coach.get("app"):
        coach.update(on=False, out_since=None, app=False)
    hub.update(coach=coach_state(hub.snapshot().get("hr")))


def appwk_done(result: dict) -> dict:
    """Antrenman bitti ya da silindi: bölge uyarısı kapanır, liste yenilensin diye sonuç arayüze gider."""
    appwk_coach(False)
    appwk_last.clear()
    appwk_last.update(result, at=int(time.time()))
    hub.update(app_workout=appwk.state(), app_workout_last=dict(appwk_last))
    return result


def appwk_tick(now: float | None = None) -> None:
    """Saniyede bir: hedef bildirimi, uzun sessizlikte ya da üst sınırda kendiliğinden bitiş."""
    if not appwk.active:
        if mirror["on"]:
            mirror_stop()
        return
    mirror_stats()
    connected = hub.snapshot()["link"] == "connected"
    done = appwk.tick(now, connected)
    if done and done[0] == "goal":
        text = done[1]
        title = i18n.t("Hedefe ulaşıldı")
        hub.jobs.put(lambda band: band.notify(title, text))
    elif done:
        mirror_stop()
        appwk_done(done[1])
        return
    before = hub.snapshot()["app_workout"]
    with hub.lock:
        shown = hub.state.get("app_workout") or {}
    # süre her saniye değişir; yalnızca durum değişince (veri kesildi, hedef doldu) sayfalara yollanır
    if (shown.get("stale"), (shown.get("goal") or {}).get("reached")) != (before["stale"], (before["goal"] or {}).get("reached")):
        hub.update(app_workout=before)


def appwk_loop() -> None:
    while True:
        try:
            appwk_tick()
            phone_flush()
        except Exception as exc:  # kayıt döngüsü tek bir hatayla durmasın
            print(f"Antrenman kaydı: {exc!r}", flush=True)
        time.sleep(1)


# --- telefonun bildirimleri ve müziği (yalnızca telefondaki uygulamada; bilgisayarda bunlar çağrılmaz) ---
# Telefon tarafı (Kotlin) yalnızca olayı toplar ve saatten gelen isteği uygular; kodlama bandmsg.py'dedir.
# Gizlilik: bildirim metni yalnızca saate Bluetooth ile gider. Bellekte, sırası gelene dek (en çok
# PHONE_RATE saniye; saat bağlı değilse hiç) bekleyen bildirimde durur; gönderilenlerin yalnızca kimliği,
# paketi, anahtarı ve metnin özeti (CRC) tutulur. Günlüğe ve veritabanına metin yazılmaz.
PHONE_RATE = 2      # aynı uygulamadan en çok bu kadar saniyede bir bildirim gider; aradakiler sonuncuda birleşir
PHONE_DEDUPE = 5    # aynı bildirimin aynı içerikle yinelenmesi bu süre içinde yok sayılır
PHONE_STALE = 20    # sıraya giren bildirim bu sürede saate yazılamazsa atılır
PHONE_KEEP = 128    # kaldırma eşlemesi için hatırlanan bildirim sayısı
phone = {"sent": {}, "pending": {}, "last_at": {}, "music": None, "music_sent": None, "volume": 0,
         "callbacks": {}, "call": False}
phone_lock = threading.Lock()


def phone_enable(notifications: bool = True, music: bool = True) -> None:
    """Telefon bildirimi ve müzik kaynağı var (state.caps); arayüz ilgili kutuyu gösterir."""
    hub.update(caps=dict(hub.snapshot()["caps"], notifications=bool(notifications), music=bool(music)))


def phone_on(event: str, fn) -> None:
    """Saatten gelen istekler için alıcı: "notification_dismiss" {"key", "package"}, "media" {"key", "volume"},
    "call" {"action": "reject" | "ignore"}."""
    phone["callbacks"][event] = fn


def phone_emit(event: str, data: dict) -> None:
    fn = phone["callbacks"].get(event)
    if fn is None:
        return
    try:
        fn(data)
    except Exception as exc:
        print(f"Telefon isteği uygulanamadı ({event}): {exc!r}", flush=True)


def phone_id(key: str) -> int:
    """Bildirim anahtarından saatin kullandığı sayısal kimlik (aynı anahtar hep aynı kimlik)."""
    return (zlib.crc32(key.encode()) & 0x7FFFFFFF) or 1


# --- tartı (Xiaomi S400): telefon, tartının şifreli yayınını dinleyip servis verisini buraya verir ---
SCALE_OTHER_KG = 12     # profildeki kilodan bu kadar farklıysa başka biri tartılmıştır; kaydedilmez
SCALE_PAIR = 120        # aynı tartılmanın iki paketi (kilo ve 250 kHz empedans) en çok bu kadar saniye arayla gelir
scale_lock = threading.Lock()
scale_seen: dict = {"key": None, "at": 0.0, "row": None, "high": None, "high_at": 0.0}


def scale_address() -> str:
    """Dinlenecek tartının adresi; ayarlı değilse ya da anahtar geçersizse boş."""
    sc = config.get("scale") or {}
    return sc.get("address", "").strip().upper() if scale.valid_key((sc.get("bind_key") or "").strip()) else ""


def scale_frame(data: bytes) -> dict | None:
    """Tartının bir yayını. Yeni bir ölçümse kaydeder ve çözülmüş değerleri döndürür; değilse None."""
    sc = config.get("scale") or {}
    r = scale.decode(data, sc.get("address", "").strip(), (sc.get("bind_key") or "").strip())
    if not r:
        return None
    key = (r["counter"], r.get("weight"), r.get("impedance_low"), r.get("impedance_high"))
    now = time.time()
    with scale_lock:
        if scale_seen["key"] == key and now - scale_seen["at"] < 900:
            return None  # aynı paket defalarca yayınlanır
        scale_seen.update(key=key, at=now)
        if "weight" in r:
            kg, ref = r["weight"], config["profile"].get("weight")
            if ref and abs(kg - ref) > SCALE_OTHER_KG:
                scale_seen["row"] = None
                store.log_event("scale", i18n.t("profildeki kilodan çok farklı bir ölçüm geldi, kaydedilmedi"))
                return None
            ts = int(now)
            high = scale_seen["high"] if now - scale_seen["high_at"] < SCALE_PAIR else None
            with store.lock:
                store.db.execute("INSERT OR REPLACE INTO weights VALUES (?,?)", (ts, kg))
                store.db.execute("INSERT OR REPLACE INTO scale VALUES (?,?,?,?,?)",
                                 (ts, kg, r.get("heart_rate"), r.get("impedance_low"), high))
                store.db.commit()
            scale_seen.update(row=ts, high=None)
            config["profile"]["weight"] = kg
            save_config()
            pr = config["profile"]
            hub.update(profile=dict(pr, max_hr=insights.max_hr(pr), bmr=round(insights.bmr(pr))), scale_at=ts)
            store.log_event("scale", i18n.t("tartıdan {kg} kg", kg=i18n.num(kg)))
            auto_write()
        elif "impedance_high" in r:
            row = scale_seen["row"]
            if row and now - row < SCALE_PAIR:
                with store.lock:
                    store.db.execute("UPDATE scale SET imp_high = ? WHERE ts = ?", (r["impedance_high"], row))
                    store.db.commit()
                hub.update(scale_at=int(now))
            else:  # kilo paketi henüz gelmedi: gelince eklenir
                scale_seen.update(high=r["impedance_high"], high_at=now)
    return r


def phone_notify(n: dict) -> str:
    """Telefondan bir bildirim: {"key", "package", "app", "title", "body", "when"?}.

    Dönen değer: "sent" | "queued" (aynı uygulamadan az önce gitti; PHONE_RATE dolunca sonuncusu gider) |
    "duplicate" | "offline" (saat bağlı değil: bekletilmez, atılır).
    """
    if hub.snapshot()["link"] != "connected":
        return "offline"
    now = time.monotonic()
    mark = zlib.crc32(f"{n.get('title')}\0{n.get('body')}".encode())
    with phone_lock:
        old = phone["sent"].get(n["key"])
        if old and old["mark"] == mark and now - old["at"] < PHONE_DEDUPE:
            return "duplicate"
        if now - phone["last_at"].get(n["package"], -1e9) < PHONE_RATE:
            phone["pending"][n["package"]] = dict(n, mark=mark)   # aynı uygulamanın bekleyeni varsa yerine geçer
            return "queued"
        phone_send(n, mark, now)
    return "sent"


def phone_send(n: dict, mark: int, now: float) -> None:
    """Kilit çağıranda. Bildirimi saat döngüsünün sırasına koyar."""
    nid = phone_id(n["key"])
    sent = phone["sent"]
    sent.pop(n["key"], None)
    sent[n["key"]] = {"id": nid, "package": n["package"], "mark": mark, "at": now}
    while len(sent) > PHONE_KEEP:
        sent.pop(next(iter(sent)))
    phone["last_at"][n["package"]] = now
    cmd = bandmsg.notification(dict(n, id=nid))
    # bağlantı o arada koparsa bayat bildirim sırada beklemez: PHONE_STALE saniyeden eskiyse gönderilmez
    hub.jobs.put(lambda band: time.monotonic() - now < PHONE_STALE and band._send(cmd))
    hub.update(phone_last={"app": str(n.get("app") or n["package"])[:40], "at": int(time.time())})


def phone_flush() -> None:
    """Saniyede bir: sırası gelen bekleyen bildirimleri gönderir."""
    if not phone["pending"]:
        return
    now = time.monotonic()
    connected = hub.snapshot()["link"] == "connected"
    with phone_lock:
        for package in list(phone["pending"]):
            if not connected:
                del phone["pending"][package]
            elif now - phone["last_at"].get(package, -1e9) >= PHONE_RATE:
                n = phone["pending"].pop(package)
                phone_send(n, n.pop("mark"), now)


def phone_notify_remove(key: str) -> bool:
    """Bildirim telefonda kapandı: saatte de kaldırılır. Saate gitmiş bir bildirimse True."""
    with phone_lock:
        for package, n in list(phone["pending"].items()):
            if n["key"] == key:
                del phone["pending"][package]
        old = phone["sent"].pop(key, None)
    if not old:
        return False
    cmd = bandmsg.notification_dismiss([{"id": old["id"], "package": old["package"], "key": key}])
    hub.jobs.put(lambda band: band._send(cmd))
    return True


def phone_call(name: str | None, number: str | None = None) -> None:
    """Gelen arama çalıyor: saatte arayan gösterilir (kaynaktaki gibi kimlik 0, paket "phone")."""
    if hub.snapshot()["link"] != "connected":
        return
    phone["call"] = True
    cmd = bandmsg.notification({"id": 0, "package": "phone", "app": "phone", "title": name or "?",
                                "body": number or "?", "call": True})
    hub.jobs.put(lambda band: band._send(cmd))


def phone_call_end() -> None:
    if not phone["call"]:
        return
    phone["call"] = False
    cmd = bandmsg.notification_dismiss([{"id": 0, "package": "phone"}])
    hub.jobs.put(lambda band: band._send(cmd))


def music_state(m: dict | None, volume: int | None = None, force: bool = False) -> bool:
    """Telefonda çalan: None ya da {"state", "track", "artist", "position", "duration"}; volume yüzde.

    Yalnızca değişince saate yazılır: durum, parça, sanatçı, süre, ses ya da konumda atlama (ileri/geri sarma).
    """
    now = time.monotonic()
    with phone_lock:
        if volume is not None:
            phone["volume"] = int(volume)
        phone["music"] = dict(m, at=now) if m else None
        cur, last = phone["music"], phone["music_sent"]
        sig = (None if not cur else (cur.get("state"), cur.get("track"), cur.get("artist"), int(cur.get("duration") or 0)),
               phone["volume"])
        changed = force or last is None or last["sig"] != sig
        if not changed and cur:   # konum: çalarken kendiliğinden ilerler; beklenenden 3 sn'den fazla saparsa atlamadır
            ahead = now - last["at"] if cur.get("state") == "playing" else 0
            changed = abs((cur.get("position") or 0) - (last["position"] + ahead)) > 3
        if not changed or hub.snapshot()["link"] != "connected":
            return False
        phone["music_sent"] = {"sig": sig, "position": (cur or {}).get("position") or 0, "at": now}
        cmd = bandmsg.music_info(cur, phone["volume"])
    hub.jobs.put(lambda band: band._send(cmd))
    return True


def music_resend() -> None:
    """Saat istedi (18/0) ya da yeniden bağlanıldı: bilinen son durum, geçen süre eklenerek gider."""
    with phone_lock:
        cur = phone["music"]
        m = None
        if cur:
            m = {k: v for k, v in cur.items() if k != "at"}
            if cur.get("state") == "playing":
                m["position"] = (cur.get("position") or 0) + time.monotonic() - cur["at"]
        phone["music_sent"] = None
    music_state(m, force=True)


def phone_on_command(cmd) -> None:
    """Saatten gelen bildirim (7) ve müzik (18) komutları."""
    if cmd.type == 7 and cmd.subtype == 1:   # saatte kapatıldı: telefonda da kapansın
        ids = set(bandmsg.notification_dismissed(cmd))
        with phone_lock:
            gone = [(k, v) for k, v in phone["sent"].items() if v["id"] in ids]
            for k, _ in gone:
                del phone["sent"][k]
        for k, v in gone:
            phone_emit("notification_dismiss", {"key": k, "package": v["package"]})
    elif cmd.type == 7 and cmd.subtype in (2, 5):
        phone_emit("call", {"action": "reject" if cmd.subtype == 2 else "ignore"})
    elif cmd.type == 18 and cmd.subtype == 0:
        music_resend()
    elif cmd.type == 18 and cmd.subtype == 2:
        key = bandmsg.music_key(cmd)
        if key:
            if key["key"] == "volume":   # saat istenen düzeyi yollar; kaynaktaki gibi artır/azalt'a çevrilir
                key["direction"] = "up" if (key["volume"] or 0) > phone["volume"] else "down"
            phone_emit("media", key)


LIVE_SECONDS = 30    # "Canlı ölç" düğmesinin canlı akışı açık tuttuğu süre
LIVE_GRACE = 15      # sensörün açılıp ilk değerin gelmesi için tanınan süre
livereq = {"req": False, "until": 0.0, "first": False}


def coach_state(hr=None) -> dict:
    top = insights.max_hr(config["profile"])
    lo, hi = (round(top * x) for x in ZONES[coach["zone"]])
    where = None if not coach["on"] or not hr else "low" if hr < lo else "high" if hr > hi else "in"
    return {"on": coach["on"], "zone": coach["zone"], "lo": lo, "hi": hi, "where": where,
            "started": int(coach["since"]) if coach["on"] else None}


def coach_check(hr) -> None:
    """Canlı nabız seçilen bölgenin dışına çıkınca saate bildirim gönderir."""
    now = time.time()
    if coach["on"] and now - coach["since"] > COACH_MAX and not (coach.get("app") and appwk.active):
        coach["on"] = False  # uygulama antrenmanının uyarısı antrenmanla birlikte biter
    st = coach_state(hr)
    hub.update(coach=st)
    if st["where"] in (None, "in"):
        coach["out_since"] = None
        return
    coach["out_since"] = coach["out_since"] or now
    if now - coach["out_since"] >= COACH_GRACE and now - coach["last_alert"] >= COACH_REPEAT:
        coach["last_alert"] = now
        if st["where"] == "low":
            title = i18n.t("Bölge {zone} altı", zone=st["zone"])
            text = i18n.t("Nabız {hr}. Hızlan, hedef {lo}-{hi}.", hr=hr, lo=st["lo"], hi=st["hi"])
        else:
            title = i18n.t("Bölge {zone} üstü", zone=st["zone"])
            text = i18n.t("Nabız {hr}. Yavaşla, hedef {lo}-{hi}.", hr=hr, lo=st["lo"], hi=st["hi"])
        hub.jobs.put(lambda band: band.notify(title, text))


if appwk_restored:  # servis kapalıyken süresi dolmuş oturum açılışta bitirildi
    appwk_done(appwk_restored)
elif appwk.active and not appwk.s["paused"]:
    appwk_coach(True)


def session(band: Band) -> None:
    t = {"event": 0.0, "saved_at": 0.0, "saved": None, "polling": False}

    def on_command(cmd) -> None:
        if probe["type"] == cmd.type and time.time() < probe["until"]:   # tanılama: yalnızca okuma sorgusunun yanıtı
            raw = cmd.SerializeToString()
            store.log_event("probe", i18n.t("{cmd} yanıt, durum {status}, {n} bayt: {hex}", cmd=f"{cmd.type}/{cmd.subtype}",
                                            status=cmd.status if cmd.HasField("status") else "-", n=len(raw), hex=raw[:900].hex()))
        if cmd.type in (7, 18):
            phone_on_command(cmd)
        elif cmd.type == 2 and cmd.subtype == 1:
            b = cmd.system.power.battery
            hub.update(battery=b.level, charging=b.state == 1)
        elif cmd.type == 2 and cmd.subtype == 2:
            d = cmd.system.deviceInfo
            hub.update(model=d.model, firmware=d.firmware)
        elif cmd.type == 17 and cmd.subtype == 0:
            hub.update(alarms=[
                {"id": a.id, "hour": a.alarmDetails.time.hour, "minute": a.alarmDetails.time.minute,
                 "enabled": a.alarmDetails.enabled, "repeat": a.alarmDetails.repeatMode,
                 "flags": a.alarmDetails.repeatFlags, "smart": a.alarmDetails.smart == 1}
                for a in cmd.schedule.alarms.alarm], max_alarms=cmd.schedule.alarms.maxAlarms or 10)
        elif cmd.type == 2 and cmd.subtype == 78:
            got = bandmsg.device_basic(cmd.system.basicDeviceState) if cmd.system.HasField("basicDeviceState") else None
            store.log_event("device", i18n.t("durum sorgusu: {detail}", detail=", ".join(f"{k}={v}" for k, v in got.items()) if got else i18n.t("saat alan göndermedi")))
            if got is not None:
                device_changed(got)
        elif cmd.type == 2 and cmd.subtype == 79:
            if cmd.system.HasField("deviceState"):
                got = bandmsg.device_update(cmd.system.deviceState)
                store.log_event("device", i18n.t("durum bildirimi: {detail}", detail=", ".join(f"{k}={v}" for k, v in got.items()) or i18n.t("bilinen alan yok")))
                device_changed(got)
            hub.jobs.put(lambda band: band.send(2, 1))  # pil düzeyi ayrıca okunur
        elif cmd.type == 2 and cmd.subtype == 29:
            if cmd.system.HasField("displayItems"):
                band_cfg["display"] = cmd.system.displayItems
                hub.update(display=bandmsg.display_state(cmd.system.displayItems))
        elif cmd.type == 2 and cmd.subtype == 30:
            hub.jobs.put(lambda band: band.send(2, 29))
        elif cmd.type == 8 and cmd.subtype == 0:
            sent = (hub.snapshot().get("userinfo") or {}).get("sent")
            if sent:
                hub.update(userinfo={"sent": sent, "written": int(time.time()), "ok": cmd.status == 0})
        elif cmd.type == 8 and cmd.subtype == 42:
            if cmd.health.HasField("goalsConfig"):
                band_cfg["goals"] = bandmsg.goals_config(cmd.health.goalsConfig)
                hub.update(settings=settings_state())
        elif cmd.type == 8 and cmd.subtype in (26, 29):
            ev = bandmsg.workout_event(cmd)
            if ev:
                workout_on_event(ev)
        elif cmd.type == 8 and cmd.subtype == 50:
            if cmd.health.HasField("workoutStatsWatch"):
                workout_on_live(bandmsg.workout_stats(cmd.health.workoutStatsWatch))
        elif cmd.type == 17 and cmd.subtype == 14:
            if cmd.schedule.HasField("reminders"):
                items, most = bandmsg.reminders(cmd.schedule.reminders)
                hub.update(reminders=items, max_reminders=most or None)
        elif cmd.type == 17 and cmd.subtype in (15, 17, 18):
            hub.jobs.put(lambda band: band.send(17, 14))  # onay geldi, liste yeniden okunur
        elif cmd.type == 10:
            print(f"HAVA yanıt 10/{cmd.subtype} durum={cmd.status if cmd.HasField('status') else '-'}:", str(cmd).replace(chr(10), ' ')[:300], flush=True)
            weather_on_command(cmd)
        elif cmd.type == 17 and cmd.subtype == 8:
            if cmd.schedule.HasField("sleepMode"):
                band_cfg["sleep"] = cmd.schedule.sleepMode
                hub.update(settings=settings_state())
        elif cmd.type == 8 and cmd.subtype in (10, 8, 12, 14):
            key, field = {10: ("hr", "heartRate"), 8: ("spo2", "spo2"), 12: ("standing", "standingReminder"),
                          14: ("stress", "stress")}[cmd.subtype]
            if cmd.health.HasField(field):
                band_cfg[key] = getattr(cmd.health, field)
                hub.update(settings=settings_state())
        elif cmd.type == 2 and cmd.system.HasField("raiseToWake"):
            band_cfg["raise"] = cmd.system.raiseToWake
            hub.update(settings=settings_state())
        elif cmd.type == 2 and cmd.system.HasField("dndStatus"):
            band_cfg["dnd"] = cmd.system.dndStatus.status == 0
            hub.update(settings=settings_state())
        elif cmd.type == 8 and cmd.subtype == 47:
            s = cmd.health.realTimeStats
            hr = s.heartRate if s.heartRate > 10 else None
            t["event"] = time.monotonic()
            was_polling = t["polling"]
            if hr:  # sensör kilitlenmeden gelen ilk paketlerde nabız boş olur; ölçüm gerçek değer gelince biter
                t["polling"] = False
            hub.update(steps=s.steps, calories=s.calories, hr=hr, updated=int(time.time()))
            # uygulamanın kendi ölçümü sayılanlar: "Canlı ölç" ve sayfa açıkken dakikada bir yapılan ölçüm
            if hr and (time.time() < livereq["until"] or (was_polling and (hub.viewers or t.pop("fill", False)))):
                hub.update(last_hr={"value": hr, "ts": int(time.time())})
                store.add_live(hr)
                if livereq["first"] and time.time() < livereq["until"]:
                    livereq.update(first=False, until=time.time() + LIVE_SECONDS)
                    hub.update(live={"state": "on", "until": int(livereq["until"])})
            coach_check(hr)
            workout_on_realtime(s.steps, s.calories, hr)
            if appwk.active:
                appwk.on_reading(s.steps, s.calories, hr)
                appwk_push()
            current = (s.steps, s.calories, hr)
            if current != t["saved"] or t["event"] - t["saved_at"] >= 60:
                store.add(s.steps, s.calories, hr, hub.snapshot()["battery"])
                t["saved"], t["saved_at"] = current, t["event"]

    band.on_command = on_command
    hub.update(coach=coach_state())
    if phone["music"] is not None:   # telefonda çalan (yalnızca müzik kaynağı olan kurulumda dolar)
        hub.jobs.put(lambda band: music_resend())
    if appwk.active and not mirror["on"] and BAND_SPORT.get((appwk.s or {}).get("sport")) is not None:
        mirror.update(on=True, sport=BAND_SPORT[appwk.s["sport"]])  # yeniden bağlanınca yansıtma sürer
    if user_info_pending.is_set():  # saat yokken kaydedilen profil/hedef
        user_info_pending.clear()
        hub.jobs.put(write_user_info)
    band.send(2, 2)   # cihaz bilgisi
    band.send(2, 1)   # pil
    band.send(17, 0)  # alarmlar
    request_settings(band)
    for req in ((2, 78), (17, 14), (2, 29), (8, 42)):  # cihaz durumu, hatırlatıcılar, ekran öğeleri, hedef seçimi
        band.send(*req)
    wx.update(multi=None, on_band=set(), asked=False, want=0.0)
    if config["weather"]["enabled"] and config["weather"]["city"]:
        wx["asked"] = True
        band.send(10, 5)   # saat birden çok hava konumu tutuyor mu (yalnızca hava durumu açıkken sorulur)
    calendar_push(band)    # saat yokken değişen takvim; değişmediyse hiçbir şey yazılmaz
    last_calendar = time.monotonic()
    if workout["active"]:  # saat başlangıcı yinelemez; antrenman sürüyorsa canlı değerleri gelir
        wk.update(heard=time.monotonic(), resumed=True)
    now = time.monotonic()
    last_battery = now
    last_poll = now - POLL_EVERY
    # yeniden başlatmalar art arda eşitleme yapıp saatin kaydını bölmesin: son indirmenin üstünden geçen süre sayılır
    last_sync = now - min(SYNC_EVERY, max(0.0, time.time() - last_fetch()))
    live = False
    live_since = 0.0

    while True:
        now = time.monotonic()
        while not hub.jobs.empty():
            hub.jobs.get_nowait()(band)
        sync_due = hub.sync_now.is_set() or now - last_sync >= (SYNC_VIEWED if hub.viewers else SYNC_EVERY)
        if sync_due and not livereq["req"] and time.time() >= livereq["until"]:  # canlı ölçüm eşitlemeyi beklemez
            hub.sync_now.clear()
            last_sync = now
            if not sync_files(band):
                hub.sync_now.set()
            now = time.monotonic()
            # Eşitleme saatin o anki dosyasını kapatır; içinde bulunulan dakikanın nabzı dosyaya girmez. Hemen ardından
            # kısa bir anlık ölçüm alınır; okuma, saatin kaydı olmayan dakikayı doldurur (store.minutes).
            t["polling"], last_poll = True, now
            t["fill"] = True
        poll_gap = POLL_VIEWED if hub.viewers else POLL_EVERY
        if not t["polling"] and poll_gap and now - last_poll >= poll_gap:
            t["polling"], last_poll = True, now
        if t["polling"] and now - last_poll > POLL_WINDOW:
            t["polling"] = False
        workout_check(now)
        # canlı akış yalnızca istenince açılır: "Canlı ölç" (30 sn), bölge antrenmanı, süren antrenman, kısa aralıklı ölçüm
        if livereq["req"]:
            livereq.update(req=False, until=time.time() + LIVE_GRACE + LIVE_SECONDS, first=True)
        elif livereq["until"] and time.time() >= livereq["until"]:
            livereq.update(until=0.0, first=False)
            hub.update(live=None)
        wanted = time.time() < livereq["until"] or t["polling"] or coach["on"] or workout["active"] or appwk.active

        if wanted and (not live or (now - max(t["event"], live_since) > 30)):
            band.realtime_start()
            live, live_since = True, now
        elif not wanted and live:
            band.realtime_stop()
            live = False
            hub.update(hr=None)
        if now - last_battery >= BATTERY_EVERY:
            band.send(2, 1)
            last_battery = now
        w = water_state()
        if w["enabled"] and time.time() >= w["next"]:
            global water_last
            band.notify(i18n.t("Su zamanı"), i18n.t("Bir bardak su iç."), app=i18n.t("Su hatırlatıcısı"))
            water_last = time.time()
            hub.update(water=water_state())
        weather_tick(band)
        if now - last_calendar >= CALENDAR_CHECK:  # pencereye giren ya da biten etkinlik
            last_calendar = now
            calendar_push(band)
        mr = config["meal_reminder"]
        if mr["enabled"]:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            if stamp[11:] in mr["times"] and stamp not in meal_fired:
                meal_fired.add(stamp)
                band.notify(i18n.t("Öğün zamanı"), i18n.t("Yediklerini uygulamaya yazmayı unutma."), app=i18n.t("Öğün hatırlatıcısı"))
        band.read(1.0)


# --- web ----------------------------------------------------------------
def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # paket gönderilmez, yalnızca arayüz seçilir
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


TYPES = {".html": "text/html; charset=utf-8", ".svg": "image/svg+xml",
         ".json": "application/json", ".webmanifest": "application/manifest+json",
         ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def is_local(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1")

    def authorized(self, query: dict) -> tuple[bool, bool]:
        """(izinli mi, çerez yazılsın mı)"""
        if self.is_local():
            return True, False
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        if "k" in jar and secrets.compare_digest(jar["k"].value, config["token"]):
            return True, False
        given = query.get("k", [""])[0]
        if given and secrets.compare_digest(given, config["token"]):
            return True, True
        return False, False

    def send_body(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, code: int = 200) -> None:
        self.send_body(code, json.dumps(data).encode(), "application/json")

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        ok, set_cookie = self.authorized(query)
        if not ok:
            self.send_body(403, i18n.t("Bu sayfayı açmak için bilgisayardaki bağlantıyı kullan.").encode(),
                           "text/plain; charset=utf-8")
            return
        if set_cookie:  # anahtarı adresten çıkar, çereze taşı
            self.send_response(303)
            self.send_header("Location", url.path)
            self.send_header("Set-Cookie", f"k={config['token']}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        path = url.path
        if path == "/api/state":
            self.send_json(hub.snapshot())
        elif path == "/api/day":
            try:
                day = date.fromisoformat(query.get("d", [date.today().isoformat()])[0])
            except ValueError:
                self.send_json({"error": i18n.t("Geçersiz tarih")}, 400)
                return
            self.send_json(store.day(day))
        elif path == "/api/days":
            try:
                count = max(7, min(730, int(query.get("n", ["14"])[0])))
            except ValueError:
                count = 14
            self.send_json(store.days(count))
        elif path == "/api/minutes":
            try:
                day = date.fromisoformat(query.get("d", [date.today().isoformat()])[0])
            except ValueError:
                self.send_json({"error": i18n.t("Geçersiz tarih")}, 400)
                return
            self.send_json(dict(store.minutes(day), off_wrist=store.off_wrist(day)))
        elif path == "/api/body":
            self.send_json(insights.body_report(store, config))
        elif path in ("/api/report", "/api/week"):
            try:
                day = date.fromisoformat(query.get("d", [date.today().isoformat()])[0])
            except ValueError:
                self.send_json({"error": i18n.t("Geçersiz tarih")}, 400)
                return
            if path == "/api/week":
                self.send_json(insights.week_report(store, config, day))
            else:
                self.send_json(insights.day_report(store, config, day, hub.snapshot()))
        elif path == "/api/foods":
            limit = 30 if query.get("all") else 8
            self.send_json(foods.search(query.get("q", [""])[0][:40], query.get("kind", [None])[0], limit))
        elif path == "/api/export":
            what = "days" if query.get("what", [""])[0] == "days" else "minutes"
            name = i18n.t("bileklik-gunler.csv") if what == "days" else i18n.t("bileklik-dakikalar.csv")
            body = ("\ufeff" + store.csv(what)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/api/sleep", "/api/manual"):
            try:
                day = date.fromisoformat(query.get("d", [date.today().isoformat()])[0])
            except ValueError:
                self.send_json({"error": i18n.t("Geçersiz tarih")}, 400)
                return
            if path == "/api/sleep":
                self.send_json(store.sleep(day, rr=bool(query.get("rr"))))
            else:
                self.send_json(store.manual(day))
        elif path == "/api/workouts":
            self.send_json(store.workouts())
        elif path in ("/api/workout", "/api/workout.gpx"):
            try:
                wid = int(query.get("id", [""])[0])
            except ValueError:
                self.send_json({"error": i18n.t("Geçersiz antrenman")}, 400)
                return
            if path == "/api/workout":
                found = store.workout(wid)
                if found is None:
                    self.send_json({"error": i18n.t("Antrenman yok")}, 404)
                else:
                    self.send_json(found)
                return
            body = store.workout_gpx(wid)
            if body is None:
                self.send_json({"error": i18n.t("Bu antrenmanın GPS izi yok")}, 404)
                return
            name = i18n.t("antrenman-{stamp}.gpx", stamp=f"{datetime.fromtimestamp(wid):%Y%m%d-%H%M}")
            self.send_body(200, body.encode(), "application/gpx+xml; charset=utf-8",
                           {"Content-Disposition": f'attachment; filename="{name}"'})
        elif path == "/api/workout/live-track":
            # süren antrenmanın izi: sayfa elindeki nokta sayısını verir, yalnızca yeni noktalar döner
            try:
                start = max(0, int(query.get("from", ["0"])[0]))
            except ValueError:
                start = 0
            with appwk.lock:
                pts = appwk.track[start:]
                total = len(appwk.track)
            self.send_json({"from": min(start, total), "len": total, "ts": [p["ts"] for p in pts],
                            "lat": [p["lat"] for p in pts], "lon": [p["lon"] for p in pts]})
        elif path == "/api/eventlog":
            self.send_json(store.events())
        elif path == "/api/pending":
            self.send_json(store.pending())
        elif path == "/api/phone":
            if not self.is_local():
                self.send_json({"error": i18n.t("Yalnızca bilgisayardan")}, 403)
                return
            import io
            try:
                import segno
            except ImportError:  # Android paketinde yok; orada arayüz zaten telefonun içinde
                self.send_json({"error": i18n.t("Bu kurulumda yok")}, 404)
                return
            link = f"http://{lan_ip()}:{PORT}/?k={config['token']}"
            buf = io.BytesIO()
            segno.make(link, error="m").save(buf, kind="svg", scale=6, border=2, dark="#101b3b", light="#ffffff",
                                             xmldecl=False, svgns=True, nl=False, omitsize=True)
            self.send_json({"url": link, "qr": buf.getvalue().decode()})
        elif path == "/api/events":
            self.stream()
        else:
            name = "index.html" if path == "/" else path.lstrip("/")
            file = (WEB / name).resolve()
            if WEB.resolve() not in file.parents or not file.is_file():
                self.send_body(404, i18n.t("Yok").encode(), "text/plain; charset=utf-8")
                return
            self.send_body(200, file.read_bytes(), TYPES.get(file.suffix, "application/octet-stream"))

    def do_POST(self):
        url = urlparse(self.path)
        ok, _ = self.authorized({})
        # JSON şartı, başka sitelerin tarayıcı üzerinden bu servise istek atmasını engeller
        if not ok or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.send_json({"error": i18n.t("Yetkisiz")}, 403)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")) or 0) or b"{}")
        except ValueError:
            self.send_json({"error": i18n.t("Geçersiz istek")}, 400)
            return

        if url.path == "/api/goals":
            try:
                goals = {k: int(body[k]) for k in ("steps", "calories")}
                goals.update({k: int(body[k]) for k in ("standing", "moving") if body.get(k) not in (None, "")})
                if not (100 <= goals["steps"] <= 100000 and 50 <= goals["calories"] <= 10000):
                    raise ValueError
                if not (1 <= goals.get("standing", 12) <= 24 and 5 <= goals.get("moving", 30) <= 300):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                self.send_json({"error": i18n.t("Adım 100–100.000, kalori 50–10.000, ayakta 1–24 saat, hareket 5–300 dakika arasında olmalı.")}, 400)
                return
            config["goals"].update(goals)
            save_config()
            hub.update(goals=dict(config["goals"]))
            auto_write()
            self.send_json(config["goals"])
            return

        try:
            if url.path == "/api/meal":
                ts = None
                if body.get("time"):
                    day = date.fromisoformat(body.get("date") or date.today().isoformat())
                    ts = int(datetime.combine(day, datetime.strptime(body["time"], "%H:%M").time()).timestamp())
                    if ts > time.time() + 60:
                        raise ValueError(i18n.t("Gelecekteki bir saat için kayıt girilemez."))
                if body.get("food"):
                    qty = float(body.get("qty", 1))
                    if not 0.25 <= qty <= 10:
                        raise ValueError(i18n.t("Porsiyon 0,25 ile 10 arasında olmalı."))
                    insights.add_food(store, str(body["food"]), qty, ts)
                else:
                    name, kcal = str(body["name"]).strip()[:40], int(body["kcal"] or 0)
                    if not name or not 0 < kcal <= 5000:
                        raise ValueError(i18n.t("Ad yaz; kalori 1–5.000 arasında olmalı."))
                    macro = [body.get(k) for k in ("p", "c", "f")]
                    macro = [round(float(v), 1) if v not in (None, "") else None for v in macro]
                    if any(v is not None and not 0 <= v <= 500 for v in macro):
                        raise ValueError(i18n.t("Makro değerleri 0–500 g arasında olmalı."))
                    if any(v is not None for v in macro):
                        macro = [v or 0 for v in macro]
                    insights.add_meal(store, name, kcal, ts, "", *macro)
            elif url.path == "/api/meal/delete":
                insights.delete_meal(store, int(body["id"]))
            elif url.path == "/api/water/add":
                ml = int(body["ml"])
                if not -1 <= ml <= 2000:
                    raise ValueError(i18n.t("Miktar 2.000 ml'yi geçemez."))
                insights.add_water(store, ml)
            elif url.path == "/api/rpk":   # saat uygulaması: listele, yükle (b64: rpk dosyası), sil
                action = str(body.get("action"))
                if action not in ("list", "install", "delete"):
                    raise ValueError(i18n.t("Bilinmeyen işlem."))
                data = base64.b64decode(body["b64"]) if action == "install" else None
                if data is not None and not 200 < len(data) <= 2_000_000:
                    raise ValueError(i18n.t("Dosya boyutu uygun değil."))
                hub.jobs.put(rpk_job(action, data, str(body.get("package", "")) or None))
            elif url.path == "/api/probe":   # tanılama: saate gövdesiz bir okuma komutu gönderir (yalnızca izinli türler)
                t, st = int(body["type"]), int(body["subtype"])
                if (t, st) not in ((20, 0), (2, 78), (2, 1), (4, 0)):
                    raise ValueError(i18n.t("Bu sorguya izin yok."))
                probe.update(type=t, until=time.time() + 20)
                hub.jobs.put(lambda band: band.send(t, st))
            elif url.path == "/api/pause":   # tanılama: saat bağlantısını bir süre kes
                minutes = float(body.get("minutes", 0))
                if not 0 <= minutes <= 180:
                    raise ValueError(i18n.t("Süre 0–180 dakika arasında olmalı."))
                pause_link(minutes)
            elif url.path == "/api/body/fat":   # tartının ekranında yazan yağ oranı; hesabı ona göre ayarlar
                fat = None if body.get("fat") in (None, "") else round(float(body["fat"]), 1)
                if fat is not None and not 3 <= fat <= 60:
                    raise ValueError(i18n.t("Yağ oranı yüzde 3–60 arasında olmalı."))
                if not insights.set_fat_shown(store, int(body["ts"]), fat):
                    raise ValueError(i18n.t("Bu ölçüm tartıdan gelmemiş."))
            elif url.path == "/api/weight":
                kg = round(float(body["kg"]), 1)
                if not 30 <= kg <= 250:
                    raise ValueError(i18n.t("Kilo 30–250 kg arasında olmalı."))
                insights.add_weight(store, kg)
                config["profile"]["weight"] = kg
                save_config()
            elif url.path == "/api/profile":
                pr = {"age": int(body["age"]), "height": int(body["height"]), "weight": round(float(body["weight"]), 1),
                      "sex": "f" if body.get("sex") == "f" else "m"}
                if not (10 <= pr["age"] <= 100 and 120 <= pr["height"] <= 230 and 30 <= pr["weight"] <= 250):
                    raise ValueError(i18n.t("Yaş 10–100, boy 120–230 cm, kilo 30–250 kg arasında olmalı."))
                if body.get("max_hr") not in (None, "", 0):  # boşsa 220 − yaş kullanılır
                    pr["max_hr_set"] = int(body["max_hr"])
                    if not 100 <= pr["max_hr_set"] <= 220:
                        raise ValueError(i18n.t("Azami nabız 100–220 arasında olmalı; boş bırakırsan 220 − yaş kullanılır."))
                config["profile"] = pr
                config["goals"]["water"] = max(500, min(6000, int(body.get("water", config["goals"]["water"]))))
                save_config()
            elif url.path == "/api/band-write":
                config["band_write"] = bool(body.get("on"))
                save_config()
                hub.update(band_write=config["band_write"])
            elif url.path == "/api/lang":   # arayüz dili; servisin ürettiği metinler de bu dille yazılır
                if body.get("lang") not in LANGS:
                    raise ValueError(i18n.t("Dil \"tr\" ya da \"en\" olmalı."))
                set_language(body["lang"])
            elif url.path == "/api/meal-reminder":
                times = sorted({datetime.strptime(str(t), "%H:%M").strftime("%H:%M") for t in body["times"] if t})[:6]
                config["meal_reminder"] = {"enabled": bool(body["enabled"]) and bool(times), "times": times}
                save_config()
            else:
                raise LookupError
            pr = config["profile"]
            hub.update(goals=dict(config["goals"]), meal_reminder=config["meal_reminder"],
                       profile=dict(pr, max_hr=insights.max_hr(pr), bmr=round(insights.bmr(pr))))
            if url.path in ("/api/weight", "/api/profile"):
                auto_write()
            self.send_json({"ok": True})
            return
        except LookupError:
            pass
        except (ValueError, KeyError, TypeError) as exc:
            msg = str(exc) if isinstance(exc, ValueError) and "invalid literal" not in str(exc) and "does not match" not in str(exc) and "could not convert" not in str(exc) else i18n.t("Girilen değerleri kontrol et.")
            self.send_json({"error": msg}, 400)
            return

        if url.path == "/api/water":
            global water_last
            try:
                w = {"enabled": bool(body["enabled"]), "every": int(body["every"]),
                     "from": str(body["from"]), "to": str(body["to"])}
                datetime.strptime(w["from"], "%H:%M"), datetime.strptime(w["to"], "%H:%M")
                if not (15 <= w["every"] <= 240 and len(w["from"]) == 5 and len(w["to"]) == 5 and w["from"] < w["to"]):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                self.send_json({"error": i18n.t("Aralık 15–240 dakika olmalı, başlangıç saati bitişten önce gelmeli.")}, 400)
                return
            if w["enabled"] and not config["water"]["enabled"]:
                water_last = time.time()  # ilk hatırlatma bir aralık sonra
            config["water"] = w
            save_config()
            hub.update(water=water_state())
            self.send_json(water_state())
            return

        if url.path == "/api/weather/search":   # dışarı yalnızca yazılan şehir adı gider
            name = str(body.get("city") or "").strip()[:60]
            if len(name) < 2:
                self.send_json({"error": i18n.t("Şehir adını yaz.")}, 400)
                return
            try:
                found = weather.geocode(name)
            except weather.WeatherError as exc:
                self.send_json({"error": str(exc)}, 502)
                return
            if not found:
                self.send_json({"error": i18n.t("Bu adla bir şehir bulunamadı.")}, 404)
                return
            self.send_json({"results": found})
            return
        if url.path == "/api/weather":   # ayar saat yokken de kaydedilir; veri bağlanınca yazılır
            w = dict(config["weather"])
            try:
                if "city" in body:
                    w.update(city=str(body["city"]).strip()[:60], lat=round(float(body["lat"]), 2), lon=round(float(body["lon"]), 2))
                    if not w["city"] or not (-90 <= w["lat"] <= 90 and -180 <= w["lon"] <= 180):
                        raise ValueError
                if "every" in body:
                    w["every"] = int(body["every"])
                    if not 15 <= w["every"] <= 720:
                        raise ValueError
                if "enabled" in body:
                    w["enabled"] = bool(body["enabled"])
            except (ValueError, KeyError, TypeError):
                self.send_json({"error": i18n.t("Şehri listeden seç; aralık 15–720 dakika olmalı.")}, 400)
                return
            if w["enabled"] and w["lat"] is None:
                self.send_json({"error": i18n.t("Önce şehir seç.")}, 400)
                return
            before = dict(config["weather"])
            config["weather"] = w
            save_config()
            moved = (w["city"], w["lat"], w["lon"]) != (before["city"], before["lat"], before["lon"])
            if moved or not w["enabled"]:
                wx.update(error=None, pending=False, want=0.0)
            if w["enabled"] and (moved or not before["enabled"]):
                wx["tried"] = time.time()
                threading.Thread(target=weather_fetch, daemon=True).start()
            hub.update(weather=weather_state())
            self.send_json(dict(weather_state(), ok=True))
            return
        if url.path == "/api/calendar":   # liste saat yokken de kaydedilir; bağlanınca yazılır
            try:
                if body.get("delete"):
                    if not store.calendar_delete(int(body["id"])):
                        raise LookupError
                    eid = None
                else:
                    e = calendar_event(body)
                    if e["id"] is None and len(store.calendar_list()) >= CALENDAR_KEEP:
                        raise Reject(i18n.t("En fazla {n} etkinlik saklanır. Önce birini sil.", n=CALENDAR_KEEP))
                    eid = store.calendar_save(e)
                    if eid is None:
                        raise LookupError
            except LookupError:
                hub.update(calendar=calendar_state())
                self.send_json({"error": i18n.t("Bu etkinlik artık yok. Liste yenilendi.")}, 409)
                return
            except Reject as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            except (ValueError, KeyError, TypeError):
                self.send_json({"error": i18n.t("Geçersiz istek")}, 400)
                return
            queued = calendar_changed()
            self.send_json({"ok": True, "id": eid, "queued": queued, "calendar": calendar_state()})
            return

        if url.path == "/api/workout/distance":
            try:
                ok = store.set_workout_distance(int(body.get("id")), float(body.get("metres")))
            except (TypeError, ValueError):
                ok = False
            if ok:
                self.send_json({"ok": True})
            else:
                self.send_json({"error": i18n.t("Mesafe düzeltilemedi: yalnızca uygulama kayıtları, makul bir değerle.")}, 400)
            return
        if url.path == "/api/workout/delete":
            try:
                ok = store.delete_app_workout(int(body.get("id")))
            except (TypeError, ValueError):
                ok = False
            if ok:
                self.send_json({"ok": True})
            else:
                self.send_json({"error": i18n.t("Yalnızca uygulamadan kaydedilen antrenmanlar silinebilir.")}, 400)
            return
        # süren antrenmanı yönetmek için saatin bağlı olması gerekmez (bağlantı kopukken de bitirilebilir)
        if url.path in ("/api/workout/pause", "/api/workout/resume", "/api/workout/finish", "/api/workout/discard"):
            if not appwk.active:
                self.send_json({"error": i18n.t("Süren bir antrenman yok.")}, 409)
                return
            what = url.path.rsplit("/", 1)[1]
            if what == "pause":
                appwk.pause()
                appwk_coach(False)
                mirror_send(1)
            elif what == "resume":
                appwk.resume()
                appwk_coach(True)
                mirror_send(2)
            elif what == "finish":
                mirror_stop()
                self.send_json(dict(appwk_done(appwk.finish(time.time())), ok=True))
                return
            else:
                mirror_stop()
                appwk.discard()
                appwk_done({"saved": False, "id": None, "message": i18n.t("Antrenman kaydedilmedi."), "auto": None, "discarded": True})
            appwk_push()
            self.send_json({"ok": True})
            return

        if url.path not in ("/api/find", "/api/notify", "/api/sync", "/api/setting", "/api/alarm", "/api/coach", "/api/live",
                            "/api/userinfo", "/api/reminder", "/api/display", "/api/workout/start", "/api/weather/refresh"):
            self.send_json({"error": i18n.t("Yok")}, 404)
            return
        if hub.snapshot()["link"] != "connected":
            self.send_json({"error": i18n.t("Saat bağlı değil. Yaklaşınca tekrar dene.")}, 409)
            return
        if url.path == "/api/live":
            if body.get("on", True):   # süre, saat ilk değeri gönderince başlar; eşitleme sürüyorsa istek bekler
                livereq.update(req=True, until=0.0, first=False)
                hub.update(live={"state": "starting", "until": None})
            else:
                livereq.update(req=False, until=0.0, first=False)
                hub.update(live=None)
        elif url.path == "/api/coach":
            zone = body.get("zone", coach["zone"])
            if zone not in ZONES:
                self.send_json({"error": i18n.t("Bölge 1 ile 5 arasında olmalı.")}, 400)
                return
            coach.update(on=bool(body.get("on")), zone=zone, since=time.time(), out_since=None, last_alert=0.0,
                         app=appwk.active and bool(body.get("on")))
            appwk.set_zone(zone if body.get("on") else None)  # süren antrenmanın uyarısı da buradan değişir
            appwk_push()
            hub.update(coach=coach_state(hub.snapshot().get("hr")))
        elif url.path == "/api/workout/start":
            goal, zone = body.get("goal"), body.get("zone")
            try:
                if not (goal is None or isinstance(goal, dict)) or isinstance(zone, bool):
                    raise ValueError(i18n.t("Hedef türü ya da değeri geçersiz."))
                # konum yalnızca kaynağı olan kurulumda ve sayfa izin alındığını bildirince kullanılır
                appwk.start(str(body.get("sport")), goal, zone,
                            gps=body.get("gps") is True and hub.snapshot()["caps"]["gps"])
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 409 if appwk.active else 400)
                return
            if workout["active"]:
                appwk.note_band_workout()
            else:
                mirror_start(str(body.get("sport")))  # saatin ekranında da antrenman açılır
            appwk_coach(True)
            appwk_push()
        elif url.path == "/api/find":
            on = bool(body.get("on", True))
            hub.jobs.put(lambda band: band.find(on))
        elif url.path == "/api/notify":
            title, text = str(body.get("title", "")).strip()[:60], str(body.get("body", "")).strip()[:240]
            if not title and not text:
                self.send_json({"error": i18n.t("Başlık ya da mesaj yaz.")}, 400)
                return
            hub.jobs.put(lambda band: band.notify(title or "Smart Band", text or " "))
        elif url.path == "/api/setting":
            key = str(body.get("key"))
            if key not in SETTINGS:
                self.send_json({"error": i18n.t("Bilinmeyen ayar.")}, 400)
                return
            try:
                value = setting_value(key, body.get("value"))
            except (ValueError, TypeError):
                self.send_json({"error": i18n.t("Bu ayar için geçersiz değer.")}, 400)
                return
            need = SETTINGS[key][0]
            if need is not None and need not in band_cfg:
                self.send_json({"error": i18n.t("Bu ayar saatten okunamadı; saat desteklemiyor olabilir.")}, 409)
                return
            hub.jobs.put(lambda band: apply_setting(band, key, value))
        elif url.path == "/api/userinfo":
            hub.jobs.put(write_user_info)
        elif url.path == "/api/weather/refresh":
            if not config["weather"]["enabled"]:
                self.send_json({"error": i18n.t("Hava durumu kapalı. Önce anahtarı aç.")}, 409)
                return
            error = weather_fetch()  # en çok 10 saniye sürer; hata doğrudan gösterilir
            if error:
                self.send_json({"error": error}, 502)
                return
        elif url.path == "/api/reminder":
            snap = hub.snapshot()
            try:
                r = {"id": int(body["id"]) if body.get("id") is not None else None, "delete": bool(body.get("delete"))}
                known = next((x for x in snap["reminders"] if x["id"] == r["id"]), None)
                if r["id"] is not None and known is None:
                    raise LookupError
                if not r["delete"]:
                    r.update(title=str(body.get("title", "")).strip()[:40], repeat=int(body.get("repeat", 0)),
                             date=date.fromisoformat(str(body.get("date"))).isoformat(),
                             time=datetime.strptime(str(body.get("time")), "%H:%M").strftime("%H:%M"))
                    # haftalık tekrar yalnızca saatte zaten haftalık olan kayıtta, gün bilgisiyle birlikte korunur
                    keep = known is not None and known["repeat"] == r["repeat"]
                    if not r["title"] or not (r["repeat"] in bandmsg.REMINDER_REPEATS or keep):
                        raise ValueError
                    r["flags"] = known["flags"] if keep else None
            except LookupError:
                self.send_json({"error": i18n.t("Bu hatırlatıcı saatte artık yok. Liste yenilendi, tekrar dene.")}, 409)
                hub.jobs.put(lambda band: band.send(17, 14))
                return
            except (ValueError, TypeError):
                self.send_json({"error": i18n.t("Başlık yaz, tarih ve saati seç.")}, 400)
                return
            if r["id"] is None and len(snap["reminders"]) >= (snap["max_reminders"] or 50):
                self.send_json({"error": i18n.t("Saat en fazla {n} hatırlatıcı tutar. Önce birini sil.", n=snap["max_reminders"] or 50)}, 409)
                return
            hub.jobs.put(lambda band: apply_reminder(band, r))
        elif url.path == "/api/display":
            if "display" not in band_cfg:
                self.send_json({"error": i18n.t("Ekran listesi saatten okunamadı; saat desteklemiyor olabilir.")}, 409)
                return
            try:
                order, off = [str(c) for c in body["order"]], {str(c) for c in body.get("off", [])}
                bandmsg.display_items(band_cfg["display"], order, off)  # yalnızca denetim
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 409)
                return
            except (KeyError, TypeError):
                self.send_json({"error": i18n.t("Geçersiz istek")}, 400)
                return
            hub.jobs.put(lambda band: apply_display(band, order, off))
        elif url.path == "/api/alarm":
            try:
                a = {"id": int(body["id"]) if body.get("id") is not None else None, "delete": bool(body.get("delete")),
                     "hour": int(body.get("hour", 0)), "minute": int(body.get("minute", 0)),
                     "repeat": int(body.get("repeat", 0)), "enabled": bool(body.get("enabled", True)),
                     "smart": bool(body.get("smart", False)), "flags": int(body.get("flags") or 0)}
                # saat, hafta içi alarmını tekrar 4 (günler bayraklarda) olarak geri bildirir; açıp kapatırken aynen döner
                if not a["delete"] and not (0 <= a["hour"] <= 23 and 0 <= a["minute"] <= 59 and a["repeat"] in (0, 1, 4, 5)
                                            and 0 <= a["flags"] <= 127):
                    raise ValueError
            except (ValueError, TypeError):
                self.send_json({"error": i18n.t("Alarm saati geçersiz.")}, 400)
                return
            snap = hub.snapshot()
            if a["id"] is None and not a["delete"] and len(snap["alarms"]) >= snap.get("max_alarms", 10):
                self.send_json({"error": i18n.t("Saat en fazla 10 alarm tutar.")}, 409)
                return
            hub.jobs.put(lambda band: apply_alarm(band, a))
        else:
            hub.sync_now.set()
        self.send_json({"ok": True})

    def peer_gone(self) -> bool:
        """Karşı taraf bağlantıyı kapattı mı (okunacak veri var ama boş)."""
        try:
            if not select.select([self.connection], [], [], 0)[0]:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        q: queue.Queue = queue.Queue(maxsize=50)
        hub.listeners.add(q)
        if time.time() - max(hub.snapshot()["synced"] or 0, last_fetch()) > SYNC_VIEWED:  # sayfa açılınca güncelle
            hub.sync_now.set()
        try:
            self.wfile.write(f"data: {json.dumps(hub.snapshot())}\n\n".encode())
            self.wfile.flush()
            idle = 0
            while True:
                try:
                    data = q.get(timeout=2)
                    self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
                    idle = 0
                except queue.Empty:
                    # Kapanan sayfa birkaç saniyede fark edilir: kapalı bağlantıya ilk yazma hata vermediği
                    # için yalnızca 15 saniyelik nabız yazısına güvenilirse "izleyen var" sanılıp saat sık yoklanır.
                    if self.peer_gone():
                        break
                    idle += 2
                    if idle < 15:
                        continue
                    idle = 0
                    self.wfile.write(b": bekle\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.listeners.discard(q)
            self.close_connection = True


def main() -> None:
    threading.Thread(target=band_loop, daemon=True).start()
    threading.Thread(target=appwk_loop, daemon=True).start()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    server.daemon_threads = True
    print(f"Arayüz: http://localhost:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
