"""Saatin ilk eşleştirmesi ("bind"): Mi Fitness ve Xiaomi sunucusu olmadan, yerel kipte.

Fabrika ayarına dönmüş bir saat, kendisini kimin sahipleneceğini bu adımla öğrenir; 16 baytlık auth key
burada üretilir. Saat iki kipten birini seçer: SERVER_PSK (Xiaomi sunucusunun imzası gerekir, burada
yapılamaz) ya da APP_LOCAL (telefon ve saat anahtarı kendi aralarında üretir). Kipi saat belirler.

    1/17  BindStartV2            ->   <- BindInfoV2 (kip, onay türü) ya da hata kodu
    1/18  BindLocal.AppVerify    ->   <- BindLocal.DeviceVerify (saatin açık anahtarı, rastgelesi, kanıtı)
    1/19  BindLocal.AppConfirm   ->   <- BindLocal.DeviceConfirm (saatte onay tuşuna basılınca)
    1/25  BindResultV2           ->   <- hata kodu 0

Bütün paketler şifresiz protobuf komutlarıdır ve girişten (1/26, 1/27) önce gönderilir.

Kaynak: AstroBox-NG (AGPL-3.0) bind bileşeni ve wear_account.proto; ayrıntılar freeband projesinin
docs/BIND.md belgesiyle karşılaştırıldı.

ÖLÇÜLEN (2026-11-24): Smart Band 9 Pro, küresel yazılım 2.1.59 (model miwear.watch.n67gl), fabrika ayarına
döndürüldükten sonra ilk soruya SERVER_PSK + "saatte tuşla onay" yanıtını verdi. Yani bu saatte yerel
eşleştirme YAPILAMIYOR; anahtar Mi Fitness ile eşleştirilip oradan alınmalı. "sor" adımı gerçek saatte
çalıştı (sıfırlanmış saat, bilgisayarla Bluetooth eşleştirmesinin hemen ardından klasik RFCOMM kanal 5'ten
şifresiz komut kabul ediyor); "esle" adımları yalnızca test_bind.py içindeki sahte saate karşı doğrulandı.

Kullanım (saatle konuşur; saat eşleştirme ekranındayken):
    python bind.py sor      yalnızca ilk soruyu sorar: saat hangi kipi istiyor?
    python bind.py esle     eşleştirmeyi tamamlar, anahtarı config.json'a yazar (ekrana basmaz)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

TYPE_ACCOUNT = 1
BIND_START_V2, BIND_VERIFY, BIND_CONFIRM, BIND_RESULT_V2 = 17, 18, 19, 25

VERIFY_SERVER_PSK, VERIFY_APP_LOCAL = 1, 2
VERIFY_NAMES = {1: "SERVER_PSK (sunucu onaylı)", 2: "APP_LOCAL (yerel)"}
OOB_NONE, OOB_NUMERIC, OOB_CODE, OOB_BUTTON = 0, 1, 2, 3
OOB_NAMES = {0: "onay yok", 1: "sayı karşılaştırma", 2: "dinamik kod", 3: "saatte tuşla onay"}

ERRORS = {
    1: "Saat zaten başka bir uygulamaya bağlı; önce fabrika ayarına döndürülmeli.",
    2: "Saat bir alanın eksik olduğunu bildirdi (bizim hatamız).",
    3: "Saat bir alanı geçersiz buldu (bizim hatamız).",
    4: "Saat bağlı değil.",
    5: "Saat sunucu yolunu istedi.",
    6: "Saatte reddedildi ya da onay süresi doldu.",
    7: "Kanıt uyuşmadı; yeniden deneyin.",
    8: "Adımlar sırasız geldi; yeniden bağlanıp baştan başlayın.",
    9: "Kullanıcı kimliği saatin bildiğinden farklı.",
    10: "Saate çok fazla telefon bağlı.",
    100: "Saat neden belirtmedi.",
}

RESULT_NONCE = bytes(range(0x10, 0x1C))  # sabit 12 bayt: 10 11 … 1B
RESULT_AAD = b"bind-data"
HKDF_INFO = b"miwear-bind"


# --- en küçük protobuf kodlayıcı/çözücü ---------------------------------
# pb/ altındaki derlenmiş tanımlar bu mesajları içermiyor ve protoc kurulu değil; mesajlar küçük ve düz.
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb_int(num: int, value: int) -> bytes:
    return _varint(num << 3) + _varint(int(value))


def pb_bytes(num: int, value: bytes | str) -> bytes:
    if isinstance(value, str):
        value = value.encode()
    return _varint(num << 3 | 2) + _varint(len(value)) + value


def pb_float(num: int, value: float) -> bytes:
    return _varint(num << 3 | 5) + struct.pack("<f", value)


def pb_parse(data: bytes) -> dict[int, list]:
    """Alan numarası -> değerler. Bozuk veride ValueError atar."""
    out: dict[int, list] = {}
    i, n = 0, len(data)

    def varint() -> int:
        nonlocal i
        shift = value = 0
        while True:
            if i >= n or shift > 63:
                raise ValueError("bozuk protobuf")
            b = data[i]
            i += 1
            value |= (b & 0x7F) << shift
            if not b & 0x80:
                return value
            shift += 7

    while i < n:
        key = varint()
        num, wire = key >> 3, key & 7
        if wire == 0:
            value = varint()
        elif wire == 2:
            size = varint()
            if i + size > n:
                raise ValueError("bozuk protobuf")
            value = data[i : i + size]
            i += size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if i + size > n:
                raise ValueError("bozuk protobuf")
            value = data[i : i + size]
            i += size
        else:
            raise ValueError("bozuk protobuf")
        out.setdefault(num, []).append(value)
    return out


def _one(msg: dict, num: int, default=None):
    return msg[num][-1] if num in msg else default


def command(cmd_id: int, account: bytes) -> bytes:
    return pb_int(1, TYPE_ACCOUNT) + pb_int(2, cmd_id) + pb_bytes(3, account)


# --- kripto --------------------------------------------------------------
def new_private_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def public_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    """Hatta giden biçim: 64 bayt, X sonra Y (baştaki 04 olmadan)."""
    nums = key.public_key().public_numbers()
    return nums.x.to_bytes(32, "big") + nums.y.to_bytes(32, "big")


def shared_secret(key: ec.EllipticCurvePrivateKey, peer_public: bytes) -> bytes:
    """Ham ECDH X koordinatı. Eğri üzerinde olmayan anahtarda ValueError atar."""
    if len(peer_public) != 64:
        raise ValueError("açık anahtar 64 bayt olmalı")
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b"\x04" + peer_public)
    return key.exchange(ec.ECDH(), peer)


def proof(secret: bytes, random: bytes, code: bytes = b"") -> bytes:
    return hmac.new(secret, random + code, hashlib.sha256).digest()


def key_block(secret: bytes, app_random: bytes, device_random: bytes) -> bytes:
    """64 bayt: 0..40 oturum anahtarları (giriş anahtar bloğuyla aynı düzen), 40..56 auth key."""
    return HKDF(algorithm=hashes.SHA256(), length=64, salt=app_random + device_random, info=HKDF_INFO).derive(secret)


def encrypt_result(block: bytes, user_id: str, device_name: str, api_level: float = 34.0) -> bytes:
    companion = pb_int(1, 0) + pb_float(2, api_level) + pb_bytes(3, device_name)
    info = pb_bytes(1, user_id) + pb_bytes(2, companion)
    return AESCCM(block[16:32], tag_length=4).encrypt(RESULT_NONCE, info, RESULT_AAD)


def app_device_id(mac: str, user_id: str) -> str:
    return hashlib.md5((mac.upper() + user_id).encode()).hexdigest()


# --- durum makinesi ------------------------------------------------------
@dataclass
class Info:
    """Saatin ilk soruya yanıtı. mac, seri ve did saati tanımlar: günlüğe yazılmaz."""
    verify_mode: int
    oob_mode: int
    model: str = ""
    mac: str = ""
    serial: str = field(default="", repr=False)
    did: str = field(default="", repr=False)


@dataclass
class Bound:
    auth_key: bytes = field(repr=False)
    app_device_id: str = field(repr=False)
    block: bytes = field(repr=False)
    info: Info | None = None


class BindError(Exception):
    def __init__(self, message: str, code: int | None = None, tell_band: bytes | None = None):
        super().__init__(message)
        self.code = code
        self.tell_band = tell_band  # varsa, vazgeçmeden önce saate gönderilecek paket


class Binder:
    """Saatten gelen her şifresiz komutu receive() ile alır; ne yapılacağını döndürür.

    receive() dönüşleri:
        ("send", paket, saatte_onay_gerekli)   paketi gönder
        ("info", Info)                         yalnızca probe_only kipinde: ilk yanıt, burada durulur
        ("bound", Bound)                       bitti
        ("ignored",)                           eşleştirme yanıtı değil
    Hata durumunda BindError atar. Zamanlayıcı yoktur; süreyi çağıran tutar.
    """

    def __init__(self, user_id: str, device_name: str = "Smart Band", code: str = "", probe_only: bool = False,
                 private_key=None, app_random: bytes | None = None, device_id: str | None = None):
        if not user_id:
            raise ValueError("kullanıcı kimliği boş olamaz")
        self.user_id = user_id
        self.device_name = device_name
        self.code = code.encode()
        self.probe_only = probe_only
        self.key = private_key or new_private_key()
        self.app_random = app_random or os.urandom(16)
        self.device_id = device_id
        self.stage = "new"
        self.info: Info | None = None
        self.block = b""

    def start(self) -> bytes:
        self.stage = "info"
        # check_dynamic_code her zaman yazılır (false); device_name Mi Fitness gibi boş bırakılır
        start = pb_int(1, 0) + pb_bytes(2, hashlib.md5(self.user_id.encode()).digest())
        return command(BIND_START_V2, pb_bytes(11, start))

    def receive(self, body: bytes) -> tuple:
        try:
            cmd = pb_parse(body)
            if _one(cmd, 1) != TYPE_ACCOUNT or _one(cmd, 2) not in (BIND_START_V2, BIND_VERIFY, BIND_CONFIRM, BIND_RESULT_V2):
                return ("ignored",)
            cmd_id = _one(cmd, 2)
            account = pb_parse(_one(cmd, 3, b""))
        except ValueError:
            return ("ignored",)

        code = _one(account, 3)
        if code and isinstance(code, int):
            self.stage = "failed"
            raise BindError(ERRORS.get(code, f"Saat bilinmeyen hata kodu döndürdü: {code}"), code=code)

        if self.stage == "info" and cmd_id == BIND_START_V2 and 12 in account:
            return self._on_info(pb_parse(_one(account, 12)))
        if self.stage == "verify" and cmd_id == BIND_VERIFY and 18 in account:
            return self._on_device_verify(pb_parse(_one(account, 18)))
        if self.stage == "confirm" and cmd_id == BIND_CONFIRM and 20 in account:
            if not _one(pb_parse(_one(account, 20)), 1):
                self.stage = "failed"
                raise BindError(ERRORS[6], code=6)
            self.stage = "result"
            payload = encrypt_result(self.block, self.user_id, self.device_name)
            return ("send", command(BIND_RESULT_V2, pb_bytes(29, pb_bytes(1, payload))), False)
        if self.stage == "result" and cmd_id == BIND_RESULT_V2:
            # hata kodu 0 ya da hiç hata kodu yok: bağlandı (Mi Fitness ikisini de başarı sayar)
            self.stage = "bound"
            return ("bound", Bound(self.block[40:56], self.device_id, self.block, self.info))
        return ("ignored",)

    def _on_info(self, m: dict) -> tuple:
        def text(num: int) -> str:
            return _one(m, num, b"").decode("utf-8", "replace")

        self.info = Info(_one(m, 1, 0), _one(m, 5, 0), model=text(4), mac=text(3), serial=text(2), did=text(7))
        if self.probe_only:
            self.stage = "probed"
            return ("info", self.info)
        if self.info.verify_mode == VERIFY_SERVER_PSK:
            self.stage = "failed"
            raise BindError("Saat sunucu onaylı eşleştirme istiyor; bu yazılımda yerel eşleştirme yapılamaz.")
        if self.info.verify_mode != VERIFY_APP_LOCAL:
            self.stage = "failed"
            raise BindError(f"Saat bilinmeyen bir doğrulama kipi bildirdi: {self.info.verify_mode}")
        if self.info.oob_mode not in (OOB_NONE, OOB_CODE, OOB_BUTTON):
            self.stage = "failed"
            raise BindError(f"Saat desteklenmeyen bir onay türü istiyor: {OOB_NAMES.get(self.info.oob_mode, self.info.oob_mode)}")
        if not self.device_id:
            self.device_id = app_device_id(self.info.mac, self.user_id)
        self.stage = "verify"
        verify = pb_bytes(1, self.device_id) + pb_bytes(2, public_bytes(self.key))
        return ("send", command(BIND_VERIFY, pb_bytes(17, verify)), self.info.oob_mode == OOB_BUTTON)

    def _on_device_verify(self, m: dict) -> tuple:
        device_public, device_random, device_sign = _one(m, 1, b""), _one(m, 2, b""), _one(m, 3, b"")
        try:
            secret = shared_secret(self.key, device_public)
            ok = bool(device_random) and hmac.compare_digest(proof(secret, device_random, self.code), device_sign)
        except ValueError:
            ok = False
        if not ok:
            self.stage = "failed"
            raise BindError("Saatin kanıtı doğrulanamadı.", tell_band=command(BIND_CONFIRM, pb_int(3, 7)))
        self.block = key_block(secret, self.app_random, device_random)
        self.stage = "confirm"
        confirm = pb_bytes(1, self.app_random) + pb_bytes(2, proof(secret, self.app_random, self.code))
        return ("send", command(BIND_CONFIRM, pb_bytes(19, confirm)), False)


# --- saatle konuşan sürücü ----------------------------------------------
def run(band, binder: Binder, on_confirm=None, timeout: float = 60.0):
    """band: connect(auth=False) ile bağlanmış bir client.Band. Info (probe_only) ya da Bound döndürür."""
    band.send_plain(binder.start())
    while True:
        if band.read(timeout) is None:
            raise BindError("Saat süresi içinde yanıt vermedi.")
        try:
            step = binder.receive(band.last_body)
        except BindError as e:
            if e.tell_band:
                band.send_plain(e.tell_band)
            raise
        if step[0] == "send":
            band.send_plain(step[1])
            if step[2] and on_confirm:
                on_confirm()
        elif step[0] in ("info", "bound"):
            return step[1]


def _main() -> None:
    import argparse
    import json
    import secrets
    import shutil
    import time
    from pathlib import Path

    from client import Band

    ap = argparse.ArgumentParser(description="Saatin ilk eşleştirmesi (yerel kip). Saat eşleştirme ekranında olmalı.")
    ap.add_argument("komut", choices=["sor", "esle"])
    ap.add_argument("--adres", help="saatin Bluetooth adresi (sıfırlamadan sonra değişebilir); verilmezse config.json")
    ap.add_argument("--config", default=str(Path(__file__).parent / "config.json"))
    args = ap.parse_args()

    path = Path(args.config)
    cfg = json.loads(path.read_text())
    address = args.adres or cfg["address"]
    user_id = cfg.get("bind_user_id") or str(secrets.randbelow(9 * 10**9) + 10**9)

    band = Band(address, "00" * 16, cfg.get("channel", 5))
    band.connect(auth=False)
    try:
        binder = Binder(user_id, device_name="Linux PC", probe_only=args.komut == "sor")
        result = run(band, binder, on_confirm=lambda: print("Saatte onay tuşuna basın…"))
    except BindError as e:
        raise SystemExit(f"Eşleştirme olmadı: {e}")
    finally:
        band.close()

    info = result if isinstance(result, Info) else result.info
    print("Model:", info.model or "?")
    print("Doğrulama kipi:", VERIFY_NAMES.get(info.verify_mode, info.verify_mode))
    print("Onay türü:", OOB_NAMES.get(info.oob_mode, info.oob_mode))
    if isinstance(result, Info):
        return

    shutil.copy2(path, path.with_name(f"config.json.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    cfg.update(address=address, auth_key=result.auth_key.hex(), app_device_id=result.app_device_id, bind_user_id=user_id)
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    print("Eşleştirildi. Yeni anahtar config.json'a yazıldı (eskisi yedeklendi).")

    try:
        with Band(address, cfg["auth_key"], cfg.get("channel", 5), app_device_id=cfg["app_device_id"]):
            print("Yeni anahtarla giriş başarılı.")
    except Exception as e:  # girişin ayrıntısı gerçek saatte henüz bilinmiyor; nedenini göster
        print(f"Yeni anahtarla giriş denemesi başarısız: {type(e).__name__}: {e}")


if __name__ == "__main__":
    _main()
