"""Android köprüsü: bilgisayardaki server.py'yi telefonda değiştirmeden çalıştırır.

Kotlin tarafı (BandService) start() çağırır. Saatle konuşma, client.py'nin beklediği soket
arayüzünü (send/sendall/recv/settimeout/close) Kotlin'deki BtBridge üzerinden sunan Transport ile yapılır.
"""
import json
import os
import socket
import sys
import threading

_started = False


class Transport:
    """Bluetooth RFCOMM bağlantısı; soket gibi davranır."""

    def __init__(self, address, channel, timeout):
        from java import jclass
        bridge = jclass("band.app.BtBridge")
        self._timeout = timeout
        try:
            self._link = bridge.open(address, int(channel), int((timeout or 20) * 1000))
        except Exception as exc:  # Java istisnası: iletiyi sade bir ConnectionError'a çevir
            raise ConnectionError(_message(exc)) from None

    def settimeout(self, timeout):
        self._timeout = timeout

    def recv(self, n):
        ms = -1 if self._timeout is None else max(1, int(self._timeout * 1000))
        try:
            data = self._link.read(int(n), ms)
        except Exception as exc:
            raise ConnectionError(_message(exc)) from None
        if data is None:  # süre doldu: bilgisayardaki soketle aynı istisna
            raise socket.timeout("timed out")
        return bytes(data)  # boş bayt dizisi = karşı taraf kapattı (client.py ConnectionError atar)

    def send(self, data):
        try:
            self._link.write(bytes(data))
        except Exception as exc:
            raise ConnectionError(_message(exc)) from None
        return len(data)

    sendall = send

    def close(self):
        try:
            self._link.close()
        except Exception:
            pass


def _message(exc):
    get = getattr(exc, "getMessage", None)
    return (get() if get else None) or str(exc) or type(exc).__name__


# --- servis köprüsü ---------------------------------------------------------
# server.py'ye dokunmadan, dışarıdan sarmalayarak şunlar Kotlin'e (BandService) bildirilir:
#   * bildirimde gösterilen durum (bağlantı, pil, son eşitleme, "başka uygulama tutuyor" uyarısı)
#   * saatle etkin veri alışverişi olup olmadığı (uyanık tutma kilidi yalnızca o sırada alınır)
_host = None
_server = None
_busy = {}
_busy_lock = threading.Lock()
_WATCH = {"link", "battery", "charging", "synced", "host_hint"}
_NOTIFY = _WATCH | {"lang"}  # "lang" değişince de bildirilir (yerel metinlerin dili)


def _set_busy(key, on):
    with _busy_lock:
        if bool(_busy.get(key)) == bool(on):
            return
        _busy[key] = bool(on)
        why = ",".join(sorted(k for k, v in _busy.items() if v))
    try:
        _host.onBusy(bool(why), why)
    except Exception as exc:
        print(f"köprü: meşguliyet bildirilemedi: {exc!r}", flush=True)


def _push_state():
    import json
    st = _server.hub.state
    try:
        data = {k: st.get(k) for k in _WATCH}
        # arayüzde seçilen dil ("tr"/"en"; yoksa None: sistemin dili): durumda ya da ayarda
        data["lang"] = st.get("lang") or (getattr(_server, "config", None) or {}).get("lang")
        _host.onState(json.dumps(data))
    except Exception as exc:
        print(f"köprü: durum bildirilemedi: {exc!r}", flush=True)


def _install(server):
    hub_update = server.hub.update

    def update(**changes):
        hub_update(**changes)
        if _NOTIFY & changes.keys():
            _push_state()
        if "app_workout" in changes or "app_workout_last" in changes:
            _set_busy("workout", server.appwk.active)  # uygulamadan başlatılan antrenman
            _set_location()

    server.hub.update = update

    sync_files = server.sync_files

    def sync(band):  # dosya eşitlemesi sürerken
        _set_busy("sync", True)
        try:
            return sync_files(band)
        finally:
            _set_busy("sync", False)

    server.sync_files = sync

    band_cls = server.Band
    start_live, stop_live = band_cls.realtime_start, band_cls.realtime_stop

    def realtime_start(self):  # canlı akış: anlık ölçüm, bölge antrenmanı, süren antrenman
        _set_busy("live", True)
        return start_live(self)

    def realtime_stop(self):
        try:
            return stop_live(self)
        finally:
            _set_busy("live", False)

    band_cls.realtime_start, band_cls.realtime_stop = realtime_start, realtime_stop

    band_close = band_cls.close

    def close(self):  # bağlantı bitince canlı akış da bitmiştir
        try:
            return band_close(self)
        finally:
            _set_busy("live", False)
            _set_busy("sync", False)

    band_cls.close = close
    _set_busy("workout", server.appwk.active)  # süreç ölümünden sonra geri yüklenen antrenman
    server.location_enable(True)  # bu kurulumda konum kaynağı var: arayüz açık hava antrenmanında harita gösterir
    server.phone_enable(notifications=True, music=True)  # telefon bildirimleri ve müzik kaynağı var
    for event in ("notification_dismiss", "media", "call"):  # saatten gelen istekler Kotlin'e gider
        server.phone_on(event, lambda data, event=event: _host.onBandEvent(event, json.dumps(data)))
    _set_location()
    _push_state()


# --- telefonun konumu -------------------------------------------------------
# Konum güncellemeleri yalnızca GPS'li (koşu, yürüyüş, bisiklet) bir uygulama antrenmanı sürerken açıktır:
# 0 = kapalı, 1 = antrenman duraklatıldı (servis konum türünde kalır, güncelleme alınmaz), 2 = açık.
_location_mode = None


def _location_wanted():
    s = _server.appwk.s
    if not s or not s.get("gps"):
        return 0
    return 1 if s["paused"] else 2


def _set_location(force=False):
    global _location_mode
    mode = _location_wanted()
    if mode == _location_mode and not force:
        return
    _location_mode = mode
    try:
        _host.setLocation(mode)
    except Exception as exc:
        print(f"köprü: konum kipi bildirilemedi: {exc!r}", flush=True)


def on_location(ts, lat, lon, accuracy, speed, alt):
    """Kotlin'den bir konum noktası. accuracy/speed < 0 ve alt NaN: bilinmiyor."""
    if _server is None:
        return False
    return bool(_server.location_fix(
        float(ts), float(lat), float(lon), None if accuracy < 0 else float(accuracy),
        None if speed < 0 else float(speed), None if alt != alt else float(alt)))


def location_problem(text):
    """Konum alınamıyor (boş metin: sorun kalktı)."""
    if _server is not None:
        _server.location_problem(text or None)


def location_denied(text=None):  # text: Kotlin'in seçili dilde verdiği metin
    location_problem(text or "konum izni yok")


def location_blocked(text=None):
    location_problem(text or "uygulama açılınca konum yeniden başlar")


# --- telefonun bildirimleri ve müziği (NotifyService çağırır; kodlama server.py / bandmsg.py'de) ---
def phone_notify(key, package, app, title, body, when_ms):
    """Seçili bir uygulamanın bildirimi. Metin günlüğe yazılmaz."""
    if _server is None:
        return ""
    result = _server.phone_notify({"key": str(key), "package": str(package), "app": str(app), "title": str(title),
                                   "body": str(body), "when": int(when_ms) / 1000})
    print(f"bildirim {package}: {result}", flush=True)
    return result


def phone_remove(key):
    if _server is not None:
        _server.phone_notify_remove(str(key))


def phone_call(name, number):
    if _server is not None:
        _server.phone_call(str(name) or None, str(number) or None)


def phone_call_end():
    if _server is not None:
        _server.phone_call_end()


# --- tartı (ScaleFeed çağırır; çözme server.py / scale.py'de). Ölçüm günlüğe yazılmaz. ---
def scale_address():
    """Dinlenecek tartının Bluetooth adresi; ayarlı değilse boş metin."""
    return _server.scale_address() if _server is not None else ""


def scale_frame(hex_data):
    r = _server.scale_frame(bytes.fromhex(str(hex_data))) if _server is not None else None
    if r:  # değerler yazılmaz, yalnızca paketin türü
        print("tartı paketi:", "kilo" if "weight" in r else "yalnızca empedans" if "impedance_high" in r else "boş", flush=True)


def music(state_json, volume):
    """Telefonda çalan (boş metin: çalan yok). Saate yalnızca değişince yazılır."""
    if _server is not None:
        _server.music_state(json.loads(state_json) if state_json else None, int(volume))


def _supervise(name, target):
    """İş parçacığı ölürse 5 sn sonra yeniden başlatır."""
    import time

    def run():
        while True:
            try:
                target()
                print(f"{name} beklenmedik biçimde bitti; yeniden başlatılıyor", flush=True)
            except BaseException as exc:
                print(f"{name} durdu: {exc!r}; yeniden başlatılıyor", flush=True)
            time.sleep(5)

    threading.Thread(target=run, name=name, daemon=True).start()


def _serve_http():
    server = _server
    httpd = server.ThreadingHTTPServer((server.BIND, server.PORT), server.Handler)
    httpd.daemon_threads = True
    print(f"Arayüz: http://{server.BIND}:{server.PORT}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def start(code_dir, data_dir, port=8377, host=None):
    """server.py'yi içe aktarır ve üç döngüsünü gözetim altında başlatır. Bir kez çalışır.

    server.main() ile aynı işi yapar (saat döngüsü, uygulama antrenmanı döngüsü, HTTP); fark, döngülerden
    biri ölürse yeniden başlatılmasıdır. host: durum bildirimlerini alan nesne (verilmezse BandService).
    """
    global _started, _host, _server
    if _started:
        return
    os.environ["BILEKLIK_DATA"] = data_dir
    os.environ["BILEKLIK_PORT"] = str(port)
    os.environ["BILEKLIK_BIND"] = "127.0.0.1"  # arayüz telefonun dışına açılmaz
    if code_dir not in sys.path:
        sys.path.insert(0, code_dir)
    if host is None:
        from java import jclass
        host = jclass("band.app.BandService")
    import server
    _host, _server = host, server
    server.transport_factory = Transport
    _install(server)
    _supervise("saat döngüsü", server.band_loop)
    _supervise("antrenman döngüsü", server.appwk_loop)
    _supervise("web sunucusu", _serve_http)
    _started = True


def kick():
    """Yeniden deneme beklemesini keser (Bluetooth açıldı, eşleşme tamamlandı)."""
    if _server is not None:
        _server.retry_now.set()


def resync():
    """Durumu ve meşguliyeti Kotlin'e yeniden bildirir (servis yeniden yaratıldığında, periyodik denetimde)."""
    if _server is None:
        return
    _set_busy("workout", _server.appwk.active)
    with _busy_lock:
        why = ",".join(sorted(k for k, v in _busy.items() if v))
    _host.onBusy(bool(why), why)
    _set_location(force=True)
    _push_state()


def viewers():
    """Açık canlı bağlantı (sayfa) sayısı; arka planda 0 olmalı."""
    return _server.hub.viewers if _server is not None else -1
