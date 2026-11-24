"""Bileklik istemcisi (klasik Bluetooth / SPP protokol v2).

Protokol Gadgetbridge'in Xiaomi desteğinden uyarlanmıştır.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import socket
import struct
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

sys.path.insert(0, str(Path(__file__).parent / "pb"))
import i18n  # noqa: E402
import xiaomi_pb2  # noqa: E402
from xiaomi import wear_account_pb2  # noqa: E402

PREAMBLE = b"\xa5\xa5"
TYPE_NACK, TYPE_ACK, TYPE_SESSION, TYPE_DATA = 0, 1, 2, 3
CH_PROTOBUF, CH_DATA, CH_ACTIVITY = 1, 2, 5
OP_PLAIN, OP_ENCRYPTED = 1, 2

# V1 çerçevesiyle protokol sürümü sorgusu
VERSION_REQUEST = bytes.fromhex("badcfe00c00300000000ef")
SESSION_CONFIG = bytes([
    1,                          # START_SESSION_REQUEST
    1, 3, 0, 1, 0, 0,           # sürüm 1.0.0
    2, 2, 0, 0x00, 0xFC,        # azami çerçeve boyu
    3, 2, 0, 0x20, 0x00,        # gönderim penceresi
    4, 2, 0, 0x10, 0x27,        # zaman aşımı 10 sn
])


def crc16_arc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def frame(ptype: int, seq: int, payload: bytes) -> bytes:
    return PREAMBLE + struct.pack("<BBHH", ptype & 0xF, seq & 0xFF, len(payload), crc16_arc(payload)) + payload


def _hmac(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def derive_session(secret: bytes, phone_nonce: bytes, watch_nonce: bytes) -> bytes:
    key = _hmac(phone_nonce + watch_nonce, secret)
    out, tmp, counter = b"", b"", 1
    while len(out) < 64:
        tmp = _hmac(key, tmp + b"miwear-auth" + bytes([counter]))
        out += tmp
        counter += 1
    return out[:64]


class AuthError(Exception):
    pass


class Band:
    def __init__(self, address: str, auth_key: str, channel: int = 5, verbose: bool = False, transport=None,
                 app_device_id: str | None = None):
        # transport: soket yerine kullanılacak nesne (send/sendall/recv/settimeout/close sunar; recv zaman
        # aşımında TimeoutError atar) ya da onu bağlanmış olarak döndüren işlev (adres, kanal, zaman aşımı).
        self.transport = transport
        # yerel eşleştirmede (bind.py) üretilen telefon kimliği; varsa girişte saate bildirilir
        self.app_device_id = app_device_id
        self.last_body = b""  # son okunan protobuf komutunun ham hâli (pb tanımında olmayan alanlar için)
        self.address = address
        self.secret = bytes.fromhex(auth_key.removeprefix("0x"))
        self.channel = channel
        self.last_ack = -1
        self.verbose = verbose
        self.sock: socket.socket | None = None
        self.buf = b""
        self.seq = 0
        self.enc_key = self.dec_key = b""
        self.enc_nonce = self.dec_nonce = b""
        self.authenticated = False
        self.on_command = None  # her gelen komut için çağrılır

    # --- bağlantı -----------------------------------------------------
    def connect(self, timeout: float = 20.0, auth: bool = True) -> None:
        if self.transport is None:
            s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
            s.settimeout(timeout)
            s.connect((self.address, self.channel))
        else:
            t = self.transport
            s = t(self.address, self.channel, timeout) if callable(t) else t
            s.settimeout(timeout)
        self.sock = s
        s.send(VERSION_REQUEST)
        version = self._read_v1()
        self._log("protokol sürümü", version.hex())
        if not version or version[0] < 2:
            raise RuntimeError(f"Desteklenmeyen SPP protokol sürümü: {version.hex()}")
        s.send(frame(TYPE_SESSION, 0, SESSION_CONFIG))
        while True:
            ptype, _, payload = self._read_frame()
            if ptype == TYPE_SESSION:
                self._log("oturum yanıtı", payload.hex())
                break
        if auth:  # ilk eşleştirme (bind.py) girişten önce, şifresiz yapılır
            self._authenticate()

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # --- kimlik doğrulama ---------------------------------------------
    def _authenticate(self) -> None:
        phone_nonce = os.urandom(16)
        cmd = xiaomi_pb2.Command(type=1, subtype=26)
        cmd.auth.appVerify.nonce = phone_nonce
        if self.app_device_id:
            cmd.auth.appVerify.deviceId = self.app_device_id
        self._send(cmd, encrypted=False)

        reply = self._wait(1, 26)
        dv = reply.auth.deviceVerify
        if not dv.nonce:
            raise AuthError(f"Saat nonce göndermedi: {reply}")
        keys = derive_session(self.secret, phone_nonce, dv.nonce)
        self.dec_key, self.enc_key = keys[0:16], keys[16:32]
        self.dec_nonce, self.enc_nonce = keys[32:36], keys[36:40]
        if _hmac(self.dec_key, dv.nonce + phone_nonce) != dv.hmac:
            raise AuthError("Saatin HMAC'i uyuşmuyor: auth key yanlış ya da geçersiz.")

        info = wear_account_pb2.CompanionDevice(
            unknown1=0, phoneApiLevel=34, phoneName="Linux PC", unknown3=224, region="TR"
        )
        ccm_nonce = self.enc_nonce + struct.pack("<II", 0, 0)
        cmd = xiaomi_pb2.Command(type=1, subtype=27)
        cmd.auth.appConfirm.encryptedNonces = _hmac(self.enc_key, phone_nonce + dv.nonce)
        cmd.auth.appConfirm.encryptedDeviceInfo = AESCCM(self.enc_key, tag_length=4).encrypt(
            ccm_nonce, info.SerializeToString(), None
        )
        self._send(cmd, encrypted=False)

        reply = self._wait(1, 27)
        self._log("auth yanıtı", reply)
        self.authenticated = True

    # --- çerçeveler ---------------------------------------------------
    def _recv_more(self) -> None:
        data = self.sock.recv(4096)
        if not data:
            raise ConnectionError("Saat bağlantıyı kapattı")
        self.buf += data

    def _read_v1(self) -> bytes:
        while True:
            start = self.buf.find(b"\xba\xdc\xfe")
            if start >= 0 and len(self.buf) >= start + 7:
                (length,) = struct.unpack_from("<H", self.buf, start + 5)
                end = start + 7 + length + 1
                if len(self.buf) >= end:
                    body = self.buf[start + 7 : end - 1]
                    self.buf = self.buf[end:]
                    return body[3:]  # opcode, seri, veri türü sonrası
            self._recv_more()

    def _read_frame(self) -> tuple[int, int, bytes]:
        while True:
            start = self.buf.find(PREAMBLE)
            if start > 0:
                self.buf = self.buf[start:]
            if start >= 0 and len(self.buf) >= 8:
                flags, seq, length, crc = struct.unpack_from("<BBHH", self.buf, 2)
                if len(self.buf) >= 8 + length:
                    payload = self.buf[8 : 8 + length]
                    self.buf = self.buf[8 + length :]
                    if crc16_arc(payload) != crc:
                        self._log("CRC hatası, çerçeve atlandı")
                        continue
                    return flags & 0xF, seq, payload
            self._recv_more()

    def _ctr(self, key: bytes, data: bytes) -> bytes:
        c = Cipher(algorithms.AES(key), modes.CTR(key)).encryptor()
        return c.update(data) + c.finalize()

    def _send(self, cmd, encrypted: bool = True) -> None:
        body = cmd.SerializeToString()
        if encrypted:
            payload = bytes([CH_PROTOBUF, OP_ENCRYPTED]) + self._ctr(self.enc_key, body)
        else:
            payload = bytes([CH_PROTOBUF, OP_PLAIN]) + body
        self.sock.sendall(frame(TYPE_DATA, self.seq, payload))
        self.seq = (self.seq + 1) & 0xFF

    def _send_raw(self, body: bytes) -> None:
        """Hazır kodlanmış bir protobuf komutunu şifreleyip gönderir (pb tanımında olmayan alanlar için)."""
        self.sock.sendall(frame(TYPE_DATA, self.seq, bytes([CH_PROTOBUF, OP_ENCRYPTED]) + self._ctr(self.enc_key, body)))
        self.seq = (self.seq + 1) & 0xFF

    def send_plain(self, body: bytes) -> None:
        """Hazır kodlanmış bir protobuf komutunu şifresiz gönderir (giriş öncesi eşleştirme paketleri)."""
        self.sock.sendall(frame(TYPE_DATA, self.seq, bytes([CH_PROTOBUF, OP_PLAIN]) + body))
        self.seq = (self.seq + 1) & 0xFF

    def send_chunk(self, chunk: bytes, timeout: float = 10.0) -> None:
        """Veri kanalından (şifresiz) bir parça gönderir ve saatin onayını bekler; gelen komutlar bu sırada işlenir."""
        seq = self.seq
        self.sock.sendall(frame(TYPE_DATA, seq, bytes([CH_DATA, OP_PLAIN]) + chunk))
        self.seq = (self.seq + 1) & 0xFF
        deadline = time.monotonic() + timeout
        while self.last_ack != seq:
            if (left := deadline - time.monotonic()) <= 0:
                raise TimeoutError(i18n.t("Saat veri parçasını onaylamadı"))
            self.read(min(left, 0.5))

    def read(self, timeout: float = 10.0):
        """Sıradaki protobuf komutunu döndürür; zaman aşımında None."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                ptype, seq, payload = self._read_frame()
            except (TimeoutError, socket.timeout):
                return None
            if ptype == TYPE_ACK:
                self.last_ack = seq
            if ptype != TYPE_DATA:
                continue
            self.sock.send(frame(TYPE_ACK, seq, b""))
            channel, opcode, body = payload[0] & 0xF, payload[1], payload[2:]
            if opcode == OP_ENCRYPTED:
                body = self._ctr(self.dec_key, body)
            if channel == CH_ACTIVITY:
                self._on_chunk(body)
                continue
            if channel != CH_PROTOBUF:
                self._log(f"kanal {channel} verisi", len(body), "bayt")
                continue
            self.last_body = body
            cmd = xiaomi_pb2.Command()
            cmd.ParseFromString(body)
            self._log("<-", cmd)
            if self.on_command:
                self.on_command(cmd)
            return cmd

    def _wait(self, ctype: int, subtype: int, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            cmd = self.read(remaining)
            if cmd is not None and cmd.type == ctype and cmd.subtype == subtype:
                return cmd
        raise TimeoutError(i18n.t("Saatten yanıt gelmedi (tür {type}, alt tür {subtype})", type=ctype, subtype=subtype))

    def send(self, ctype: int, subtype: int) -> None:
        """Yanıt beklemeden komut gönderir; yanıt read() ile gelir."""
        self._send(xiaomi_pb2.Command(type=ctype, subtype=subtype))

    def request(self, ctype: int, subtype: int, timeout: float = 10.0):
        self._send(xiaomi_pb2.Command(type=ctype, subtype=subtype))
        return self._wait(ctype, subtype, timeout)

    def _log(self, *args) -> None:
        if self.verbose:
            print("[band]", *args, file=sys.stderr)

    # --- aktivite dosyaları -------------------------------------------
    def _on_chunk(self, chunk: bytes) -> None:
        total, num = struct.unpack_from("<HH", chunk)
        if num == 1:
            self._file_buf = b""
        self._file_buf = getattr(self, "_file_buf", b"") + chunk[4:]
        if num != total:
            return
        data, self._file_buf = self._file_buf, b""
        import zlib
        if len(data) < 13 or zlib.crc32(data[:-4]) != struct.unpack("<I", data[-4:])[0]:
            self._log("aktivite dosyası bozuk, atlandı", len(data))
            self._file_done = b""
            return
        self._file_done = data

    def list_files(self, today: bool) -> list[bytes]:
        """Saatte bekleyen aktivite dosyalarının 7 baytlık kimlikleri."""
        cmd = xiaomi_pb2.Command(type=8, subtype=1 if today else 2)
        if today:
            cmd.health.activitySyncRequestToday.unknown1 = 0
        self._send(cmd)
        raw = self._wait(8, cmd.subtype, 15).health.activityRequestFileIds
        return [raw[i : i + 7] for i in range(0, len(raw) - 6, 7)]

    def ack_file(self, file_id: bytes) -> None:
        """Dosyanın alındığını saate bildirir (sağlık 8/5); saat dosyayı siler. Yanıt beklenmez."""
        cmd = xiaomi_pb2.Command(type=8, subtype=5)
        cmd.health.activitySyncAckFileIds = file_id
        self._send(cmd)

    def fetch_file(self, file_id: bytes, timeout: float = 30.0) -> bytes | None:
        """Dosyayı indirir. Onay ayrıdır (ack_file); onaylanmayan dosya saatte kalır."""
        self._file_done = None
        cmd = xiaomi_pb2.Command(type=8, subtype=3)
        cmd.health.activityRequestFileIds = file_id
        self._send(cmd)
        deadline = time.monotonic() + timeout
        while self._file_done is None and (left := deadline - time.monotonic()) > 0:
            self.read(min(left, 5.0))
        return self._file_done or None

    # --- üst düzey ----------------------------------------------------
    def battery(self) -> dict:
        b = self.request(2, 1).system.power.battery
        return {"level": b.level, "charging": b.state == 1, "state": b.state}

    def device_info(self) -> dict:
        d = self.request(2, 2).system.deviceInfo
        return {"serial": d.serialNumber, "firmware": d.firmware, "model": d.model}

    def find(self, on: bool) -> None:
        cmd = xiaomi_pb2.Command(type=2, subtype=18)
        cmd.system.findDevice = 0 if on else 1
        self._send(cmd)

    def notify(self, title: str, body: str, app: str = "Smart Band") -> None:
        cmd = xiaomi_pb2.Command(type=7, subtype=0)
        n = cmd.notification.notification2.notification3
        n.package, n.appName, n.title, n.unknown4, n.body = "pc.bileklik", app, title, "", body
        n.timestamp = time.strftime("%Y%m%dT%H%M%S")
        n.id = int(time.time() * 10) & 0x7FFFFFFF
        self._send(cmd)

    def realtime_start(self) -> None:
        self._send(xiaomi_pb2.Command(type=8, subtype=45))

    def realtime_stop(self) -> None:
        self._send(xiaomi_pb2.Command(type=8, subtype=46))
