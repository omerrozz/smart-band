"""Servisin ürettiği metinlerin dili: Türkçe (kaynak) ya da İngilizce.

Kullanım
    import i18n
    i18n.t("Adım hedefi tamam.")                         # seçili dildeki metin
    i18n.t("Protein düşük: {p} g aldın, hedef {target} g.", p=40, target=130)
    i18n.num(1.5)        # "1,5" (tr) / "1.5" (en);  i18n.num(12345, ",") -> "12.345" / "12,345"
    X = i18n.mark("…")   # çevrilecek metni yalnızca işaretler; gösterilirken i18n.t(X) denir

Anahtar, Türkçe kaynak metnin kendisidir; değişkenler adlı alanlardır ({p}, {target}), böylece İngilizcede söz
dizimi değişebilir. Yeni metin eklemek: kodda i18n.t("Türkçe metin") yaz, aşağıdaki EN sözlüğüne karşılığını ekle
(test_features.py koddaki her i18n.t / i18n.mark anahtarının burada olduğunu denetler). Sözlükte olmayan anahtar
Türkçe döner ve `missing` kümesine yazılır.

Bağımlılığı ve yan etkisi yoktur; dil seçilmeden (testlerde, komut satırı araçlarında) Türkçe üretir.
"""
from __future__ import annotations

LANGS = ("tr", "en")
_lang = "tr"
missing: set[str] = set()   # İngilizcesi bulunamayan anahtarlar (denetim için)


def set_lang(code: str | None) -> None:
    """Bilinmeyen ya da boş değer Türkçe sayılır."""
    global _lang
    _lang = code if code in LANGS else "tr"


def lang() -> str:
    return _lang


def mark(text: str) -> str:
    """Modül düzeyindeki sabitler için: metni değiştirmez, yalnızca çevrilecek diye işaretler."""
    return text


def t(text: str, **vars) -> str:
    """Türkçe kaynak metnin seçili dildeki karşılığı; adlı alanlar vars ile doldurulur."""
    if _lang == "en":
        found = EN.get(text)
        if found is None:
            missing.add(text)
        else:
            text = found
    return text.format(**vars) if vars else text


def switch(text):
    """Daha önce çevrilip saklanmış, değişkensiz bir metni (ör. state'teki uyarı) seçili dile taşır."""
    key = text if text in EN else _back().get(text)
    return t(key) if key else text


def _back() -> dict:
    if not _BACK:
        _BACK.update({en: tr for tr, en in EN.items()})
    return _BACK


_BACK: dict = {}
_SWAP = str.maketrans(",.", ".,")


def num(value, spec: str = "g") -> str:
    """Sayı: Türkçede ondalık virgül ve binlik nokta, İngilizcede tersi. spec: format() belirteci ("g", ",", ".1f")."""
    s = format(value, spec)
    return s if _lang == "en" else s.translate(_SWAP)


EN = {
    # --- günlük notlar (insights.py) ---
    "Kafein {mg} mg: günlük 400 mg sınırının üstünde.":
        "Caffeine is at {mg} mg, above the 400 mg daily limit.",
    "Son kafeini {time} saatinde aldın; geç saatte kafein uykuyu geciktirebilir.":
        "Your last caffeine was at {time}. Caffeine late in the day can delay sleep.",
    "Protein düşük: {p} g aldın, hedef {target} g.":
        "Protein is low: {p} g so far, goal {target} g.",
    "Şu ana kadar {net} kcal açıktasın: {eaten} yedin, {burned} harcadın.":
        "You're in a {net} kcal deficit so far: {eaten} eaten, {burned} burned.",
    "Şu ana kadar {net} kcal fazladasın: {eaten} yedin, {burned} harcadın.":
        "You're in a {net} kcal surplus so far: {eaten} eaten, {burned} burned.",
    "Son öğünden beri {dur} geçti.":
        "It's been {dur} since your last meal.",
    "{name} sonrası nabzın {n} atım yükseldi ({before} → {after}).":
        "After {name}, your heart rate rose by {n} bpm ({before} → {after}).",
    "{name} sonrası nabzın {n} atım düştü ({before} → {after}).":
        "After {name}, your heart rate dropped by {n} bpm ({before} → {after}).",
    "Öğün girersen kalori dengesi ve yeme düzeni hesaplanır.":
        "Log a meal to see your calorie balance and eating pattern.",
    "En uzun hareketsiz süre {dur}: {start}–{end}.":
        "Longest inactive stretch: {dur} ({start}–{end}).",
    "Su geride: {ml} ml içtin, günün bu saatinde yaklaşık {expected} ml beklenir.":
        "You're behind on water: {ml} ml so far, about {expected} ml expected by this time of day.",
    "Adım hedefi tamam.":
        "Step goal reached.",
    "Gündüz ortalama nabzın ({avg}) dinlenik nabzının {diff} üstünde.":
        "Your average daytime heart rate ({avg}) is {diff} bpm above your resting heart rate.",
    "{h} sa {m} dk": "{h} h {m} min",
    "{m} dk": "{m} min",
    "Bu yiyecek listede yok; elle gir.":
        "That food isn't in the list. Enter it manually.",
    # hazır öğün düğmeleri
    "Kahvaltı": "Breakfast",
    "Öğle yemeği": "Lunch",
    "Akşam yemeği": "Dinner",
    "Ara öğün": "Snack",
    "Türk kahvesi": "Turkish coffee",
    "Meyve": "Fruit",

    # --- saate giden bildirimler ---
    "Su zamanı": "Time to hydrate",
    "Bir bardak su iç.": "Drink a glass of water.",
    "Su hatırlatıcısı": "Water reminder",
    "Öğün zamanı": "Meal time",
    "Yediklerini uygulamaya yazmayı unutma.": "Don't forget to log what you ate.",
    "Öğün hatırlatıcısı": "Meal reminder",
    "Hedefe ulaşıldı": "Goal reached",
    "{v} dakika doldu.": "{v} minutes completed.",
    "{v} km tamamlandı.": "{v} km completed.",
    "Tahmini {v} km tamamlandı.": "About {v} km completed (estimated).",
    "{v} kcal yakıldı.": "{v} kcal burned.",
    "Bölge {zone} altı": "Below Zone {zone}",
    "Bölge {zone} üstü": "Above Zone {zone}",
    "Nabız {hr}. Hızlan, hedef {lo}-{hi}.": "Heart rate {hr}. Speed up, target {lo}-{hi}.",
    "Nabız {hr}. Yavaşla, hedef {lo}-{hi}.": "Heart rate {hr}. Slow down, target {lo}-{hi}.",

    # --- saatin bildirdiği spor adları (bandmsg.SPORTS) ---
    "Açık hava koşusu": "Outdoor run",
    "Yürüyüş": "Walk",
    "Doğa yürüyüşü": "Hike",
    "Trekking": "Trekking",
    "Patika koşusu": "Trail run",
    "Açık hava bisikleti": "Outdoor cycling",

    # --- uygulamadan başlatılan antrenman (appworkout.py) ---
    "Zaten süren bir antrenman var. Önce onu bitir.":
        "A workout is already in progress. Finish it first.",
    "Bilinmeyen spor türü.": "Unknown workout type.",
    "Hedef türü ya da değeri geçersiz.": "The goal type or value isn't valid.",
    "Bu sporda mesafe tahmin edilemediği için mesafe hedefi konamaz.":
        "Distance can't be estimated for this workout type, so a distance goal isn't available.",
    "Süre hedefi 1–360 dakika arasında olmalı.": "The time goal must be between 1 and 360 minutes.",
    "Mesafe hedefi 0,1–100 km arasında olmalı.": "The distance goal must be between 0.1 and 100 km.",
    "Kalori hedefi 10–5.000 kcal arasında olmalı.": "The calorie goal must be between 10 and 5,000 kcal.",
    "Bölge 1 ile 5 arasında olmalı.": "Zone must be between 1 and 5.",
    "Süren bir antrenman yok.": "No workout in progress.",
    "Antrenman 1 dakikadan kısa sürdü, kaydedilmedi.":
        "Workout was shorter than 1 minute and wasn't saved.",
    "Hiç nabız okunamadı, antrenman kaydedilmedi. Saat menzilde ve bilekte miydi?":
        "No heart rate was recorded, so the workout wasn't saved. Was the band in range and on your wrist?",
    "Antrenman kaydedildi.": "Workout saved.",
    "Antrenman kaydedilmedi.": "Workout discarded.",
    "Bu aralıkta saatin kendi antrenman kaydı var; uygulama kaydı ayrıca saklanmadı.":
        "The band recorded its own workout during this time, so the app recording wasn't saved separately.",

    # --- hava durumu ---
    "Hava durumu alınamadı": "Couldn't get the weather",
    "Şehir aranamadı": "Couldn't search for the city",
    "{what}: sunucu hata verdi ({code}).": "{what}: server error ({code}).",
    "{what}: sunucu yanıt vermedi.": "{what}: the server didn't respond.",
    "{what}: internet yok.": "{what}: no internet connection.",
    "{what}: beklenmeyen yanıt.": "{what}: unexpected response.",
    "Hava durumu kapalı.": "Weather is turned off.",
    "Hava durumu kapalı. Önce anahtarı aç.": "Weather is turned off. Turn it on first.",
    "Saat hava tahminini kabul etmedi (kod {code}).": "The band rejected the forecast (code {code}).",
    "Şehir adını yaz.": "Enter a city name.",
    "Bu adla bir şehir bulunamadı.": "No city found with that name.",
    "Önce şehir seç.": "Choose a city first.",
    "Şehri listeden seç; aralık 15–720 dakika olmalı.":
        "Pick a city from the list. The interval must be between 15 and 720 minutes.",
    # WMO kodlarının metni (weather.WMO_TEXT)
    "açık": "clear",
    "az bulutlu": "mostly clear",
    "parçalı bulutlu": "partly cloudy",
    "kapalı": "overcast",
    "sisli": "fog",
    "kırağılı sis": "freezing fog",
    "hafif çisenti": "light drizzle",
    "çisenti": "drizzle",
    "yoğun çisenti": "heavy drizzle",
    "donan çisenti": "freezing drizzle",
    "hafif yağmur": "light rain",
    "yağmur": "rain",
    "kuvvetli yağmur": "heavy rain",
    "donan yağmur": "freezing rain",
    "hafif kar": "light snow",
    "kar": "snow",
    "yoğun kar": "heavy snow",
    "kar taneleri": "snow grains",
    "hafif sağanak": "light showers",
    "sağanak": "showers",
    "kuvvetli sağanak": "heavy showers",
    "kar sağanağı": "snow showers",
    "yoğun kar sağanağı": "heavy snow showers",
    "gök gürültülü fırtına": "thunderstorm",
    "dolu ile fırtına": "thunderstorm with hail",

    # --- bağlantı uyarıları (state.host_hint) ---
    "Saat bağlantısı birkaç saniyede kopuyor. Saati büyük olasılıkla başka bir uygulama (Mi Fitness) "
    "tutuyor; o uygulamayı tümüyle kapatınca bağlantı kendiliğinden kurulur.":
        "The connection to the band keeps dropping after a few seconds. Another app (Mi Fitness) is most likely "
        "holding the band. Close that app completely and the connection will be restored automatically.",
    "Saat bağlantısı duraklatıldı": "Band connection paused",

    # --- istek hataları (server.py) ---
    "Yetkisiz": "Not authorized",
    "Geçersiz istek": "Invalid request",
    "Yok": "Not found",
    "Geçersiz tarih": "Invalid date",
    "Geçersiz antrenman": "Invalid workout",
    "Antrenman yok": "Workout not found",
    "Bu antrenmanın GPS izi yok": "This workout has no GPS track",
    "Yalnızca bilgisayardan": "Only available on the computer",
    "Bu kurulumda yok": "Not available in this setup",
    "Bu sayfayı açmak için bilgisayardaki bağlantıyı kullan.":
        "To open this page, use the link shown on the computer.",
    "Saat bağlı değil. Yaklaşınca tekrar dene.":
        "The band isn't connected. Move closer and try again.",
    "Girilen değerleri kontrol et.": "Check the values you entered.",
    "Dil \"tr\" ya da \"en\" olmalı.": "Language must be \"tr\" or \"en\".",
    "Adım 100–100.000, kalori 50–10.000, ayakta 1–24 saat, hareket 5–300 dakika arasında olmalı.":
        "Steps must be 100–100,000, calories 50–10,000, standing 1–24 hours, and moving 5–300 minutes.",
    "Gelecekteki bir saat için kayıt girilemez.": "You can't log an entry for a future time.",
    "Porsiyon 0,25 ile 10 arasında olmalı.": "Servings must be between 0.25 and 10.",
    "Ad yaz; kalori 1–5.000 arasında olmalı.": "Enter a name. Calories must be between 1 and 5,000.",
    "Makro değerleri 0–500 g arasında olmalı.": "Macros must be between 0 and 500 g.",
    "Miktar 2.000 ml'yi geçemez.": "The amount can't exceed 2,000 ml.",
    "Bilinmeyen işlem.": "Unknown action.",
    "Dosya boyutu uygun değil.": "The file size isn't supported.",
    "Bu sorguya izin yok.": "This query isn't allowed.",
    "Süre 0–180 dakika arasında olmalı.": "Duration must be between 0 and 180 minutes.",
    "Yağ oranı yüzde 3–60 arasında olmalı.": "Body fat must be between 3% and 60%.",
    "Bu ölçüm tartıdan gelmemiş.": "This measurement didn't come from the scale.",
    "Kilo 30–250 kg arasında olmalı.": "Weight must be between 30 and 250 kg.",
    "Yaş 10–100, boy 120–230 cm, kilo 30–250 kg arasında olmalı.":
        "Age must be 10–100, height 120–230 cm, and weight 30–250 kg.",
    "Azami nabız 100–220 arasında olmalı; boş bırakırsan 220 − yaş kullanılır.":
        "Max heart rate must be between 100 and 220. Leave it blank to use 220 − age.",
    "Aralık 15–240 dakika olmalı, başlangıç saati bitişten önce gelmeli.":
        "The interval must be between 15 and 240 minutes, and the start time must be before the end time.",
    "Mesafe düzeltilemedi: yalnızca uygulama kayıtları, makul bir değerle.":
        "Couldn't update the distance. Only app-recorded workouts can be edited, and the value must be realistic.",
    "Yalnızca uygulamadan kaydedilen antrenmanlar silinebilir.":
        "Only workouts recorded in the app can be deleted.",
    "Başlık ya da mesaj yaz.": "Enter a title or a message.",
    "Bilinmeyen ayar.": "Unknown setting.",
    "Bu ayar için geçersiz değer.": "That value isn't valid for this setting.",
    "Bu ayar saatten okunamadı; saat desteklemiyor olabilir.":
        "This setting couldn't be read from the band. The band may not support it.",
    "Ekran listesi saatten okunamadı; saat desteklemiyor olabilir.":
        "The display list couldn't be read from the band. The band may not support it.",
    "Ekran listesi saattekiyle uyuşmuyor. Sayfayı yenileyip tekrar dene.":
        "The display list doesn't match the one on the band. Refresh the page and try again.",
    "Alarm saati geçersiz.": "The alarm time isn't valid.",
    "Saat en fazla 10 alarm tutar.": "The band can store up to 10 alarms.",
    "Saat en fazla {n} hatırlatıcı tutar. Önce birini sil.":
        "The band can store up to {n} reminders. Delete one first.",
    "Bu hatırlatıcı saatte artık yok. Liste yenilendi, tekrar dene.":
        "This reminder is no longer on the band. The list has been refreshed, so try again.",
    "Başlık yaz, tarih ve saati seç.": "Enter a title and choose a date and time.",
    # takvim
    "Başlık yaz.": "Enter a title.",
    "Tarih ve saati seç.": "Choose a date and time.",
    "Bitiş başlangıçtan sonra olmalı.": "The end must be after the start.",
    "Bir etkinlik en çok 31 gün sürebilir.": "An event can last up to 31 days.",
    "Bitmiş bir etkinlik eklenemez.": "You can't add an event that has already ended.",
    "En fazla {n} etkinlik saklanır. Önce birini sil.": "Up to {n} events can be stored. Delete one first.",
    "Bu etkinlik artık yok. Liste yenilendi.": "This event no longer exists. The list has been refreshed.",

    # --- dışa aktarım: dosya adları ve CSV başlıkları ---
    "bileklik-gunler.csv": "band-days.csv",
    "bileklik-dakikalar.csv": "band-minutes.csv",
    "antrenman-{stamp}.gpx": "workout-{stamp}.gpx",
    "zaman": "time",
    "gun": "day",
    "ayakta_saat": "standing_hours",

    # --- bağlantı günlüğü (olayların ayrıntı metni) ---
    "servis başladı": "service started",
    ", pil %{battery}": ", battery {battery}%",
    "pil %{battery}": "battery {battery}%",
    "saati başka bir uygulama tutuyor olabilir": "another app may be holding the band",
    "saat bağlantısı {minutes} dakika duraklatıldı": "band connection paused for {minutes} minutes",
    "{kind}, {n} bayt": "{kind}, {n} bytes",
    "{n} dosya için saate onay gönderildi": "acknowledged {n} files to the band",
    "profildeki kilodan çok farklı bir ölçüm geldi, kaydedilmedi":
        "a measurement far from the profile weight was received and not saved",
    "tartıdan {kg} kg": "{kg} kg from the scale",
    "{cmd} yanıt, durum {status}, {n} bayt: {hex}": "{cmd} response, status {status}, {n} bytes: {hex}",
    "durum sorgusu: {detail}": "status query: {detail}",
    "durum bildirimi: {detail}": "status update: {detail}",
    "saat alan göndermedi": "the band sent no fields",
    "bilinen alan yok": "no known fields",
    "{sec} sn kesintiden sonra konum geldi; o ana dek {n} nokta süzüldü":
        "location returned after a {sec} s gap; {n} points filtered out so far",
    # saat uygulamaları (rpk)
    "{package} gönderildi, saat yüklendiğini bildirdi": "{package} sent, the band reported it installed",
    "{package} gönderildi, saatten yüklendi bildirimi gelmedi":
        "{package} sent, but the band didn't confirm the install",
    "{package} için silme isteği gönderildi": "delete request sent for {package}",
    "{package} saatte bulunamadı": "{package} not found on the band",
    "saatteki uygulamalar: {apps}": "apps on the band: {apps}",
    "yok": "none",
    "başarısız: {error}": "failed: {error}",
    "Saat yüklemeyi reddetti (kod {code})": "The band refused the install (code {code})",
    "Saat yükleme isteğine beklenen yanıtı vermedi": "The band didn't give the expected response to the install request",
    "Saat uygulamayı kabul etmedi (kod {code})": "The band rejected the app (code {code})",
    "çözülemeyen alan": "unreadable field",
    "Saatten yanıt gelmedi (tür {type}, alt tür {subtype})": "No response from the band (type {type}, subtype {subtype})",
    "Saat veri parçasını onaylamadı": "The band didn't acknowledge the data chunk",
}
