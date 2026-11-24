"""Antrenman özeti dosyalarının alan tabloları.

Gadgetbridge'in WorkoutSummaryParser.java dosyasındaki builder çağrılarından satır satır,
betikle çevrildi (466 add* çağrısı, sıra ve boyutlar aynen). Elle düzenleme yapma; alan adları
Java'daki sabit adlarıdır, birim dönüşümü ve adlandırma parse_more.workout_summary içinde yapılır.

Her tablo: header_size (geçerlilik bitlerinin bayt sayısı) + sıralı alan listesi.
Alan türleri: int = int32 LE (işaretli), short = uint16 LE, byte = uint8, float = float32 LE,
unknown = atlanan baytlar, unknown_if = bir önceki bayt verilen değerlerden biriyse atlanan baytlar.
"""
from __future__ import annotations


class _Builder:
    def __init__(self):
        self.header_size = 0
        self.entries: list[tuple] = []  # (tür, anahtar, birim, çarpan | boyut | (boyut, değerler))

    def add_int(self, key, unit):
        self.entries.append(("int", key, unit, 1))

    def add_short(self, key, unit, multiplier=1):
        self.entries.append(("short", key, unit, multiplier))

    def add_byte(self, key, unit):
        self.entries.append(("byte", key, unit, 1))

    def add_float(self, key, unit):
        self.entries.append(("float", key, unit, 1))

    def add_unknown(self, size):
        self.entries.append(("unknown", None, None, size))

    def add_unknown_if_previous_byte(self, size, values):
        self.entries.append(("unknown_if", None, None, (size, values)))


def _freestyle(version):
    if version in (5,):
        header_size = 3
    elif version in (7,):
        header_size = 5
    elif version in (8, 9, 10):
        header_size = 6
    else:
        return None
    b = _Builder()
    b.header_size = header_size
    b.add_int("TIME_START", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("TIME_END", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("ACTIVE_SECONDS", "UNIT_SECONDS")
    b.add_short("CALORIES_BURNT", "UNIT_KCAL")
    b.add_byte("HR_AVG", "UNIT_BPM")
    b.add_byte("HR_MAX", "UNIT_BPM")
    b.add_byte("HR_MIN", "UNIT_BPM")
    if version > 5:
        b.add_unknown(6)
    b.add_float("TRAINING_EFFECT_AEROBIC", "UNIT_NONE")
    if version > 6:
        b.add_unknown(1)
    b.add_unknown(1)
    b.add_short("RECOVERY_TIME", "UNIT_HOURS")
    b.add_int("HR_ZONE_EXTREME", "UNIT_SECONDS")
    b.add_int("HR_ZONE_ANAEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_AEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_FAT_BURN", "UNIT_SECONDS")
    b.add_int("HR_ZONE_WARM_UP", "UNIT_SECONDS")
    if version == 5:
        b.add_unknown(10)  # Probably the same as for v8 below?
        b.add_short("XIAOMI_WORKOUT_TYPE", "XIAOMI_WORKOUT_TYPE")
        b.add_unknown(2)
        b.add_int("TIME_GOAL", "UNIT_SECONDS")
        b.add_short("CALORIES_GOAL", "UNIT_KCAL")
    else:
        b.add_short("CALORIES_TOTAL", "UNIT_KCAL")
        b.add_unknown(4)  # activeSeconds again?, UNIT_SECONDS
        b.add_float("TRAINING_EFFECT_ANAEROBIC", "UNIT_NONE")
        b.add_unknown(1)
        b.add_short("XIAOMI_WORKOUT_TYPE", "XIAOMI_WORKOUT_TYPE")
        b.add_unknown(2)
        b.add_int("TIME_GOAL", "UNIT_SECONDS")
        b.add_short("CALORIES_GOAL", "UNIT_KCAL")
        b.add_short("WORKOUT_LOAD", "UNIT_NONE")
        b.add_unknown(1)
        if version > 7:
            b.add_byte("VITALITY_GAIN", "UNIT_NONE")
            b.add_short("PUNCH_TOTAL", "UNIT_NONE")
            b.add_short("PUNCH_PERFECT", "UNIT_NONE")
            b.add_short("PUNCH_GOOD", "UNIT_NONE")
            b.add_short("PUNCH_MISS", "UNIT_NONE")
            b.add_unknown(7)
        if version >= 10:
            b.add_short("THROWS_LOW", "UNIT_NONE")
            b.add_short("THROWS_MEDIUM", "UNIT_NONE")
            b.add_short("THROWS_HIGH", "UNIT_NONE")
            b.add_unknown(1)
    return b


def _indoor_cycling(version):
    if version in (8,):
        header_size = 7
    elif version in (9,):
        header_size = 8
    else:
        return None
    b = _Builder()
    b.header_size = header_size
    b.add_int("TIME_START", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("TIME_END", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("ACTIVE_SECONDS", "UNIT_SECONDS")
    b.add_unknown(4)
    b.add_short("CALORIES_BURNT", "UNIT_KCAL")
    b.add_unknown(4)
    b.add_byte("HR_AVG", "UNIT_BPM")
    b.add_byte("HR_MAX", "UNIT_BPM")
    b.add_byte("HR_MIN", "UNIT_BPM")
    b.add_float("TRAINING_EFFECT_AEROBIC", "UNIT_NONE")
    b.add_unknown(1)
    b.add_unknown(1)
    b.add_short("RECOVERY_TIME", "UNIT_HOURS")
    b.add_int("HR_ZONE_EXTREME", "UNIT_SECONDS")
    b.add_int("HR_ZONE_ANAEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_AEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_FAT_BURN", "UNIT_SECONDS")
    b.add_int("HR_ZONE_WARM_UP", "UNIT_SECONDS")
    b.add_unknown(2)
    b.add_unknown(4)
    b.add_float("TRAINING_EFFECT_ANAEROBIC", "UNIT_NONE")
    # FIXME identify field lengths to align with the header
    b.add_unknown(3)
    b.add_int("TIME_GOAL", "UNIT_SECONDS")
    b.add_short("CALORIES_GOAL", "UNIT_KCAL")
    b.add_short("CALORIES_GOAL_MAX", "UNIT_KCAL")  # TODO: mhm?
    b.add_unknown(28)
    b.add_short("WORKOUT_LOAD", "UNIT_NONE")  # training load
    b.add_unknown(24)
    b.add_byte("configuredSets", "UNIT_NONE")
    b.add_unknown(13)
    #builder.addInt("startTime2", UNIT_SECONDS);
    #builder.addInt("endTime2", UNIT_SECONDS);
    #builder.addInt("goal", UNIT_NONE); // TODO match against goalType
    #builder.addInt("duration2", UNIT_SECONDS);
    #builder.addInt("intervalTime", UNIT_SECONDS);
    #builder.addUnknown(56);
    #builder.addInt("hrZoneExtreme2", UNIT_SECONDS);
    #builder.addInt("hrZoneAnaerobic2", UNIT_SECONDS);
    #builder.addInt("hrZoneAerobic2", UNIT_SECONDS);
    #builder.addInt("hrZoneFatBurn2", UNIT_SECONDS);
    #builder.addInt("hrZoneWarmUp2", UNIT_SECONDS);
    #builder.addUnknown(16);
    #builder.addShort(VITALITY_GAIN, UNIT_NONE);
    #builder.addShort("training_load2", UNIT_NONE);
    #builder.addShort("recovery_time2", UNIT_HOURS);
    return b


def _outdoor_walking_v1(version):
    if version in (3,):
        return _outdoor_v3(True)
    elif version in (3, 4):
        header_size = 4
    else:
        return None
    b = _Builder()
    b.header_size = header_size
    b.add_int("TIME_START", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("TIME_END", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("ACTIVE_SECONDS", "UNIT_SECONDS")
    b.add_int("DISTANCE_METERS", "UNIT_METERS")
    b.add_int("CALORIES_BURNT", "UNIT_KCAL")
    b.add_int("PACE_MAX", "UNIT_SECONDS_PER_M")
    b.add_int("PACE_MIN", "UNIT_SECONDS_PER_M")
    b.add_unknown(4)
    b.add_int("STEPS", "UNIT_STEPS")
    b.add_unknown(2)  # pace?
    b.add_byte("HR_AVG", "UNIT_BPM")
    b.add_byte("HR_MAX", "UNIT_BPM")
    b.add_byte("HR_MIN", "UNIT_BPM")
    # FIXME identify field lengths to align with the header
    b.add_unknown(20)
    b.add_float("TRAINING_EFFECT_ANAEROBIC", "UNIT_NONE")
    b.add_unknown(9)
    b.add_byte("RECOVERY_TIME", "UNIT_SECONDS")
    b.add_unknown(2)
    b.add_int("HR_ZONE_EXTREME", "UNIT_SECONDS")
    b.add_int("HR_ZONE_ANAEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_AEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_FAT_BURN", "UNIT_SECONDS")
    b.add_int("HR_ZONE_WARM_UP", "UNIT_SECONDS")
    b.add_int("TIME_GOAL", "UNIT_SECONDS")
    return b


def _outdoor_walking_v2(version):
    if version in (1,):
        header_size = 5
    elif version in (2,):
        header_size = 6
    elif version in (4,):
        header_size = 7
    elif version in (5, 6):
        header_size = 9
    elif version in (9,):
        header_size = 13
    else:
        return None
    b = _Builder()
    b.header_size = header_size
    b.add_short("XIAOMI_WORKOUT_TYPE", "XIAOMI_WORKOUT_TYPE")
    b.add_int("TIME_START", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("TIME_END", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("ACTIVE_SECONDS", "UNIT_SECONDS")
    # Verified against a captured v6 walking SUMMARY: bytes at this offset ==
    # ACTIVE_SECONDS (171), so this is the validDuration counter (paused-time excluded).
    b.add_unknown(4)
    b.add_int("DISTANCE_METERS", "UNIT_METERS")
    b.add_short("CALORIES_TOTAL", "UNIT_KCAL")
    b.add_short("CALORIES_BURNT", "UNIT_KCAL")
    if version >= 5:
        b.add_int("PACE_AVG_SECONDS_KM", "UNIT_SECONDS_PER_KM")
    b.add_int("PACE_MAX", "UNIT_SECONDS_PER_KM")
    b.add_int("PACE_MIN", "UNIT_SECONDS_PER_KM")
    if version >= 5:
        b.add_float("SPEED_AVG", "UNIT_KMPH")
    b.add_float("SPEED_MAX", "UNIT_KMPH")
    b.add_int("STEPS", "UNIT_STEPS")
    if version >= 5:
        b.add_short("STEP_LENGTH_AVG", "UNIT_CM")
        b.add_short("STEP_RATE_AVG", "UNIT_SPM")
    b.add_short("STEP_RATE_MAX", "UNIT_SPM")
    b.add_byte("HR_AVG", "UNIT_BPM")
    b.add_byte("HR_MAX", "UNIT_BPM")
    b.add_byte("HR_MIN", "UNIT_BPM")
    b.add_unknown(20)
    b.add_float("TRAINING_EFFECT_AEROBIC", "UNIT_NONE")
    if version == 1:
        b.add_unknown(9)
    else:
        b.add_unknown(1)
        b.add_float("TRAINING_EFFECT_ANAEROBIC", "UNIT_NONE")
        if version >= 9:
            b.add_unknown(6)
        else:
            b.add_unknown(1)
        b.add_byte("MAXIMUM_OXYGEN_UPTAKE", "UNIT_ML_KG_MIN")
        b.add_unknown(2)
        b.add_short("RECOVERY_TIME", "UNIT_HOURS")
        b.add_unknown(1)
    b.add_int("HR_ZONE_EXTREME", "UNIT_SECONDS")
    b.add_int("HR_ZONE_ANAEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_AEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_FAT_BURN", "UNIT_SECONDS")
    b.add_int("HR_ZONE_WARM_UP", "UNIT_SECONDS")
    if version == 6:
        # 68-byte v6 tail anchored against a captured walk:
        #   VITALITY_GAIN byte at tail offset 38 (file offset 0xAA) = 4,
        #                  matching the +4 vitality shown on the watch.
        #   WORKOUT_LOAD short at tail offset 21-22 = 6 for that walk.
        b.add_int("TIME_GOAL", "UNIT_SECONDS")
        b.add_short("CALORIES_GOAL", "UNIT_KCAL")
        b.add_int("DISTANCE_GOAL", "UNIT_METERS")
        b.add_int("PACE_GOAL", "UNIT_SECONDS_PER_KM")
        b.add_float("SPEED_GOAL", "UNIT_KMPH")
        b.add_short("CADENCE_GOAL", "UNIT_SPM")
        b.add_unknown(1)
        b.add_short("WORKOUT_LOAD", "UNIT_NONE")
        b.add_unknown(7)
        b.add_short("PROJECTED_TIME_5KM", "UNIT_SECONDS")
        b.add_short("PROJECTED_TIME_10KM", "UNIT_SECONDS")
        b.add_short("PROJECTED_TIME_MARATHON", "UNIT_SECONDS")
        b.add_short("PROJECTED_TIME_HALF_MARATHON", "UNIT_SECONDS")
        b.add_byte("VITALITY_GAIN", "UNIT_NONE")
        b.add_unknown(18)
        b.add_short("AVG_GROUND_CONTACT_TIME", "UNIT_MILLISECONDS")
        b.add_short("MIN_GROUND_CONTACT_TIME", "UNIT_MILLISECONDS")
        b.add_unknown(7)
    # TODO walking_v2 v5/v7/v8: 68 trailing bytes after HR_ZONE_WARM_UP not yet decoded.
    if version >= 9:
        b.add_unknown(46)
        b.add_short("WORKOUT_LOAD", "UNIT_NONE")
        b.add_unknown(15)
        # Same spacing after the load as the v6 tail, where the vitality byte matches the watch.
        b.add_byte("VITALITY_GAIN", "UNIT_NONE")
        b.add_unknown(18)
        b.add_short("AVG_GROUND_CONTACT_TIME", "UNIT_MILLISECONDS")
        b.add_short("MIN_GROUND_CONTACT_TIME", "UNIT_MILLISECONDS")
        b.add_unknown(6)
        b.add_short("AVG_VERTICAL_RATIO", "UNIT_PERCENTAGE", 0.1)
        b.add_short("MIN_VERTICAL_RATIO", "UNIT_PERCENTAGE", 0.1)
        b.add_short("MAX_VERTICAL_OSCILLATION", "UNIT_MM")
        b.add_short("AVG_VERTICAL_OSCILLATION", "UNIT_CM")
        b.add_short("MIN_VERTICAL_OSCILLATION", "UNIT_CM")
    return b


def _outdoor_cycling(version):
    if version in (4, 5):
        header_size = 6
    elif version in (6,):
        header_size = 7
    elif version in (8,):
        header_size = 8
    else:
        return None
    b = _Builder()
    b.header_size = header_size
    b.add_short("XIAOMI_WORKOUT_TYPE", "XIAOMI_WORKOUT_TYPE")
    b.add_int("TIME_START", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("TIME_END", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("ACTIVE_SECONDS", "UNIT_SECONDS")
    b.add_unknown(4)
    b.add_int("DISTANCE_METERS", "UNIT_METERS")
    b.add_short("CALORIES_TOTAL", "UNIT_KCAL")
    b.add_short("CALORIES_BURNT", "UNIT_KCAL")
    b.add_int("PACE_MAX", "UNIT_SECONDS_PER_KM")
    b.add_int("PACE_MIN", "UNIT_SECONDS_PER_KM")
    if version >= 5:
        b.add_float("SPEED_AVG", "UNIT_KMPH")
    b.add_float("SPEED_MAX", "UNIT_KMPH")
    b.add_byte("HR_AVG", "UNIT_BPM")
    b.add_byte("HR_MAX", "UNIT_BPM")
    b.add_byte("HR_MIN", "UNIT_BPM")
    b.add_float("ELEVATION_GAIN", "UNIT_METERS")
    b.add_float("ELEVATION_LOSS", "UNIT_METERS")
    b.add_float("ALTITUDE_AVG", "UNIT_METERS")
    b.add_float("ALTITUDE_MAX", "UNIT_METERS")
    b.add_float("ALTITUDE_MIN", "UNIT_METERS")
    b.add_float("TRAINING_EFFECT_AEROBIC", "UNIT_NONE")
    b.add_unknown(1)
    b.add_float("TRAINING_EFFECT_ANAEROBIC", "UNIT_NONE")
    b.add_unknown(1)
    if version >= 7:
        b.add_unknown(5)  # reserved (5 bytes)
    b.add_byte("MAXIMUM_OXYGEN_UPTAKE", "UNIT_ML_KG_MIN")
    b.add_unknown(1)
    b.add_unknown(1)
    b.add_short("RECOVERY_TIME", "UNIT_HOURS")
    b.add_unknown(1)
    b.add_int("HR_ZONE_EXTREME", "UNIT_SECONDS")
    b.add_int("HR_ZONE_ANAEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_AEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_FAT_BURN", "UNIT_SECONDS")
    b.add_int("HR_ZONE_WARM_UP", "UNIT_SECONDS")
    if version >= 6:
        if version >= 7:
            b.add_unknown(5)  # reserved (5 bytes)
        b.add_int("TIME_GOAL", "UNIT_SECONDS")
        b.add_short("CALORIES_GOAL", "UNIT_KCAL")
        b.add_int("DISTANCE_GOAL", "UNIT_METERS")
        b.add_float("SPEED_GOAL", "UNIT_KMPH")
        # Route byte: 0xFC, 0xFD or 0xFF when the ride follows a route, in which case
        # 8 more bytes follow it.
        b.add_unknown(1)
        b.add_unknown_if_previous_byte(8, (0xFC, 0xFD, 0xFF))
        b.add_short("WORKOUT_LOAD", "UNIT_NONE")
        b.add_unknown(15)  # reserved (15 bytes)
        b.add_byte("VITALITY_GAIN", "UNIT_NONE")
        b.add_unknown(5)  # reserved (5 bytes)
        if version >= 7:
            b.add_unknown(2)  # reserved (2 bytes)
        if version >= 8:
            b.add_unknown(1)  # reserved (1 byte)
    return b


def _hiit(version):
    # v6 (e.g. Smart Band 10): identical layout to v5, headerSize 4.
    # Verified against a captured ~57-min HIIT: start/end give a 3408 s
    # duration matching ACTIVE_SECONDS, calories 286, HR 126/153/78 avg/max/min.
    if version in (5, 6):
        header_size = 4
    else:
        return None
    b = _Builder()
    b.header_size = header_size
    b.add_int("TIME_START", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("TIME_END", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("ACTIVE_SECONDS", "UNIT_SECONDS")
    b.add_short("CALORIES_BURNT", "UNIT_KCAL")
    b.add_byte("HR_AVG", "UNIT_BPM")
    b.add_byte("HR_MAX", "UNIT_BPM")
    b.add_byte("HR_MIN", "UNIT_BPM")
    b.add_float("TRAINING_EFFECT_AEROBIC", "UNIT_NONE")
    b.add_unknown(1)
    b.add_unknown(1)
    b.add_short("RECOVERY_TIME", "UNIT_HOURS")
    b.add_int("HR_ZONE_EXTREME", "UNIT_SECONDS")
    b.add_int("HR_ZONE_ANAEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_AEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_FAT_BURN", "UNIT_SECONDS")
    b.add_int("HR_ZONE_WARM_UP", "UNIT_SECONDS")
    return b


def _pool_swimming(version):
    if version in (6,):
        header_size = 4
    elif version in (7,):
        header_size = 5
    elif version in (8,):
        header_size = 8
    else:
        return None
    # For v6 the total expected byte count is 76, matching the previous
    # addUnknown(N) total in this parser. Field positions verified against
    # captured pool-swim summaries.
    b = _Builder()
    b.header_size = header_size
    b.add_int("TIME_START", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("TIME_END", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("ACTIVE_SECONDS", "UNIT_SECONDS")
    b.add_int("DISTANCE_METERS", "UNIT_METERS")
    b.add_short("CALORIES_BURNT", "UNIT_KCAL")
    if version >= 7:
        b.add_int("PACE_AVG_SECONDS_KM", "UNIT_SECONDS_PER_KM")
    b.add_int("PACE_MAX", "UNIT_SECONDS_PER_KM")
    b.add_int("PACE_MIN", "UNIT_SECONDS_PER_KM")
    b.add_unknown(1)
    b.add_short("RECOVERY_TIME", "UNIT_HOURS")
    b.add_short("STROKES", "UNIT_STROKES")
    b.add_byte("SWIM_STYLE", "UNIT_NONE")
    if version >= 7:
        b.add_byte("STROKE_RATE_AVG", "UNIT_STROKES_PER_MINUTE")
    b.add_byte("STROKE_RATE_MAX", "UNIT_STROKES_PER_MINUTE")
    b.add_short("LAPS", "UNIT_LAPS")
    b.add_short("SWOLF_AVG", "UNIT_NONE")
    b.add_short("minSWOLF", "UNIT_NONE")
    b.add_byte("POOL_LENGTH", "UNIT_METERS")
    b.add_short("CALORIES_TOTAL", "UNIT_KCAL")
    b.add_unknown(4)
    b.add_int("TIME_GOAL", "UNIT_SECONDS")
    b.add_short("CALORIES_GOAL", "UNIT_KCAL")
    b.add_int("DISTANCE_GOAL", "UNIT_METERS")
    b.add_int("PACE_GOAL", "UNIT_SECONDS_PER_KM")
    b.add_short("LENGTHS_GOAL", "UNIT_NONE")
    b.add_float("TRAINING_EFFECT_AEROBIC", "UNIT_NONE")
    b.add_unknown(1)
    b.add_float("TRAINING_EFFECT_ANAEROBIC", "UNIT_NONE")
    b.add_unknown(1)
    b.add_short("WORKOUT_LOAD", "UNIT_NONE")
    b.add_unknown(1)
    b.add_byte("VITALITY_GAIN", "UNIT_NONE")
    return b


def _elliptical(version):
    if version in (3, 4, 5, 6):
        header_size = 4
    else:
        return None
    b = _Builder()
    b.header_size = header_size
    b.add_int("TIME_START", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("TIME_END", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("ACTIVE_SECONDS", "UNIT_SECONDS")
    b.add_short("CALORIES_BURNT", "UNIT_KCAL")
    b.add_int("STEPS", "UNIT_STEPS")
    if version >= 6:
        b.add_short("CADENCE_AVG", "UNIT_SPM")
    b.add_short("CADENCE_MAX", "UNIT_SPM")
    b.add_byte("HR_AVG", "UNIT_BPM")
    b.add_byte("HR_MAX", "UNIT_BPM")
    b.add_byte("HR_MIN", "UNIT_BPM")
    b.add_float("TRAINING_EFFECT_AEROBIC", "UNIT_NONE")
    if version >= 4:
        b.add_unknown(1)
    b.add_unknown(1)
    b.add_short("RECOVERY_TIME", "UNIT_HOURS")
    b.add_int("HR_ZONE_EXTREME", "UNIT_SECONDS")
    b.add_int("HR_ZONE_ANAEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_AEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_FAT_BURN", "UNIT_SECONDS")
    b.add_int("HR_ZONE_WARM_UP", "UNIT_SECONDS")
    if version >= 6:
        # Checked on an elliptical v6 summary: total calories 774 against 678 active,
        # active time 1795 s equal to the duration, anaerobic effect 0.3, load 96,
        # vitality 3. The body tiles to its 79 bytes.
        b.add_short("CALORIES_TOTAL", "UNIT_KCAL")
        b.add_unknown(4)  # active time again
        b.add_float("TRAINING_EFFECT_ANAEROBIC", "UNIT_NONE")
        b.add_unknown(1)  # reserved (1 byte)
        # Route byte, as in outdoor cycling: 8 more bytes follow 0xFC, 0xFD or 0xFF.
        b.add_unknown(1)
        b.add_unknown_if_previous_byte(8, (0xFC, 0xFD, 0xFF))
        b.add_unknown(1)  # reserved (1 byte)
        b.add_int("TIME_GOAL", "UNIT_SECONDS")
        b.add_short("CALORIES_GOAL", "UNIT_KCAL")
        b.add_short("CADENCE_GOAL", "UNIT_SPM")
        b.add_short("WORKOUT_LOAD", "UNIT_NONE")
        b.add_unknown(1)  # reserved (1 byte)
        b.add_byte("VITALITY_GAIN", "UNIT_NONE")
        b.add_unknown(1)  # reserved (1 byte)
    return b


def _rowing(version):
    if version in (4,):
        header_size = 4
    elif version in (6, 7):
        header_size = 5
    elif version in (8,):
        header_size = 6
    else:
        return None
    b = _Builder()
    b.header_size = header_size
    b.add_int("TIME_START", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("TIME_END", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("ACTIVE_SECONDS", "UNIT_SECONDS")
    b.add_short("CALORIES_BURNT", "UNIT_KCAL")
    b.add_byte("HR_AVG", "UNIT_BPM")
    b.add_byte("HR_MAX", "UNIT_BPM")
    b.add_byte("HR_MIN", "UNIT_BPM")
    if version >= 7:
        # Verified across three rowing v7 SUMMARY files:
        #   aerobic effect: 3.4 / 3.5 / 3.0
        #   recovery (hours): 23 / 28 / 44
        b.add_float("TRAINING_EFFECT_AEROBIC", "UNIT_NONE")
        b.add_unknown(2)
        b.add_short("RECOVERY_TIME", "UNIT_HOURS")
    else:
        b.add_unknown(7)
        if version > 4:
            b.add_unknown(1)
    b.add_int("HR_ZONE_EXTREME", "UNIT_SECONDS")
    b.add_int("HR_ZONE_ANAEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_AEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_FAT_BURN", "UNIT_SECONDS")
    b.add_int("HR_ZONE_WARM_UP", "UNIT_SECONDS")
    if version >= 8:
        # v8 inserts five bytes here. Confirmed on a rowing v8 file: without the padding
        # the stroke fields land on nonsense (318767104 strokes), with it they read 46
        # strokes at 18 average and 27 maximum strokes per minute, which is consistent
        # with 46 strokes over the 147 recorded seconds.
        b.add_unknown(5)
    if version >= 7:
        # totalCal (active + basal). Decoded as 94 kcal for a workout whose
        # CALORIES_BURNT (active) = 70 kcal — diff 24 kcal over 872 s matches basal
        # metabolic contribution at ~1.65 kcal/min.
        b.add_short("CALORIES_TOTAL", "UNIT_KCAL")
    else:
        b.add_unknown(2)
    b.add_int("STROKES", "UNIT_STROKES")
    b.add_int("STROKE_RATE_AVG", "UNIT_STROKES_PER_MINUTE")
    if version >= 7:
        # Verified across three rowing v7 SUMMARY files:
        #   stroke rate max: 71 / 41 / 53
        #   anaerobic effect: 0.4 / 1.1 / 1.9
        #   workout load:    69 / 77 / 39
        #   vitality gain:    0 /  0 / 10
        b.add_int("STROKE_RATE_MAX", "UNIT_STROKES_PER_MINUTE")
        b.add_unknown(12)
        b.add_byte("GROUP_COUNT", "UNIT_NONE")
        b.add_unknown(4)
        b.add_float("TRAINING_EFFECT_ANAEROBIC", "UNIT_NONE")
        b.add_unknown(1)
        if version >= 8:
            # reserved (5 bytes). With it the v8 body tiles to its 108 bytes and the
            # workout load of a rowing v8 file reads 2 instead of 0.
            b.add_unknown(5)
        b.add_unknown(2)
        b.add_int("TIME_GOAL", "UNIT_SECONDS")
        b.add_short("CALORIES_GOAL", "UNIT_KCAL")
        b.add_short("GOAL_COUNT", "UNIT_NONE")
        b.add_short("WORKOUT_LOAD", "UNIT_NONE")
        b.add_unknown(1)
        b.add_byte("VITALITY_GAIN", "UNIT_NONE")
        if version >= 8:
            b.add_unknown(3)  # reserved (3 bytes)
    else:
        b.add_unknown(4)
    return b


def _treadmill(version):
    if version in (5,):
        header_size = 4
    elif version in (9,):
        header_size = 6
    elif version in (10,):
        header_size = 8
    elif version in (11,):
        header_size = 9
    else:
        return None
    b = _Builder()
    b.header_size = header_size
    b.add_int("TIME_START", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("TIME_END", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("ACTIVE_SECONDS", "UNIT_SECONDS")
    b.add_int("DISTANCE_METERS", "UNIT_METERS")
    b.add_short("CALORIES_BURNT", "UNIT_KCAL")
    if version >= 10:
        b.add_int("PACE_AVG_SECONDS_KM", "UNIT_SECONDS_PER_KM")
    b.add_int("PACE_MAX", "UNIT_SECONDS_PER_KM")
    b.add_int("PACE_MIN", "UNIT_SECONDS_PER_KM")
    b.add_int("STEPS", "UNIT_STEPS")
    # Field positions derived empirically from captured treadmill SUMMARY binaries
    # and verified against the on-watch UI; every byte is typed end-to-end for v11.
    if version >= 10:
        b.add_short("STEP_LENGTH_AVG", "UNIT_CM")
        b.add_short("CADENCE_AVG", "UNIT_SPM")
    b.add_short("CADENCE_MAX", "UNIT_SPM")
    b.add_byte("HR_AVG", "UNIT_BPM")
    b.add_byte("HR_MAX", "UNIT_BPM")
    b.add_byte("HR_MIN", "UNIT_BPM")
    b.add_float("TRAINING_EFFECT_AEROBIC", "UNIT_NONE")
    if version == 9:
        b.add_unknown(4)  # reserved (4 bytes)
    else:
        if version >= 10:
            b.add_unknown(1)
        b.add_byte("MAXIMUM_OXYGEN_UPTAKE", "UNIT_ML_KG_MIN")
        if version >= 10:
            b.add_byte("V02MAX_LEVEL", "UNIT_NONE")
        b.add_unknown(1)
    b.add_short("RECOVERY_TIME", "UNIT_HOURS")
    b.add_int("HR_ZONE_EXTREME", "UNIT_SECONDS")
    b.add_int("HR_ZONE_ANAEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_AEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_FAT_BURN", "UNIT_SECONDS")
    b.add_int("HR_ZONE_WARM_UP", "UNIT_SECONDS")
    if version == 9:
        # v9 treadmill tail anchored to a captured workout's on-watch UI:
        #   recovery (h): 32, training load: 28, vitality: +15.
        # (avg cadence and avg pace are derived from steps/distance over the
        #  active time, not stored separately in v9.)
        b.add_unknown(39)  # reserved (39 bytes)
        b.add_short("WORKOUT_LOAD", "UNIT_NONE")  # training load
        b.add_unknown(7)  # reserved (7 bytes)
        b.add_byte("VITALITY_GAIN", "UNIT_NONE")  # vitality
    if version >= 11:
        # Trailing zone verified across four captured v11 treadmill workouts:
        #   distance goal:    4000 / 4000 / 2500 / 3000 m  (matches user-set goals)
        #   anaerobic effect: 1.8 / 1.5 / 0.0 / 1.6        (float at this offset)
        #   workout load:     104 / 88 / 25 / 67
        #   vitality gain:     12 /  0 / 13 /  0
        b.add_short("CALORIES_TOTAL", "UNIT_KCAL")
        b.add_unknown(4)
        b.add_float("TRAINING_EFFECT_ANAEROBIC", "UNIT_NONE")
        b.add_unknown(5)
        b.add_int("TIME_GOAL", "UNIT_SECONDS")
        b.add_short("CALORIES_GOAL", "UNIT_KCAL")
        b.add_int("DISTANCE_GOAL", "UNIT_METERS")
        b.add_int("PACE_GOAL", "UNIT_SECONDS_PER_KM")
        b.add_float("SPEED_GOAL", "UNIT_NONE")
        b.add_short("CADENCE_GOAL", "UNIT_SPM")
        # Post-calibration distance — when nonzero, displaces the raw DISTANCE_METERS
        # for display. Verified: 4500 (raw 4479) and 4000 (raw 3934). Zero when no
        # calibration was applied for that workout.
        b.add_int("DISTANCE_METERS_CALIBRATED", "UNIT_METERS")
        b.add_short("WORKOUT_LOAD", "UNIT_NONE")
        b.add_unknown(7)
        b.add_short("PROJECTED_TIME_5KM", "UNIT_SECONDS")
        b.add_short("PROJECTED_TIME_10KM", "UNIT_SECONDS")
        b.add_short("PROJECTED_TIME_MARATHON", "UNIT_SECONDS")
        b.add_short("PROJECTED_TIME_HALF_MARATHON", "UNIT_SECONDS")
        b.add_byte("VITALITY_GAIN", "UNIT_NONE")
        b.add_unknown(18)
        b.add_short("AVG_GROUND_CONTACT_TIME", "UNIT_MILLISECONDS")
        b.add_short("MIN_GROUND_CONTACT_TIME", "UNIT_MILLISECONDS")
        b.add_unknown(7)
    return b


def _outdoor_cycling_v2(version):
    if version in (3,):
        return _outdoor_v3(False)
    elif version in (3, 4):
        header_size = 5
    else:
        return None
    b = _Builder()
    b.header_size = header_size
    b.add_int("TIME_START", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("TIME_END", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("ACTIVE_SECONDS", "UNIT_SECONDS")
    b.add_int("DISTANCE_METERS", "UNIT_METERS")
    b.add_short("CALORIES_BURNT", "UNIT_KCAL")
    b.add_unknown(8)
    b.add_float("SPEED_MAX", "UNIT_KMPH")
    b.add_byte("HR_AVG", "UNIT_BPM")
    b.add_byte("HR_MAX", "UNIT_BPM")
    b.add_byte("HR_MIN", "UNIT_BPM")
    b.add_unknown(28)
    b.add_int("HR_ZONE_EXTREME", "UNIT_SECONDS")
    b.add_int("HR_ZONE_ANAEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_AEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_FAT_BURN", "UNIT_SECONDS")
    b.add_int("HR_ZONE_WARM_UP", "UNIT_SECONDS")
    b.add_unknown(18)
    b.add_unknown(2)  # configuredTimeGoal, UNIT_SECONDS
    b.add_unknown(6)
    return b
# 
# Version 3 of the outdoor walking v1 and outdoor cycling v2 summaries, as sent by the
# Mi Watch Color Sport. Both share one layout; walking adds the steps and max cadence.
# Field order and sizes follow the official app's report tables for these sports, which
# decode the captured files byte for byte.
# 


def _outdoor_v3(with_steps):
    b = _Builder()
    b.header_size = 4
    b.add_int("TIME_START", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("TIME_END", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("ACTIVE_SECONDS", "UNIT_SECONDS")
    b.add_int("DISTANCE_METERS", "UNIT_METERS")
    b.add_short("CALORIES_BURNT", "UNIT_KCAL")
    b.add_int("PACE_MAX", "UNIT_SECONDS_PER_KM")
    b.add_int("PACE_MIN", "UNIT_SECONDS_PER_KM")
    b.add_float("SPEED_MAX", "UNIT_KMPH")
    if with_steps:
        b.add_int("STEPS", "UNIT_STEPS")
        b.add_short("CADENCE_MAX", "UNIT_SPM")
    b.add_byte("HR_AVG", "UNIT_BPM")
    b.add_byte("HR_MAX", "UNIT_BPM")
    b.add_byte("HR_MIN", "UNIT_BPM")
    b.add_float("ELEVATION_GAIN", "UNIT_METERS")
    b.add_float("ELEVATION_LOSS", "UNIT_METERS")
    b.add_float("ALTITUDE_AVG", "UNIT_METERS")
    b.add_float("ALTITUDE_MAX", "UNIT_METERS")
    b.add_float("ALTITUDE_MIN", "UNIT_METERS")
    b.add_float("TRAINING_EFFECT_AEROBIC", "UNIT_NONE")
    b.add_byte("MAXIMUM_OXYGEN_UPTAKE", "UNIT_ML_KG_MIN")
    b.add_unknown(1)  # energy consumed
    b.add_short("RECOVERY_TIME", "UNIT_HOURS")
    b.add_int("HR_ZONE_EXTREME", "UNIT_SECONDS")
    b.add_int("HR_ZONE_ANAEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_AEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_FAT_BURN", "UNIT_SECONDS")
    b.add_int("HR_ZONE_WARM_UP", "UNIT_SECONDS")
    b.add_short("CALORIES_TOTAL", "UNIT_KCAL")
    b.add_unknown(4)  # valid duration - marked invalid in the header, always 0
    b.add_float("TRAINING_EFFECT_ANAEROBIC", "UNIT_NONE")
    return b


def _jump_roping(version):
    if version in (3, 5):
        header_size = 5
    else:
        return None
    b = _Builder()
    b.header_size = header_size
    b.add_int("TIME_START", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("TIME_END", "UNIT_UNIX_EPOCH_SECONDS")
    b.add_int("ACTIVE_SECONDS", "UNIT_SECONDS")
    b.add_short("CALORIES_BURNT", "UNIT_KCAL")
    b.add_byte("HR_AVG", "UNIT_BPM")
    b.add_byte("HR_MAX", "UNIT_BPM")
    b.add_byte("HR_MIN", "UNIT_BPM")
    b.add_float("TRAINING_EFFECT_AEROBIC", "UNIT_NONE")
    if version == 3:
        b.add_unknown(3)  # unknown byte and recovery time?
    else:
        b.add_unknown(2)
        b.add_short("RECOVERY_TIME", "UNIT_HOURS")
    b.add_int("HR_ZONE_EXTREME", "UNIT_SECONDS")
    b.add_int("HR_ZONE_ANAEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_AEROBIC", "UNIT_SECONDS")
    b.add_int("HR_ZONE_FAT_BURN", "UNIT_SECONDS")
    b.add_int("HR_ZONE_WARM_UP", "UNIT_SECONDS")
    b.add_unknown(2)
    b.add_int("JUMPS", "UNIT_JUMPS")
    b.add_short("JUMP_RATE_AVG", "UNIT_JUMPS_PER_MINUTE")
    b.add_unknown(2)
    b.add_short("JUMP_RATE_MAX", "UNIT_JUMPS_PER_MINUTE")
    if version == 3:
        b.add_unknown(43)  # probably the same as for v5 below
        b.add_unknown(2)  # configuredJumpsGoal, UNIT_JUMPS, probably includes 2 more bytes
        b.add_unknown(2)
    else:
        b.add_unknown(27)
        b.add_unknown(4)  # activeSeconds again?, UNIT_SECONDS
        b.add_float("TRAINING_EFFECT_ANAEROBIC", "UNIT_NONE")
        b.add_unknown(3)
        b.add_int("TIME_GOAL", "UNIT_SECONDS")
        b.add_short("CALORIES_GOAL", "UNIT_KCAL")
        b.add_int("configuredJumpsGoal", "UNIT_JUMPS")
        b.add_short("WORKOUT_LOAD", "UNIT_NONE")
        b.add_unknown(1)
        b.add_byte("VITALITY_GAIN", "UNIT_NONE")  # vitality, UNIT_NONE
    return b


# dosya alt türü (XiaomiActivityFileId.Subtype) -> (spor adı, tablo işlevi); WorkoutSummaryParser.updateSummaryFromData
SPORTS = {
    0x02: ("walking", _outdoor_walking_v1),
    0x01: ("running", _outdoor_walking_v1),
    0x07: ("indoor_cycling", _indoor_cycling),
    0x08: ("freestyle", _freestyle),
    0x09: ("pool_swimming", _pool_swimming),
    0x10: ("hiit", _hiit),
    0x0B: ("elliptical", _elliptical),
    0x16: (None, _outdoor_walking_v2),  # tür, dosyadaki XIAOMI_WORKOUT_TYPE alanından gelir
    0x06: ("cycling", _outdoor_cycling_v2),
    0x17: ("cycling", _outdoor_cycling),
    0x03: ("treadmill", _treadmill),
    0x0D: ("rowing", _rowing),
    0x0E: ("jump_rope", _jump_roping),
}


def table(subtype: int, version: int):
    """(spor adı | None, _Builder | None). Bilinmeyen alt tür ya da sürümde tablo None'dır."""
    name, fn = SPORTS.get(subtype, (None, None))
    return name, fn(version) if fn else None
