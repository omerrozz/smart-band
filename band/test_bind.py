"""bind.py sınamaları: saate bağlanmaz. Saat rolünü buradaki sahte saat oynar.

Sahte saat kriptoyu bind.py'den bağımsız kurar (HKDF elle, HMAC döngüsüyle) ki iki taraf aynı hatayı
paylaşmasın. Uçtan uca sınama gerçek client.Band çerçevelemesinden geçer.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
import sys

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

import bind
from bind import pb_bytes, pb_int, pb_parse
from client import CH_PROTOBUF, OP_PLAIN, TYPE_ACK, TYPE_DATA, TYPE_SESSION, Band, crc16_arc, derive_session, frame

fails = count = 0


def check(name, got, want=True):
    global fails, count
    count += 1
    if got != want:
        fails += 1
        print(f"HATA  {name}: {got!r} != {want!r}")


def raises(name, fn, code=None, text=None):
    try:
        fn()
    except bind.BindError as e:
        check(name + " kod", e.code, code)
        if text:
            check(name + " metin", text in str(e))
        return e
    check(name + " hata bekleniyordu", False)


def hkdf_manual(secret, salt, info, n=64):
    prk = hmac.new(salt, secret, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < n:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:n]


class Watch:
    """Protobuf düzeyinde sahte saat: gelen komuta yanıt gövdesi döndürür."""

    MAC, MODEL = "AA:BB:CC:DD:EE:FF", "miwear.watch.test"

    def __init__(self, verify_mode=2, oob=3, code=b"", accept=True, bad_proof=False, bound=False, off_curve=False):
        self.verify_mode, self.oob, self.code, self.accept = verify_mode, oob, code, accept
        self.bad_proof, self.bound, self.off_curve = bad_proof, bound, off_curve
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.random = os.urandom(16)
        self.secret = self.block = b""
        self.seen, self.result, self.done, self.told = [], None, False, None

    def reply(self, body: bytes) -> bytes | None:
        cmd = pb_parse(body)
        cid, acc = cmd[2][0], pb_parse(cmd[3][0])
        self.seen.append((cid, acc))
        if 3 in acc:  # telefon hata bildirdi
            self.told = (cid, acc[3][0])
            return None
        if cid == 17:
            if self.bound:
                return bind.command(17, pb_int(3, 1))
            self.hash_user = pb_parse(acc[11][0])[2][0]
            info = (pb_int(1, self.verify_mode) + pb_bytes(2, "SN0001") + pb_bytes(3, self.MAC) + pb_bytes(4, self.MODEL)
                    + pb_int(5, self.oob) + pb_bytes(6, os.urandom(16)) + pb_bytes(7, "did0001"))
            return bind.command(17, pb_bytes(12, info))
        if cid == 18:
            v = pb_parse(acc[17][0])
            self.app_id, app_pub = v[1][0].decode(), v[2][0]
            peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b"\x04" + app_pub)
            self.secret = self.key.exchange(ec.ECDH(), peer)
            sign = hmac.new(self.secret, self.random + self.code, hashlib.sha256).digest()
            if self.bad_proof:
                sign = bytes(32)
            n = self.key.public_key().public_numbers()
            pub = n.x.to_bytes(32, "big") + n.y.to_bytes(32, "big")
            if self.off_curve:
                pub = bytes(63) + b"\x01"
            return bind.command(18, pb_bytes(18, pb_bytes(1, pub) + pb_bytes(2, self.random) + pb_bytes(3, sign)))
        if cid == 19:
            c = pb_parse(acc[19][0])
            app_random, app_sign = c[1][0], c[2][0]
            if hmac.new(self.secret, app_random + self.code, hashlib.sha256).digest() != app_sign:
                return bind.command(19, pb_int(3, 7))
            if self.accept is None:
                return bind.command(19, pb_int(3, 6))
            if not self.accept:
                return bind.command(19, pb_bytes(20, pb_int(1, 0)))
            self.block = hkdf_manual(self.secret, app_random + self.random, b"miwear-bind")
            return bind.command(19, pb_bytes(20, pb_int(1, 1)))
        if cid == 25:
            enc = pb_parse(acc[29][0])[1][0]
            plain = AESCCM(self.block[16:32], tag_length=4).decrypt(bytes(range(0x10, 0x1C)), enc, b"bind-data")
            self.result = pb_parse(plain)
            self.done = True
            return bind.command(25, pb_int(3, 0))
        return None


def drive(watch: Watch, binder: bind.Binder):
    """Çerçeveleme olmadan: durum makinesini sahte saatle sonuna dek yürütür."""
    out, confirms = binder.start(), 0
    while True:
        back = watch.reply(out)
        if back is None:
            return None, confirms
        step = binder.receive(back)
        if step[0] == "send":
            out, confirms = step[1], confirms + bool(step[2])
        elif step[0] in ("bound", "info"):
            return step[1], confirms


# ---------- 1. protobuf yardımcıları ----------
check("varint 300", bind._varint(300), b"\xac\x02")
m = pb_parse(pb_int(1, 1) + pb_bytes(2, b"ab") + bind.pb_float(5, 34.0))
check("parse int", m[1], [1])
check("parse bytes", m[2], [b"ab"])
check("parse float", struct.unpack("<f", m[5][0])[0], 34.0)
try:
    pb_parse(b"\x12\x05ab")
    check("kısa veri hata vermeli", False)
except ValueError:
    check("kısa veri", True)

# ---------- 2. başlangıç paketi ----------
b = bind.Binder("1234567890")
start = pb_parse(b.start())
check("başlangıç tür", start[1], [1])
check("başlangıç kimlik", start[2], [17])
s = pb_parse(pb_parse(start[3][0])[11][0])
check("check_dynamic_code yazılı ve false", s[1], [0])
check("hash_user_id md5", s[2], [hashlib.md5(b"1234567890").digest()])
check("device_name yok", 3 in s, False)

# ---------- 3. tam eşleştirme, tuşla onay ----------
w = Watch(oob=3)
b = bind.Binder("1234567890", device_name="Linux PC")
res, confirms = drive(w, b)
check("bağlandı", isinstance(res, bind.Bound))
check("saat tamamladı", w.done)
check("onay bir kez istendi", confirms, 1)
check("auth key = blok 40..56", res.auth_key, w.block[40:56])
check("auth key 16 bayt", len(res.auth_key), 16)
check("blok saatinkiyle aynı", res.block, w.block)
check("app_device_id", res.app_device_id, hashlib.md5((Watch.MAC + "1234567890").encode()).hexdigest())
check("saate giden app_device_id", w.app_id, res.app_device_id)
check("sonuç user_id", w.result[1], [b"1234567890"])
comp = pb_parse(w.result[2][0])
check("companion tür 0 açıkça yazılı", comp[1], [0])
check("companion sürüm", struct.unpack("<f", comp[2][0])[0], 34.0)
check("companion ad", comp[3], [b"Linux PC"])
check("açık anahtar 64 bayt", len(pb_parse(w.seen[1][1][17][0])[2][0]), 64)
check("repr anahtarı göstermez", res.auth_key.hex() in repr(res), False)
check("repr seri/did göstermez", "SN0001" in repr(res.info) or "did0001" in repr(res.info), False)

# aynı anahtarla sonraki giriş: iki taraf aynı oturum anahtarlarını türetir
pn, wn = os.urandom(16), os.urandom(16)
check("giriş türetimi", derive_session(res.auth_key, pn, wn), derive_session(w.block[40:56], pn, wn))

# ---------- 4. onaysız ve kodlu kipler ----------
res, confirms = drive(Watch(oob=0), bind.Binder("u1"))
check("oob 0 bağlanır", isinstance(res, bind.Bound))
check("oob 0 onay istemez", confirms, 0)
w = Watch(oob=2, code=b"4821")
res, _ = drive(w, bind.Binder("u1", code="4821"))
check("kodlu bağlanır", isinstance(res, bind.Bound) and res.auth_key == w.block[40:56])
raises("yanlış kod", lambda: drive(Watch(oob=2, code=b"4821"), bind.Binder("u1", code="0000")), text="kanıtı")
w = Watch(oob=2)
res, _ = drive(w, bind.Binder("u1"))
check("boş kod", isinstance(res, bind.Bound))

# ---------- 5. yalnızca sorma ----------
w = Watch(verify_mode=1, oob=3)
info, _ = drive(w, bind.Binder("u1", probe_only=True))
check("sor: Info döner", isinstance(info, bind.Info))
check("sor: kip", (info.verify_mode, info.oob_mode, info.model), (1, 3, Watch.MODEL))
check("sor: tek paket gitti", [c for c, _ in w.seen], [17])
info, _ = drive(Watch(verify_mode=2, oob=0), bind.Binder("u1", probe_only=True))
check("sor: yerel", info.verify_mode, 2)

# ---------- 6. retler ----------
raises("sunucu kipi", lambda: drive(Watch(verify_mode=1), bind.Binder("u1")), text="sunucu")
raises("bilinmeyen kip", lambda: drive(Watch(verify_mode=9), bind.Binder("u1")), text="bilinmeyen")
raises("sayı karşılaştırma", lambda: drive(Watch(oob=1), bind.Binder("u1")), text="onay türü")
raises("zaten bağlı", lambda: drive(Watch(bound=True), bind.Binder("u1")), code=1)
raises("sor: zaten bağlı", lambda: drive(Watch(bound=True), bind.Binder("u1", probe_only=True)), code=1)
raises("saatte ret (false)", lambda: drive(Watch(accept=False), bind.Binder("u1")), code=6)
raises("saatte iptal (kod 6)", lambda: drive(Watch(accept=None), bind.Binder("u1")), code=6)
w = Watch(bad_proof=True)
e = raises("yanlış kanıt", lambda: drive(w, bind.Binder("u1")), text="kanıtı")
t = pb_parse(e.tell_band)
check("saate bildirim: 19 / kod 7", (t[2], pb_parse(t[3][0])[3]), ([19], [7]))
check("yanlış kanıtta AppConfirm gitmez", [c for c, _ in w.seen], [17, 18])
e = raises("eğri dışı anahtar", lambda: drive(Watch(off_curve=True), bind.Binder("u1")), text="kanıtı")
check("eğri dışı: bildirim var", e.tell_band is not None)
for code in (2, 3, 5, 8, 9, 10, 100, 77):
    b = bind.Binder("u1")
    b.start()
    raises(f"hata kodu {code}", lambda: b.receive(bind.command(17, pb_int(3, code))), code=code)
try:
    bind.Binder("")
    check("boş kullanıcı hata vermeli", False)
except ValueError:
    check("boş kullanıcı", True)

# ---------- 7. ilgisiz ve sırasız paketler ----------
b = bind.Binder("u1")
b.start()
check("başka tür yok sayılır", b.receive(pb_int(1, 2) + pb_int(2, 78)), ("ignored",))
check("bozuk veri yok sayılır", b.receive(b"\xff\xff\xff"), ("ignored",))
check("sırasız yanıt yok sayılır", b.receive(bind.command(19, pb_bytes(20, pb_int(1, 1)))), ("ignored",))
check("aşama değişmedi", b.stage, "info")

# ---------- 8. sabit girdilerle belirlenimlilik ----------
k = ec.derive_private_key(0x1234567890ABCDEF, ec.SECP256R1())
peer = ec.derive_private_key(0xFEDCBA0987654321, ec.SECP256R1())
sec = bind.shared_secret(k, bind.public_bytes(peer))
check("ECDH iki yönden aynı", sec, bind.shared_secret(peer, bind.public_bytes(k)))
ar, dr = bytes(range(16)), bytes(range(16, 32))
check("HKDF elle hesapla aynı", bind.key_block(sec, ar, dr), hkdf_manual(sec, ar + dr, b"miwear-bind"))
check("kanıt = HMAC(S, rastgele ‖ kod)", bind.proof(sec, ar, b"12"), hmac.new(sec, ar + b"12", hashlib.sha256).digest())
check("sonuç nonce", bind.RESULT_NONCE.hex(), "101112131415161718191a1b")


# ---------- 9. uçtan uca: gerçek Band çerçevelemesi üzerinden ----------
class Sock:
    """client.Band için sahte soket; karşı uçta Watch durur."""

    def __init__(self, watch):
        self.watch, self.out, self.inbuf, self.seq = watch, b"", b"", 0

    def settimeout(self, t):
        pass

    def close(self):
        pass

    def send(self, data):
        self.sendall(data)
        return len(data)

    def sendall(self, data):
        if data.startswith(b"\xba\xdc\xfe"):  # V1 sürüm sorgusu -> sürüm 2
            body = bytes(3) + b"\x02"
            self.out += b"\xba\xdc\xfe\x00\xc0" + struct.pack("<H", len(body)) + body + b"\x00"
            return
        self.inbuf += data
        while len(self.inbuf) >= 8:
            flags, seq, length, crc = struct.unpack_from("<BBHH", self.inbuf, 2)
            payload, self.inbuf = self.inbuf[8 : 8 + length], self.inbuf[8 + length :]
            assert crc16_arc(payload) == crc
            if flags & 0xF == TYPE_SESSION:
                self.out += frame(TYPE_SESSION, 0, b"\x02")
            elif flags & 0xF == TYPE_DATA:
                self.out += frame(TYPE_ACK, seq, b"")
                assert payload[:2] == bytes([CH_PROTOBUF, OP_PLAIN]), "eşleştirme paketleri şifresiz olmalı"
                back = self.watch.reply(payload[2:])
                if back is not None:
                    self.out += frame(TYPE_DATA, self.seq, bytes([CH_PROTOBUF, OP_PLAIN]) + back)
                    self.seq += 1

    def recv(self, n):
        if not self.out:
            raise TimeoutError
        data, self.out = self.out[:n], self.out[n:]
        return data


w = Watch(oob=3)
band = Band(Watch.MAC, "00" * 16, transport=Sock(w))
band.connect(auth=False)
check("girişsiz bağlantı", band.authenticated, False)
asked = []
res = bind.run(band, bind.Binder("1234567890"), on_confirm=lambda: asked.append(1), timeout=1.0)
check("uçtan uca bağlandı", isinstance(res, bind.Bound) and res.auth_key == w.block[40:56])
check("uçtan uca onay istendi", asked, [1])
check("uçtan uca sıra", [c for c, _ in w.seen], [17, 18, 19, 25])

w = Watch(verify_mode=1)
band = Band(Watch.MAC, "00" * 16, transport=Sock(w))
band.connect(auth=False)
info = bind.run(band, bind.Binder("u1", probe_only=True), timeout=1.0)
check("uçtan uca sor", (info.verify_mode, [c for c, _ in w.seen]), (1, [17]))

w = Watch(bad_proof=True)
band = Band(Watch.MAC, "00" * 16, transport=Sock(w))
band.connect(auth=False)
raises("uçtan uca yanlış kanıt", lambda: bind.run(band, bind.Binder("u1"), timeout=1.0), text="kanıtı")
check("uçtan uca saate bildirildi", w.told, (19, 7))


class Silent(Sock):
    def sendall(self, data):
        super().sendall(data)
        if TYPE_DATA == (data[2] & 0xF if data[:2] == b"\xa5\xa5" else -1):
            self.out = b""  # veri paketine hiç yanıt verme


band = Band(Watch.MAC, "00" * 16, transport=Silent(Watch()))
band.connect(auth=False)
raises("yanıtsız saat", lambda: bind.run(band, bind.Binder("u1"), timeout=0.3), text="yanıt vermedi")

print(f"{count - fails}/{count} sınama geçti")
sys.exit(1 if fails else 0)
