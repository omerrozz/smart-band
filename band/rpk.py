"""Saate hızlı uygulama (.rpk) yükleme, listeleme ve silme: komut türü 20 + veri yükleme servisi (tür 22).

Sıra Gadgetbridge'in XiaomiRpkService / XiaomiDataUploadService kodundaki gibidir. Rpk mesajı bizim pb
tanımımızda olmadığı için (Command alanı 22) elle kodlanır ve çözülür.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import zipfile
import zlib
from io import BytesIO

import i18n
from client import xiaomi_pb2

TYPE_RPK = 64


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _f(num: int, value) -> bytes:
    """Tek alan: int ise varint, bytes/str ise uzunluklu."""
    if isinstance(value, int):
        return _varint(num << 3) + _varint(value)
    if isinstance(value, str):
        value = value.encode()
    return _varint(num << 3 | 2) + _varint(len(value)) + value


def _parse(data: bytes) -> dict:
    """Protobuf baytlarını {alan: [değerler]} sözlüğüne çözer (varint ve uzunluklu alanlar)."""
    out: dict = {}
    i = 0
    while i < len(data):
        key = shift = 0
        while True:
            b = data[i]; i += 1
            key |= (b & 0x7F) << shift; shift += 7
            if not b & 0x80:
                break
        num, wire = key >> 3, key & 7
        if wire == 0:
            v = shift = 0
            while True:
                b = data[i]; i += 1
                v |= (b & 0x7F) << shift; shift += 7
                if not b & 0x80:
                    break
        elif wire == 2:
            n = shift = 0
            while True:
                b = data[i]; i += 1
                n |= (b & 0x7F) << shift; shift += 7
                if not b & 0x80:
                    break
            v = data[i:i + n]; i += n
        elif wire == 5:
            v = data[i:i + 4]; i += 4
        elif wire == 1:
            v = data[i:i + 8]; i += 8
        else:
            raise ValueError(i18n.t("çözülemeyen alan"))
        out.setdefault(num, []).append(v)
    return out


def _rpk(cmd) -> dict:
    """Bir komutun Rpk alanı (22); yoksa boş sözlük."""
    top = _parse(cmd.SerializeToString())
    return _parse(top[22][0]) if 22 in top else {}


def manifest(data: bytes) -> dict:
    """rpk (zip) içindeki manifest: package, name, versionCode."""
    with zipfile.ZipFile(BytesIO(data)) as z:
        return json.loads(z.read("manifest.json"))


def list_apps(band, timeout: float = 8.0) -> list[dict]:
    band.send(20, 0)
    r = _rpk(band._wait(20, 0, timeout))
    apps = []
    for raw in (_parse(r[1][0]).get(1, []) if 1 in r else []):
        a = _parse(raw)
        apps.append({"id": a.get(1, [b""])[0].decode(errors="replace"), "name": a.get(5, [b""])[0].decode(errors="replace"),
                     "sha": a.get(2, [b""])[0]})
    return apps


def upload(band, kind: int, data: bytes, progress=None) -> None:
    """Veri yükleme servisi: istek, saatin onayı, sonra veri kanalından parçalar."""
    md5 = hashlib.md5(data).digest()
    req = xiaomi_pb2.Command(type=22, subtype=0)
    req.dataUpload.dataUploadRequest.type = kind
    req.dataUpload.dataUploadRequest.md5sum = md5
    req.dataUpload.dataUploadRequest.size = len(data)
    band._send(req)
    ack = band._wait(22, 0, 15).dataUpload.dataUploadAck
    if ack.errno:
        raise RuntimeError(i18n.t("Saat yüklemeyi reddetti (kod {code})", code=ack.errno))
    chunk = ack.chunkSize or 2048
    body = bytes([0, kind]) + md5 + struct.pack("<I", len(data)) + data[ack.resumePosition:]
    body += struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)
    part = chunk - 4
    total = math.ceil(len(body) / part)
    for n in range(total):
        band.send_chunk(struct.pack("<HH", total, n + 1) + body[n * part:(n + 1) * part])
        if progress:
            progress(n + 1, total)


def install(band, data: bytes, progress=None) -> dict:
    """rpk dosyasını saate yükler. Dönen sözlük: package, name, installed (saat 'yüklendi' bildirdiyse True)."""
    m = manifest(data)
    info = _f(1, m["package"]) + _f(2, int(m["versionCode"])) + _f(3, len(data))
    band._send_raw(xiaomi_pb2.Command(type=20, subtype=1).SerializeToString() + _f(22, _f(2, info)))
    r = _rpk(band._wait(20, 1, 15))
    if 3 not in r:
        raise RuntimeError(i18n.t("Saat yükleme isteğine beklenen yanıtı vermedi"))
    code = _parse(r[3][0]).get(1, [0])[0]
    if code != 0:
        raise RuntimeError(i18n.t("Saat uygulamayı kabul etmedi (kod {code})", code=code))
    upload(band, TYPE_RPK, data, progress)
    try:
        band._wait(20, 2, 40)
        done = True
    except TimeoutError:
        done = False
    return {"package": m["package"], "name": m.get("name"), "installed": done}


def delete(band, package: str) -> bool:
    app = next((a for a in list_apps(band) if a["id"] == package), None)
    if not app:
        return False
    band._send_raw(xiaomi_pb2.Command(type=20, subtype=3).SerializeToString() + _f(22, _f(5, _f(1, package) + _f(2, app["sha"]))))
    return True
