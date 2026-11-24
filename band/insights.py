"""Yemek, su ve kilo kayıtları ile bunlardan ve saat verisinden çıkarılan hesaplar."""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta

import foods
import i18n

# adlar Türkçe kaynaktır; presets() seçili dile çevirir
DEFAULT_PRESETS = [[i18n.mark("Kahvaltı"), 450], [i18n.mark("Öğle yemeği"), 700], [i18n.mark("Akşam yemeği"), 750],
                   [i18n.mark("Ara öğün"), 200], [i18n.mark("Türk kahvesi"), 10], [i18n.mark("Meyve"), 90]]


def init(store) -> None:
    with store.lock:
        store.db.executescript(
            "CREATE TABLE IF NOT EXISTS meals (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, name TEXT, kcal INTEGER);"
            "CREATE TABLE IF NOT EXISTS water (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, ml INTEGER);"
            "CREATE TABLE IF NOT EXISTS weights (ts INTEGER PRIMARY KEY, kg REAL);"
            # tartıdan gelen ölçümün ek değerleri (kilo, weights tablosuna da yazılır)
            "CREATE TABLE IF NOT EXISTS scale (ts INTEGER PRIMARY KEY, kg REAL, hr INTEGER, imp_low REAL, imp_high REAL);"
        )
        if "fat_shown" not in {r[1] for r in store.db.execute("PRAGMA table_info(scale)")}:
            store.db.execute("ALTER TABLE scale ADD COLUMN fat_shown REAL")  # tartının ekranında yazan yağ oranı
        have = {r[1] for r in store.db.execute("PRAGMA table_info(meals)")}
        for col, typ in (("portion", "TEXT"), ("p", "REAL"), ("c", "REAL"), ("f", "REAL"), ("caf", "INTEGER"),
                         ("ml", "INTEGER"), ("kind", "TEXT")):
            if col not in have:
                store.db.execute(f"ALTER TABLE meals ADD COLUMN {col} {typ}")
        store.db.commit()


def _bounds(day: date) -> tuple[int, int]:
    start = int(datetime.combine(day, datetime.min.time()).timestamp())
    return start, start + 86400


def bmr(profile: dict) -> float:
    """Mifflin-St Jeor denklemi, kcal/gün."""
    return 10 * profile["weight"] + 6.25 * profile["height"] - 5 * profile["age"] + (5 if profile["sex"] == "m" else -161)


# S400 yağ oranı: Xiaomi'nin hesap sunucusunun 356 gerçek yanıtına oturtulmuş doğrusal model (openScale PR 1558,
# S400BodyFatV235; 339 yanıtı birebir, kalanını 0,1 puan içinde veriyor). Girdi 250 kHz empedanstır.
_S400_FAT = {"m": (-22.581459185537234, 0.33161141027191327, 0.37821882356760034, -0.018465167384231713, -0.008462253886384885),
             "f": (-22.637482793351495, 0.33294993852603716, 0.2488996917777127, -0.01975349413536753, -0.007229906660150276)}
_S400_RATIO = 0.894   # 250 kHz / 50 kHz empedans oranı (kullanıcının ölçümlerinden); yalnızca 250 kHz paketi kaçtıysa kullanılır


def fat_estimate(kg: float, profile: dict, impedance: float, impedance_high: float | None = None) -> float | None:
    """Empedanstan yağ oranı (%). impedance: 50 kHz, impedance_high: 250 kHz (ohm).

    Modelin doğrulandığı aralığın dışında (boy 145–200, kilo 40–120, yaş 16–68, empedans 300–1000) None döner.
    """
    r = impedance_high or (impedance * _S400_RATIO if impedance else None)
    if not kg or not r:
        return None
    h, age = profile["height"], profile["age"]
    if not (145 <= h <= 200 and 40 <= kg <= 120 and 16 <= age <= 68 and 300 <= r <= 1000):
        return None
    c = _S400_FAT["f" if profile["sex"] == "f" else "m"]
    lean = c[0] + c[1] * h + c[2] * kg + c[3] * age + c[4] * r
    return max(3.0, min(75.0, 100 * (1 - lean / kg)))


def body_report(store, config: dict) -> dict:
    """Vücut ekranı: bütün kilo kayıtları; tartıdan gelenlerde nabız, empedans ve yağ oranı.

    Yağ oranı: tartının ekranından girilmişse o (fat_from = "shown"), yoksa formül + girilmiş değerlerle formül
    arasındaki ortalama fark (fat_from = "calc"; hiç değer girilmediyse fark 0 ve calibrated False).
    """
    pr = config["profile"]
    with store.lock:
        rows = store.db.execute(
            "SELECT w.ts, w.kg, s.hr, s.imp_low, s.imp_high, s.fat_shown, s.ts IS NOT NULL FROM weights w"
            " LEFT JOIN scale s ON s.ts = w.ts ORDER BY w.ts").fetchall()
    diffs = [shown - est for ts, kg, hr, lo, hi, shown, sc in rows
             if shown is not None and (est := fat_estimate(kg, pr, lo, hi)) is not None]
    offset = sum(diffs) / len(diffs) if diffs else 0.0
    out = []
    for ts, kg, hr, lo, hi, shown, sc in rows:
        e = {"ts": ts, "kg": kg, "scale": bool(sc)}
        e.update({k: v for k, v in (("hr", hr), ("imp_low", lo), ("imp_high", hi)) if v is not None})
        est = fat_estimate(kg, pr, lo, hi)
        if shown is not None:
            e.update(fat=round(shown, 1), fat_from="shown")
        elif est is not None:
            e.update(fat=round(max(3.0, min(60.0, est + offset)), 1), fat_from="calc")
        out.append(e)
    return {"weights": out, "height": pr["height"], "age": pr["age"], "sex": pr["sex"],
            "calibrated": bool(diffs), "calibration_points": len(diffs)}


def set_fat_shown(store, ts: int, fat: float | None) -> bool:
    """Bir tartı ölçümü için tartının ekranında yazan yağ oranını kaydeder (None: siler)."""
    with store.lock:
        n = store.db.execute("UPDATE scale SET fat_shown = ? WHERE ts = ?", (fat, ts)).rowcount
        store.db.commit()
    return n > 0


def max_hr(profile: dict) -> int:
    """Kullanıcının girdiği azami nabız; girilmediyse 220 − yaş."""
    return profile.get("max_hr_set") or 220 - profile["age"]


def _dur(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return i18n.t("{h} sa {m} dk", h=h, m=m) if h else i18n.t("{m} dk", m=m)


def _hm(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


# --- kayıt ---------------------------------------------------------------
def add_meal(store, name: str, kcal: int, ts: int | None = None, portion: str = "", p=None, c=None, f=None,
             caf: int = 0, ml: int = 0, kind: str = "food") -> None:
    with store.lock:
        store.db.execute(
            "INSERT INTO meals (ts, name, kcal, portion, p, c, f, caf, ml, kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts or int(time.time()), name, kcal, portion, p, c, f, caf, ml, kind))
        store.db.commit()


def add_food(store, name: str, qty: float, ts: int | None = None) -> None:
    """Listeden bir yiyecek ya da içeceği porsiyon sayısıyla ekler."""
    item = foods.find(name)
    if item is None:
        raise ValueError(i18n.t("Bu yiyecek listede yok; elle gir."))
    label = item["portion"] if qty == 1 else f"{i18n.num(qty)} × {item['portion']}"
    add_meal(store, item["name"], round(item["kcal"] * qty), ts, label, round(item["p"] * qty, 1),
             round(item["c"] * qty, 1), round(item["f"] * qty, 1), round(item["caf"] * qty), round(item["ml"] * qty),
             item["kind"])


def delete_meal(store, meal_id: int) -> None:
    with store.lock:
        store.db.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
        store.db.commit()


def add_water(store, ml: int) -> None:
    """Eksi değer son eklenen kaydı geri alır."""
    with store.lock:
        if ml < 0:
            store.db.execute("DELETE FROM water WHERE id = (SELECT MAX(id) FROM water WHERE ts >= ?)", (_bounds(date.today())[0],))
        else:
            store.db.execute("INSERT INTO water (ts, ml) VALUES (?,?)", (int(time.time()), ml))
        store.db.commit()


def add_weight(store, kg: float) -> None:
    with store.lock:
        store.db.execute("INSERT OR REPLACE INTO weights VALUES (?,?)", (int(time.time()), kg))
        store.db.commit()


def presets(store) -> list:
    with store.lock:
        rows = store.db.execute(
            "SELECT name, CAST(ROUND(AVG(kcal)) AS INTEGER) FROM meals GROUP BY name ORDER BY COUNT(*) DESC, MAX(ts) DESC LIMIT 6"
        ).fetchall()
    used = [list(r) for r in rows]
    names = {r[0] for r in used}
    return (used + [[i18n.t(n), kcal] for n, kcal in DEFAULT_PRESETS if n not in names and i18n.t(n) not in names])[:8]


# --- günlük hesap ----------------------------------------------------------
def day_report(store, config: dict, day: date, live: dict | None = None) -> dict:
    start, end = _bounds(day)
    today = day == date.today()
    now = int(time.time())
    profile, goals = config["profile"], config["goals"]
    m = store.minutes(day)
    summary = m["summary"] or {}
    with store.lock:
        cols = ("id", "ts", "name", "kcal", "portion", "p", "c", "f", "caf", "ml", "kind")
        entries = [dict(zip(cols, r)) for r in store.db.execute(
            f"SELECT {', '.join(cols)} FROM meals WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end))]
        plain_water = store.db.execute("SELECT COALESCE(SUM(ml), 0) FROM water WHERE ts >= ? AND ts < ?", (start, end)).fetchone()[0]
    meals = [x for x in entries if x["kind"] != "drink"]      # yeme düzeni yalnızca yiyeceklere bakar
    drinks = [x for x in entries if x["kind"] == "drink"]
    water_ml = plain_water + sum(x["ml"] or 0 for x in drinks)

    # kalori dengesi
    bmr_day = bmr(profile)
    fraction = min(1.0, (now - start) / 86400) if today else 1.0
    bmr_so_far = round(bmr_day * fraction)
    active = summary.get("active_kcal") or 0
    eaten = sum(x["kcal"] for x in entries)
    # Uyanıkken oturmak ve ayakta durmak dinlenik harcamanın biraz üstündedir (oturma yaklaşık 1,3 MET; dinlenik 1,0).
    # Aktif kalori yalnızca hareketten sayıldığı için bu pay ayrıca eklenir: uyanık, hareketsiz dakika başına 0,3 MET.
    sedentary = 0
    if "move_minutes" in summary:
        wake = start + 7 * 3600 + 1800
        awake = max(0, (min(now, start + 86400 - 1800) if today else wake + 16 * 3600) - wake) // 60
        sedentary = round(max(0, awake - summary["move_minutes"]) * 0.3 * profile["weight"] / 60)
    burned = bmr_so_far + active + sedentary
    balance = {"bmr_day": round(bmr_day), "bmr_so_far": bmr_so_far, "active": active, "sedentary": sedentary,
               "active_band": summary.get("active_band"), "burned": burned,
               "eaten": eaten, "net": eaten - burned, "projected_burn": round(bmr_day) + active + sedentary}

    # hareketsizlik: saat bilekteyken (nabız var) adımsız geçen en uzun süre, 07:00 sonrası
    hours = [0] * 24
    longest, run_start, best = 0, None, None
    for i in range(1440):
        st, hr = m["steps"][i], m["hr"][i]
        if st:
            hours[i // 60] += st
        idle = i >= 420 and st == 0 and hr
        if idle and run_start is None:
            run_start = i
        if (not idle or i == 1439) and run_start is not None:
            length = i - run_start
            if length > longest:
                longest, best = length, (run_start, i)
            run_start = None
    recorded = [i for i in range(1440) if m["steps"][i] is not None]
    sedentary = {"longest_min": longest, "from": best[0] if best else None, "to": best[1] if best else None,
                 "hours": hours, "recorded_from": recorded[0] if recorded else None}

    # nabız: gündüz ve gece ortalaması
    def avg(lo, hi):
        vals = [v for v in m["hr"][lo:hi] if v]
        return round(sum(vals) / len(vals)) if vals else None
    heart = {"day_avg": avg(480, 1440), "night_avg": avg(0, 480), "max_hr": max_hr(profile)}

    # yeme düzeni
    eating = None
    if meals:
        first, last = meals[0]["ts"], meals[-1]["ts"]
        post = []
        for x in meals:
            i = (x["ts"] - start) // 60
            before = [v for v in m["hr"][max(0, i - 30):i] if v]
            after = [v for v in m["hr"][i + 20:i + 80] if v]
            if len(before) >= 10 and len(after) >= 20:
                post.append({"name": x["name"], "ts": x["ts"], "before": round(sum(before) / len(before)),
                             "after": round(sum(after) / len(after))})
        eating = {"first": first, "last": last, "window_min": (last - first) // 60,
                  "since_last_min": (now - last) // 60 if today else None, "post": post}

    # makrolar ve kafein
    burn_target = round(bmr_day) + active if not today else round(bmr_day) + max(active, 300)
    protein_t = round(1.6 * profile["weight"])
    fat_t = round(0.30 * burn_target / 9)
    carb_t = max(0, round((burn_target - protein_t * 4 - fat_t * 9) / 4))
    total = lambda k: round(sum(x[k] or 0 for x in entries))
    untracked = sum(1 for x in entries if x["p"] is None and x["kcal"])
    macros = {"p": total("p"), "c": total("c"), "f": total("f"), "p_target": protein_t, "c_target": carb_t,
              "f_target": fat_t, "untracked": untracked}
    caf_entries = [x for x in entries if x["caf"]]
    caffeine = {"mg": total("caf"), "limit": 400, "last": caf_entries[-1]["ts"] if caf_entries else None}

    # notlar
    notes = []
    if caffeine["mg"] > 400:
        notes.append(i18n.t("Kafein {mg} mg: günlük 400 mg sınırının üstünde.", mg=caffeine["mg"]))
    elif caffeine["last"] and datetime.fromtimestamp(caffeine["last"]).hour >= 16:
        notes.append(i18n.t("Son kafeini {time} saatinde aldın; geç saatte kafein uykuyu geciktirebilir.", time=_hm(caffeine["last"])))
    if entries and today and datetime.now().hour >= 18 and macros["p"] < protein_t * 0.6 and not untracked:
        notes.append(i18n.t("Protein düşük: {p} g aldın, hedef {target} g.", p=macros["p"], target=protein_t))
    if entries:
        net = balance["net"]
        tr = lambda n: i18n.num(n, ",")   # binlik ayırıcı: Türkçede nokta, İngilizcede virgül
        notes.append((i18n.t("Şu ana kadar {net} kcal açıktasın: {eaten} yedin, {burned} harcadın.", net=tr(abs(net)), eaten=tr(eaten), burned=tr(burned))
                      if net < 0 else
                      i18n.t("Şu ana kadar {net} kcal fazladasın: {eaten} yedin, {burned} harcadın.", net=tr(abs(net)), eaten=tr(eaten), burned=tr(burned))))
        if eating and eating["since_last_min"] is not None and eating["since_last_min"] >= 240:
            notes.append(i18n.t("Son öğünden beri {dur} geçti.", dur=_dur(eating["since_last_min"])))
        for p in (eating["post"] if eating else []):
            diff = p["after"] - p["before"]
            if abs(diff) >= 5:
                notes.append((i18n.t("{name} sonrası nabzın {n} atım yükseldi ({before} → {after}).", name=p["name"], n=abs(diff), before=p["before"], after=p["after"])
                              if diff > 0 else
                              i18n.t("{name} sonrası nabzın {n} atım düştü ({before} → {after}).", name=p["name"], n=abs(diff), before=p["before"], after=p["after"])))
    else:
        notes.append(i18n.t("Öğün girersen kalori dengesi ve yeme düzeni hesaplanır."))
    if not meals:
        eating = None
    if longest >= 60:
        notes.append(i18n.t("En uzun hareketsiz süre {dur}: {start}–{end}.", dur=_dur(longest),
                            start=f"{best[0] // 60:02d}:{best[0] % 60:02d}", end=f"{best[1] // 60:02d}:{best[1] % 60:02d}"))
    water_goal = goals.get("water", 2500)
    if today and datetime.now().hour >= 10 and water_ml < water_goal * fraction * 0.6:
        notes.append(i18n.t("Su geride: {ml} ml içtin, günün bu saatinde yaklaşık {expected} ml beklenir.", ml=water_ml,
                            expected=round(water_goal * fraction / 50) * 50))
    steps = (live or {}).get("steps") if today else None
    steps = steps if steps is not None else summary.get("steps")
    if steps is not None and steps >= goals["steps"]:
        notes.append(i18n.t("Adım hedefi tamam."))
    if heart["day_avg"] and summary.get("hr_rest") and heart["day_avg"] - summary["hr_rest"] >= 25:
        notes.append(i18n.t("Gündüz ortalama nabzın ({avg}) dinlenik nabzının {diff} üstünde.", avg=heart["day_avg"],
                            diff=heart["day_avg"] - summary["hr_rest"]))

    return {"date": day.isoformat(), "balance": balance, "meals": meals, "drinks": drinks, "macros": macros,
            "caffeine": caffeine, "presets": presets(store),
            "water_ml": water_ml, "water_goal": water_goal, "sedentary": sedentary, "heart": heart,
            "eating": eating, "notes": notes}


# --- hafta ve rekorlar -----------------------------------------------------
def week_report(store, config: dict, end=None) -> dict:
    """Seçili günde biten 7 günün özeti (end verilmezse bugün). Rekorlar bütün geçmişe bakar."""
    goals = config["goals"]
    days = store.days(400)
    if end is not None:
        days = [d for d in days if d["date"] <= end.isoformat()]
    known = [d for d in days if d["steps"] is not None]
    last7 = [d for d in days[-7:] if d["steps"] is not None]

    streak = 0
    for d in reversed(days):
        if d["steps"] is not None and d["steps"] >= goals["steps"]:
            streak += 1
        elif d["date"] == date.today().isoformat():
            continue  # bugün henüz bitmedi, seriyi bozmaz
        else:
            break

    def best(key):
        rows = [d for d in known if d.get(key)]
        top = max(rows, key=lambda d: d[key], default=None)
        return {"date": top["date"], "value": top[key]} if top else None

    rest = [d["hr_rest"] for d in last7 if d.get("hr_rest")]
    start7 = _bounds(date.today() - timedelta(days=6))[0]
    with store.lock:
        weights = [{"ts": ts, "kg": kg} for ts, kg in store.db.execute("SELECT ts, kg FROM weights ORDER BY ts")]
        extra = {r[0]: r[1:] for r in store.db.execute("SELECT ts, hr, imp_low, imp_high FROM scale")}
    for w in weights:  # tartıdan gelen kayıt: nabız ve empedans da var
        if w["ts"] in extra:
            w["scale"] = True
            w.update({k: v for k, v in zip(("hr", "imp_low", "imp_high"), extra[w["ts"]]) if v is not None})
    with store.lock:
        eaten7 = store.db.execute("SELECT COALESCE(SUM(kcal), 0), COUNT(DISTINCT date(ts, 'unixepoch', 'localtime')) FROM meals WHERE ts >= ?", (start7,)).fetchone()
        water7 = store.db.execute("SELECT COALESCE(SUM(ml), 0) FROM water WHERE ts >= ?", (start7,)).fetchone()[0]
    return {
        "days_recorded": len(last7),
        "steps_avg": round(sum(d["steps"] for d in last7) / len(last7)) if last7 else None,
        "steps_total": sum(d["steps"] for d in last7),
        "goal_days": sum(1 for d in last7 if d["steps"] >= goals["steps"]),
        "active_kcal": sum(d.get("active_kcal") or 0 for d in last7),
        "rest_avg": round(sum(rest) / len(rest)) if rest else None,
        "eaten_avg": round(eaten7[0] / eaten7[1]) if eaten7[1] else None,
        "water_avg": round(water7 / max(1, len(last7))) if water7 else None,
        "streak": streak,
        "records": {"steps": best("steps"), "active_kcal": best("active_kcal")},
        "weights": weights,
        "bmi": round(config["profile"]["weight"] / (config["profile"]["height"] / 100) ** 2, 1),
    }
