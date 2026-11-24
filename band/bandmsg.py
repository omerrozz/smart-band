"""Saat komutlarının kurulması ve çözülmesi (saf işlevler; saate bağlanmaz, ayar okumaz).

Komut tür/alt tür numaraları ve alanlar Gadgetbridge'in Xiaomi servislerinden alınmıştır:
  2/78  cihaz durumu sorgusu (BasicDeviceState)      2/79  cihaz durumu bildirimi (DeviceState)
  2/29  ekran öğeleri oku                            2/30  ekran öğeleri yaz
  8/0   kullanıcı bilgisi ve hedefler yaz (UserInfo) 8/42  ikincil hedef seçimi oku
  8/26  antrenman durumu                             8/50  antrenman canlı değerleri
  17/8  uyku modu oku      17/9  uyku modu yaz
  17/14 hatırlatıcılar oku 17/15 ekle  17/17 düzenle  17/18 sil
  10/0  anlık hava durumu  10/1 günlük tahmin  10/2 saatlik tahmin  10/3 saat güncel veri ister
  10/5  konum listesi oku  10/6 konum listesi yaz  10/7 konum ekle  10/8 konum sil
  12/1  takvim etkinlikleri (tam liste her seferinde yeniden yazılır)
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "pb"))
import i18n  # noqa: E402
import xiaomi_pb2  # noqa: E402

Command = xiaomi_pb2.Command

# Gadgetbridge hatırlatıcı tarih ve saatini saate UTC olarak yollar ve UTC olarak okur
# (XiaomiPreferences.toDate: "the watch expects those in UTC"). Saat hatırlatıcıyı yanlış saatte
# gösterirse bu değer False yapılır; başka hiçbir yer değişmez.
REMINDER_UTC = True

REPEAT_ONCE, REPEAT_DAILY, REPEAT_WEEKLY, REPEAT_MONTHLY, REPEAT_YEARLY = 0, 1, 5, 7, 8
REMINDER_REPEATS = (REPEAT_ONCE, REPEAT_DAILY, REPEAT_MONTHLY, REPEAT_YEARLY)  # arayüzden seçilebilenler

# antrenman durumu (8/26): 0 başladı, 1 duraklatıldı, 2 sürüyor, 3 bitti
# ikinci sürüm (8/29) sırayı değiştirir: 0 başladı, 1 sürüyor, 2 duraklatıldı, 3 bitti
WORKOUT_EVENTS = {0: "start", 1: "pause", 2: "resume", 3: "finish"}
WORKOUT_EVENTS_V2 = {0: "start", 1: "resume", 2: "pause", 3: "finish"}
# yalnızca kaynaktaki adlandırılmış kodlar; diğerleri bilinmiyor sayılır
SPORTS = {1: i18n.mark("Açık hava koşusu"), 2: i18n.mark("Yürüyüş"), 3: i18n.mark("Doğa yürüyüşü"), 4: i18n.mark("Trekking"),
          5: i18n.mark("Patika koşusu"), 6: i18n.mark("Açık hava bisikleti")}  # saatin bildirdiği spor; gösterilirken çevrilir


# --- cihaz durumu -------------------------------------------------------
def device_basic(m) -> dict:
    """2/78 yanıtı: üç durum da zorunlu alan, pil düzeyi isteğe bağlı."""
    out = {"worn": bool(m.isWorn), "charging": bool(m.isCharging), "asleep": bool(m.isUserAsleep)}
    if m.HasField("batteryLevel"):
        out["battery"] = m.batteryLevel
    return out


def device_update(m) -> dict:
    """2/79 bildirimi: yalnızca gelen alanlar döner (1 evet, 2 hayır; başka değer yok sayılır)."""
    out = {}
    for key, field in (("charging", "chargingState"), ("worn", "wearingState"), ("asleep", "sleepState")):
        if m.HasField(field) and getattr(m, field) in (1, 2):
            out[key] = getattr(m, field) == 1
    return out


# --- kullanıcı bilgisi ve hedefler --------------------------------------
def user_info(profile: dict, goals: dict, max_hr: int, today: date | None = None) -> Command:
    """8/0. Doğum günü bilinmediği için yaştan hesaplanan yılın 1 Ocak'ı gider."""
    year = (today or date.today()).year - int(profile["age"])
    cmd = Command(type=8, subtype=0)
    u = cmd.health.userInfo
    u.height = int(profile["height"])
    u.weight = float(profile["weight"])
    u.birthday = year * 10000 + 101
    u.gender = 2 if profile.get("sex") == "f" else 1
    u.maxHeartRate = int(max_hr)
    u.goalCalories = int(goals["calories"])
    u.goalSteps = int(goals["steps"])
    u.goalStanding = int(goals["standing"])
    u.goalMoving = int(goals["moving"])
    return cmd


def goals_config(m) -> dict:
    """8/42: saatin gösterdiği ve desteklediği hedefler (3 hareket süresi, 4 ayakta saat)."""
    return {"current": [g.id for g in m.currentGoals], "supported": [g.id for g in m.supportedGoals]}


# --- antrenman ----------------------------------------------------------
def workout_event(cmd) -> dict | None:
    """8/26 ya da 8/29 bildirimi -> {"event", "sport", "ts"}; tanınmayan durum None."""
    h = cmd.health
    if cmd.subtype == 26 and h.HasField("workoutStatusWatch"):
        w = h.workoutStatusWatch
        event, sport, ts = WORKOUT_EVENTS.get(w.status), w.sport, w.timestamp
    elif cmd.subtype == 29 and h.HasField("workoutStatusWatchV2"):
        w = h.workoutStatusWatchV2
        event, sport, ts = WORKOUT_EVENTS_V2.get(w.status), w.sport.type, w.timestamp
    else:
        return None
    if event is None:
        return None
    return {"event": event, "sport": i18n.t(SPORTS[sport]) if sport in SPORTS else None, "sport_code": sport or None, "ts": ts or None}


def workout_stats(m) -> dict:
    """8/50: saat antrenman açıkken saniyede bir yollar. Kalori ve adım antrenmanın başından sayılır."""
    return {"hr": m.heartRate if 10 < m.heartRate < 255 else None, "calories": m.calories, "steps": m.steps,
            "distance": m.distance}


# --- hatırlatıcılar -----------------------------------------------------
def _to_band(when: datetime) -> datetime:
    return when.astimezone(timezone.utc) if REMINDER_UTC else when


def _from_band(d, t) -> datetime:
    naive = datetime(d.year, d.month, d.day, t.hour, t.minute)
    if REMINDER_UTC:
        return naive.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return naive


def reminders(m) -> tuple[list[dict], int]:
    """17/14 yanıtı -> (liste, saatin tuttuğu en fazla hatırlatıcı sayısı)."""
    out = []
    for r in m.reminder:
        d = r.reminderDetails
        try:
            when = _from_band(d.date, d.time)
        except ValueError:  # saat geçersiz tarih yolladı
            continue
        out.append({"id": r.id, "title": d.title, "date": when.strftime("%Y-%m-%d"), "time": when.strftime("%H:%M"),
                    "repeat": d.repeatMode, "flags": d.repeatFlags if d.HasField("repeatFlags") else None})
    return out, m.maxReminders


def reminder(r: dict) -> Command:
    """Ekle (17/15), düzenle (17/17) ya da sil (17/18).

    r: {"id", "delete"} ya da {"id" (yeni için None), "title", "date" YYYY-MM-DD, "time" HH:MM, "repeat",
    "flags" (yalnızca haftalık bir hatırlatıcı düzenlenirken, saatten okunan değer)}.
    """
    if r.get("delete"):
        cmd = Command(type=17, subtype=18)
        cmd.schedule.deleteReminder.id.append(int(r["id"]))
        return cmd
    when = _to_band(datetime.strptime(f"{r['date']} {r['time']}", "%Y-%m-%d %H:%M").astimezone())
    if r.get("id") is None:
        cmd = Command(type=17, subtype=15)
        d = cmd.schedule.createReminder
    else:
        cmd = Command(type=17, subtype=17)
        cmd.schedule.editReminder.id = int(r["id"])
        d = cmd.schedule.editReminder.reminderDetails
    d.date.year, d.date.month, d.date.day = when.year, when.month, when.day
    d.time.hour, d.time.minute, d.time.second, d.time.millisecond = when.hour, when.minute, 0, 0
    d.repeatMode = int(r["repeat"])
    if d.repeatMode == REPEAT_WEEKLY and r.get("flags") is not None:
        d.repeatFlags = int(r["flags"])
    d.title = r["title"]
    return cmd


# --- uyku modu ----------------------------------------------------------
def sleep_mode_state(m) -> dict:
    s = m.schedule
    return {"on": bool(m.enabled), "from": f"{s.start.hour:02d}:{s.start.minute:02d}",
            "to": f"{s.end.hour:02d}:{s.end.minute:02d}"}


def sleep_mode(current, on: bool | None = None, start: str | None = None, end: str | None = None) -> Command:
    """17/9. Saatten okunan mesaj kopyalanır, yalnızca verilen alanlar değişir."""
    cmd = Command(type=17, subtype=9)
    m = cmd.schedule.sleepMode
    m.CopyFrom(current)
    if on is not None:
        m.enabled = on
    for value, hm, default in ((start, m.schedule.start, (22, 0)), (end, m.schedule.end, (6, 0))):
        if value is not None:
            hm.hour, hm.minute = (int(x) for x in value.split(":"))
        elif not hm.IsInitialized():
            hm.hour, hm.minute = default
    if not m.schedule.HasField("unknown3"):
        m.schedule.unknown3 = 0
    return cmd


# --- ekran öğeleri ------------------------------------------------------
def display_state(m) -> list[dict]:
    return [{"code": i.code, "name": i.name, "on": not i.disabled, "more": bool(i.inMoreSection),
             "fixed": i.isSettings == 1} for i in m.displayItem]


def display_items(current, order: list[str], off: set[str]) -> Command:
    """2/30. Saatin kendi öğeleri yeni sırayla geri gider; yalnızca sıra ve açık/kapalı değişir.

    Ayarlar öğesi kapatılamaz. Kapalı öğeler listenin sonuna, "daha fazla" bölümündekiler
    ana bölümdekilerin arkasına dizilir (Gadgetbridge'in yazdığı düzen).
    """
    items = {i.code: i for i in current.displayItem}
    if sorted(order) != sorted(items):
        raise ValueError(i18n.t("Ekran listesi saattekiyle uyuşmuyor. Sayfayı yenileyip tekrar dene."))
    cmd = Command(type=2, subtype=30)
    out = cmd.system.displayItems

    def closed(code: str) -> bool:
        return code in off and items[code].isSettings != 1

    ranked = sorted(order, key=lambda c: 2 if closed(c) else 1 if items[c].inMoreSection else 0)
    for code in ranked:
        item = out.displayItem.add()
        item.CopyFrom(items[code])
        if closed(code):
            item.disabled = True
            item.ClearField("inMoreSection")
        else:
            item.ClearField("disabled")
        if not item.HasField("unknown5"):
            item.unknown5 = 1
    return cmd


# --- hava durumu --------------------------------------------------------
# Alanlar ve birimler XiaomiWeatherService'teki gibidir: sıcaklık tam sayı ve "℃", nem "%", rüzgâr
# Beaufort değeri (birim alanında yön, derece), UV tam sayı, basınç Pa, tarihler saat dilimi iki
# noktalı ISO metni. Konum anahtarı kaynaktaki yer tutucudur: "accu:" + adın Java hashCode'u.
#
# Saatin durum kodları (XiaomiWeatherConditions bu depoda yok; Xiaomi/Huami'nin ortak kod tablosu):
# 0 açık, 1 parçalı bulutlu, 2 kapalı, 3 sağanak, 4 gök gürültülü, 5 dolu ile fırtına, 6 karla karışık,
# 7 hafif yağmur, 8 yağmur, 9 kuvvetli yağmur, 10 çok kuvvetli yağmur, 13 kar sağanağı, 14 hafif kar,
# 15 kar, 16 yoğun kar, 18 sis, 19 donan yağmur.
WMO_TO_BAND = {
    0: 0, 1: 1, 2: 1, 3: 2, 45: 18, 48: 18,
    51: 7, 53: 7, 55: 7, 56: 19, 57: 19,
    61: 7, 63: 8, 65: 9, 66: 19, 67: 19,
    71: 14, 73: 15, 75: 16, 77: 14,
    80: 3, 81: 3, 82: 10, 85: 13, 86: 13,
    95: 4, 96: 5, 99: 5,
}
WEATHER_DAYS = 7     # bugün + kaynaktaki en fazla 6 gün
WEATHER_HOURS = 23   # kaynaktaki sınır
AQI_PLACEHOLDER = True   # kaynak hava kalitesi yokken 0 / "Unknown" yollar; burada alan boş bırakılır
_BEAUFORT = (2, 6, 12, 19, 30, 40, 51, 62, 75, 87, 103, 117)  # km/sa üst sınırları


def band_condition(wmo) -> int:
    """Open-Meteo'nun WMO kodu -> saatin durum kodu; bilinmeyen kod kapalı hava sayılır."""
    return WMO_TO_BAND.get(wmo, 2)


def beaufort(kmh) -> int:
    return sum(1 for limit in _BEAUFORT if limit < (kmh or 0))


def _java_hash(text: str) -> int:
    h = 0
    raw = text.encode("utf-16-be")
    for i in range(0, len(raw), 2):  # Java UTF-16 birimleri üzerinden hesaplar
        h = (31 * h + int.from_bytes(raw[i:i + 2], "big")) & 0xFFFFFFFF
    return h - (1 << 32) if h >= 1 << 31 else h


def location_key(name: str) -> str:
    h = _java_hash(name)
    r = abs(h) % 1000000
    # Java'da Math.abs(Integer.MIN_VALUE) eksi kalır ve % işareti bölüneni izler
    return "accu:%d" % (-r if h == -(1 << 31) else r)


def iso_colon(ts) -> str:
    """Yerel saatle "2026-11-20T21:05:00+03:00"; ts yoksa boş metin (kaynaktaki gibi)."""
    if not ts:
        return ""
    s = datetime.fromtimestamp(int(ts)).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    return s[:22] + ":" + s[22:]


def _unit(m, value, unit: str) -> None:
    m.unit, m.value = unit, int(round(value or 0))


def _meta(m, w: dict) -> None:
    m.publicationTimestamp = iso_colon(w["ts"])
    m.cityName = ""
    m.locationName = w["city"]
    m.locationKey = location_key(w["city"])
    m.isCurrentLocation = True  # tek konum gönderiyoruz; saat "geçerli konum" işaretli olmayan veriyi göstermeyebilir


def _location(m, name: str) -> None:
    m.code, m.name = location_key(name), name


def weather_location(name: str) -> Command:
    """10/7: konumu saate tanıtır."""
    cmd = Command(type=10, subtype=7)
    _location(cmd.weather.location, name)
    return cmd


def weather_locations(names: list[str]) -> Command:
    """10/6: saatteki konum listesi ve sırası (yalnızca birden çok konum tutan saatlerde)."""
    cmd = Command(type=10, subtype=6)
    cmd.weather.locations.SetInParent()
    for name in names:
        _location(cmd.weather.locations.location.add(), name)
    return cmd


def weather_remove(locations: list[tuple[str, str]]) -> Command:
    """10/8: (kod, ad) çiftleriyle verilen konumları saatten siler."""
    cmd = Command(type=10, subtype=8)
    for code, name in locations:
        m = cmd.weather.locations.location.add()
        m.code, m.name = code, name
    return cmd


def weather_location_list(m) -> list[tuple[str, str]]:
    """10/5 yanıtı -> [(kod, ad)]."""
    return [(x.code, x.name) for x in m.location]


def weather_current(w: dict) -> Command:
    """10/0. w: weather.forecast() çıktısı."""
    cmd = Command(type=10, subtype=0)
    c = cmd.weather.current
    _meta(c.metadata, w)
    c.weatherCondition = band_condition(w["code"])
    _unit(c.temperature, w["temp"], "℃")
    _unit(c.humidity, w.get("humidity"), "%")
    _unit(c.wind, beaufort(w.get("wind")), str(int(round(w.get("wind_dir") or 0))))
    _unit(c.uv, w.get("uv"), "")
    if w.get("aqi") is not None:
        _unit(c.aqi, w["aqi"], "Unknown")
    elif AQI_PLACEHOLDER:
        _unit(c.aqi, 0, "Unknown")
    c.warning.SetInParent()
    if w.get("pressure") is not None:
        c.pressure = float(w["pressure"]) * 100.0  # hPa -> Pa
    return cmd


def weather_daily(w: dict) -> Command:
    """10/1: ilk kayıt bugündür ve durumu anlık durumdan alır (kaynaktaki gibi); aralıkta from = en yüksek."""
    cmd = Command(type=10, subtype=1)
    f = cmd.weather.forecast
    _meta(f.metadata, w)
    f.entries.SetInParent()
    for n, d in enumerate(w.get("days", [])[:WEATHER_DAYS]):
        e = f.entries.entry.add()
        if w.get("aqi") is not None and n == 0:
            _unit(e.aqi, w["aqi"], "Unknown")
        elif AQI_PLACEHOLDER:
            _unit(e.aqi, 0, "Unknown")
        code = band_condition(w["code"] if n == 0 else d["code"])
        e.conditionRange.to = code
        setattr(e.conditionRange, "from", code)
        setattr(e.temperatureRange, "from", int(round(d["max"])))
        e.temperatureRange.to = int(round(d["min"]))
        e.temperatureSymbol = "℃"
        e.sunriseSunset.sunrise = iso_colon(d.get("sunrise"))
        e.sunriseSunset.sunset = iso_colon(d.get("sunset"))
    return cmd


def weather_hourly(w: dict) -> Command:
    """10/2: önümüzdeki saatler; aralıkların from alanı zorunlu olduğu için 0 gider (kaynaktaki gibi)."""
    cmd = Command(type=10, subtype=2)
    f = cmd.weather.forecast
    _meta(f.metadata, w)
    f.entries.SetInParent()
    for h in w.get("hours", [])[:WEATHER_HOURS]:
        e = f.entries.entry.add()
        if AQI_PLACEHOLDER:
            _unit(e.aqi, 0, "Unknown")
        setattr(e.temperatureRange, "from", 0)
        e.temperatureRange.to = int(round(h["temp"]))
        setattr(e.conditionRange, "from", 0)
        e.conditionRange.to = band_condition(h["code"])
        e.temperatureSymbol = "℃"
        _unit(e.wind, beaufort(h.get("wind")), str(int(round(h.get("wind_dir") or 0))))
    return cmd


# --- takvim -------------------------------------------------------------
CALENDAR_MAX = 50    # kaynaktaki sınır ("TODO confirm actual limit")
CALENDAR_DAYS = 7    # Gadgetbridge'in varsayılan ileri bakış süresi
# Kaynak metinleri kısaltmadan yollar; saatin gerçek sınırı bilinmiyor. Bunlar uygulamanın kendi sınırı.
CALENDAR_TITLE, CALENDAR_PLACE, CALENDAR_NOTE = 60, 60, 200


def calendar_window(events: list[dict], now: int) -> list[dict]:
    """Saate gidecek etkinlikler: bitmemiş, önümüzdeki CALENDAR_DAYS gün içinde başlayan, en çok CALENDAR_MAX."""
    until = now + CALENDAR_DAYS * 86400
    live = [e for e in events if e["end"] > now and e["start"] < until]
    return sorted(live, key=lambda e: (e["start"], e["id"]))[:CALENDAR_MAX]


def calendar_sync(events: list[dict]) -> Command:
    """12/1: listenin tamamı. start ve end Unix saniyesidir (UTC anı); tüm gün etkinlikte yerel gece yarıları."""
    cmd = Command(type=12, subtype=1)
    cmd.calendar.calendarSync.SetInParent()
    for e in events:
        m = cmd.calendar.calendarSync.event.add()
        m.title = e["title"]
        m.description = e.get("note") or ""
        m.location = e.get("location") or ""
        m.start, m.end = int(e["start"]), int(e["end"])
        m.allDay = bool(e.get("all_day"))
        m.notifyMinutesBefore = 0
    return cmd


# --- telefon bildirimleri ve müzik ----------------------------------------
# Kaynak: Gadgetbridge XiaomiNotificationService (tür 7) ve XiaomiMusicService (tür 18).
# 7/0 bildirim gönder, 7/1 kaldır (iki yönde: telefon -> saat ve saatte kapatılınca saat -> telefon),
# 7/2 aramayı reddet, 7/5 aramayı yok say (saatten). 18/0 saat müzik durumunu ister, 18/1 durum gönder,
# 18/2 saatteki düğme.
# Kaynak metinleri kısaltmadan yollar; saatin gerçek sınırı bilinmiyor. Bunlar uygulamanın kendi sınırı.
NOTIFY_TITLE, NOTIFY_BODY, NOTIFY_APP = 64, 400, 40
MUSIC_TEXT = 80
MUSIC_KEYS = {0: "play", 1: "pause", 3: "previous", 4: "next", 5: "volume"}
MUSIC_STATES = {"playing": 1, "paused": 2}   # 0: çalan bir şey yok


def _cut(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def notification(n: dict) -> Command:
    """7/0. n: {"id", "package", "app", "title", "body", "key"?, "when"? (unix sn), "call"? (gelen arama)}.

    Saat gövdesi olmayan bildirimi göstermediği için başlık ve gövde boş da olsa yazılır (kaynaktaki not).
    """
    cmd = Command(type=7, subtype=0)
    m = cmd.notification.notification2.notification3
    m.package = n["package"]
    m.appName = _cut(n.get("app"), NOTIFY_APP) or "UNKNOWN"
    m.title = _cut(n.get("title"), NOTIFY_TITLE)
    m.unknown4 = ""
    m.body = _cut(n.get("body"), NOTIFY_BODY)
    m.timestamp = datetime.fromtimestamp(n.get("when") or datetime.now().timestamp()).strftime("%Y%m%dT%H%M%S")
    m.id = int(n["id"])
    if n.get("call"):
        m.isCall = True
        m.repliesAllowed = False
    elif n.get("key"):
        m.key = n["key"]   # kaynak ayrıca openOnPhone=true yollar; telefonda açma desteklenmediği için yollanmaz
    return cmd


def notification_dismiss(items: list[dict]) -> Command:
    """7/1, telefon -> saat. items: [{"id", "package", "key"?}]."""
    cmd = Command(type=7, subtype=1)
    cmd.notification.notificationDismiss.SetInParent()
    for it in items:
        m = cmd.notification.notificationDismiss.notificationId.add()
        m.id = int(it["id"])
        m.package = it["package"]
        if it.get("key"):
            m.key = it["key"]
    return cmd


def notification_dismissed(cmd) -> list[int]:
    """Saatten gelen 7/1: saatte kapatılan bildirimlerin kimlikleri."""
    return [i.id for i in cmd.notification.notificationDismiss.notificationId]


def music_info(m: dict | None, volume: int = 0) -> Command:
    """18/1. m: None (çalan yok) ya da {"state": "playing" | "paused", "track", "artist", "position", "duration"} (sn).

    volume: telefonun medya sesi, yüzde. Kaynakta albüm alanı yoktur.
    """
    cmd = Command(type=18, subtype=1)
    info = cmd.music.musicInfo
    info.volume = max(0, min(100, int(volume)))
    if not m or m.get("state") not in MUSIC_STATES:
        info.state = 0
        return cmd
    info.state = MUSIC_STATES[m["state"]]
    info.track = _cut(m.get("track"), MUSIC_TEXT)
    info.artist = _cut(m.get("artist"), MUSIC_TEXT)
    info.position = max(0, int(m.get("position") or 0))
    info.duration = max(0, int(m.get("duration") or 0))
    return cmd


def music_key(cmd) -> dict | None:
    """Saatten gelen 18/2 -> {"key": "play" | "pause" | "previous" | "next" | "volume", "volume": istenen yüzde}."""
    k = cmd.music.mediaKey
    name = MUSIC_KEYS.get(k.key)
    if name is None:
        return None
    return {"key": name, "volume": k.volume if name == "volume" else None}
