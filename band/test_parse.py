"""Uyku / antrenman / elle ölçüm çözücülerinin ve kayıt katmanının sınaması.

Çalıştırma:  ../.venv/bin/python test_parse.py
Sentetik dosyaları geçici klasöre üretir, çözer, geçici veritabanına yükler. band.db'ye, config.json'a
ve saate dokunmaz; server.py içe aktarılmaz (Handler sınıfı kaynak metninden alınıp sahte ortamda denenir).
"""
from __future__ import annotations

import ast
import json
import math
import struct
import tempfile
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import date, datetime
from pathlib import Path

import i18n
import parse
import parse_more
import testfiles
from store import Store

HERE = Path(__file__).parent
checks = 0


def ok(cond, what: str) -> None:
    global checks
    assert cond, what
    checks += 1


def near(a, b, tol, what: str) -> None:
    ok(a is not None and abs(a - b) <= tol, f"{what}: {a} != {b} (±{tol})")


def reversion(data: bytes, version: int) -> bytes:
    """Dosyanın sürüm baytını değiştirir, CRC'yi yeniden hesaplar."""
    body = data[:5] + bytes([version]) + data[6:-4]
    return body + struct.pack("<I", zlib.crc32(body))


def haversine(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def naive_rmssd(segments) -> float:
    """Çözücüdekinden bağımsız yazılmış RMSSD (aynı süzme kuralları)."""
    sq = []
    for seg in segments:
        for a, b in zip(seg, seg[1:]):
            if 300 <= a <= 2000 and 300 <= b <= 2000 and abs(b - a) <= 0.2 * a:
                sq.append((b - a) ** 2)
    return math.sqrt(sum(sq) / len(sq))


# --- çözücüler ---------------------------------------------------------------
def test_sleep() -> None:
    """Gadgetbridge'in v5 düzeni: 17 no'lu evre paketi, RR aralıkları, RMSSD."""
    data, truth = testfiles.sleep_file_v5()
    ok(zlib.crc32(data[:-4]) == struct.unpack("<I", data[-4:])[0], "uyku CRC")
    kind, s = parse.parse(data)
    ok(kind == "sleep_summary" and s is not None, "uyku türü")
    for key in ("bed_time", "wake_time", "into_bed_time", "score", "duration", "awake", "light", "rem", "deep",
                "wake_count"):
        ok(s[key] == truth[key], f"uyku {key}: {s[key]} != {truth[key]}")
    ok(s["is_awake"] is False and s["complete"] and s["stages_inferred"] is False, "uyku tamam")
    near(s["wake_time"] - s["bed_time"], 8 * 3600, 600, "yatakta süre ≈ 8 sa")
    near(s["duration"], 7.87 * 3600, 600, "uyku süresi")
    ok(len(s["stages"]) == truth["stages"], "evre sayısı")
    ok(s["stages"][0]["ts"] == s["bed_time"] and s["stages"][-1]["end"] == s["wake_time"], "evre uçları")
    ok(all(a["end"] == b["ts"] for a, b in zip(s["stages"], s["stages"][1:])), "evreler bitişik")
    tot = {}
    for st in s["stages"]:
        tot[st["stage"]] = tot.get(st["stage"], 0) + st["end"] - st["ts"]
    ok(all(tot[k] == s[k] for k in ("awake", "light", "rem", "deep")), f"evre toplamları {tot}")
    ok(sum(1 for st in s["stages"] if st["stage"] == "awake") == 2, "iki uyanma")
    ok(sum(1 for st in s["stages"] if st["stage"] == "deep") == 5, "beş döngü")
    ok(s["hr"] == {"start": truth["bed_time"], "step": 60, "values": truth["hr"]}, "uyku nabız dizisi")
    ok(s["spo2"]["step"] == 300 and s["spo2"]["values"] == truth["spo2"], "uyku SpO2 dizisi")
    ok(len(s["snore"]["values"]) == 24 and all(abs(a - b) < 0.01 for a, b in zip(s["snore"]["values"], truth["snore"])),
       "horlama dizisi")
    ok([seg["rr"] for seg in s["rr"]] == truth["rr_segments"], "RR parçaları birebir")
    ok(len(s["rr"]) == 3 and s["rr"][0]["ts_ms"] == truth["bed_time"] * 1000, "RR uyanmalarda bölünür")
    beats = sum(len(seg["rr"]) for seg in s["rr"])
    ok(22000 < beats < 32000, f"atım sayısı {beats}")
    ok(20 <= s["rmssd"] <= 80, f"RMSSD makul: {s['rmssd']}")
    near(s["rmssd"], naive_rmssd(truth["rr_segments"]), 0.06, "RMSSD bağımsız hesapla aynı")
    raw = [v for seg in truth["rr_segments"] for v in seg]
    ok(any(v > 2000 for v in raw) and s["rmssd"] < naive_unfiltered(truth["rr_segments"]) - 3, "süzme etkili")
    ok(len(s["breathing"]) == truth["breathing"] and all(8 <= r <= 22 for _, r in s["breathing"]), "soluk hızı")
    ok(s["bed_time"] < s["breathing"][0][0] < s["breathing"][-1][0] <= s["wake_time"], "soluk zamanları")
    hrv = parse_more.hrv_series(s["rr"])
    ok(80 <= len(hrv) <= 97 and all(10 < v < 120 for _, v in hrv), f"5 dakikalık HRV dizisi ({len(hrv)})")
    # desteklenmeyen sürüm ve kırpılmış dosya
    ok(parse.parse(reversion(data, 6))[1] is None, "uyku v6 desteklenmiyor")
    for cut in (9, 12, 30, 400, len(data) // 2, len(data) - 9):
        part = parse_more.sleep_details(data[:cut] + b"\0\0\0\0")
        ok(part is None or isinstance(part, dict), "kırpık uyku dosyası çökmez")
    half = parse_more.sleep_details(data[: len(data) // 2])
    ok(half["bed_time"] == truth["bed_time"] and half["hr"]["values"] == truth["hr"], "yarım dosyada başlık okunur")
    # geçerlilik bitleri: puan (3) ve SpO2 (10) geçersiz işaretlenirse o alanlar dosyada yer almaz
    rng_free = data[8:10]
    ok(rng_free == b"\xff\xf0", "uyku başlık bitleri")

    # eski biçimli evre dosyası
    kind, nap = parse.parse(testfiles.sleep_stages_file())
    ok(kind == "sleep_stages_details" and nap["duration"] == 36 * 60 and nap["deep"] == 15 * 60, "öğle uykusu")
    ok([st["stage"] for st in nap["stages"]] == ["light", "deep", "light", "awake"], "öğle uykusu evreleri")
    ok(nap["stages"][-1]["end"] == testfiles.NAP_WAKE and nap["rmssd"] is None, "öğle uykusu sonu")
    ok(parse.parse(reversion(testfiles.sleep_stages_file(), 3))[1] is None, "evre dosyası v3 desteklenmiyor")


def test_sleep_real_layout() -> None:
    """Gerçek Band 9 Pro (v4) düzenindeki sentetik gece: evre paketi boş, evreler olay çiftlerinden."""
    data, truth = testfiles.sleep_file()
    kind, s = parse.parse(data)
    ok(kind == "sleep_summary" and s["version"] == 4 and s["complete"], "v4 gece türü")
    for key in ("bed_time", "wake_time", "into_bed_time", "score", "duration", "awake", "light", "rem", "deep",
                "wake_count"):
        ok(s[key] == truth[key], f"v4 gece {key}: {s[key]} != {truth[key]}")
    near(s["duration"], 8 * 3600, 60, "v4 uyku süresi 8 sa")
    ok(s["stages"] == truth["stages"] and s["stages_inferred"] is True, "v4 evreler olaylardan çıkarılır")
    ok(len(s["segments"]) == truth["segments"] and s["sleep_index"] == truth["records"], "v4 bölümler / özet kayıtları")
    ok(s["hr"] == {"start": truth["bed_time"], "step": 60, "values": truth["hr"]}, "v4 nabız dizisi (0 = ölçüm yok)")
    ok(s["spo2"] == {"start": truth["bed_time"], "step": 60, "values": truth["spo2"]}, "v4 SpO2 dizisi başlık biti kaymış")
    ok(sum(1 for v in s["spo2"]["values"] if v) == len(truth["spo2"]) // 10, "SpO2 10 dakikada bir")
    ok(s["snore"] is None and s["rr"] == [] and s["rmssd"] is None, "v4: horlama / RR yok")
    ok(len(s["breathing"]) == truth["events"], "0x0a kayıt sayısı")
    ok(all(truth["bed_time"] - 16 <= ts < truth["wake_time"] + 600 for ts, _ in s["breathing"])
       and s["breathing"] == sorted(s["breathing"], key=lambda x: x[0]), "0x0a zamanı = pencere başı + saniye")
    p = s["packets"]
    ok(p[0] == truth["windows"] and p[2] == p[3] == p[16] == p[17] == 10 and p[101] == 1 and 1 not in p, f"paket dökümü {p}")
    ok(parse.parse(reversion(data, 6))[1] is None, "uyku v6 desteklenmiyor")


REAL_NIGHT = HERE / "raw" / "20261121T005000_sleep_summary_v4.bin"


def test_real_night() -> bool:
    """Saatten inen ilk gerçek uyku dosyası (yoksa atlanır)."""
    if not REAL_NIGHT.is_file():
        return False
    data = REAL_NIGHT.read_bytes()
    ok(zlib.crc32(data[:-4]) == struct.unpack("<I", data[-4:])[0] and data[8] == 0xD8, "gerçek dosya CRC / başlık")
    kind, s = parse.parse(data)
    ok(kind == "sleep_summary" and s["version"] == 4 and s["complete"], "gerçek gece türü")
    ok(datetime.fromtimestamp(s["bed_time"]).strftime("%d %H:%M") == "21 00:50"
       and datetime.fromtimestamp(s["wake_time"]).strftime("%d %H:%M") == "21 07:46", "gerçek gece 00:50–07:46")
    ok(s["is_awake"] is False and s["score"] is None and s["into_bed_time"] is None, "gerçek gece: puan yok")
    ok(len(s["segments"]) == 10 and s["sleep_index"] == 7, "10 olay çifti, özet paketinde 7 kayıt")
    ok(len(s["stages"]) == 19 and s["stages_inferred"], "19 evre parçası (tahmin)")
    ok(s["stages"][0]["ts"] == s["bed_time"] and s["stages"][-1]["end"] == s["wake_time"]
       and all(a["end"] == b["ts"] for a, b in zip(s["stages"], s["stages"][1:])), "evreler geceyi boşluksuz örter")
    ok((s["light"], s["deep"], s["awake"], s["rem"]) == (303 * 60, 107 * 60, 6 * 60, 0), "evre toplamları")
    ok(s["duration"] == 410 * 60 and s["wake_count"] == 3, "uyku 6 sa 50 dk, 3 kısa uyanma")
    ok(s["light"] + s["deep"] + s["awake"] == s["wake_time"] - s["bed_time"], "toplam = yatakta süre")
    ok(s["hr"]["step"] == 60 and len(s["hr"]["values"]) == 390
       and s["hr"]["start"] + 390 * 60 == s["wake_time"], "nabız: 390 dakika, kalkışta biter")
    hr = [v for v in s["hr"]["values"] if v]
    ok(len(hr) == 359 and min(hr) == 61 and max(hr) == 84, "nabız aralığı 61–84")
    sp = [v for v in s["spo2"]["values"] if v]
    ok(s["spo2"]["start"] == s["hr"]["start"] and len(s["spo2"]["values"]) == 390 and len(sp) == 33
       and min(sp) == 91 and max(sp) == 98, "SpO2: 33 ölçüm, %91–98")
    ok(s["snore"] is None and s["rr"] == [] and s["rmssd"] is None, "horlama ve RR paketi yok")
    ok(len(s["breathing"]) == 137 and s["bed_time"] - 60 < s["breathing"][0][0] < s["breathing"][-1][0] < s["wake_time"],
       "0x0a: 137 kayıt, gece içinde")
    ok(s["packets"] == {101: 3, 12: 3, 0: 39, 13: 2, 18: 2, 10: 29, 11: 29, 9: 8, 2: 10, 3: 10, 16: 10, 17: 10, 14: 36},
       f"paket dökümü {s['packets']}")
    with tempfile.TemporaryDirectory() as d:
        st = Store(Path(d) / "t.db", Path(d))
        st.upgrade()
        st.save_file(data[:7], data)
        night = st.sleep(date(2026, 11, 21))
        ok(night["duration"] == 24600 and len(night["stages"]) == 19 and night["hrv"] == []
           and night["source"] == "sleep_summary v4", "gerçek gece kayıt katmanından")
    return True


def naive_unfiltered(segments) -> float:
    sq = [(b - a) ** 2 for seg in segments for a, b in zip(seg, seg[1:])]
    return math.sqrt(sum(sq) / len(sq))


def test_sleep_validity() -> None:
    """Başlık bitleri kapalı alanlar dosyada hiç bulunmaz; v2'de puan ve horlama alanı yoktur."""
    bed, wake = testfiles.NIGHT_BED, testfiles.NIGHT_WAKE
    stages = testfiles._packet(17, bed, struct.pack(">HH", 1 << 12 | 30, 2 << 12 | 45))
    # v5: puan (bit 3) ve SpO2 (bit 10) geçersiz -> o baytlar yazılmaz
    body = bytes([0b11101111, 0b11010000]) + struct.pack("<Bii", 1, bed, wake) + bytes(9)
    body += struct.pack("<ii", bed - 600, wake) + struct.pack("<hhI", 60, 3, bed) + bytes([50, 51, 52])
    body += struct.pack("<hh", 60, 0) + stages          # horlama: adet 0 -> zaman alanı da yok
    s = parse_more.sleep_details(testfiles.wrap(bed, 5, 0, 8, 1, body))
    ok(s["score"] is None and s["spo2"] is None and s["snore"] is None and s["is_awake"], "v5 geçersiz alanlar")
    ok(s["hr"]["values"] == [50, 51, 52] and s["into_bed_time"] == bed - 600, "v5 nabız + yatağa giriş")
    ok([(x["stage"], x["end"] - x["ts"]) for x in s["stages"]] == [("light", 1800), ("deep", 2700)], "v5 evreler")
    ok(s["duration"] == 4500 and s["deep"] == 2700, "özet paketi yoksa süreler evrelerden")
    # v2: 1 baytlık başlık; alan sırası isAwake, yatış, kalkış, nabız, SpO2
    body = bytes([0b11111000]) + struct.pack("<Bii", 0, bed, wake) + struct.pack("<hhI", 60, 2, bed) + bytes([48, 49])
    body += struct.pack("<hhI", 60, 1, bed + 5) + bytes([97]) + stages
    s = parse_more.sleep_details(testfiles.wrap(bed, 2, 0, 8, 0, body))
    ok(s["hr"]["values"] == [48, 49] and s["spo2"] == {"start": bed + 5, "step": 60, "values": [97]}, "v2 diziler")
    ok(s["score"] is None and s["snore"] is None and len(s["stages"]) == 2, "v2 puansız")
    # v1: ilk kayıt zamanı alanı yok, yatış zamanı kullanılır
    body = bytes([0b11110000]) + struct.pack("<Bii", 0, bed, wake) + struct.pack("<hh", 60, 2) + bytes([48, 49]) + stages
    s = parse_more.sleep_details(testfiles.wrap(bed, 1, 0, 8, 0, body))
    ok(s["hr"] == {"start": bed, "step": 60, "values": [48, 49]} and len(s["stages"]) == 2, "v1 dizisi")


def test_manual() -> None:
    kind, m = parse.parse(testfiles.manual_file())
    ok(kind == "manual_details" and [(x["ts"], x["type"], x["value"]) for x in m] == testfiles.MANUAL, "elle ölçümler")
    t = testfiles.MANUAL[0][0]
    extra = struct.pack("<iBB", t + 1, 1 << 4 | 1, 0)             # değer 0: atılır
    extra += struct.pack("<iBH", t + 2, 2 << 4 | 5, 777)          # bilinmeyen tür: atlanır
    extra += struct.pack("<iBH", t + 3, 2 << 4 | 1, 143)          # 2 baytlık nabız
    extra += struct.pack("<iBi", t + 4, 4 << 4 | 4, 3651)         # sıcaklık
    m = parse_more.manual_samples(testfiles.manual_file(extra=extra))
    ok([(x["type"], x["value"]) for x in m[-2:]] == [("hr", 143), ("temperature", 3651)] and len(m) == 7, "elle uç durumlar")
    v1 = testfiles.wrap(t, 1, 0, 6, 0, b"\xff" + struct.pack("<iBB", t, 2, 96) + struct.pack("<iBB", t + 9, 3, 41))
    ok([(x["type"], x["value"]) for x in parse_more.manual_samples(v1)] == [("spo2", 96), ("stress", 41)], "elle v1")
    ok(parse.parse(reversion(testfiles.manual_file(), 3))[1] is None, "elle v3 desteklenmiyor")


def test_run() -> None:
    files, truth = testfiles.run_files()
    kind, s = parse.parse(files["workout_summary"][0])
    v = truth["values"]
    ok(kind == "workout_summary" and s["sport"] == "running" and s["workout_type"] == 1, "koşu türü")
    ok(s["start"] == truth["start"] and s["end"] == truth["end"] and s["duration"] == 3000, "koşu zamanları")
    near(s["distance"], 8000, 1, "koşu mesafesi")
    near(s["duration"] / 60, 50, 0.01, "koşu süresi 50 dk")
    ok(s["calories"] == truth["kcal"] and 350 < s["calories"] < 900, f"koşu kalorisi {s['calories']}")
    ok((s["hr_avg"], s["hr_max"], s["hr_min"]) == (v["HR_AVG"], v["HR_MAX"], v["HR_MIN"]), "koşu nabız özeti")
    ok(s["hr_zones"] == truth["zones"] and sum(s["hr_zones"].values()) <= 3000, f"nabız bölgeleri {s['hr_zones']}")
    ok(sum(1 for z in s["hr_zones"].values() if z > 60) >= 4, "nabız en az dört bölgeden geçer")
    near(s["pace_avg"], 375, 1, "ortalama tempo s/km")
    near(s["speed_avg"], 8000 / 3000, 0.01, "ortalama hız m/s")
    near(s["stride_avg"], v["STEP_LENGTH_AVG"] / 100, 0.001, "adım boyu m")
    ok(s["cadence_avg"] == v["STEP_RATE_AVG"] and s["steps"] == truth["steps"], "kadans / adım")
    near(s["training_effect_aerobic"], 3.8, 0.001, "aerobik etki")
    near(s["training_effect_anaerobic"], 1.6, 0.001, "anaerobik etki")
    ok(s["vo2max"] == 46 and s["recovery_time"] == 38 * 3600 and s["training_load"] == 142, "VO2max / toparlanma / yük")
    ok(s["vitality_gain"] == 14 and s["ground_contact_time_avg_ms"] == 262 and "time_goal" not in s, "kuyruk alanları")
    ok(parse.parse(reversion(files["workout_summary"][0], 7))[1] is None, "koşu özeti v7 desteklenmiyor")
    cut = parse_more.workout_summary(files["workout_summary"][0][:60])
    ok(cut["truncated"] and cut["duration"] == 3000, "kırpık özet çökmez")

    kind, d = parse.parse(files["workout_details"][0])
    ok(kind == "workout_details" and len(d) == 3000, "koşu saniyelik kayıt sayısı")
    ok([r["hr"] for r in d] == truth["hr"], "koşu nabız dizisi birebir")
    ok(d[0]["ts"] == truth["start"] and d[1500]["ts"] - d[1499]["ts"] == 41 and d[-1]["ts"] == truth["end"] - 1,
       "duraklama yeni bölüm açar")
    ok(sum(r["distance_dm"] for r in d) == 80000 and sum(r["steps"] for r in d) == truth["steps"], "mesafe / adım toplamı")
    ok(sum(r["km_mark"] for r in d) == 8, "kilometre işaretleri")
    near(sum(r["speed"] for r in d) / len(d), 8000 / 3000, 0.05, "saniyelik hız ortalaması")
    col = parse_more.series(d)
    ok(set(col) == {"ts", "hr", "speed", "pace", "cadence", "stride", "distance", "steps", "kcal"}, f"seri sütunları {set(col)}")
    ok(col["distance"][-1] == 8000.0 and col["kcal"][-1] == truth["kcal"], "birikimli seriler")
    ok(parse.parse(reversion(files["workout_details"][0], 7))[1] is None, "koşu ayrıntısı v7 desteklenmiyor")
    ok(parse_more.workout_details(files["workout_details"][0][:5000] + b"\0\0\0\0") is None, "kırpık ayrıntı None")

    kind, g = parse.parse(files["workout_gps"][0])
    ok(kind == "workout_gps" and len(g) == 3000 and [p["ts"] for p in g] == [r["ts"] for r in d], "GPS nokta zamanları")
    length = sum(haversine(a, b) for a, b in zip(g, g[1:]))
    near(length, 8000, 240, "GPS iz uzunluğu")
    ok(haversine(g[0], g[-1]) < 30, "iz başladığı yere döner")
    ok(all(abs(p["lat"] - 39.92) < 0.01 and abs(p["lon"] - 32.85) < 0.012 for p in g), "enlem/boylam sırası doğru")
    near(sum(p["speed"] for p in g) / len(g), 8000 / 3000, 0.05, "GPS hızı")
    ok(30 < g[0]["alt"] < 55 and g[0]["hdop"] == 0.9, "yükseklik / hdop")
    ok(parse.parse(reversion(files["workout_gps"][0], 4))[1] is None, "GPS v4 desteklenmiyor")
    # v2 ve v1 kayıt boyutları (18 / 12 bayt)
    v2 = testfiles.wrap(1, 2, 1, 0x16, 2, b"\0" + struct.pack("<ifffH", 100, 29.5, 41.25, 4.0, 31 << 4))
    ok(parse_more.workout_gps(v2) == [{"ts": 100, "lat": 41.25, "lon": 29.5, "accuracy": 4.0, "speed": 3.1}], "GPS v2")
    v1 = testfiles.wrap(1, 1, 1, 0x16, 2, b"\0" + struct.pack("<iff", 100, 29.5, 41.25) * 2)
    ok(parse_more.workout_gps(v1) == [{"ts": 100, "lat": 41.25, "lon": 29.5}] * 2, "GPS v1")


def test_treadmill() -> None:
    files, truth = testfiles.treadmill_files()
    kind, s = parse.parse(files["workout_summary"][0])
    ok(s["sport"] == "treadmill" and s["duration"] == 1500 and s["distance"] == 3600, "bant özeti")
    ok(s["vo2max"] == 46 and s["vo2max_level"] == 3 and s["recovery_time"] == 17 * 3600, "bant VO2max / toparlanma")
    ok(s["training_load"] == 61 and s["vitality_gain"] == 9 and s["distance_goal"] == 3500, "bant kuyruk alanları")
    near(s["training_effect_anaerobic"], 0.4, 0.001, "bant anaerobik etki")
    ok(s["cadence_avg"] == truth["values"]["CADENCE_AVG"] and s["steps"] == truth["steps"], "bant kadans / adım")
    kind, d = parse.parse(files["workout_details"][0])
    ok(len(d) == 1500 and [r["hr"] for r in d] == truth["hr"], "bant saniyelik nabız")
    ok(sum(r["distance_dm"] for r in d) == 36000 and sum(r["steps"] for r in d) == truth["steps"], "bant mesafe / adım")
    near(sum(r["speed"] for r in d), 3600, 1, "bant: hızların toplamı mesafeyi verir")
    ok("workout_gps" not in files, "bantta GPS yok")


# --- kayıt katmanı -------------------------------------------------------------
def all_files() -> list[bytes]:
    out = [testfiles.sleep_file()[0], testfiles.sleep_stages_file(), testfiles.manual_file()]
    out += [f for f, _ in testfiles.run_files()[0].values()] + [f for f, _ in testfiles.treadmill_files()[0].values()]
    return out


def check_store(st: Store) -> None:
    night = st.sleep(date(2026, 11, 20))
    ok(night["date"] == "2026-11-20" and night["bed_time"] == testfiles.NIGHT_BED, "gece, bittiği güne yazılır")
    ok("rr" not in night and night["hrv"] == [] and night["rmssd"] is None, "gece: RR paketi yok, hrv boş")
    ok(night["source"] == "sleep_summary v4" and night["naps"] == 0 and night["stages_inferred"], "gece kaynağı")
    ok(len(night["stages"]) == 19 and night["duration"] == 8 * 3600 and night["deep"] == 110 * 60, "gece evreleri")
    ok(st.sleep(date(2026, 11, 20), rr=True)["rr"] == [], "rr istenirse gelir")
    nap = st.sleep(date(2026, 11, 19))
    ok(nap["bed_time"] == testfiles.NAP_BED and nap["rmssd"] is None, "öğle uykusu 19'unda")
    ok(st.sleep(date(2026, 11, 18)) is None, "kayıt yoksa None")
    json.dumps(night)
    row = st.db.execute("SELECT day, duration, deep, light, rem, awake, score, rmssd FROM sleep WHERE bed_time = ?",
                        (testfiles.NIGHT_BED,)).fetchone()
    ok(row[0] == "2026-11-20" and row[1] == night["duration"] and row[6] is None and row[7] is None, "uyku satırı")

    ws = st.workouts()
    ok([w["id"] for w in ws] == [testfiles.MILL_START, testfiles.RUN_START], "antrenmanlar yeniden eskiye")
    ok(ws[0]["sport"] == "treadmill" and not ws[0]["has_gps"] and ws[0]["has_series"], "bant satırı")
    ok(ws[1]["sport"] == "running" and ws[1]["has_gps"] and ws[1]["distance"] == 8000, "koşu satırı")
    ok("series" not in ws[1] and "track" not in ws[1], "listede yalnız özet")
    w = st.workout(testfiles.RUN_START)
    ok(len(w["series"]["ts"]) == 3000 and len(w["track"]["lat"]) == 3000, "koşu ayrıntısı")
    ok(w["track"]["hr"] == w["series"]["hr"] and w["duration"] == 3000, "ize nabız eşlenir")
    ok(st.workout(testfiles.MILL_START)["track"] is None and st.workout(5) is None, "bant izi yok / olmayan kimlik")
    json.dumps(w)

    root = ET.fromstring(st.workout_gpx(testfiles.RUN_START))
    ns = {"g": "http://www.topografix.com/GPX/1/1", "x": "http://www.garmin.com/xmlschemas/TrackPointExtension/v1"}
    pts = root.findall("g:trk/g:trkseg/g:trkpt", ns)
    ok(root.tag == "{http://www.topografix.com/GPX/1/1}gpx" and len(pts) == 3000, "GPX nokta sayısı")
    ok(all(-90 <= float(p.get("lat")) <= 90 and p.find("g:time", ns).text.endswith("Z") for p in pts), "GPX nokta alanları")
    first = datetime.strptime(pts[0].find("g:time", ns).text + "+0000", "%Y-%m-%dT%H:%M:%SZ%z")
    ok(int(first.timestamp()) == testfiles.RUN_START, "GPX zamanı UTC")
    hrs = [int(p.find("g:extensions/x:TrackPointExtension/x:hr", ns).text) for p in pts]
    ok(hrs == w["series"]["hr"], "GPX nabız uzantısı")
    ok(pts[0].find("g:ele", ns) is not None, "GPX yükseklik")
    ok(st.workout_gpx(testfiles.MILL_START) is None and st.workout_gpx(5) is None, "GPS yoksa GPX yok")

    m = st.manual(date(2026, 11, 19))
    ok([(x["type"], x["value"]) for x in m] == [("hr", 72), ("spo2", 97), ("stress", 34)], "19'undaki elle ölçümler")
    ok(len(st.manual(date(2026, 11, 20))) == 2 and st.manual(date(2026, 11, 18)) == [], "20'si / boş gün")
    ok(st.pending() == [], "çözülemeyen dosya kalmadı")


def test_store(tmp: Path) -> Store:
    raw = tmp / "raw"
    raw.mkdir()
    st = Store(tmp / "t.db", raw)
    st.upgrade()
    files = all_files()
    for data in reversed(files):  # GPS ve ayrıntı özetten önce gelse de aynı satırda birleşmeli
        st.save_file(data[:7], data)
    ok(len(list(raw.glob("*.bin"))) == len(files), "ham dosyalar yazıldı")
    check_store(st)
    for data in files:  # aynı dosyalar yeniden gelirse kayıtlar çoğalmaz
        st.save_file(data[:7], data)
    ok(st.db.execute("SELECT COUNT(*) FROM sleep").fetchone()[0] == 2, "uyku satırları çoğalmaz")
    ok(st.db.execute("SELECT COUNT(*) FROM workouts").fetchone()[0] == 2, "antrenman satırları çoğalmaz")
    ok(st.reparse() == 0, "işlenmiş dosyalar yeniden çözülmez")
    # henüz bitmemiş gece sonradan biten kaydı ezmez
    early = dict(parse.parse(testfiles.sleep_file()[0])[1], wake_time=testfiles.REAL_WAKE - 3600, stages=[])
    with st.lock:
        st._ingest("sleep_summary", {"version": 4}, early)
    ok(st.sleep(date(2026, 11, 20))["wake_time"] == testfiles.REAL_WAKE, "erken kayıt geç kaydı ezmez")
    # RR'li (v5) gece: RMSSD satıra ve 5 dakikalık HRV dizisine yansır; gerçek evreli kayıt tahminle ezilmez
    v5 = parse.parse(testfiles.sleep_file_v5()[0])[1]
    inferred = dict(parse.parse(testfiles.sleep_file()[0])[1], wake_time=v5["wake_time"])
    with st.lock:
        st.db.execute("DELETE FROM sleep WHERE bed_time = ?", (v5["bed_time"],))
        st._ingest("sleep_summary", {"version": 5}, v5)
        st._ingest("sleep_summary", {"version": 4}, inferred)
    night = st.sleep(date(2026, 11, 20))
    ok(night["source"] == "sleep_summary v5" and 20 <= night["rmssd"] <= 80 and len(night["hrv"]) > 80, "v5 gece: RMSSD + hrv")
    ok(len(st.sleep(date(2026, 11, 20), rr=True)["rr"]) == 3 and night["score"] == 86, "v5 gece: rr / puan")
    with st.lock:
        st.db.execute("DELETE FROM sleep WHERE bed_time = ?", (v5["bed_time"],))
        st._ingest("sleep_summary", {"version": 4}, parse.parse(testfiles.sleep_file()[0])[1])
        st.db.commit()

    # eski davranış: gerçek günlük dosyalar aynı yoldan işlenmeye devam eder
    real = sorted((HERE / "raw").glob("*_daily_details_*.bin"))
    if real:
        data = real[0].read_bytes()
        ok(st.save_file(data[:7], data) == "daily_details", "günlük dosya türü")
        day = date.fromtimestamp(parse.file_id(data)["ts"])
        mins = st.minutes(day)
        ok(len(mins["hr"]) == 1440 and "zaman,steps" in st.csv("minutes"), "minutes / csv çalışıyor")
        ok(st.db.execute("SELECT COUNT(*) FROM minutes").fetchone()[0] > 0, "dakikalar yazıldı")
    return st


def test_reparse(tmp: Path) -> None:
    """Çözücü yokken inmiş dosyalar (files.parsed = 0 ya da hiç kayıt yok) açılışta işlenir."""
    raw = tmp / "raw2"
    testfiles.generate(raw, everything=True)
    st = Store(tmp / "t2.db", raw)
    data = testfiles.sleep_file()[0]
    st.db.execute("INSERT INTO files VALUES (?,?,?,?,?,0)", (data[:7].hex(), "sleep_summary", 0, len(data), 0))
    st.db.commit()
    ok(len(st.pending()) == 1, "bekleyen dosya")
    st.upgrade()
    check_store(st)
    ok(st.db.execute("SELECT COUNT(*) FROM files WHERE parsed = 1").fetchone()[0] == 8, "files tablosu güncellendi")
    st.upgrade()
    ok(st.reparse() == 0, "ikinci açılışta iş yok")


# --- HTTP ----------------------------------------------------------------------
def test_http(st: Store) -> None:
    """server.py'yi içe aktarmadan Handler sınıfını kaynak metninden alır, sahte ortamda rastgele bir kapıda dener."""
    import secrets
    from http import cookies
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    src = (HERE / "server.py").read_text()
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "Handler")
    ns = {"BaseHTTPRequestHandler": BaseHTTPRequestHandler, "cookies": cookies, "secrets": secrets, "json": json,
          "urlparse": urlparse, "parse_qs": parse_qs, "date": date, "datetime": datetime, "store": st,
          "config": {"token": "x"}, "WEB": HERE / "yok", "TYPES": {}, "i18n": i18n}
    exec("from __future__ import annotations\n" + ast.get_source_segment(src, node), ns)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ns["Handler"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(path):
        try:
            with urllib.request.urlopen(base + path) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    try:
        code, _, body = get("/api/sleep?d=2026-11-20")
        ok(code == 200 and json.loads(body)["bed_time"] == testfiles.NIGHT_BED, "/api/sleep")
        ok(json.loads(get("/api/sleep?d=2026-11-18")[2]) is None, "/api/sleep boş gün -> null")
        ok(get("/api/sleep?d=abc")[0] == 400, "/api/sleep geçersiz tarih")
        ws = json.loads(get("/api/workouts")[2])
        ok([w["id"] for w in ws] == [testfiles.MILL_START, testfiles.RUN_START], "/api/workouts")
        w = json.loads(get(f"/api/workout?id={testfiles.RUN_START}")[2])
        ok(len(w["track"]["ts"]) == 3000 and len(w["series"]["hr"]) == 3000, "/api/workout")
        ok(get("/api/workout?id=7")[0] == 404 and get("/api/workout?id=x")[0] == 400, "/api/workout hataları")
        code, headers, body = get(f"/api/workout.gpx?id={testfiles.RUN_START}")
        ok(code == 200 and headers["Content-Type"].startswith("application/gpx+xml")
           and ".gpx" in headers["Content-Disposition"], "/api/workout.gpx başlıkları")
        ok(len(ET.fromstring(body).findall(".//{http://www.topografix.com/GPX/1/1}trkpt")) == 3000, "/api/workout.gpx")
        ok(get(f"/api/workout.gpx?id={testfiles.MILL_START}")[0] == 404, "GPS yoksa 404")
        ok(len(json.loads(get("/api/manual?d=2026-11-19")[2])) == 3, "/api/manual")
        ok(get("/api/minutes?d=2026-11-19")[0] == 200 and get("/api/pending")[0] == 200, "eski uçlar çalışıyor")
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    test_sleep()
    print("uyku çözücüsü tamam")
    test_sleep_validity()
    print("uyku başlık/sürüm dalları tamam")
    test_sleep_real_layout()
    print("gerçek düzenli (v4) sentetik gece tamam")
    print("gerçek uyku dosyası tamam" if test_real_night() else "gerçek uyku dosyası yok, atlandı")
    test_manual()
    print("elle ölçümler tamam")
    test_run()
    print("açık hava koşusu tamam")
    test_treadmill()
    print("koşu bandı tamam")
    with tempfile.TemporaryDirectory() as d:
        store = test_store(Path(d))
        print("kayıt katmanı tamam")
        test_reparse(Path(d))
        print("açılışta yeniden çözme tamam")
        test_http(store)
        print("HTTP uçları tamam")
        ok(len(testfiles.generate(Path(d) / "out")) == 7, "python testfiles.py <klasör> 7 dosya yazar")
    print(f"TAMAM: {checks} denetim geçti")
