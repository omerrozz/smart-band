"""Saat özelliklerinin çevrimdışı testi: saate bağlanmaz, 8377'yi açmaz, gerçek config.json ve band.db'ye dokunmaz.

Çalıştırma:  ../.venv/bin/python test_features.py
server.py içe aktarılırken ayar dosyasını yazıp veritabanını açtığı için proje geçici bir klasöre kopyalanır.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
tmp = Path(tempfile.mkdtemp(prefix="bandtest-"))
for f in HERE.glob("*.py"):
    shutil.copy(f, tmp / f.name)
shutil.copytree(HERE / "pb", tmp / "pb", ignore=shutil.ignore_patterns("__pycache__"))
(tmp / "config.json").write_text(json.dumps({"address": "00:00:00:00:00:00", "auth_key": "00" * 16}))
sys.path.insert(0, str(tmp))

import bandmsg  # noqa: E402
import server  # noqa: E402
from bandmsg import Command  # noqa: E402

assert Path(server.__file__).parent == tmp, "test gerçek server.py'yi içe aktardı"
ran = 0


def check(name: str, got, want) -> None:
    global ran
    ran += 1
    if got != want:
        raise SystemExit(f"HATA {name}: {got!r} != {want!r}")
    print("ok ", name)


def wire(cmd) -> Command:
    """Komutu tele yazıp geri okur: alanların gerçekten kodlandığını gösterir."""
    return Command.FromString(cmd.SerializeToString())


class FakeBand:
    """session() için sahte saat: sıradaki komutları verir, gönderilenleri toplar."""

    def __init__(self, incoming):
        self.incoming, self.sent, self.on_command, self.syncs = list(incoming), [], None, 0

    def _send(self, cmd, encrypted=True):
        self.sent.append(wire(cmd))

    def send(self, t, st):
        self._send(Command(type=t, subtype=st))

    def read(self, timeout=1.0):
        if not self.incoming:
            raise EOFError
        item = self.incoming.pop(0)
        if callable(item):  # komutlar arasında çalışacak adım
            item(self)
            return None
        cmd = wire(item)
        self.on_command(cmd)
        return cmd

    def list_files(self, today):
        self.syncs += today
        return []

    def notify(self, *a, **k):
        self.sent.append(("notify", a))

    def realtime_start(self):
        self.send(8, 45)

    def realtime_stop(self):
        self.send(8, 46)

    def kinds(self):
        return [(c.type, c.subtype) for c in self.sent if not isinstance(c, tuple)]


def run(incoming) -> FakeBand:
    band = FakeBand(incoming)
    server.hub.update(link="connected")
    try:
        server.session(band)
    except EOFError:
        pass
    return band


# ---------- 1. cihaz durumu ----------
c = Command(type=2, subtype=78)
b = c.system.basicDeviceState
b.isCharging, b.isWorn, b.isUserAsleep, b.batteryLevel = False, True, False, 64
check("78 çözüm", bandmsg.device_basic(wire(c).system.basicDeviceState),
      {"worn": True, "charging": False, "asleep": False, "battery": 64})
off = Command(type=2, subtype=79)
off.system.deviceState.wearingState = 2
check("79 yalnızca gelen alan", bandmsg.device_update(wire(off).system.deviceState), {"worn": False})
on = Command(type=2, subtype=79)
on.system.deviceState.wearingState, on.system.deviceState.chargingState, on.system.deviceState.sleepState = 1, 1, 2
check("79 üç alan", bandmsg.device_update(on.system.deviceState), {"charging": True, "worn": True, "asleep": False})
odd = Command(type=2, subtype=79)
odd.system.deviceState.wearingState = 7
check("79 bilinmeyen değer yok sayılır", bandmsg.device_update(odd.system.deviceState), {})

band = run([c, off])
snap = server.hub.snapshot()
check("oturum başında yalnızca okuma komutları",
      sorted(set(band.kinds()) - {(8, 45), (8, 46)}),
      sorted({(2, 2), (2, 1), (17, 0), (8, 10), (8, 8), (8, 12), (8, 14), (2, 24), (17, 8), (2, 78), (17, 14), (2, 29), (8, 42)}))
check("8/5 onayı hiç gitmez", (8, 5) in band.kinds(), False)
check("state.device", {k: snap["device"][k] for k in ("worn", "charging", "asleep")},
      {"worn": False, "charging": False, "asleep": False})
check("değişim anı kaydedildi", snap["device"]["worn_since"] is not None, True)
check("pil cihaz durumundan", snap["battery"], 64)
rows = server.store.db.execute("SELECT worn FROM wear ORDER BY ts, rowid").fetchall()
check("bilek kaydı", [r[0] for r in rows][-1], 0)
server.device_lost()
check("kopunca device boşalır", server.hub.snapshot()["device"], None)

# bilekte olmama aralıkları
db = server.store.db
db.execute("DELETE FROM wear")
day = date(2026, 11, 19)
t0 = int(datetime.combine(day, datetime.min.time()).timestamp())
for ts, worn in ((t0 - 600, 0), (t0 + 300, 1), (t0 + 3600, 0), (t0 + 4500, None), (t0 + 7200, 1), (t0 + 80000, 0)):
    db.execute("INSERT INTO wear VALUES (?,?)", (ts, worn))
db.commit()
check("off_wrist: gece yarısını aşan, kopmayla biten, açık kalan",
      server.store.off_wrist(day, now=t0 + 90000), [[0, 5], [60, 75], [1333, 1440]])
check("off_wrist: başka gün boş", server.store.off_wrist(day - timedelta(days=3), now=t0), [])

# ---------- 2. profil ve hedefler ----------
profile = {"age": 22, "height": 180, "weight": 80, "sex": "m"}
goals = {"steps": 9000, "calories": 550, "standing": 12, "moving": 30, "water": 2500}
u = wire(bandmsg.user_info(profile, goals, 198, today=date(2026, 11, 20)))
check("8/0 tür", (u.type, u.subtype), (8, 0))
ui = u.health.userInfo
check("8/0 alanlar", (ui.height, round(ui.weight, 1), ui.birthday, ui.gender, ui.maxHeartRate, ui.goalCalories, ui.goalSteps,
                      ui.goalStanding, ui.goalMoving), (180, 80, 20040101, 1, 198, 550, 9000, 12, 30))
check("8/0 kadın", bandmsg.user_info(dict(profile, sex="f"), goals, 190).health.userInfo.gender, 2)
check("azami nabız elle", server.insights.max_hr({"age": 22, "max_hr_set": 185}), 185)
check("azami nabız yaştan", server.insights.max_hr({"age": 22}), 198)

ack = Command(type=8, subtype=0, status=0)
band = run([lambda b: server.hub.jobs.put(server.write_user_info), lambda b: None, ack])
check("profil yazıldı", [(c.type, c.subtype) for c in band.sent if not isinstance(c, tuple) and c.health.HasField("userInfo")], [(8, 0)])
check("onay state'e yansır", server.hub.snapshot()["userinfo"]["ok"], True)
band = run([])
check("kendiliğinden profil yazılmaz", any(k == (8, 0) for k in band.kinds()), False)
# 20 Kasım 2026: kullanıcı isteğiyle kaydedince her zaman saate yazılır; ayrı seçenek kaldırıldı
server.config["band_write"] = True
server.auto_write()
check("seçenek açıkken sıraya girer", server.hub.jobs.qsize(), 1)
server.hub.jobs.get_nowait()
server.hub.update(link="away")
server.auto_write()
check("saat bağlı değilken sıraya girmez", server.hub.jobs.empty(), True)
server.config["band_write"] = False

# ---------- 3. nabız, SpO2, stres ayarları ----------
hr = Command(type=8, subtype=10)
h = hr.health.heartRate
h.disabled, h.interval, h.alarmHighEnabled, h.alarmHighThreshold, h.unknown7 = False, 1, True, 120, 1
h.advancedMonitoring.enabled = True
h.heartRateAlarmLow.alarmLowEnabled, h.heartRateAlarmLow.alarmLowThreshold = False, 45
h.breathingScore, h.irregularRhythmAlert, h.sleepRespiratoryRate = 2, 0, 1
sp = Command(type=8, subtype=8)
sp.health.spo2.allDayCapable, sp.health.spo2.mode, sp.health.spo2.interval = 1, 1, 0
sp.health.spo2.alarmLow.alarmLowEnabled, sp.health.spo2.alarmLow.alarmLowThreshold = True, 90
stc = Command(type=8, subtype=14)
stc.health.stress.allDayTracking = True
stc.health.stress.relaxReminder.enabled, stc.health.stress.relaxReminder.unknown2 = False, 0
slp = Command(type=17, subtype=8)
slp.schedule.sleepMode.enabled = False
slp.schedule.sleepMode.schedule.start.hour, slp.schedule.sleepMode.schedule.start.minute = 23, 0
slp.schedule.sleepMode.schedule.end.hour, slp.schedule.sleepMode.schedule.end.minute = 7, 30
slp.schedule.sleepMode.schedule.unknown3 = 0


def setting(key, value):
    band = run([hr, sp, stc, slp, lambda b: server.hub.jobs.put(lambda bb: server.apply_setting(bb, key, value)), lambda b: None])
    return [c for c in band.sent if not isinstance(c, tuple) and (c.type, c.subtype) in ((8, 11), (8, 9), (8, 15), (17, 9))]


run([hr, sp, stc, slp])
st = server.hub.snapshot()["settings"]
check("ayar durumu nabız", st["hr"], {"on": True, "alarm": True, "threshold": 120, "interval": 1, "low": 0, "sleep": True,
                                      "irregular": None, "breathing": False, "resp": True})
check("ayar durumu spo2", st["spo2"], {"mode": 1, "low": 90, "allday": False})
check("ayar durumu stres", st["stress"], {"on": True, "relax": False})
check("ayar durumu uyku modu", st["sleep_mode"], {"on": False, "from": "23:00", "to": "07:30"})

(out,) = setting("hr_interval", 10)
m = out.health.heartRate
check("hr_interval tek alan", (m.interval, m.alarmHighThreshold, m.unknown7, m.advancedMonitoring.enabled, m.breathingScore,
                               m.sleepRespiratoryRate, m.heartRateAlarmLow.alarmLowThreshold), (10, 120, 1, True, 2, 1, 45))
(out,) = setting("hr_high", 140)
check("hr_high", (out.health.heartRate.alarmHighThreshold, out.health.heartRate.alarmHighEnabled), (140, True))
(out,) = setting("hr_low", 50)
check("hr_low açar", (out.health.heartRate.heartRateAlarmLow.alarmLowEnabled, out.health.heartRate.heartRateAlarmLow.alarmLowThreshold), (True, 50))
(out,) = setting("hr_low", 0)
check("hr_low kapatır, eşik kalır", (out.health.heartRate.heartRateAlarmLow.alarmLowEnabled, out.health.heartRate.heartRateAlarmLow.alarmLowThreshold), (False, 45))
(out,) = setting("hr_breathing", True)
check("hr_breathing 1/2", out.health.heartRate.breathingScore, 1)
check("desteklenmeyen düzensiz ritim gönderilmez", setting("hr_irregular", True), [])
(out,) = setting("hr_on", False)
check("eski hr_on çalışıyor", (out.health.heartRate.disabled, out.health.heartRate.interval), (True, 1))
(out,) = setting("hr_alarm", False)
check("eski hr_alarm çalışıyor", (out.health.heartRate.alarmHighEnabled, out.health.heartRate.alarmHighThreshold), (False, 120))
(out,) = setting("spo2_allday", True)
check("spo2_allday", (out.health.spo2.mode, out.health.spo2.allDayCapable, out.health.spo2.alarmLow.alarmLowThreshold), (2, 1, 90))
(out,) = setting("spo2_sleep", False)
check("eski spo2_sleep çalışıyor", out.health.spo2.mode, 0)
(out,) = setting("spo2_low", 0)
check("spo2_low kapatır, eşik kalır", (out.health.spo2.alarmLow.alarmLowEnabled, out.health.spo2.alarmLow.alarmLowThreshold), (False, 90))
(out,) = setting("stress_relax", True)
check("stress_relax", (out.health.stress.relaxReminder.enabled, out.health.stress.allDayTracking), (True, True))
(out,) = setting("sleep_mode", True)
sm = out.schedule.sleepMode
check("uyku modu aç", (sm.enabled, sm.schedule.start.hour, sm.schedule.end.hour, sm.schedule.end.minute, sm.schedule.unknown3), (True, 23, 7, 30, 0))
(out,) = setting("sleep_from", "22:15")
sm = out.schedule.sleepMode
check("uyku modu başlangıç", (sm.enabled, sm.schedule.start.hour, sm.schedule.start.minute, sm.schedule.end.hour), (False, 22, 15, 7))
check("değer denetimi", [server.setting_value("hr_interval", 10), server.setting_value("sleep_to", "6:05"), server.setting_value("hr_on", 1)],
      [10, "06:05", True])
for key, bad in (("hr_interval", 5), ("hr_high", 90), ("spo2_low", True), ("sleep_from", "25:00")):
    try:
        server.setting_value(key, bad)
        raise SystemExit(f"HATA {key} {bad!r} kabul edildi")
    except ValueError:
        pass
check("geçersiz değerler reddedilir", True, True)

# ---------- 4. canlı antrenman ----------
def status(code, sport=1, ts=None):
    c = Command(type=8, subtype=26)
    w = c.health.workoutStatusWatch
    w.status, w.sport, w.timestamp = code, sport, ts or int(time.time())
    return c


def live(hr_, kcal, steps, dist):
    c = Command(type=8, subtype=50)
    w = c.health.workoutStatsWatch
    w.heartRate, w.calories, w.steps, w.distance = hr_, kcal, steps, dist
    return c


def rt(steps, kcal, hr_):
    c = Command(type=8, subtype=47)
    c.health.realTimeStats.steps, c.health.realTimeStats.calories, c.health.realTimeStats.heartRate = steps, kcal, hr_
    return c


v2 = Command(type=8, subtype=29)
v2.health.workoutStatusWatchV2.status, v2.health.workoutStatusWatchV2.sport.type = 2, 6
check("8/29 sırası", bandmsg.workout_event(wire(v2))["event"], "pause")
check("8/26 sırası", [bandmsg.workout_event(status(i))["event"] for i in range(4)], ["start", "pause", "resume", "finish"])

server.hub.sync_now.clear()
band = run([status(0, 1, 1700000000), live(121, 3, 40, 30)])
w = server.hub.snapshot()["workout"]
check("antrenman başladı", (w["active"], w["paused"], w["sport"], w["hr"], w["steps"], w["calories"], w["distance"]),
      (True, False, "Açık hava koşusu", 121, 40, 3, 30))
check("eski zaman damgası yerine şimdi", abs(w["started"] - time.time()) < 5, True)
check("antrenman sürerken canlı akış istenir", (8, 45) in band.kinds(), True)
band = run([live(125, 5, 90, 70), status(1), status(2), live(130, 9, 150, 120), status(3)])
w = server.hub.snapshot()["workout"]
check("antrenman bitti", (w["active"], w["hr"], w["steps"]), (False, 130, 150))
check("bitince dosyalar eşitlenir (oturum başındakine ek)", band.syncs, 2)

server.hub.sync_now.clear()
band = run([status(0, 99), rt(5000, 800, 110), rt(5120, 812, 132)])
w = server.hub.snapshot()["workout"]
check("canlı değer gelmezse sayaç farkı", (w["active"], w["sport"], w["hr"], w["steps"], w["calories"]), (True, None, 132, 120, 12))
server.wk["heard"] -= 4000
server.workout_check(time.monotonic())
check("canlı değer görülmediyse susunca kapanmaz", server.workout["active"], True)
band = run([status(1)])
check("duraklatıldı", server.hub.snapshot()["workout"]["paused"], True)
server.wk.update(resumed=True, heard=time.monotonic() - 100)
server.workout_check(time.monotonic())
check("yeniden bağlanınca işaret gelmezse kapanır", (server.workout["active"], server.hub.sync_now.is_set()), (False, True))

band = run([live(140, 50, 900, 800)])
w = server.hub.snapshot()["workout"]
check("başlangıcı görülmeyen antrenman", (w["active"], w["started"], w["hr"]), (True, None, 140))
server.wk["heard"] -= 61
server.workout_check(time.monotonic())
check("canlı değer susunca kapanır", server.workout["active"], False)
server.hub.sync_now.clear()

# ---------- 5. hatırlatıcılar ----------
new = wire(bandmsg.reminder({"id": None, "title": "İlaç", "date": "2026-12-01", "time": "09:30", "repeat": 1}))
d = new.schedule.createReminder
utc = datetime(2026, 12, 1, 9, 30).astimezone().astimezone(bandmsg.timezone.utc)
check("17/15 ekle", (new.type, new.subtype, d.title, d.repeatMode, d.HasField("repeatFlags")), (17, 15, "İlaç", 1, False))
check("17/15 tarih UTC (kaynaktaki gibi)", (d.date.year, d.date.month, d.date.day, d.time.hour, d.time.minute, d.time.second),
      (utc.year, utc.month, utc.day, utc.hour, utc.minute, 0))
lst = Command(type=17, subtype=14)
lst.schedule.reminders.maxReminders = 50
r1 = lst.schedule.reminders.reminder.add()
r1.id = 3
r1.reminderDetails.CopyFrom(d)
r2 = lst.schedule.reminders.reminder.add()
r2.id = 4
r2.reminderDetails.CopyFrom(d)
r2.reminderDetails.repeatMode, r2.reminderDetails.repeatFlags, r2.reminderDetails.title = 5, 3, "Haftalık"
items, most = bandmsg.reminders(wire(lst).schedule.reminders)
check("17/14 gidiş dönüş", (items[0], most), ({"id": 3, "title": "İlaç", "date": "2026-12-01", "time": "09:30", "repeat": 1, "flags": None}, 50))
ed = wire(bandmsg.reminder(dict(items[1], title="Yeni ad")))
e = ed.schedule.editReminder
check("17/17 düzenle, haftalık günler korunur", (ed.subtype, e.id, e.reminderDetails.title, e.reminderDetails.repeatMode, e.reminderDetails.repeatFlags),
      (17, 4, "Yeni ad", 5, 3))
dl = wire(bandmsg.reminder({"id": 4, "delete": True}))
check("17/18 sil", (dl.type, dl.subtype, list(dl.schedule.deleteReminder.id)), (17, 18, [4]))
bandmsg.REMINDER_UTC = False
local = bandmsg.reminder({"id": None, "title": "x", "date": "2026-12-01", "time": "09:30", "repeat": 0}).schedule.createReminder
check("REMINDER_UTC kapalıyken yerel saat", (local.date.day, local.time.hour, local.time.minute), (1, 9, 30))
bandmsg.REMINDER_UTC = True
band = run([lst, lambda b: server.hub.jobs.put(lambda bb: server.apply_reminder(bb, {"id": 3, "delete": True})), lambda b: None,
            Command(type=17, subtype=18)])
snap = server.hub.snapshot()
check("state.reminders", (len(snap["reminders"]), snap["max_reminders"]), (2, 50))
check("silince liste yeniden okunur", band.kinds()[-3:], [(17, 18), (17, 14), (17, 14)])

# ---------- 6. ekran öğeleri ----------
di = Command(type=2, subtype=29)
for code, name, disabled, settings, more in (("hr", "Nabız", False, 0, False), ("wx", "Hava", False, 0, True),
                                             ("set", "Ayarlar", False, 1, False), ("mus", "Müzik", True, 0, False)):
    i = di.system.displayItems.displayItem.add()
    i.code, i.name, i.unknown5 = code, name, 1
    if disabled:
        i.disabled = True
    if settings:
        i.isSettings = 1
    if more:
        i.inMoreSection = True
cur = wire(di).system.displayItems
check("ekran durumu", [(x["code"], x["on"], x["more"], x["fixed"]) for x in bandmsg.display_state(cur)],
      [("hr", True, False, False), ("wx", True, True, False), ("set", True, False, True), ("mus", False, False, False)])
w30 = wire(bandmsg.display_items(cur, ["mus", "set", "hr", "wx"], {"hr", "set"}))
check("2/30 sıra ve kapalılar", [(i.code, i.name, i.disabled, i.inMoreSection, i.isSettings, i.unknown5) for i in w30.system.displayItems.displayItem],
      [("mus", "Müzik", False, False, 0, 1), ("set", "Ayarlar", False, False, 1, 1), ("wx", "Hava", False, True, 0, 1), ("hr", "Nabız", True, False, 0, 1)])
check("2/30 tür", (w30.type, w30.subtype), (2, 30))
try:
    bandmsg.display_items(cur, ["hr", "wx"], set())
    raise SystemExit("HATA eksik liste kabul edildi")
except ValueError:
    check("eksik liste reddedilir", True, True)

# ---------- eski davranışlar ----------
band = run([rt(100, 10, 70)])
check("canlı değerler state'te", (server.hub.snapshot()["steps"], server.hub.snapshot()["calories"]), (100, 10))
check("saate kendiliğinden bildirim gitmez", any(isinstance(c, tuple) for c in band.sent), False)
allowed = {(2, 2), (2, 1), (17, 0), (8, 10), (8, 8), (8, 12), (8, 14), (2, 24), (17, 8), (2, 78), (17, 14), (2, 29), (8, 42), (8, 45), (8, 46)}
check("kullanıcı işlemi yokken yalnızca okuma ve canlı akış", set(band.kinds()) <= allowed, True)

# ---------- HTTP uçları (rastgele boş kapıda, 8377 değil) ----------
import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{httpd.server_address[1]}"


def post(path, body):
    req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


AWAY = "Saat bağlı değil. Yaklaşınca tekrar dene."
server.hub.update(link="away")
for path in ("/api/userinfo", "/api/reminder", "/api/display", "/api/setting"):
    check(f"bağlı değilken {path}", post(path, {}), (409, {"error": AWAY}))
check("hedefler: ayakta ve hareket", post("/api/goals", {"steps": 8000, "calories": 500, "standing": 10, "moving": 45})[0], 200)
check("hedefler ayara yazıldı", {k: server.config["goals"][k] for k in ("steps", "standing", "moving")}, {"steps": 8000, "standing": 10, "moving": 45})
check("hedefler: eski istek biçimi", post("/api/goals", {"steps": 9000, "calories": 500})[0], 200)
check("eski istek ayakta hedefini silmez", server.config["goals"]["standing"], 10)
check("hedefler: geçersiz ayakta", post("/api/goals", {"steps": 9000, "calories": 500, "standing": 40})[0], 400)
check("profil: azami nabız", post("/api/profile", {"age": 30, "height": 180, "weight": 80, "sex": "m", "max_hr": 185})[0], 200)
check("state.profile.max_hr", server.hub.snapshot()["profile"]["max_hr"], 185)
check("profil: azami nabız boş", post("/api/profile", {"age": 30, "height": 180, "weight": 80, "sex": "m", "max_hr": ""})[0], 200)
check("boşsa 220 − yaş", server.hub.snapshot()["profile"]["max_hr"], 190)
check("profil: geçersiz azami nabız", post("/api/profile", {"age": 30, "height": 180, "weight": 80, "sex": "m", "max_hr": 300})[0], 400)
check("seçenek varsayılanı kapalı", server.hub.snapshot()["band_write"], False)
check("seçenek açılır", (post("/api/band-write", {"on": True})[0], server.config["band_write"], server.hub.snapshot()["band_write"]), (200, True, True))
post("/api/band-write", {"on": False})

server.hub.update(link="connected", reminders=items, max_reminders=50)
server.band_cfg["display"] = cur
check("ayar: geçersiz değer", post("/api/setting", {"key": "hr_interval", "value": 7})[0], 400)
check("ayar: bilinmeyen", post("/api/setting", {"key": "x", "value": 1})[0], 400)
check("ayar: seçim", (post("/api/setting", {"key": "hr_low", "value": 45})[0], server.hub.jobs.qsize()), (200, 1))
check("hatırlatıcı: başlıksız", post("/api/reminder", {"title": " ", "date": "2026-12-01", "time": "09:00", "repeat": 0})[0], 400)
check("hatırlatıcı: yenide haftalık yok", post("/api/reminder", {"title": "a", "date": "2026-12-01", "time": "09:00", "repeat": 5})[0], 400)
check("hatırlatıcı: saatte olmayan kayıt", post("/api/reminder", {"id": 99, "delete": True})[0], 409)
check("hatırlatıcı: haftalık kayıt düzenlenir", post("/api/reminder", {"id": 4, "title": "b", "date": "2026-12-01", "time": "09:00", "repeat": 5})[0], 200)
check("hatırlatıcı: yeni", post("/api/reminder", {"title": "a", "date": "2026-12-01", "time": "09:00", "repeat": 7})[0], 200)
check("ekran: eksik liste", post("/api/display", {"order": ["hr"]})[0], 409)
check("ekran: geçerli", post("/api/display", {"order": ["hr", "wx", "set", "mus"], "off": ["mus"]})[0], 200)
check("profil yaz sıraya girer", post("/api/userinfo", {})[0], 200)
sent = FakeBand([])
while not server.hub.jobs.empty():
    server.hub.jobs.get_nowait()(sent)
check("sıradaki işler doğru komutları yollar", [k for k in sent.kinds() if k not in ((17, 14), (2, 29), (8, 10), (8, 8), (8, 12), (8, 14), (2, 24), (17, 8))],
      [(8, 11), (17, 17), (17, 15), (2, 30), (8, 0)])
with urllib.request.urlopen(base + "/api/minutes") as r:
    check("dakikalar bilek aralıklarını taşır", "off_wrist" in json.loads(r.read()), True)
with urllib.request.urlopen(base + "/api/state") as r:
    keys = set(json.loads(r.read()))
check("state anahtarları", {"device", "workout", "reminders", "max_reminders", "display", "band_write", "userinfo"} <= keys, True)

# ---------- 7. uygulamadan başlatılan antrenman ----------
import appworkout  # noqa: E402

AW = appworkout
check("adım boyu sabitleri", (round(AW.stride(180, "walk"), 4), round(AW.stride(180, "run"), 3)), (0.747, 1.17))
check("bölge sınırları arayüzle aynı", [AW.zone_of(h, 190) for h in (94, 95, 114, 133, 152, 171, 200, None)], [-1, 0, 1, 2, 3, 4, 4, -1])
R = AW.Recorder(server.store, lambda: {"height": 180}, lambda: 190)


def refused(*args):
    try:
        R.start(*args, now=1000.0)
    except ValueError as exc:
        return str(exc)
    R.discard()
    return None


check("bilinmeyen spor reddedilir", refused("yoga", None, None), "Bilinmeyen spor türü.")
check("bisiklette mesafe hedefi yok", "mesafe hedefi konamaz" in refused("cycling", {"type": "distance", "value": 5}, None), True)
check("hedef aralığı", refused("walking", {"type": "time", "value": 0}, None), "Süre hedefi 1–360 dakika arasında olmalı.")
check("bölge aralığı", refused("walking", None, 9), "Bölge 1 ile 5 arasında olmalı.")
check("geçerli istek kabul edilir", refused("walking", {"type": "distance", "value": 2}, 3), None)

# yürüyüş: 5 saniyede bir okuma, her okumada 10 adım (120 adım/dk), 1 kcal
T0 = 1_800_000_000.0
R.start("walking", {"type": "time", "value": 2}, None, now=T0)
for i in range(1, 13):   # 60. saniyeye kadar
    R.on_reading(5000 + 10 * i, 300 + i, 100, now=T0 + 5 * i)
st = R.state(T0 + 60)
check("adım ve kalori başlangıca göre", (st["steps"], st["calories"], st["elapsed"]), (110, 11, 60))
check("mesafe adım × adım boyu", st["distance"], round(110 * 0.747))
check("ritim ve hız pencereden", (st["cadence"], abs(st["speed"] - 2 * 0.747) < 0.02), (120, True))
check("süre okumayı beklemez", R.state(T0 + 63)["elapsed"], 63)
check("ikinci kez başlatılamaz", refused("walking", None, None), "Zaten süren bir antrenman var. Önce onu bitir.")
# duraklatma: aradaki adımlar sayılmaz, süre durur
R.pause(T0 + 60)
R.on_reading(5200, 320, 90, now=T0 + 65)
st = R.state(T0 + 100)
check("duraklatılmışken sayılmaz", (st["paused"], st["steps"], st["elapsed"], st["hr"]), (True, 110, 60, None))
R.resume(T0 + 120)
R.on_reading(5300, 330, 120, now=T0 + 125)   # ilk okuma yeni taban
R.on_reading(5310, 331, 120, now=T0 + 130)
st = R.state(T0 + 130)
check("sürdürünce yeni taban", (st["paused"], st["steps"], st["calories"], st["elapsed"]), (False, 120, 12, 70))
# hedef: 2 dakika etkin süre
check("hedef dolmadan bildirim yok", R.tick(T0 + 170, True), None)
check("hedef dolunca bir bildirim", R.tick(T0 + 181, True), ("goal", "2 dakika doldu."))
check("ikinci bildirim gitmez", R.tick(T0 + 190, True), None)
check("hedef durumu", (R.state(T0 + 190)["goal"]["reached"], R.state(T0 + 190)["goal"]["progress"]), (True, 1.0))
# bağlantı boşluğu: saat aradaki adımları saymıştır, eklenir; boşluk işaretlenir
R.on_reading(5320, 332, 121, now=T0 + 135)
R.on_reading(5720, 352, 125, now=T0 + 435)
st = R.state(T0 + 435)
check("boşluktaki adımlar eklenir", (st["steps"], st["calories"]), (530, 33))
check("boşluk işaretlenir", (R.s["gaps"], R.samples[-2][1], R.samples[-1][5]), ([[int(T0) + 135, int(T0) + 435]], None, None))
check("veri kesilince arayüze bildirilir", (R.state(T0 + 440)["stale"], R.state(T0 + 460)["stale"], R.state(T0 + 460)["hr"]), (False, True, None))
# servis yeniden başlarsa oturum sürer
R2 = AW.Recorder(server.store, lambda: {"height": 180}, lambda: 190)
check("yeniden başlatmada sürer", (R2.restore(T0 + 500), R2.active, len(R2.samples), R2.s["steps"]), (None, True, len(R.samples), 530))
R2.on_reading(5730, 353, 130, now=T0 + 440)
check("yeniden başlatmadan sonra fark sürer", R2.state(T0 + 440)["steps"], 540)
# gece yarısı sayaç sıfırlanması
R2.on_reading(4, 0, 131, now=T0 + 445)
R2.on_reading(14, 1, 131, now=T0 + 450)
check("sayaç sıfırlanınca fark 0", (R2.s["steps"], R2.s["calories"]), (550, 35))
res = R2.finish(T0 + 450)
check("kaydedildi", (res["saved"], res["id"], R2.active), (True, int(T0), False))
check("oturum tablosu boşaldı", server.store.app_session_load(), None)
row = next(w for w in server.store.workouts() if w["id"] == int(T0))
check("özet biçimi", {k: row[k] for k in ("start", "end", "sport", "duration", "steps", "calories", "source", "distance_estimated", "has_series", "has_gps", "paused")},
      {"start": int(T0), "end": int(T0) + 450, "sport": "walking", "duration": 390, "steps": 550, "calories": 35, "source": "app",
       "distance_estimated": True, "has_series": True, "has_gps": False, "paused": 60})
check("mesafe ve tempo", (row["distance"], row["pace_avg"], row["cadence_avg"]), (round(550 * 0.747), round(390 / round(550 * 0.747) * 1000), round(550 / 390 * 60)))
check("nabız özeti", (row["hr_min"], row["hr_max"], row["hr_avg"] > 100), (100, 131, True))
check("bölge anahtarları", sorted(row["hr_zones"]), sorted(AW.ZONE_KEYS))
check("bölge süreleri: 100 → B1, 120-131 → B2", (row["hr_zones"]["warm_up"], row["hr_zones"]["fat_burn"], row["hr_zones"]["aerobic"]), (60, 35, 0))
check("hedef özette", row["goal"], {"type": "time", "value": 2.0, "reached": True})
full = server.store.workout(int(T0))
ser = full["series"]
check("seri sütunları", sorted(ser), ["cadence", "distance", "hr", "kcal", "speed", "steps", "ts"])
check("seri boyları eşit", len({len(v) for v in ser.values()}), 1)
check("seri birikimli ve boşluk satırı boş", (ser["steps"][-1], ser["kcal"][-1], ser["distance"][-1], None in ser["hr"]), (550, 35, round(550 * 0.747, 1), True))
check("GPS izi yok", (full["track"], server.store.workout_gpx(int(T0))), (None, None))

# koşu: düşük ritim yürüyüş boyuyla, yüksek ritim koşu boyuyla; bisiklette mesafe yok
R.discard()
R.start("running", None, None, now=T0 + 1000)
R.on_reading(100, 10, 140, now=T0 + 1005)
R.on_reading(110, 11, 140, now=T0 + 1010)   # 120 adım/dk → yürüyüş boyu
R.on_reading(125, 12, 150, now=T0 + 1015)   # 180 adım/dk → koşu boyu
check("koşuda ritme göre adım boyu", round(R.s["distance"], 3), round(10 * 0.747 + 15 * 1.17, 3))
check("kısa antrenman kaydedilmez", (R.finish(T0 + 1030)["saved"], R.active), (False, False))
R.start("cycling", {"type": "calories", "value": 10}, None, now=T0 + 2000)
for i in range(1, 30):
    R.on_reading(100 + i, 10 + i, None, now=T0 + 2000 + 5 * i)
st = R.state(T0 + 2145)
check("bisiklette mesafe yok", (st["distance"], st["distance_estimated"], st["speed"]), (None, False, None))
check("kalori hedefi bağlantı yokken beklenir", R.tick(T0 + 2146, False), None)
check("bağlantı dönünce gider", R.tick(T0 + 2150, True), ("goal", "10 kcal yakıldı."))
res = R.finish(T0 + 2150)
check("nabız yoksa kaydedilmez", (res["saved"], "nabız" in res["message"]), (False, True))
R.start("treadmill", {"type": "time", "value": 1}, None, now=T0 + 3000)
check("geç kalan hedef bildirimi atılır", (R.tick(T0 + 3061, False), R.tick(T0 + 3200, True)), (None, None))
# uzun sessizlik: son okumanın anında biter
for i in range(1, 31):
    R.on_reading(100 + 14 * i, 10 + i, 150, now=T0 + 3000 + 5 * i)
check("sessizlik dolmadan sürer", R.tick(T0 + 3150 + AW.SILENCE - 1, False), None)
kind, res = R.tick(T0 + 3150 + AW.SILENCE, False)
check("30 dk sessizlikte kendiliğinden biter", (kind, res["saved"], res["auto"]), ("finished", True, "silence"))
check("bitiş son okumanın anı", server.store.workout(int(T0) + 3000)["end"], int(T0) + 3150)
# üst sınır
R.start("freestyle", None, None, now=T0 + 10000)
R.on_reading(1, 1, 99, now=T0 + 10005)
R.on_reading(2, 2, 99, now=T0 + 10000 + AW.CAP - 5)
kind, res = R.tick(T0 + 10000 + AW.CAP + 3, True)
w6 = server.store.workout(int(T0) + 10000)
check("6 saatte kendiliğinden biter", (kind, res["auto"], w6["duration"], "distance" in w6, sorted(w6["series"])), ("finished", "cap", AW.CAP, False, ["cadence", "hr", "kcal", "steps", "ts"]))
# yeniden başlatma: servis kapalıyken süresi dolmuş oturum açılışta bitirilir
R.start("walking", None, None, now=T0 + 40000)
for i in range(1, 21):
    R.on_reading(10 * i, i, 110, now=T0 + 40000 + 5 * i)
R3 = AW.Recorder(server.store, lambda: {"height": 180}, lambda: 190)
res = R3.restore(T0 + 40100 + AW.SILENCE + 5)
check("eski oturum açılışta bitirilir", (res["saved"], res["auto"], R3.active, server.store.workout(int(T0) + 40000)["duration"]), (True, "restart", False, 100))
R.discard()

# çakışma kuralı: saatin kaydı kalır, çakışan uygulama kaydı silinir
ids = lambda: {w["id"] for w in server.store.workouts()}  # noqa: E731
A1, A2 = int(T0), int(T0) + 3000
check("iki uygulama kaydı var", {A1, A2} <= ids(), True)
server.store.db.execute("INSERT INTO workouts (start, end, sport, duration, summary) VALUES (?,?,?,?,?)",
                        (A2 - 4000, A2 - 3990, "running", 10, json.dumps({"start": A2 - 4000, "end": A2 - 3990, "sport": "running", "source": "mifitness"})))
with server.store.lock:
    server.store._ingest("workout_summary", {"ts": A1 + 100}, {"sport": "walking", "end": A1 + 400, "duration": 300})
    server.store._ingest("workout_summary", {"ts": A2 + 120}, {"sport": "treadmill", "end": A2 + 600, "duration": 480})  # yalnızca 30 sn çakışır
    server.store.db.commit()
now_ids = ids()
check("çakışan uygulama kaydı silinir, saatinki kalır", (A1 in now_ids, A1 + 100 in now_ids), (False, True))
check("60 saniyeden az çakışan kalır", (A2 in now_ids, A2 + 120 in now_ids, A2 - 4000 in now_ids), (True, True, True))
R.start("walking", None, None, now=A1 + 150)
for i in range(1, 30):
    R.on_reading(10 * i, i, 110, now=A1 + 150 + 5 * i)
res = R.finish(A1 + 300)
check("saatin kaydı zaten varsa uygulama kaydı yazılmaz", (res["saved"], res["id"], A1 + 150 in ids()), (False, A1 + 100, False))
with server.store.lock:   # aynı saniyede başlamış uygulama kaydı saatin dosyasıyla karışmaz
    server.store._ingest("workout_details", {"ts": A2}, [])
    server.store.db.commit()
check("aynı başlangıçta saatin dosyası uygulama kaydının yerini alır", server.store.workout(A2)["sport"], None)

# servis bağlantıları
server.hub.update(link="away")
check("bağlı değilken başlatılamaz", post("/api/workout/start", {"sport": "walking"}), (409, {"error": AWAY}))
check("süren antrenman yokken", post("/api/workout/pause", {})[0], 409)
server.hub.update(link="connected")
check("geçersiz spor", post("/api/workout/start", {"sport": "yoga"})[0], 400)
check("geçersiz hedef", post("/api/workout/start", {"sport": "walking", "goal": {"type": "time", "value": "x"}})[0], 400)
check("başlat", post("/api/workout/start", {"sport": "walking", "goal": {"type": "time", "value": 1}, "zone": 2}), (200, {"ok": True}))
check("ikinci başlatma reddedilir", post("/api/workout/start", {"sport": "running"})[0], 409)
snap = server.hub.snapshot()
check("state.app_workout", {k: snap["app_workout"][k] for k in ("active", "paused", "sport", "zone", "distance_estimated")},
      {"active": True, "paused": False, "sport": "walking", "zone": 2, "distance_estimated": True})
check("bölge uyarısı antrenmanla açılır", (snap["coach"]["on"], snap["coach"]["zone"]), (True, 2))


def back(sec):
    """Süren oturumu geçmişe kaydırır: gerçek zaman beklemeden sonraki okuma ayrı bir örnek olur."""
    s = server.appwk.s
    s["start"] -= sec
    s["heard"] -= sec
    if s["last"]:
        s["last"]["t"] -= sec
    for x in server.appwk.samples:
        x[0] -= sec


server.coach["on"] = False   # bu bölümde yalnızca hedef bildirimi sınansın
band = run([rt(7000, 400, 101), lambda b: back(5), rt(7010, 401, 102), lambda b: back(55), rt(7130, 409, 103),
            lambda b: server.appwk_tick(), lambda b: None, status(0, 2)])
aw = server.hub.snapshot()["app_workout"]
check("canlı akış açık tutulur", (8, 45) in band.kinds(), True)
check("okumalar örnek olur", (aw["steps"], aw["calories"], aw["hr"], len(server.appwk.samples)), (130, 9, 103, 3))
check("hedefe ulaşınca tek bildirim", [c for c in band.sent if isinstance(c, tuple)], [("notify", ("Hedefe ulaşıldı", "1 dakika doldu."))])
check("etkinlik onayı gitmez", (8, 5) in band.kinds(), False)
# yansıtılan antrenmanda saatten gelen "başladı" bildirimi bizim isteğimizin yankısıdır: ayrı antrenman sayılmaz
check("yansıtılan antrenmanda saatin yankısı ayrı antrenman sayılmaz", (aw["active"], server.hub.snapshot()["workout"]["active"], server.appwk.s["band_workout"]), (True, False, False))
server.workout_end()
server.hub.sync_now.clear()
server.appwk_coach(True)
check("duraklat", (post("/api/workout/pause", {})[0], server.hub.snapshot()["app_workout"]["paused"], server.coach["on"]), (200, True, False))
check("sürdür", (post("/api/workout/resume", {})[0], server.hub.snapshot()["app_workout"]["paused"], server.coach["on"]), (200, False, True))
check("bölge kutusundan kapatılınca antrenmanın bölgesi de kalkar", (post("/api/coach", {"on": False, "zone": 2})[0], server.appwk.s["zone"]), (200, None))
post("/api/coach", {"on": True, "zone": 3})
check("bölge kutusundan açılınca antrenmana işlenir", (server.appwk.s["zone"], server.coach["zone"]), (3, 3))
server.hub.update(link="away")   # bağlantı yokken de bitirilebilir
back(5)
wid = server.appwk.s["start"]
code, res = post("/api/workout/finish", {})
check("bitir", (code, res["saved"], res["id"], server.hub.snapshot()["app_workout"], server.coach["on"]), (200, True, wid, {"active": False}, False))
check("son sonuç state'te", server.hub.snapshot()["app_workout_last"]["id"], wid)
with urllib.request.urlopen(f"{base}/api/workout?id={wid}") as r:
    got = json.loads(r.read())
check("kayıt uçtan okunur", (got["source"], got["sport"], got["steps"], got["coach_zone"], len(got["series"]["ts"])), ("app", "walking", 130, 3, 3))
server.hub.update(link="connected")
post("/api/workout/start", {"sport": "cycling"})
check("bölgesiz başlatınca uyarı açılmaz", server.coach["on"], False)
check("kaydetmeden sil", (post("/api/workout/discard", {})[0], server.appwk.active, server.store.app_session_load()), (200, False, None))
band = run([rt(7200, 410, 90)])
check("antrenman yokken okuma kaydedilmez", (server.appwk.samples, any(isinstance(c, tuple) for c in band.sent)), ([], False))
with urllib.request.urlopen(base + "/api/state") as r:
    check("state.app_workout anahtarı", json.loads(r.read())["app_workout"], {"active": False})

# ---------- hava durumu (ağa çıkılmaz: weather.http_json sahtesiyle değiştirilir) ----------
import re  # noqa: E402

import weather  # noqa: E402

check("konum anahtarı Java hashCode ile", (bandmsg.location_key("a"), bandmsg.location_key("ab")), ("accu:97", "accu:3105"))
check("konum anahtarı: Integer.MIN_VALUE", bandmsg.location_key("polygenelubricants"), "accu:-483648")
check("tarih biçimi", bool(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", bandmsg.iso_colon(1795200000))), True)
check("boş tarih boş metin", bandmsg.iso_colon(0), "")
check("WMO -> saat kodu", [bandmsg.band_condition(c) for c in (0, 2, 3, 45, 55, 63, 65, 67, 75, 81, 86, 95, 99, 1234)],
      [0, 1, 2, 18, 7, 8, 9, 19, 16, 3, 13, 4, 5, 2])
check("her WMO kodunun metni var", sorted(weather.WMO_TEXT) == sorted(bandmsg.WMO_TO_BAND), True)
check("Beaufort", [bandmsg.beaufort(v) for v in (0, 2, 3, 15, 50, 200, None)], [0, 0, 1, 3, 6, 12, 0])

T0 = int(time.time()) // 3600 * 3600
calls = []


def fake_http(url, params):
    calls.append((url, dict(params)))
    if url == weather.GEOCODE_URL:
        return {"results": [{"name": "Ankara", "admin1": "Ankara", "country": "Türkiye", "latitude": 39.91987, "longitude": 32.85427,
                             "population": 3517182}, {"name": "Ankara", "country": "Kolombiya", "latitude": 4.5, "longitude": -75.123}]}
    hours = [T0 + 3600 * (i - 2) for i in range(48)]
    return {"current": {"time": T0, "temperature_2m": 11.6, "relative_humidity_2m": 71, "weather_code": 61,
                        "wind_speed_10m": 14.4, "wind_direction_10m": 225, "pressure_msl": 1013.2},
            "hourly": {"time": hours, "temperature_2m": [5.4 + i for i in range(48)], "weather_code": [3] * 48,
                       "wind_speed_10m": [20.0] * 48, "wind_direction_10m": [90] * 48, "uv_index": [0.0, 1.0, 3.6] + [2.0] * 45},
            "daily": {"time": [T0 + 86400 * i for i in range(7)], "weather_code": [63, 0, 2, 3, 71, 95, 80],
                      "temperature_2m_max": [12.4, 13, 14, 15, 1, 9, 8], "temperature_2m_min": [3.5, 4, 5, 6, -4.6, 2, 1],
                      "sunrise": [T0 + 86400 * i + 100 for i in range(7)], "sunset": [T0 + 86400 * i + 36000 for i in range(7)]}}


def offline(url, params):
    raise urllib.error.URLError("ağ yok")


weather.http_json = fake_http
check("şehir adayları yuvarlanır", weather.geocode("Ankara"),
      [{"name": "Ankara", "admin": "Ankara", "country": "Türkiye", "lat": 39.92, "lon": 32.85},
       {"name": "Ankara", "admin": "", "country": "Kolombiya", "lat": 4.5, "lon": -75.12}])
check("aramada yalnızca şehir adı gider", calls[-1], (weather.GEOCODE_URL, {"name": "Ankara", "count": 5, "language": "tr"}))
W = weather.forecast(39.91987, 32.85427, "Ankara", now=T0 + 60)
check("tahminde yalnızca yuvarlanmış konum gider", (calls[-1][0], calls[-1][1]["latitude"], calls[-1][1]["longitude"],
                                                    sorted(calls[-1][1])),
      (weather.FORECAST_URL, "39.92", "32.85", ["current", "daily", "forecast_days", "hourly", "latitude", "longitude",
                                                "timeformat", "timezone", "wind_speed_unit"]))
check("tahmin sözlüğü", (W["temp"], W["code"], W["uv"], len(W["days"]), len(W["hours"]), W["hours"][0]["ts"], W["aqi"]),
      (11.6, 61, 3.6, 7, 23, T0 + 3600, None))
bandmsg.AQI_PLACEHOLDER = False  # aşağıdaki denetimler hava kalitesi alanının boş bırakıldığı kipi sınar
cur = wire(bandmsg.weather_current(W))
c = cur.weather.current
check("10/0 anlık", ((cur.type, cur.subtype), c.weatherCondition, (c.temperature.unit, c.temperature.value), (c.humidity.unit, c.humidity.value),
                     (c.wind.unit, c.wind.value), (c.uv.unit, c.uv.value), c.HasField("aqi"), c.HasField("warning"), round(c.pressure)),
      ((10, 0), 7, ("℃", 12), ("%", 71), ("225", 3), ("", 4), False, True, 101320))
check("10/0 konum bilgisi", (c.metadata.locationName, c.metadata.locationKey, c.metadata.cityName, c.metadata.isCurrentLocation,
                             c.metadata.publicationTimestamp), ("Ankara", bandmsg.location_key("Ankara"), "", True, bandmsg.iso_colon(T0)))
day = wire(bandmsg.weather_daily(W))
e = day.weather.forecast.entries.entry
check("10/1 günlük", ((day.type, day.subtype), len(e), (getattr(e[0].temperatureRange, "from"), e[0].temperatureRange.to),
                      (getattr(e[0].conditionRange, "from"), e[0].conditionRange.to), e[4].temperatureRange.to, e[5].conditionRange.to,
                      e[0].temperatureSymbol, e[1].sunriseSunset.sunrise, e[0].HasField("aqi")),
      ((10, 1), 7, (12, 4), (7, 7), -5, 4, "℃", bandmsg.iso_colon(T0 + 86500), False))
hr = wire(bandmsg.weather_hourly(W))
e = hr.weather.forecast.entries.entry
check("10/2 saatlik", ((hr.type, hr.subtype), len(e), (getattr(e[0].temperatureRange, "from"), e[0].temperatureRange.to),
                       (getattr(e[0].conditionRange, "from"), e[0].conditionRange.to), (e[0].wind.unit, e[0].wind.value)),
      ((10, 2), 23, (0, 8), (0, 2), ("90", 4)))
loc = wire(bandmsg.weather_location("Ankara"))
check("10/7 konum", ((loc.type, loc.subtype), loc.weather.location.code, loc.weather.location.name), ((10, 7), bandmsg.location_key("Ankara"), "Ankara"))
weather.http_json = offline
try:
    weather.forecast(1, 2, "x")
    got = None
except weather.WeatherError as exc:
    got = str(exc)
check("internet yokken ileti", got, "Hava durumu alınamadı: internet yok.")
weather.http_json = lambda url, params: {"current": {}}
try:
    weather.forecast(1, 2, "x")
    got = None
except weather.WeatherError as exc:
    got = str(exc)
check("bozuk yanıt iletisi", got, "Hava durumu alınamadı: beklenmeyen yanıt.")


def settle():
    """Arka plandaki indirme bitene kadar bekler."""
    for _ in range(200):
        if not server.wx["busy"] and not any(t.name.startswith("Thread") and t.is_alive() and getattr(t, "_target", None) is server.weather_fetch
                                             for t in threading.enumerate()):
            return
        time.sleep(0.01)


def state_now():
    with urllib.request.urlopen(base + "/api/state") as r:
        return json.loads(r.read())


IDLE = [lambda b: None] * 3
calls.clear()
weather.http_json = fake_http
server.hub.update(link="connected")
check("varsayılan: kapalı", {k: state_now()["weather"][k] for k in ("enabled", "city", "lat", "every", "last_ok")},
      {"enabled": False, "city": "", "lat": None, "every": 60, "last_ok": None})
ask = Command(type=10, subtype=3)
ask.weather.location.code = "accu:1"
band = run([ask] + IDLE)
check("kapalıyken saate hava ya da takvim yazılmaz", [k for k in band.kinds() if k[0] in (10, 12)], [])
check("kapalıyken ağa çıkılmaz", calls, [])
check("şehirsiz açılamaz", post("/api/weather", {"enabled": True}), (400, {"error": "Önce şehir seç."}))
check("kapalıyken şimdi güncelle", post("/api/weather/refresh", {})[0], 409)
check("şehir arama ucu", (post("/api/weather/search", {"city": "Ankara"})[1]["results"][0]["lat"], post("/api/weather/search", {"city": ""})[0]), (39.92, 400))
weather.http_json = offline
check("arama: internet yok", post("/api/weather/search", {"city": "Ankara"}), (502, {"error": "Şehir aranamadı: internet yok."}))
weather.http_json = fake_http
server.hub.update(link="away")
check("bağlı değilken şimdi güncelle", post("/api/weather/refresh", {}), (409, {"error": AWAY}))
calls.clear()
code, res = post("/api/weather", {"enabled": True, "city": "Ankara", "lat": 39.91987, "lon": 32.85427, "every": 30})
settle()
snap = state_now()["weather"]
check("ayar saat yokken kaydedilir", (code, server.config["weather"]), (200, {"enabled": True, "city": "Ankara", "lat": 39.92, "lon": 32.85, "every": 30}))
check("açınca indirilir, saat gelince yazılmak üzere bekler", (len(calls), snap["pending"], snap["last_ok"], snap["temp"], snap["text"], snap["error"]),
      (1, True, None, 12, "hafif yağmur", None))
no_multi = Command(type=10, subtype=5, status=1)
band = run(IDLE + [no_multi] + IDLE)
check("tek konumlu saat: sor, tanıt, üç veri", [k for k in band.kinds() if k[0] == 10], [(10, 5), (10, 7), (10, 0), (10, 1), (10, 2)])
snap = state_now()["weather"]
check("yazılınca durum", (snap["pending"], snap["last_ok"] is not None and abs(snap["last_ok"] - time.time()) < 5), (False, True))
check("yazılan anlık veri", [c.weather.current.temperature.value for c in band.sent if not isinstance(c, tuple) and (c.type, c.subtype) == (10, 0)], [12])
band = run(IDLE + [no_multi] + IDLE)
check("süresi dolmadan yeniden bağlanınca yazılmaz", ([k for k in band.kinds() if k[0] == 10], len(calls)), ([(10, 5)], 1))
band = run(IDLE + [no_multi, ask] + IDLE)
check("saat isteyince (10/3) eldeki veri gider", [k for k in band.kinds() if k[0] == 10], [(10, 5), (10, 7), (10, 0), (10, 1), (10, 2)])
# konum listesi tutan saat: eksik konum eklenir, liste yazılır, veriler gider, yabancı konum silinir
have = Command(type=10, subtype=5, status=0)
x = have.weather.locations.location.add()
x.code, x.name = "accu:1", "Eski"
server.wx["pending"] = True
band = run(IDLE + [have] + IDLE + [have] + IDLE)
ten = [c for c in band.sent if not isinstance(c, tuple) and c.type == 10]
check("çok konumlu saat sırası", [c.subtype for c in ten][:8], [5, 8, 7, 6, 0, 1, 2, 5])
check("yabancı konum silinir, bizimki listeye yazılır", ([(l.code, l.name) for l in ten[1].weather.locations.location],
                                                         [l.name for l in ten[3].weather.locations.location]), ([("accu:1", "Eski")], ["Ankara"]))
# süre dolunca kendiliğinden yenilenir
server.wx.update(last_ok=int(time.time()) - 31 * 60, tried=0.0)
server.wx["data"]["fetched"] -= 31 * 60
before = len(calls)
band = run(IDLE + [no_multi, lambda b: settle()] + IDLE)
check("aralık dolunca yeniden indirilip yazılır", (len(calls) - before, [k for k in band.kinds() if k[0] == 10][-3:]), (1, [(10, 0), (10, 1), (10, 2)]))
check("etkinlik onayı hiç gitmez", (8, 5) in band.kinds(), False)
server.hub.update(link="connected")
weather.http_json = offline
check("şimdi güncelle: internet yok", post("/api/weather/refresh", {}), (502, {"error": "Hava durumu alınamadı: internet yok."}))
check("hata state'te", state_now()["weather"]["error"], "Hava durumu alınamadı: internet yok.")
weather.http_json = fake_http
check("şimdi güncelle", (post("/api/weather/refresh", {}), state_now()["weather"]["error"], server.hub.jobs.qsize()), ((200, {"ok": True}), None, 1))
band = run(IDLE + [no_multi] + IDLE)
check("şimdi güncelle saate yazar", [k for k in band.kinds() if k[0] == 10][-3:], [(10, 0), (10, 1), (10, 2)])
server.wx["pending"], server.WEATHER_WAIT = True, -1   # saat 10/5'e yanıt vermezse tek konumlu yol
band = run(IDLE * 2)
check("konum sorusu yanıtsız kalırsa yine yazılır", [k for k in band.kinds() if k[0] == 10], [(10, 5), (10, 7), (10, 0), (10, 1), (10, 2)])
server.WEATHER_WAIT = 10
check("kapat", (post("/api/weather", {"enabled": False})[0], server.config["weather"]["enabled"], server.config["weather"]["city"]), (200, False, "Ankara"))
before = len(calls)
server.wx.update(last_ok=0, tried=0.0)
band = run(IDLE + [ask] + IDLE)
check("kapatınca saate de ağa da gidilmez", ([k for k in band.kinds() if k[0] == 10], len(calls) - before), ([], 0))
check("geçersiz aralık", post("/api/weather", {"every": 5})[0], 400)

# ---------- takvim ----------
NOW = int(time.time())
evs = [{"id": i, "title": f"e{i}", "note": "", "location": "", "start": NOW + 600 + i, "end": NOW + 4200 + i, "all_day": False} for i in range(60)]
evs += [{"id": 100, "title": "geçmiş", "note": "", "location": "", "start": NOW - 7200, "end": NOW - 3600, "all_day": False},
        {"id": 101, "title": "süren", "note": "", "location": "", "start": NOW - 600, "end": NOW + 600, "all_day": False},
        {"id": 102, "title": "uzak", "note": "", "location": "", "start": NOW + 8 * 86400, "end": NOW + 8 * 86400 + 60, "all_day": False}]
due = bandmsg.calendar_window(evs, NOW)
check("takvim penceresi", (len(due), due[0]["title"], any(e["title"] in ("geçmiş", "uzak") for e in due)), (50, "süren", False))
cs = wire(bandmsg.calendar_sync([{"id": 1, "title": "Diş hekimi", "note": "Röntgeni götür", "location": "Kızılay", "start": NOW + 60,
                                  "end": NOW + 3660, "all_day": False}]))
ev = cs.calendar.calendarSync.event[0]
check("12/1 alanları", ((cs.type, cs.subtype), ev.title, ev.description, ev.location, ev.start, ev.end, ev.allDay, ev.notifyMinutesBefore,
                        cs.calendar.calendarSync.HasField("disabled")),
      ((12, 1), "Diş hekimi", "Röntgeni götür", "Kızılay", NOW + 60, NOW + 3660, False, 0, False))
check("boş liste de kodlanır", wire(bandmsg.calendar_sync([])).calendar.HasField("calendarSync"), True)

tomorrow = date.today() + timedelta(days=1)
server.hub.update(link="away")
check("başlıksız etkinlik", post("/api/calendar", {"title": " ", "date": tomorrow.isoformat(), "time": "10:00", "duration": 60}), (400, {"error": "Başlık yaz."}))
check("bitiş başlangıçtan önce", post("/api/calendar", {"title": "x", "date": tomorrow.isoformat(), "time": "10:00", "end_time": "09:00"})[0], 400)
check("bitmiş etkinlik", post("/api/calendar", {"title": "x", "date": "2020-01-01", "time": "10:00", "duration": 60}), (400, {"error": "Bitmiş bir etkinlik eklenemez."}))
check("tarihsiz etkinlik", post("/api/calendar", {"title": "x"}), (400, {"error": "Tarih ve saati seç."}))
check("olmayan etkinlik", post("/api/calendar", {"id": 999, "delete": True})[0], 409)
code, res = post("/api/calendar", {"title": "Diş hekimi" + "x" * 80, "location": "Kızılay", "note": "Röntgeni götür", "date": tomorrow.isoformat(),
                                   "time": "10:00", "duration": 45})
E1 = res.get("id")
snap = state_now()["calendar"]
start1 = int(datetime.combine(tomorrow, datetime.strptime("10:00", "%H:%M").time()).timestamp())
check("saat yokken eklenir ve sıraya girer", (code, res["queued"], snap["pending"], snap["synced"], len(snap["events"]), snap["on_band"]),
      (200, True, True, None, 1, 1))
check("kayıt", {k: snap["events"][0][k] for k in ("title", "location", "note", "start", "end", "all_day")},
      {"title": ("Diş hekimi" + "x" * 80)[:bandmsg.CALENDAR_TITLE], "location": "Kızılay", "note": "Röntgeni götür", "start": start1,
       "end": start1 + 45 * 60, "all_day": False})
far = date.today() + timedelta(days=20)
code, res = post("/api/calendar", {"title": "Tatil", "all_day": True, "date": far.isoformat(), "end_date": (far + timedelta(days=2)).isoformat()})
E2 = res["id"]
tatil = next(e for e in state_now()["calendar"]["events"] if e["id"] == E2)
check("tüm gün: yerel gece yarıları", (tatil["all_day"], tatil["start"], tatil["end"]),
      (True, int(datetime.combine(far, datetime.min.time()).timestamp()), int(datetime.combine(far + timedelta(days=3), datetime.min.time()).timestamp())))
band = run(IDLE)
twelve = [c for c in band.sent if not isinstance(c, tuple) and c.type == 12]
check("bağlanınca bekleyen takvim yazılır", ([(c.type, c.subtype) for c in twelve], [e.title[:10] for e in twelve[0].calendar.calendarSync.event],
                                            twelve[0].calendar.calendarSync.event[0].start), ([(12, 1)], ["Diş hekimi"], start1))
snap = state_now()["calendar"]
check("yazılınca durum", (snap["pending"], snap["synced"] is not None), (False, True))
band = run(IDLE)
check("değişmediyse yeniden yazılmaz", [k for k in band.kinds() if k[0] == 12], [])
server.hub.update(link="connected")
code, res = post("/api/calendar", {"id": E1, "title": "Diş hekimi", "date": tomorrow.isoformat(), "time": "11:00", "end_time": "12:30"})
check("bağlıyken düzenleme", (code, res["queued"], res["id"]), (200, False, E1))
band = run(IDLE)
ev = [c for c in band.sent if not isinstance(c, tuple) and c.type == 12][0].calendar.calendarSync.event
check("düzenleme saate gider", (len(ev), ev[0].title, ev[0].start, ev[0].end - ev[0].start), (1, "Diş hekimi", start1 + 3600, 5400))
check("pencere dışındaki silinince saate yazılmaz", (post("/api/calendar", {"id": E2, "delete": True})[0],
                                                    [k for k in run(IDLE).kinds() if k[0] == 12]), (200, []))
server.hub.update(link="connected")
post("/api/calendar", {"id": E1, "delete": True})
band = run(IDLE)
last = [c for c in band.sent if not isinstance(c, tuple) and c.type == 12]
check("son etkinlik silinince boş liste yazılır", (len(last), len(last[0].calendar.calendarSync.event), state_now()["calendar"]["events"]), (1, 0, []))
old = server.store.calendar_save({"id": None, "title": "eski", "start": NOW - 7200, "end": NOW - 3600})
band = run(IDLE)
check("bitmiş etkinlik ayıklanır, saate yazılmaz", (server.store.calendar_list(), [k for k in band.kinds() if k[0] == 12]), ([], []))
check("takvimde etkinlik onayı gitmez", (8, 5) in band.kinds(), False)
# ---------- telefon bildirimleri ve müzik (Gadgetbridge XiaomiNotificationService / XiaomiMusicService) ----------
def drain():
    """Sıradaki işleri sahte saate yazdırır; gönderilen komutları döndürür."""
    b = FakeBand([])
    while not server.hub.jobs.empty():
        server.hub.jobs.get_nowait()(b)
    return b.sent


drain()
check("yetenekler bilgisayarda kapalı", server.hub.snapshot()["caps"], {"gps": False, "notifications": False, "music": False})
N1 = {"key": "0|com.whatsapp|7|null|10123", "package": "com.whatsapp", "app": "WhatsApp", "title": "Ayşe",
      "body": "Akşam   geliyor\nmusun?", "when": 1795000000}
m = wire(bandmsg.notification(dict(N1, id=42))).notification.notification2.notification3
check("7/0 alanları", (m.package, m.appName, m.title, m.unknown4, m.body, m.id, m.key, m.isCall, m.openOnPhone, len(m.timestamp), m.timestamp[8]),
      ("com.whatsapp", "WhatsApp", "Ayşe", "", "Akşam geliyor musun?", 42, N1["key"], False, False, 15, "T"))
long = wire(bandmsg.notification(dict(N1, id=1, title="b" * 200, body="g" * 2000))).notification.notification2.notification3
check("7/0 başlık ve gövde kısaltılır", (len(long.title), len(long.body), long.body[-1]), (bandmsg.NOTIFY_TITLE, bandmsg.NOTIFY_BODY, "…"))
empty = wire(bandmsg.notification({"id": 3, "package": "p", "app": None, "title": None, "body": None})).notification.notification2.notification3
check("7/0 boş başlık ve gövde yine yazılır", (empty.HasField("title"), empty.HasField("body"), empty.appName), (True, True, "UNKNOWN"))
server.hub.update(link="away")
check("saat bağlı değilken bildirim bekletilmez", (server.phone_notify(N1), drain(), server.phone["pending"]), ("offline", [], {}))
server.hub.update(link="connected")
check("ilk bildirim hemen gider", server.phone_notify(N1), "sent")
check("aynı içerik yinelenince atılır", server.phone_notify(dict(N1)), "duplicate")
check("aynı uygulamadan hemen ardından gelen bekler", server.phone_notify(dict(N1, body="ikinci")), "queued")
N2 = dict(N1, key="0|com.whatsapp|8|null|10123", title="Ali", body="üçüncü")
check("bekleyenin yerine sonuncusu geçer", (server.phone_notify(N2), len(server.phone["pending"])), ("queued", 1))
check("başka uygulama beklemez", server.phone_notify({"key": "k-mail", "package": "com.mail", "app": "Posta", "title": "t", "body": "b"}), "sent")
server.phone_flush()
out = drain()
check("süre dolmadan bekleyen gitmez", [(c.type, c.subtype, c.notification.notification2.notification3.package) for c in out],
      [(7, 0, "com.whatsapp"), (7, 0, "com.mail")])
server.phone["last_at"]["com.whatsapp"] -= server.PHONE_RATE + 0.1
server.phone_flush()
out = drain()
got = out[0].notification.notification2.notification3
check("süre dolunca yalnızca sonuncusu gider", (len(out), got.title, got.body, got.id, server.phone["pending"]),
      (1, "Ali", "üçüncü", server.phone_id(N2["key"]), {}))
last = server.hub.snapshot()["phone_last"]
check("son iletilen: yalnızca uygulama ve zaman", (sorted(last), last["app"]), (["app", "at"], "WhatsApp"))
check("bellekte metin tutulmaz", any("üçüncü" in json.dumps(v) or "Ali" in json.dumps(v) for v in server.phone["sent"].values()), False)
check("telefonda kapanan saatten kaldırılır", server.phone_notify_remove(N2["key"]), True)
d = drain()[0]
i = d.notification.notificationDismiss.notificationId[0]
check("7/1 alanları", (d.type, d.subtype, i.id, i.package, i.key), (7, 1, server.phone_id(N2["key"]), "com.whatsapp", N2["key"]))
check("bilinmeyen bildirim için komut gitmez", (server.phone_notify_remove("yok"), drain()), (False, []))
heard = []
for ev in ("notification_dismiss", "media", "call"):
    server.phone_on(ev, lambda data, ev=ev: heard.append((ev, data)))
c = Command(type=7, subtype=1)
c.notification.notificationDismiss.notificationId.add().id = server.phone_id(N1["key"])
c.notification.notificationDismiss.notificationId.add().id = 999
server.phone_on_command(wire(c))
check("saatte kapatılan telefona bildirilir", heard, [("notification_dismiss", {"key": N1["key"], "package": "com.whatsapp"})])
check("sonra telefondaki kapanış yeniden komut üretmez", (server.phone_notify_remove(N1["key"]), drain()), (False, []))
stale = dict(N1, key="bayat", package="com.bayat", body="bayat")
server.phone_notify(stale)
real_mono = time.monotonic
time.monotonic = lambda: real_mono() + server.PHONE_STALE + 1
check("sırada bayatlayan bildirim gönderilmez", drain(), [])
time.monotonic = real_mono
server.phone_call("Ayşe Yılmaz", None)
call = drain()[0].notification.notification2.notification3
check("gelen arama", (call.isCall, call.package, call.appName, call.title, call.body, call.id), (True, "phone", "phone", "Ayşe Yılmaz", "?", 0))
server.phone_call_end()
end = drain()[0]
check("arama bitti", (end.subtype, end.notification.notificationDismiss.notificationId[0].id, end.notification.notificationDismiss.notificationId[0].package), (1, 0, "phone"))
heard.clear()
server.phone_on_command(Command(type=7, subtype=2))
check("saatten arama reddi", heard, [("call", {"action": "reject"})])
SONG = {"state": "playing", "track": "Parça", "artist": "Sanatçı", "position": 10, "duration": 200}
check("müzik: ilk durum gider", server.music_state(SONG, 40), True)
mi = drain()[0]
check("18/1 alanları", (mi.type, mi.subtype, mi.music.musicInfo.state, mi.music.musicInfo.volume, mi.music.musicInfo.track,
                        mi.music.musicInfo.artist, mi.music.musicInfo.position, mi.music.musicInfo.duration), (18, 1, 1, 40, "Parça", "Sanatçı", 10, 200))
check("müzik: değişmeyince gitmez", (server.music_state(dict(SONG)), server.music_state(dict(SONG, position=11)), drain()), (False, False, []))
check("müzik: sarma, duraklatma, ses", [server.music_state(dict(SONG, position=90)), server.music_state(dict(SONG, position=90, state="paused")),
                                         server.music_state(dict(SONG, position=90, state="paused"), 55)], [True, True, True])
check("müzik: çalan yok", (server.music_state(None), [c.music.musicInfo.state for c in drain()]), (True, [1, 2, 2, 0]))
heard.clear()
server.phone_on_command(Command(type=18, subtype=0))
check("18/0: saat isteyince durum yeniden gider", [(c.type, c.subtype, c.music.musicInfo.state) for c in drain()], [(18, 1, 0)])
for k, v in ((0, 0), (1, 0), (3, 0), (4, 0), (5, 80), (5, 20), (9, 0)):
    c = Command(type=18, subtype=2)
    c.music.mediaKey.key, c.music.mediaKey.volume = k, v
    server.phone_on_command(wire(c))
check("18/2 düğmeleri", [(d["key"], d.get("direction")) for _, d in heard],
      [("play", None), ("pause", None), ("previous", None), ("next", None), ("volume", "up"), ("volume", "down")])
server.phone["callbacks"].clear()
server.phone.update(music=None, music_sent=None)
band = run(IDLE)
check("telefon kaynağı yokken oturum 7 ya da 18 göndermez", [k for k in band.kinds() if k[0] in (7, 18)], [])
# ---------- dil: Türkçe / İngilizce (i18n.py) ----------
import ast  # noqa: E402

import foods  # noqa: E402
import i18n  # noqa: E402
import insights  # noqa: E402

LANG_ERR = 'Dil "tr" ya da "en" olmalı.'
server.hub.update(link="away")
check("dil seçilmemiş: state.lang null, servis Türkçe", (state_now()["lang"], server.hub.snapshot()["lang"], "lang" in server.config, i18n.lang()),
      (None, None, False, "tr"))
check("tr: hata iletisi", post("/api/weight", {"kg": 5}), (400, {"error": "Kilo 30–250 kg arasında olmalı."}))
check("tr: sayı biçimi", (i18n.num(1.5), i18n.num(12345, ",")), ("1,5", "12.345"))
for bad in ({}, {"lang": "de"}, {"lang": None}, {"lang": "EN"}, {"lang": ["en"]}):
    check(f"/api/lang geçersiz {json.dumps(bad)}", post("/api/lang", bad), (400, {"error": LANG_ERR}))
check("geçersiz istek dili değiştirmez", ("lang" in server.config, state_now()["lang"], i18n.lang()), (False, None, "tr"))
server.hub.update(host_hint=i18n.t(server.HOST_HINT), workout=dict(server.workout, sport="Yürüyüş"))
server.workout["sport"] = "Yürüyüş"
check("/api/lang en", post("/api/lang", {"lang": "en"}), (200, {"ok": True}))
check("dil kaydedildi", (json.loads((tmp / "config.json").read_text())["lang"], server.config["lang"], i18n.lang(), state_now()["lang"]),
      ("en", "en", "en", "en"))
snap = state_now()
check("en: state'te bekleyen metinler yeniden çevrilir", (snap["host_hint"], snap["workout"]["sport"], server.workout["sport"]),
      (i18n.EN[server.HOST_HINT], "Walk", "Walk"))
server.hub.update(host_hint=None, workout=dict(server.workout, sport=None))
server.workout["sport"] = None
check("en: hata iletisi", post("/api/weight", {"kg": 5}), (400, {"error": "Weight must be between 30 and 250 kg."}))
check("en: saat bağlı değilken", post("/api/find", {}), (409, {"error": "The band isn't connected. Move closer and try again."}))
check("en: dil hatası", post("/api/lang", {"lang": "xx"}), (400, {"error": 'Language must be "tr" or "en".'}))
check("en: spor adı", (bandmsg.workout_event(wire(status(0, sport=1)))["sport"], bandmsg.SPORTS[1]), ("Outdoor run", "Açık hava koşusu"))
check("en: hava metni ve hata", (weather.condition(61), weather.condition(1234), server.weather_fetch() if not server.config["weather"]["enabled"] else "Weather is turned off."),
      ("light rain", None, "Weather is turned off."))
check("en: sayı biçimi", (i18n.num(1.5), i18n.num(12345, ","), insights._dur(135), insights._dur(40)), ("1.5", "12,345", "2 h 15 min", "40 min"))
server.store.db.execute("DELETE FROM meals")
server.store.db.commit()
rep = insights.day_report(server.store, server.config, date.today())
check("en: günlük not", "Log a meal to see your calorie balance and eating pattern." in rep["notes"], True)
check("en: hazır öğünler", [p[0] for p in rep["presets"]], ["Breakfast", "Lunch", "Dinner", "Snack", "Turkish coffee", "Fruit"])
check("en: yiyecek iki dilde aranır", ([f["name"] for f in foods.search("tea")][:2], foods.search("çay")[0]["name"], foods.find("Çay")["portion"],
                                       foods.find("tea")["kcal"], foods.search("lentil")[0]["name"]),
      (["Tea", "Tea (with sugar)"], "Tea", "1 tea glass", 2, "Lentil soup"))
check("en: yiyecek eklenir", post("/api/meal", {"food": "Tea", "qty": 1.5}), (200, {"ok": True}))
check("en: porsiyonda ondalık nokta", tuple(server.store.db.execute("SELECT name, portion FROM meals ORDER BY id DESC LIMIT 1").fetchone()),
      ("Tea", "1.5 × 1 tea glass"))
notes = insights.day_report(server.store, server.config, date.today())["notes"]
check("en: kalori dengesi cümlesi", [bool(re.fullmatch(r"You're in a [\d,]+ kcal (deficit|surplus) so far: [\d,]+ eaten, [\d,]+ burned\.", n))
                                     for n in notes if n.startswith("You're in a")], [True])
check("en: listede olmayan yiyecek", post("/api/meal", {"food": "zzz"}), (400, {"error": "That food isn't in the list. Enter it manually."}))
server.store.db.execute("DELETE FROM meals")
server.store.db.commit()
while not server.hub.jobs.empty():
    server.hub.jobs.get_nowait()
at = datetime.now()
server.config["meal_reminder"] = {"enabled": True, "times": [at.strftime("%H:%M"), (at + timedelta(minutes=1)).strftime("%H:%M")]}
server.meal_fired.clear()
band = run(IDLE)
server.config["meal_reminder"] = {"enabled": False, "times": []}
check("en: saate giden öğün hatırlatması", [c for c in band.sent if isinstance(c, tuple) and c[1][0] == "Meal time"][:1],
      [("notify", ("Meal time", "Don't forget to log what you ate."))])
lo, hi = server.coach_state()["lo"], server.coach_state()["hi"]
server.coach.update(on=True, since=time.time(), out_since=time.time() - 60, last_alert=0.0, app=False)
server.coach_check(lo - 20)
server.coach.update(on=False, out_since=None)
check("en: saate giden bölge uyarısı", [c for c in drain() if isinstance(c, tuple)],
      [("notify", (f"Below Zone {server.coach['zone']}", f"Heart rate {lo - 20}. Speed up, target {lo}-{hi}."))])
server.hub.update(coach=server.coach_state(), link="away")
with urllib.request.urlopen(base + "/api/export?what=days") as r:
    check("en: dışa aktarım", (r.headers["Content-Disposition"], r.read().decode().splitlines()[0].split(",")[0].lstrip("﻿"),
                               r.read()), ('attachment; filename="band-days.csv"', "day", b""))

# koddaki her i18n.t("…") / i18n.mark("…") anahtarının ve çevrilen tabloların İngilizcesi olmalı
used = {}
for f in sorted(tmp.glob("*.py")):
    for n in ast.walk(ast.parse(f.read_text())):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("t", "mark")
                and getattr(n.func.value, "id", None) == "i18n" and n.args and isinstance(n.args[0], ast.Constant)
                and not f.name.startswith("test_")):
            used[n.args[0].value] = f.name
tables = list(weather.WMO_TEXT.values()) + list(bandmsg.SPORTS.values()) + [p[0] for p in insights.DEFAULT_PRESETS]
check("kodda yeterince anahtar bulundu", len(used) > 150, True)
check("her i18n anahtarının İngilizcesi var", sorted(k for k in list(used) + tables if k not in i18n.EN), [])
check("sözlükte kullanılmayan çeviri yok", sorted(k for k in i18n.EN if k not in used and k not in tables), [])
check("alan adları iki dilde aynı", [k for k, v in i18n.EN.items() if sorted(re.findall(r"{(\w+)}", k)) != sorted(re.findall(r"{(\w+)}", v))], [])
check("her yiyeceğin İngilizce adı var", ([r[0] for r in foods.FOODS if r[0] not in foods.EN], len(foods.EN) == len(foods.FOODS)), ([], True))
check("çalışırken eksik anahtar görülmedi", sorted(i18n.missing), [])
check("bilinmeyen anahtar Türkçe döner ve not edilir", (i18n.t("Böyle bir metin yok {n}", n=3), "Böyle bir metin yok {n}" in i18n.missing),
      ("Böyle bir metin yok 3", True))
i18n.missing.clear()

check("/api/lang tr", post("/api/lang", {"lang": "tr"}), (200, {"ok": True}))
check("tr'ye dönüş", (server.config["lang"], state_now()["lang"], i18n.lang(), post("/api/weight", {"kg": 5})[1]["error"],
                      foods.search("tea")[0]["name"], weather.condition(61), i18n.switch("Walk")),
      ("tr", "tr", "tr", "Kilo 30–250 kg arasında olmalı.", "Çay", "hafif yağmur", "Yürüyüş"))
check("gerçek ayar dosyasına dokunulmadı", "weather" in json.loads((tmp / "config.json").read_text()), True)
httpd.shutdown()

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{ran} denetim geçti")
