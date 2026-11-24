"""Uyku, antrenman ve elle ölçüm dosyalarının çözücüleri.

Biçim kaynağı Gadgetbridge'dir (service/devices/xiaomi/activity/impl/*.java); buradaki her işlev
ilgili Java sınıfının okuma sırasını aynen izler. Java'nın atladığı alanlar burada da atlanır.

Ortak dosya yapısı: 7 bayt dosya kimliği + 1 dolgu baytı (0) + gövde + 4 bayt CRC32 (LE).

Birimler (aksi yazmıyorsa): zaman damgası = unix saniyesi, süre = saniye, mesafe = metre,
hız = m/s, tempo = saniye/km, nabız = atım/dk, RR aralığı = milisaniye.
Desteklenmeyen sürümlerde işlevler None döndürür.
"""
from __future__ import annotations

import math
import struct

import workout_fields
from parse import _Groups, file_id

# --- uyku ---------------------------------------------------------------
STAGE_NAMES = {0: "awake", 1: "light", 2: "deep", 3: "rem", 4: "not_sleep"}  # SleepDetailsParser.decodeStage
GB_STAGE_NAMES = {5: "awake", 3: "light", 2: "deep", 4: "rem", 0: "not_sleep"}  # eski dosyadaki ham kodlar
_PACKET_MAGIC = b"\xfb\xfa\xfc\xff"  # Java: buf.getInt() == 0xfffcfafb (LE)
_NO_DATA_TYPES = (0x2, 0x3, 0x9, 0xC, 0xD, 0xE, 0xF, 0x12)


def _valid(header: bytes, i: int) -> bool:
    """XiaomiActivityParser.validData: i. alanın geçerlilik biti (MSB önce)."""
    return i // 8 < len(header) and bool(header[i // 8] & (1 << (7 - i % 8)))


def rmssd(segments: list[list[int]]) -> float | None:
    """RR aralıklarından (ms) RMSSD (ms).

    Süzme: 300–2000 ms dışındaki aralıklar atılır; ardışık iki geçerli aralığın farkı, öncekinin
    %20'sinden büyükse o fark sayılmaz (ektopik atım / kaçan atım). Parçalar (segment) arası fark alınmaz.
    En az 30 geçerli fark yoksa None.
    """
    total, n = 0.0, 0
    for seg in segments:
        prev = None
        for rr in seg:
            if not 300 <= rr <= 2000:
                prev = None
                continue
            if prev is not None:
                d = rr - prev
                if abs(d) <= 0.2 * prev:
                    total += d * d
                    n += 1
            prev = rr
    return round(math.sqrt(total / n), 1) if n >= 30 else None


def hrv_series(rr: list[dict], window: int = 300) -> list[list]:
    """RR parçalarından pencere başına RMSSD: [[pencere başlangıcı (unix s), rmssd ms], ...]."""
    buckets: dict[int, list[list[int]]] = {}
    for seg in rr:
        t = seg["ts_ms"]
        cur_key, cur = None, None
        for v in seg["rr"]:
            key = int(t // 1000) // window * window
            if key != cur_key:
                cur_key, cur = key, []
                buckets.setdefault(key, []).append(cur)
            cur.append(v)
            t += v
    out = []
    for key in sorted(buckets):
        v = rmssd(buckets[key])
        if v is not None:
            out.append([key, v])
    return out


def _series_block(data: bytes, pos: int, version: int, bed_time: int, size: int, fmt: str | None):
    """Uyku dosyasındaki örnek dizisi: u16 birim, u16 adet, (v>=2 ve adet>0) u32 ilk kayıt zamanı, adet × örnek.

    Java bu örnekleri okumadan atlar; yerleşim Java'daki yorumlardan alınmıştır
    (örnek zamanı = ilk kayıt zamanı + birim × sıra).
    """
    unit, count = struct.unpack_from("<hh", data, pos)
    pos += 4
    block = None
    if count > 0:
        first = bed_time
        if version >= 2:
            (first,) = struct.unpack_from("<I", data, pos)
            pos += 4
        raw = data[pos : pos + count * size]
        if len(raw) < count * size:
            raise struct.error("örnek dizisi dosyadan uzun")
        values = list(raw) if fmt is None else [round(v, 3) for v in struct.unpack(f"<{count}{fmt}", raw)]
        block = {"start": first, "step": unit, "values": values}
        pos += count * size
    return block, pos


def _sleep_head(data: bytes, version: int, header: bytes, shifted: bool):
    """Uyku dosyasının sabit alanları + örnek dizileri. (alanlar sözlüğü, dizilerden sonraki konum)

    shifted=False: Java'nın başlık sırası (v4'te puan 3., nabız 4., SpO2 5., horlama 6. bit; puan baytı
                   yalnız biti açıksa var).
    shifted=True : gerçek Band 9 Pro v4 dosyasında görülen sıra: puan baytı her zaman var ve biti 2.,
                   nabız 3., SpO2 4., horlama 5. bit (Java'ya göre bir bit erken).
    """
    pos = 8 + len(header)
    is_awake, bed_time, wake_time = struct.unpack_from("<Bii", data, pos)
    pos += 9
    idx = 2 if shifted else 3
    score = into_bed = None
    if version >= 4:
        if shifted or _valid(header, idx):
            score = data[pos] if _valid(header, idx) else None
            pos += 1
        idx += 1
    if version >= 5:
        pos += 9  # doğrulanmamış üst veri
        into_bed, _wake2 = struct.unpack_from("<ii", data, pos)
        pos += 8
        idx += 5
    hr = spo2 = snore = None
    if _valid(header, idx):
        hr, pos = _series_block(data, pos, version, bed_time, 1, None)
    idx += 1
    if _valid(header, idx):
        spo2, pos = _series_block(data, pos, version, bed_time, 1, None)
    idx += 1
    if version >= 3:
        if _valid(header, idx):
            snore, pos = _series_block(data, pos, version, bed_time, 4, "f")
    return {"bed_time": bed_time, "wake_time": wake_time, "into_bed_time": into_bed, "is_awake": is_awake == 1,
            "score": score or None, "hr": hr, "spo2": spo2, "snore": snore}, pos


def sleep_details(data: bytes) -> dict | None:
    """Uyku dosyası (alt tür 8; sürüm 1–5). SleepDetailsParser.java + gerçek Band 9 Pro (v4) dosyası.

    Dönen sözlük:
      bed_time, wake_time      unix s
      into_bed_time            unix s (yalnız v5; uykuya dalmadan önce yatağa giriş), yoksa None
      is_awake                 True ise uyku henüz bitmemiş (dosya ara kayıt)
      score                    uyku puanı 1–100; alan geçersizse ya da 0 ise None
      duration                 uyku süresi, s (uyanıklık hariç)
      awake, light, rem, deep  saniye
      wake_count               uyanma sayısı
      sleep_index              16 no'lu paketteki son kaydın sıra numarası (uyku bölümü sayısı), yoksa None
      stages                   [{"ts", "end", "stage"}]; stage: awake|light|deep|rem|not_sleep|unknown
      stages_inferred          True: saat evre listesi (17 no'lu paket) göndermemiş; evreler uykuya dalma /
                               uyanma olaylarından (2 ve 3 no'lu paketler) ÇIKARILDI: olay çiftinin arası
                               "light", 5 dakikadan kısa boşluk "awake", daha uzun boşluk "deep".
                               Bu bir tahmindir (bkz. _infer_stages).
      segments                 [{"ts","end"}] saatin bildirdiği ham uyku bölümleri (2/3 no'lu olay çiftleri)
      hr, spo2, snore          {"start": unix s, "step": saniye, "values": [...]} ya da None; 0 = ölçüm yok
                               (Java bunları atlar; hr atım/dk, spo2 %, snore float – birimi bilinmiyor)
      breathing                [[unix s, değer], ...]  0x0a paketi; Java "soluk/dk" der, gerçek dosyada
                               seyrek olay kayıtlarına benziyor (güvenme)
      rr                       [{"ts_ms": ilk atımın zamanı (ms), "rr": [aralık ms, ...]}, ...]  (1 no'lu paket,
                               10 ms çözünürlük; Band 9 Pro v4 dosyasında bu paket yok)
      rmssd                    gecelik RMSSD (ms) ya da None
      packets                  {paket türü: adet} – bu dosyada görülen paketler
      complete                 False ise paketler okunurken dosya sonuna taşıldı (eldekiler korunur)

    Paket zaman damgaları: 2/3 milisaniye, 16/17 saniye, diğerleri nanosaniye (1 no'lu RR paketi Java'da ms).
    """
    fid = file_id(data)
    version = fid["version"]
    header_size = {1: 1, 2: 1, 3: 1, 4: 1, 5: 2}.get(version)
    if header_size is None:
        return None
    header = data[8 : 8 + header_size]
    end = len(data)
    first_packet = data.find(_PACKET_MAGIC, 8 + header_size)
    # Diziler tam olarak ilk paketin (paket yoksa CRC'nin) başında bitmeli; hangi başlık sırası bunu
    # sağlıyorsa o doğrudur. Hiçbiri sağlamıyorsa Java'nın sırası kullanılır.
    target = first_packet if first_packet >= 0 else end - 4
    head = None
    for shifted in ((False, True) if version >= 4 else (False,)):
        try:
            cand = _sleep_head(data, version, header, shifted)
        except (struct.error, IndexError):
            continue
        if cand[1] == target:
            head = cand
            break
        head = head or cand
    if head is None:
        return None
    fields, pos = head
    bed_time, wake_time = fields["bed_time"], fields["wake_time"]

    out = {**fields, "duration": None, "awake": None, "light": None, "rem": None, "deep": None,
           "sleep_index": None, "wake_count": None, "stages": [], "stages_inferred": False, "segments": [],
           "breathing": [], "rr": [], "rmssd": None, "packets": {}, "complete": True, "version": version}
    out = {k: out[k] for k in ("bed_time", "wake_time", "into_bed_time", "is_awake", "score", "duration", "awake",
                               "light", "rem", "deep", "sleep_index", "wake_count", "stages", "stages_inferred",
                               "segments", "hr", "spo2", "snore", "breathing", "rr", "rmssd", "packets",
                               "complete", "version")}

    # Paketler. Java CRC'yi ayırmadan tarar; burada da öyle.
    last_pulse = 0   # ms; 10 dakikalık RR paketleri arasında sürekliliği korur
    events = []      # (tür 2 | 3, unix s)
    summary = None   # son 16 no'lu paketin kayıtları (paket birikimlidir, sonuncusu geçerlidir)
    staged = None    # son dolu 17 no'lu paket
    while end - pos >= 17:
        found = data.find(_PACKET_MAGIC, pos)
        if found < 0 or end - found < 17:
            break
        pos = found + 4
        _header_len, ts, _parity, ptype = struct.unpack_from("<BqBB", data, pos)
        (data_len,) = struct.unpack_from(">H", data, pos + 11)
        pos += 13
        out["packets"][ptype] = out["packets"].get(ptype, 0) + 1
        if ptype in _NO_DATA_TYPES:  # uzunluk baytları burada bayraktır, ardından veri gelmez
            if ptype in (2, 3):
                events.append((ptype, ts // 1000))
            continue
        if pos + data_len > end:
            out["complete"] = False
            break
        body = data[pos : pos + data_len]
        pos += data_len
        if ptype == 1:  # RR aralıkları, 10 ms birim; ts Java'da milisaniye sayılır
            if abs(last_pulse - ts) > 30000:
                last_pulse = ts
                out["rr"].append({"ts_ms": ts, "rr": []})
            out["rr"][-1]["rr"].extend(10 * b for b in body)
            last_pulse += 10 * sum(body)
        elif ptype == 16 and data_len >= 13:
            # 13 baytlık kayıtlar (BE): bayt 0 = sıra (üst 4 bit) + uyanma sayısı (alt 4 bit), sonra dakika
            # olarak uyku / uyanık / hafif / REM / derin, bayrak baytı, önceki bölümle aradaki boşluk (dk).
            # Java yalnız ilk kaydı okur; gerçek dosyada her uyku bölümü için bir kayıt var.
            summary = [struct.unpack_from(">BHHHHHBB", body, i) for i in range(0, data_len - 12, 13)]
        elif ptype == 17 and data_len >= 2:  # evreler: u16 BE = evre (4 bit) + süre dakika (12 bit); ts saniye
            t, staged = ts, []
            for (val,) in struct.iter_unpack(">H", body[: data_len // 2 * 2]):
                minutes = val & 0xFFF
                staged.append({"ts": t, "end": t + minutes * 60, "stage": STAGE_NAMES.get(val >> 12, "unknown")})
                t += minutes * 60
        elif ptype == 0x0A:
            # 4 baytlık kayıtlar: [saniye (alt bayt), değer, ?, üst 4 bit = saniyenin 256'lık sayfası].
            # Gerçek dosyada paket zamanı (ns) 10 dakikalık pencerenin BAŞIDIR ve kayıt zamanı
            # pencere başı + sayfa × 256 + alt bayttır (hep < 600). Java pencere sonu sayıp eşit aralıkla
            # dağıtıyordu. Değer için Java'nın 1–40 süzgeci korunur.
            start = ts // 1_000_000_000
            for i in range(0, data_len - 3, 4):
                rate = body[i + 1]
                if 1 <= rate <= 40:
                    out["breathing"].append([start + (body[i + 3] >> 4) * 256 + body[i], rate])
        # 0 (ivme, 150 bayt = 600 × 2 bit), 0x0b (8 baytlık kayıtlar), 0x65 (işaret) ve bilinmeyenler atlanır

    # uyku bölümleri: uykuya dalma (2) / uyanma (3) çiftleri
    start = None
    for ptype, t in events:
        if ptype == 2:
            start = t
        elif start is not None and t > start:
            out["segments"].append({"ts": start, "end": t})
            start = None
    if start is not None and wake_time > start:
        out["segments"].append({"ts": start, "end": wake_time})

    if summary:
        out["sleep_index"] = summary[-1][0] >> 4
        out["wake_count"] = sum(r[0] & 0x0F for r in summary)
        out.update(duration=sum(r[1] for r in summary) * 60, awake=sum(r[2] for r in summary) * 60,
                   light=sum(r[3] for r in summary) * 60, rem=sum(r[4] for r in summary) * 60,
                   deep=sum(r[5] for r in summary) * 60)
    if staged:
        out["stages"] = staged
    elif out["segments"]:
        out["stages"] = _infer_stages(out["segments"])
        out["stages_inferred"] = True
    if out["stages"] and not (summary and (out["light"] or out["deep"] or out["rem"])):
        # özet paketi evre sürelerini vermiyorsa (gerçek dosyada hepsi 0) evrelerden hesaplanır
        tot = {"awake": 0, "light": 0, "rem": 0, "deep": 0}
        for s in out["stages"]:
            if s["stage"] in tot:
                tot[s["stage"]] += s["end"] - s["ts"]
        out.update(tot, duration=tot["light"] + tot["rem"] + tot["deep"],
                   wake_count=sum(1 for s in out["stages"] if s["stage"] == "awake"))
    out["rmssd"] = rmssd([seg["rr"] for seg in out["rr"]])
    return out


_SHORT_GAP = 300  # s


def _infer_stages(segments: list[dict]) -> list[dict]:
    """Saat evre listesi göndermediğinde uyku bölümlerinden evre TAHMİNİ.

    Gerçek dosyada (Band 9 Pro, v4) 17 no'lu evre paketi boş geliyor; elde yalnız "uykuya daldı / uyandı"
    olay çiftleri var. Bölümler arasındaki boşluklarda bilek hareketi ve nabız uyku bölümlerinden farklı
    değil (daha da durgun), boşlukların toplamı Mi Fitness'ın aynı kişi için gösterdiği derin uyku
    toplamlarıyla (gecelik ~%20–28) örtüşüyor ve Mi Fitness kayıtları da hafif/derin dönüşümlü, hafifle
    başlayıp hafifle biten parçalardan oluşuyor. Bu yüzden: bölüm = hafif uyku, 5 dakikadan uzun boşluk =
    derin uyku, daha kısa boşluk = uyanma (saat de 16 no'lu özetinde bu kısa boşlukları bölüme katıyor).
    Kesin değildir; Mi Fitness'ın o gece için gösterdiğiyle karşılaştırılmalı.
    """
    stages = []
    for i, seg in enumerate(segments):
        if i and seg["ts"] > segments[i - 1]["end"]:
            gap = seg["ts"] - segments[i - 1]["end"]
            stages.append({"ts": segments[i - 1]["end"], "end": seg["ts"],
                           "stage": "awake" if gap < _SHORT_GAP else "deep"})
        stages.append({"ts": seg["ts"], "end": seg["end"], "stage": "light"})
    return stages


def sleep_stages(data: bytes) -> dict | None:
    """Eski biçimli uyku evreleri dosyası (alt tür 3, yalnız sürüm 2). SleepStagesParser.java.

    Dönen sözlük sleep_details ile aynı anahtarları taşır (olmayanlar None/boş).
    Evre kaydı = (başlangıç zamanı, evre); bitiş bir sonraki kaydın başlangıcı, sonuncuda uyanma zamanıdır.
    Yatış/kalkış ya da süre 0 ise (Java da böyle dosyayı kaydetmez) None döner.
    """
    fid = file_id(data)
    if fid["version"] != 2:
        return None
    end = len(data) - 4
    try:
        sleep_d, bed_time, wake_time = struct.unpack_from("<hii", data, 8 + 7)
        deep_d, light_d, rem_d, wake_d = struct.unpack_from("<hhhh", data, 8 + 7 + 10 + 3)
    except struct.error:
        return None
    if bed_time == 0 or wake_time == 0 or sleep_d == 0:
        return None
    pos = 8 + 7 + 10 + 3 + 8 + 1
    stages = []
    while pos + 5 <= end:
        t, phase = struct.unpack_from("<iB", data, pos)
        pos += 5
        stages.append({"ts": t, "end": None, "stage": GB_STAGE_NAMES.get(phase, "unknown")})
    for a, b in zip(stages, stages[1:]):
        a["end"] = b["ts"]
    if stages:
        stages[-1]["end"] = max(wake_time, stages[-1]["ts"])
    return {"bed_time": bed_time, "wake_time": wake_time, "into_bed_time": None, "is_awake": False, "score": None,
            "duration": sleep_d * 60, "awake": wake_d * 60, "light": light_d * 60, "rem": rem_d * 60,
            "deep": deep_d * 60, "sleep_index": None, "wake_count": None, "stages": stages,
            "stages_inferred": False, "segments": [], "hr": None, "spo2": None, "snore": None, "breathing": [],
            "rr": [], "rmssd": None, "packets": {}, "complete": pos == end, "version": 2}


# --- elle ölçümler --------------------------------------------------------
MANUAL_TYPES = {1: "hr", 2: "spo2", 3: "stress", 4: "temperature"}


def manual_samples(data: bytes) -> list[dict] | None:
    """Saatte elle başlatılan ölçümler (alt tür 6; sürüm 1 ve 2). ManualSamplesParser.java.

    [{"ts": unix s, "type": "hr"|"spo2"|"stress"|"temperature", "value": int}]
    hr atım/dk, spo2 %, stress 0–100; temperature ham int32 (birimi Java'da belirtilmemiş).
    """
    fid = file_id(data)
    end = len(data) - 4
    out = []
    if fid["version"] == 1:
        if len(data) < 19 or data[7] != 0 or data[8] != 0xFF:
            return None
        pos = 9
        if end - pos == 0 or (end - pos) % 6:
            return None
        while pos < end:
            ts, kind, value = struct.unpack_from("<iBB", data, pos)
            pos += 6
            if kind not in (2, 3) or value == 0 or value > 100:
                return None
            out.append({"ts": ts, "type": MANUAL_TYPES[kind], "value": value})
        return out
    if fid["version"] != 2:
        return None
    pos = 8
    while end - pos >= 5:
        ts, tl = struct.unpack_from("<iB", data, pos)
        pos += 5
        length, measurement = tl >> 4, tl & 0x0F
        if length > end - pos:
            break
        if measurement in (1, 2, 3) and length in (1, 2):
            value = int.from_bytes(data[pos : pos + length], "little")
        elif measurement == 4 and length == 4:
            (value,) = struct.unpack_from("<i", data, pos)
        else:
            pos += length
            continue
        pos += length
        if value:
            out.append({"ts": ts, "type": MANUAL_TYPES[measurement], "value": value})
    return out


# --- antrenman özeti --------------------------------------------------------
# XIAOMI_WORKOUT_TYPE kodları. XiaomiWorkoutType.java elimizdeki kaynak kopyasında yok; yalnızca dosya alt
# türleriyle örtüşen kodlar yazıldı, gerisi "type_<kod>" olarak döner.
WORKOUT_TYPES = {1: "running", 2: "walking", 3: "treadmill", 6: "cycling", 7: "indoor_cycling", 8: "freestyle",
                 9: "pool_swimming", 11: "elliptical", 13: "rowing", 14: "jump_rope", 16: "hiit"}
SWIM_STYLES = {0: "medley", 1: "breaststroke", 2: "freestyle", 3: "backstroke", 4: "butterfly"}
_RENAME = {
    "TIME_END": "end", "ACTIVE_SECONDS": "duration", "DISTANCE_METERS": "distance",
    "DISTANCE_METERS_CALIBRATED": "distance_calibrated", "CALORIES_BURNT": "calories",
    "PACE_AVG_SECONDS_KM": "pace_avg", "STEP_LENGTH_AVG": "stride_avg", "STEP_RATE_AVG": "cadence_avg",
    "STEP_RATE_MAX": "cadence_max", "MAXIMUM_OXYGEN_UPTAKE": "vo2max", "V02MAX_LEVEL": "vo2max_level",
    "WORKOUT_LOAD": "training_load", "AVG_GROUND_CONTACT_TIME": "ground_contact_time_avg_ms",
    "MIN_GROUND_CONTACT_TIME": "ground_contact_time_min_ms", "AVG_VERTICAL_RATIO": "vertical_ratio_avg",
    "MIN_VERTICAL_RATIO": "vertical_ratio_min", "AVG_VERTICAL_OSCILLATION": "vertical_oscillation_avg",
    "MIN_VERTICAL_OSCILLATION": "vertical_oscillation_min", "MAX_VERTICAL_OSCILLATION": "vertical_oscillation_max",
    "minSWOLF": "swolf_min", "configuredJumpsGoal": "jumps_goal", "configuredSets": "sets",
}
_ZONES = {"HR_ZONE_WARM_UP": "warm_up", "HR_ZONE_FAT_BURN": "fat_burn", "HR_ZONE_AEROBIC": "aerobic",
          "HR_ZONE_ANAEROBIC": "anaerobic", "HR_ZONE_EXTREME": "extreme"}
_SCALE = {"UNIT_KMPH": 1 / 3.6, "UNIT_SECONDS_PER_M": 1000, "UNIT_CM": 0.01, "UNIT_MM": 0.001, "UNIT_HOURS": 3600}
_SIZES = {"int": "<i", "short": "<H", "byte": "<B", "float": "<f"}
_KEEP_ZERO = ("VITALITY_GAIN", "WORKOUT_LOAD")  # Java: ALWAYS_SHOW_KEYS


def workout_summary(data: bytes) -> dict | None:
    """Antrenman özeti (tür 1, ayrıntı 1). WorkoutSummaryParser.java + XiaomiSimpleActivityParser.java.

    Alan sırası workout_fields.py tablolarından gelir (spor alt türü × sürüm). Java geçerlilik bitlerini
    okur ama kullanmaz (tablolardaki bilinmeyen bloklar bit sırasını kaydırdığı için); burada da kullanılmaz.
    Java gibi 0 değerli alanlar atılır (training_load ve vitality_gain hariç).

    Her zaman bulunan anahtarlar:
      start (dosya kimliğindeki başlangıç, unix s; antrenmanın kimliği), sport, sport_code (dosya alt türü),
      version, tz (15 dakikalık dilim sayısı)
    Dosyada varsa:
      end (unix s), duration (etkin süre s), distance (m), calories (etkin kcal), calories_total (kcal),
      hr_avg, hr_max, hr_min (atım/dk), hr_zones {"warm_up","fat_burn","aerobic","anaerobic","extreme"} (s),
      pace_avg, pace_max, pace_min (s/km), speed_avg, speed_max (m/s), steps,
      cadence_avg, cadence_max (adım/dk), stride_avg (m),
      training_effect_aerobic, training_effect_anaerobic (0–5), vo2max (ml/kg/dk), recovery_time (s),
      training_load, vitality_gain, workout_type (ham kod), time_goal (s), calories_goal, distance_goal (m)...
      yüzme: laps, strokes, stroke_rate_avg/max, swolf_avg, swolf_min, pool_length (m), swim_style (ad)
      ip: jumps, jump_rate_avg/max (atlama/dk); kürek: strokes, stroke_rate_avg/max (çekiş/dk)
      diğer Java anahtarları küçük harfle (altitude_*, elevation_gain/loss (m), projected_time_* (s) ...).
    pace_avg / speed_avg dosyada yoksa süre ve mesafeden türetilir.
    truncated=True: dosya tablodan kısa çıktı, o ana kadar okunanlar döndü (Java bu durumda hepsini atar).
    """
    fid = file_id(data)
    sport, tbl = workout_fields.table(fid["subtype"], fid["version"])
    if fid["type"] != 1 or tbl is None:
        return None
    end = len(data)  # Java bu dosyada CRC'yi ayırmaz
    pos = 8 + tbl.header_size
    if pos > end:
        return None
    out = {"start": fid["ts"], "sport": sport, "sport_code": fid["subtype"], "version": fid["version"],
           "tz": fid["tz"]}
    zones = {}
    for kind, key, unit, arg in tbl.entries:
        if kind == "unknown":
            pos += arg
            if pos > end:
                out["truncated"] = True
                break
            continue
        if kind == "unknown_if":
            if data[pos - 1] in arg[1]:
                pos += arg[0]
            continue
        fmt = _SIZES[kind]
        if pos + struct.calcsize(fmt) > end:
            out["truncated"] = True
            break
        (value,) = struct.unpack_from(fmt, data, pos)
        pos += struct.calcsize(fmt)
        if kind == "float" and not math.isfinite(value):
            continue
        value = value * arg if arg != 1 else value
        if key == "XIAOMI_WORKOUT_TYPE":
            out["workout_type"] = value
            # Java: tür alanı (tanınıyorsa) dosya alt türünden gelen adı ezer
            out["sport"] = WORKOUT_TYPES.get(value) or out["sport"] or f"type_{value}"
            continue
        if key == "TIME_START":
            continue  # Java da kullanmaz; başlangıç dosya kimliğinden gelir
        if key == "SWIM_STYLE":
            out["swim_style"] = SWIM_STYLES.get(value, "unknown")
            continue
        if key in _ZONES:
            zones[_ZONES[key]] = value
            continue
        if value == 0 and key not in _KEEP_ZERO and key != "TIME_END":
            continue
        value = value * _SCALE[unit] if unit in _SCALE else value
        out[_RENAME.get(key, key.lower())] = round(value, 3) if isinstance(value, float) else value
    if out["sport"] is None:
        out["sport"] = "unknown"
    if sum(zones.values()):
        out["hr_zones"] = zones
    if out.get("duration") and out.get("distance"):
        out.setdefault("pace_avg", round(out["duration"] / out["distance"] * 1000))
        out.setdefault("speed_avg", round(out["distance"] / out["duration"], 3))
    return out


# --- antrenman GPS izi ------------------------------------------------------
def workout_gps(data: bytes) -> list[dict] | None:
    """GPS izi (tür 1, ayrıntı 2; sürüm 1–3). WorkoutGpsParser.java.

    [{"ts": unix s, "lat": derece, "lon": derece, "speed": m/s, "accuracy": m, "alt": m, "hdop"}]
    v1: yalnız ts/lat/lon; v2: + accuracy, speed; v3: + alt, hdop. Dosyada boylam enlemden önce gelir.
    """
    fid = file_id(data)
    size = {1: 12, 2: 18, 3: 26}.get(fid["version"])
    if size is None:
        return None
    end = len(data) - 4
    pos = 8 + 1  # 1 baytlık başlık
    out = []
    while pos + size <= end:
        ts, lon, lat = struct.unpack_from("<iff", data, pos)
        p = {"ts": ts, "lat": round(lat, 6), "lon": round(lon, 6)}
        if fid["version"] >= 2:
            acc, raw = struct.unpack_from("<fH", data, pos + 12)
            p["accuracy"] = round(acc, 1)
            p["speed"] = ((raw & 0xFFF0) >> 4) / 10.0
        if fid["version"] >= 3:
            alt, hdop = struct.unpack_from("<ff", data, pos + 18)
            p["alt"] = round(alt, 1)
            p["hdop"] = round(hdop, 2)
        out.append(p)
        pos += size
    return out


# --- antrenman ayrıntıları (saniyelik kayıtlar) -------------------------------
_RUN_WALK_BYTES = (1, 1, 1, 1, 1, 4, 1, 1, 2, 2, 2, 2, 2)
_SPEED_WINDOW = 5
_FREESTYLE_V3 = b"\xff\xbb"
_OUTDOOR_CYCLING = b"\xdf\xcf\xfb"
_ELLIPTICAL_V3 = b"\xff\xff"
_ROWING_V4 = b"\xff\xff"
_TREADMILL_V6 = b"\xff\xff\x8b\xff"
_INDOOR_CYCLING_V6 = b"\xdf\xbb\xbb\xbf"
_WALK_V9 = b"\xff\xff\xf8\xbf\xfb\xbb\xbf"


def _record(ts: int) -> dict:
    return {"ts": ts, "hr": 0, "steps": None, "spo2": None, "cadence": None, "speed_raw": None, "speed": None,
            "distance_dm": None, "stride_cm": None, "kcal": None, "km_mark": False, "pace": None}


def _matches(fid: dict, data: bytes, subtype: int, bitmap: bytes) -> bool:
    """Java matchesBitmap: imza aynen uyuyor ya da alt tür doğru ve alan grupları (0x88 bitleri) aynı."""
    sig = data[8 : 8 + len(bitmap)]
    if sig == bitmap:
        return True
    return (fid["subtype"] == subtype and len(sig) == len(bitmap)
            and all(a & 0x88 == b & 0x88 for a, b in zip(sig, bitmap)))


def _speed_from_distance(records: list[dict], first: int) -> None:
    """Java setSpeedFromDistance: saniyelik mesafeden hız; tempo varsa toplam mesafeye ölçeklenir."""
    last = len(records) - 1
    if first > last or records[first]["distance_dm"] is None:
        return
    half = _SPEED_WINDOW // 2
    remaining, paced = 0.0, 0.0
    for i in range(first, last + 1):
        r = records[i]
        remaining += (r["distance_dm"] or 0) / 10.0
        if r["pace"]:
            paced += 1000.0 / r["pace"]
            continue
        a, b = max(first, i - half), min(last, i + half)
        r["speed"] = sum(records[j]["distance_dm"] or 0 for j in range(a, b + 1)) / 10.0 / (b - a + 1)
        remaining -= r["speed"]
    if paced == 0:
        return
    scale = max(0.0, remaining) / paced
    for i in range(first, last + 1):
        r = records[i]
        if r["pace"]:
            r["speed"] = 1000.0 / r["pace"] * scale


def _run_walk(fid: dict, data: bytes) -> list[dict] | None:
    """Açık hava yürüyüş/koşu (alt tür 0x16) v5 ve v8: bit eşlemli gruplar. Java parseRunWalkRecords."""
    v5 = fid["version"] == 5
    bitmap_size, group_count, seg_header = (5, 9, 17) if v5 else (7, len(_RUN_WALK_BYTES), 27)
    if len(data) < 8 + bitmap_size + 4:
        return None
    bitmap = data[8 : 8 + bitmap_size]
    record_size = 0
    for g in range(group_count):
        nib = (bitmap[g // 2] >> 4 if g % 2 == 0 else bitmap[g // 2]) & 0x0F
        if nib & 8:
            record_size += _RUN_WALK_BYTES[g]
    if record_size == 0:
        return None
    end = len(data) - 4
    groups = _Groups(bitmap, data, 8 + bitmap_size)
    records: list[dict] = []
    while groups.pos < end:
        start = groups.pos
        if end - start < seg_header:
            return None
        nr, ts = struct.unpack_from("<ii", data, start + 4)
        groups.pos = start + seg_header
        if nr < 0 or nr * record_size > end - groups.pos:
            return None
        first = len(records)
        for _ in range(nr):
            groups.reset()
            r = _record(ts)
            ts += 1
            for g in range(group_count):
                if not groups.next(_RUN_WALK_BYTES[g] * 8):
                    continue
                if g == 0:
                    r["kcal"], r["steps"] = groups.get(0, 4), groups.get(4, 4)
                elif g == 1:
                    r["hr"] = groups.get(0, 8)
                elif g == 2:
                    r["km_mark"] = groups.get(0, 1) == 1
                elif g == 3:
                    r["distance_dm"] = groups.get(0, 8)
                elif g == 4:
                    if groups.get(0, 8) > 35:  # 35 cm ve altı gürültü sayılır
                        r["stride_cm"] = groups.get(0, 8)
                elif g == 7:
                    r["cadence"] = groups.get(0, 8)
                elif g == 8:
                    r["pace"] = groups.get(0, 16)
            records.append(r)
        _speed_from_distance(records, first)
    return records


def workout_details(data: bytes) -> list[dict] | None:
    """Antrenmanın saniyelik kayıtları (tür 1, ayrıntı 0). WorkoutDetailsParser.java.

    Her kayıt bir saniyedir (duraklama yeni bölüm başlatır, zaman oradan sürer):
      ts (unix s), hr (atım/dk; 0 = yok), steps (o saniyedeki adım), kcal (o saniyedeki kcal), spo2,
      cadence (adım/dk; kürekte çekiş/dk), speed (m/s), speed_raw (ham; birimi düzene göre değişir),
      distance_dm (o saniyede gidilen yol, desimetre), stride_cm, pace (s/km), km_mark (kilometre tamamlandı),
      kürek v4'te bölüm başında: segment_start=True, segment_intensity ("active"|"rest"|"unknown"), segment_strokes.
    Düzen, sürüm ve 8. bayttan başlayan imzaya göre seçilir; tanınmayan imza/sürüm None döndürür.
    """
    fid = file_id(data)
    if fid["type"] != 1 or len(data) < 12:
        return None
    version = fid["version"]
    if fid["subtype"] == 0x16 and version in (5, 8):
        return _run_walk(fid, data)
    b = data
    layout = version
    if version == 1:
        if b[8:10] == b"\xec\xfc":
            sig, rec, layout = b"\xec\xfc", 4, 102
        elif b[8:10] == b"\xcc\xf0":
            sig, rec, layout = b"\xcc\xf0", 3, 101
        else:
            return None
        seg, ts_pos, nr_pos = 12, 8, 4
    elif version == 2:
        sig, seg, rec, ts_pos, nr_pos = b"\xc0", 9, 2, 4, 0
    elif version == 3:
        if b[8:11] == b"\xcc\xcc\xc0":
            sig, seg, rec, ts_pos, nr_pos = b"\xcc\xcc\xc0", 17, 6, 8, 4
        elif _matches(fid, b, 0x08, _FREESTYLE_V3):
            sig, seg, rec, ts_pos, nr_pos, layout = b[8:10], 9, 4, 4, 0, 103
        elif _matches(fid, b, 0x17, _OUTDOOR_CYCLING):
            sig, seg, rec, ts_pos, nr_pos, layout = b[8:11], 17, 7, 8, 4, 113
        elif _matches(fid, b, 0x0B, _ELLIPTICAL_V3):
            sig, seg, rec, ts_pos, nr_pos, layout = b[8:10], 9, 3, 4, 0, 111
        else:
            return None
    elif version == 4:
        if _matches(fid, b, 0x0D, _ROWING_V4):
            sig, seg, rec, ts_pos, nr_pos = b[8:10], 13, 3, 4, 0
        else:
            return None  # EC CC C8 (Mi Band 8 yürüyüş v4) Java'da da çözülmüyor
    elif version == 5:
        if b[8:13] == b"\xec\xcc\xc0\x0c\xc0":
            sig, seg, rec, ts_pos, nr_pos = b[8:13], 17, 8, 8, 4
        elif b[8:13] == b"\xec\xcc\x80\x28\x06":
            sig, seg, rec, ts_pos, nr_pos, layout = b[8:13], 115, 8, 2, 0, 205
        elif _matches(fid, b, 0x17, _OUTDOOR_CYCLING):
            sig, seg, rec, ts_pos, nr_pos, layout = b[8:11], 23, 7, 8, 4, 113
        else:
            return None
    elif version == 6:
        if _matches(fid, b, 0x03, _TREADMILL_V6):
            sig, seg, rec, ts_pos, nr_pos = b[8:12], 13, 12, 4, 0
        elif _matches(fid, b, 0x07, _INDOOR_CYCLING_V6):
            sig, seg, rec, ts_pos, nr_pos, layout = b[8:12], 13, 9, 4, 0, 116
        else:
            return None
    elif version == 9:
        sig, seg, rec, ts_pos, nr_pos, layout = _WALK_V9, 27, 25, 8, 4, 109
    else:
        return None
    if b[8 : 8 + len(sig)] != sig:
        return None

    end = len(data) - 4
    pos = 8 + len(sig)
    records: list[dict] = []
    while pos < end:
        if end - pos < seg:
            break  # v1 sonundaki yarım başlık ya da artık baytlar
        nr, = struct.unpack_from("<i", b, pos + nr_pos)
        ts, = struct.unpack_from("<i", b, pos + ts_pos)
        seg_start = pos
        pos += seg
        if layout == 205:  # başlıkta kayıt sayısı yok: tek bölüm
            nr = (end - pos) // rec
        intensity = strokes = None
        if layout == 4:
            phase = b[seg_start + 8]
            intensity = {0x81: "active", 0x82: "rest"}.get(phase, "unknown")
            (strokes,) = struct.unpack_from("<i", b, seg_start + 9)
            strokes = strokes if strokes > 0 else None
        first = len(records)
        seg_end = min(pos + nr * rec, end)
        first_in_segment = True
        while pos < seg_end:
            if pos + rec > end:  # Java burada taşar ve dosyayı tümden atar
                return None
            r = _record(ts)
            c = b[pos : pos + rec]
            if layout == 4 and first_in_segment:
                r.update(segment_start=True, segment_intensity=intensity, segment_strokes=strokes)
            first_in_segment = False
            if layout == 2:
                r["hr"] = c[0]
            elif layout == 3:
                r["hr"] = c[1]
                (r["speed_raw"],) = struct.unpack_from("<i", c, 2)
            elif layout == 4:
                r["hr"], r["cadence"] = c[0], c[2]
            elif layout == 5:
                r["steps"], r["hr"], r["spo2"], r["cadence"] = c[0], c[1], c[4], c[5]
                # Java: v5'te hız = ham / 36 (20 m/s altındaysa); bu düzende ham hız okunmaz
            elif layout == 6:
                r["steps"], r["hr"], r["distance_dm"], r["cadence"] = c[0] & 0x0F, c[1], c[2], c[9]
                (r["pace"],) = struct.unpack_from("<H", c, 10)
            elif layout in (101, 103, 116):
                r["hr"] = c[1] if layout != 103 else c[0]
            elif layout == 102:
                r["steps"], r["hr"] = c[0] & 0x0F, c[1]
            elif layout == 205:
                r["hr"], r["speed_raw"] = c[1], c[3]  # bant hızı, 0,1 km/sa
                r["speed"] = c[3] / 36.0
            elif layout == 111:
                r["steps"], r["hr"], r["cadence"] = c[0] & 0x0F, c[1], c[2]
            elif layout == 113:
                r["hr"] = c[1]
                r["speed"] = struct.unpack_from("<H", c, 4)[0] / 36.0
            elif layout == 109:
                r["steps"], r["hr"] = c[0] & 0x0F, c[1]
            pos += rec
            records.append(r)
            ts += 1
        _speed_from_distance(records, first)
    return records


def series(records: list[dict]) -> dict:
    """Saniyelik kayıtları sütunlara çevirir; hiç dolmayan sütunlar atılır.

    {"ts": [...], "hr": [atım/dk | None], "speed": [m/s], "pace": [s/km], "cadence": [adım/dk],
     "distance": [birikimli m], "steps": [birikimli], "kcal": [birikimli], "stride": [m], "spo2": [%]}
    """
    out = {"ts": [r["ts"] for r in records], "hr": [r["hr"] or None for r in records]}
    for src, dst, fn in (("speed", "speed", lambda v: round(v, 2)), ("pace", "pace", lambda v: v or None),
                         ("cadence", "cadence", lambda v: v), ("stride_cm", "stride", lambda v: v / 100),
                         ("spo2", "spo2", lambda v: v or None)):
        if any(r[src] is not None for r in records):
            out[dst] = [None if r[src] is None else fn(r[src]) for r in records]
    for src, dst, scale in (("distance_dm", "distance", 0.1), ("steps", "steps", 1), ("kcal", "kcal", 1)):
        if any(r[src] is not None for r in records):
            total, col = 0, []
            for r in records:
                total += r[src] or 0
                col.append(round(total * scale, 1) if scale != 1 else total)
            out[dst] = col
    segs = [{"ts": r["ts"], "intensity": r.get("segment_intensity"), "strokes": r.get("segment_strokes")}
            for r in records if r.get("segment_start")]
    if segs:
        out["segments"] = segs
    return out


# --- GPX --------------------------------------------------------------------
def gpx(name: str, track: list[dict], hr_by_ts: dict[int, int] | None = None) -> str:
    """GPS izinden GPX 1.1 metni. Nokta zamanı UTC; nabız varsa Garmin TrackPointExtension olarak eklenir."""
    from datetime import datetime, timezone
    from xml.sax.saxutils import escape

    hr_by_ts = hr_by_ts or {}
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="bileklik" xmlns="http://www.topografix.com/GPX/1/1"'
             ' xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
             f"<trk><name>{escape(name)}</name><trkseg>"]
    for p in track:
        if p["lat"] == 0 and p["lon"] == 0:
            continue  # konum alınamamış nokta
        t = datetime.fromtimestamp(p["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ele = f"<ele>{p['alt']}</ele>" if p.get("alt") is not None else ""
        hr = hr_by_ts.get(p["ts"]) or p.get("hr")
        ext = (f"<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>{hr}</gpxtpx:hr>"
               f"</gpxtpx:TrackPointExtension></extensions>") if hr else ""
        lines.append(f'<trkpt lat="{p["lat"]}" lon="{p["lon"]}">{ele}<time>{t}</time>{ext}</trkpt>')
    lines.append("</trkseg></trk></gpx>")
    return "\n".join(lines) + "\n"


def parse(kind: str, data: bytes):
    """parse.parse'ın bu modüle düşen türleri. Çözülemeyen/bozuk dosyada None."""
    fn = {"sleep_details": sleep_details, "sleep_summary": sleep_details, "sleep_gps": sleep_details,
          "sleep_x": sleep_details, "sleep_stages_details": sleep_stages, "manual_details": manual_samples,
          "workout_summary": workout_summary, "workout_gps": workout_gps,
          "workout_details": workout_details}.get(kind)
    if fn is None:
        return None
    try:
        return fn(data)
    except (struct.error, IndexError, ValueError):
        return None
