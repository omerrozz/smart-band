"""Xiaomi Body Composition Scale S400 (MJTZC01YM) yayınlarını çözer.

Tartıya bağlanılmaz: ölçüm, FE95 servis verisinde şifreli bir MiBeacon v5 paketi olarak yayınlanır.
Çözmek için tartının Bluetooth adresi ve Xiaomi hesabındaki 32 haneli bağlama anahtarı (bind key) gerekir.
Biçim ESPHome (xiaomi_body_scale) ve Gadgetbridge (MiScaleS400Decryptor) ile aynıdır; alttaki örnek
paketler Gadgetbridge'in testlerinden.

Bir tartılma birkaç paket yollar: önce kilo + nabız + 50 kHz empedans, sonra yalnızca 250 kHz empedans.
"""
from __future__ import annotations

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

FRAME = 24
OBJ_S400 = 0x6E16


def valid_key(key: str | None) -> bool:
    return bool(key) and len(key) == 32 and all(c in "0123456789abcdefABCDEF" for c in key)


def decode(frame: bytes, address: str, bind_key: str) -> dict | None:
    """Bir FE95 servis verisini çözer. Ölçüm değilse ya da anahtar tutmuyorsa None.

    Dönen sözlük: weight (kg), heart_rate (atım/dk), impedance_low / impedance_high (ohm), profile, counter;
    pakette olmayan alanlar yer almaz.
    """
    if frame is None or not valid_key(bind_key):
        return None
    if len(frame) == FRAME + 2 and frame[:2] == b"\x95\xfe":
        frame = frame[2:]
    if len(frame) != FRAME or frame[0] & 0x48 != 0x48:   # veri var + şifreli
        return None
    try:
        mac = bytes.fromhex(address.replace(":", ""))
    except ValueError:
        return None
    if len(mac) != 6:
        return None
    nonce = mac[::-1] + frame[2:5] + frame[-7:-4]
    try:
        obj = AESCCM(bytes.fromhex(bind_key), tag_length=4).decrypt(nonce, frame[5:-7] + frame[-4:], b"\x11")
    except Exception:
        return None
    if len(obj) < 12 or int.from_bytes(obj[0:2], "little") != OBJ_S400 or obj[2] != 9:
        return None
    packed = int.from_bytes(obj[4:8], "little")
    raw_w, raw_hr, raw_imp = packed & 0x7FF, (packed >> 11) & 0x7F, packed >> 18
    weight = 0 if raw_w == 0x7FF else raw_w
    out: dict = {"profile": obj[3], "counter": frame[4], "ts": int.from_bytes(obj[8:12], "little")}
    if weight:
        out["weight"] = weight / 10
    if 0 < raw_hr < 127:
        out["heart_rate"] = raw_hr + 50
    if raw_imp:
        out["impedance_high" if not weight and not raw_hr else "impedance_low"] = raw_imp / 10
    return out


if __name__ == "__main__":
    MAC, KEY = "84:46:93:64:A5:E6", "58305740b64e4b425e518aa1f4e51339"
    a = decode(bytes.fromhex("4859d53b2d3314943c58b133638c7457a4000000c3e670dc"), MAC, KEY)
    b = decode(bytes.fromhex("95FE4859D53B3BDE6BC8D05B51C0CDFD9021C9000000925C5039"), MAC, KEY)
    print(a, b)
    assert a and abs(a["weight"] - 74.2) < .05 and b and abs(b["weight"] - 73.2) < .05
    assert decode(bytes.fromhex("4859d53b2d3314943c58b133638c7457a4000000c3e670dc"), MAC, KEY[:-1] + "0") is None
    assert decode(bytes.fromhex("4859d53b"), MAC, KEY) is None
    print("TAMAM")
