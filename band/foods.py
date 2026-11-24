"""Sık yenen yiyecek ve içecekler. Değerler porsiyon başına yaklaşık ortalamalardır.

Sütunlar: ad, porsiyon, kcal, protein g, karbonhidrat g, yağ g, kafein mg, sıvı ml
"""
import i18n

FOODS = [
    # kahvaltı
    ("Haşlanmış yumurta", "1 adet", 78, 6.3, 0.6, 5.3, 0, 0),
    ("Sahanda yumurta", "2 yumurta", 220, 13, 1, 18, 0, 0),
    ("Menemen", "1 porsiyon", 250, 12, 9, 18, 0, 0),
    ("Omlet", "2 yumurta", 190, 13, 1, 15, 0, 0),
    ("Beyaz peynir", "30 g", 80, 5, 0.5, 6.5, 0, 0),
    ("Kaşar peyniri", "30 g", 105, 7.5, 0.5, 8, 0, 0),
    ("Zeytin", "5 adet", 40, 0.3, 1, 4, 0, 0),
    ("Bal", "1 tatlı kaşığı", 30, 0, 8, 0, 0, 0),
    ("Tereyağı", "1 tatlı kaşığı", 36, 0, 0, 4, 0, 0),
    ("Simit", "1 adet", 320, 10, 56, 6, 0, 0),
    ("Poğaça", "1 adet", 290, 6, 32, 15, 0, 0),
    ("Börek", "1 dilim", 300, 9, 28, 17, 0, 0),
    ("Beyaz ekmek", "1 dilim", 70, 2.3, 13, 0.8, 0, 0),
    ("Tam buğday ekmeği", "1 dilim", 65, 3, 11, 1, 0, 0),
    ("Yulaf ezmesi", "40 g kuru", 150, 5.5, 26, 2.8, 0, 0),
    ("Sucuklu yumurta", "1 porsiyon", 360, 18, 2, 31, 0, 0),
    # ana yemek
    ("Mercimek çorbası", "1 kase", 160, 9, 24, 3.5, 0, 0),
    ("Ezogelin çorbası", "1 kase", 140, 6, 22, 3.5, 0, 0),
    ("Tavuk suyu çorba", "1 kase", 110, 7, 12, 3.5, 0, 0),
    ("Pilav", "1 porsiyon", 280, 5, 52, 6, 0, 0),
    ("Bulgur pilavı", "1 porsiyon", 220, 6, 40, 5, 0, 0),
    ("Makarna", "1 porsiyon", 320, 11, 60, 4, 0, 0),
    ("Kuru fasulye", "1 porsiyon", 280, 15, 38, 8, 0, 0),
    ("Nohut yemeği", "1 porsiyon", 290, 13, 38, 10, 0, 0),
    ("Tavuk göğsü ızgara", "150 g", 250, 46, 0, 6, 0, 0),
    ("Tavuk but fırın", "1 adet", 260, 26, 0, 17, 0, 0),
    ("Köfte", "4 adet", 320, 24, 6, 22, 0, 0),
    ("Dana ızgara", "150 g", 330, 39, 0, 19, 0, 0),
    ("Balık ızgara", "1 porsiyon", 280, 34, 0, 15, 0, 0),
    ("Ton balığı", "1 kutu", 180, 26, 0, 8, 0, 0),
    ("Döner dürüm", "1 adet", 520, 28, 52, 21, 0, 0),
    ("Tavuk dürüm", "1 adet", 450, 27, 50, 15, 0, 0),
    ("Lahmacun", "1 adet", 270, 11, 36, 9, 0, 0),
    ("Pide (kıymalı)", "1 porsiyon", 560, 26, 68, 20, 0, 0),
    ("Adana kebap", "1 porsiyon", 480, 30, 8, 36, 0, 0),
    ("İskender", "1 porsiyon", 720, 38, 42, 44, 0, 0),
    ("Mantı", "1 porsiyon", 480, 19, 56, 19, 0, 0),
    ("Karnıyarık", "1 adet", 300, 12, 14, 22, 0, 0),
    ("Zeytinyağlı fasulye", "1 porsiyon", 180, 4, 16, 11, 0, 0),
    ("Dolma (yaprak)", "5 adet", 190, 3, 24, 9, 0, 0),
    ("Çoban salata", "1 porsiyon", 110, 2, 9, 8, 0, 0),
    ("Mevsim salata", "1 porsiyon", 80, 2, 7, 5, 0, 0),
    ("Yoğurt", "1 kase", 120, 7, 9, 6.5, 0, 0),
    ("Cacık", "1 kase", 90, 5, 7, 4.5, 0, 0),
    ("Hamburger", "1 adet", 520, 26, 42, 27, 0, 0),
    ("Pizza", "1 dilim", 280, 12, 33, 11, 0, 0),
    ("Patates kızartması", "1 porsiyon", 320, 4, 41, 15, 0, 0),
    ("Tost (kaşarlı)", "1 adet", 300, 13, 30, 14, 0, 0),
    # ara öğün
    ("Elma", "1 adet", 80, 0.4, 21, 0.3, 0, 0),
    ("Muz", "1 adet", 105, 1.3, 27, 0.4, 0, 0),
    ("Portakal", "1 adet", 65, 1.2, 16, 0.2, 0, 0),
    ("Üzüm", "1 kase", 105, 1, 27, 0.2, 0, 0),
    ("Karpuz", "1 dilim", 85, 1.7, 21, 0.4, 0, 0),
    ("Ceviz", "3 adet", 80, 1.9, 1.7, 8, 0, 0),
    ("Badem", "10 adet", 70, 2.6, 2.6, 6, 0, 0),
    ("Fındık", "10 adet", 90, 2, 2.4, 8.5, 0, 0),
    ("Fıstık ezmesi", "1 yemek kaşığı", 95, 4, 3.5, 8, 0, 0),
    ("Protein tozu", "1 ölçek", 120, 24, 3, 1.5, 0, 0),
    ("Protein bar", "1 adet", 200, 20, 20, 7, 0, 0),
    ("Bitter çikolata", "2 kare", 110, 1.5, 9, 8, 12, 0),
    ("Sütlü çikolata", "1 küçük paket", 215, 3, 24, 12, 8, 0),
    ("Bisküvi", "3 adet", 130, 2, 20, 5, 0, 0),
    ("Kek", "1 dilim", 260, 4, 36, 11, 0, 0),
    ("Baklava", "2 dilim", 330, 5, 40, 17, 0, 0),
    ("Sütlaç", "1 kase", 240, 7, 42, 5, 0, 0),
    ("Dondurma", "2 top", 200, 3.5, 24, 10, 0, 0),
    ("Cips", "1 küçük paket", 160, 2, 15, 10, 0, 0),
    # içecek
    ("Su", "1 bardak", 0, 0, 0, 0, 0, 250),
    ("Çay", "1 bardak", 2, 0, 0.5, 0, 30, 100),
    ("Çay (şekerli)", "1 bardak", 22, 0, 5.5, 0, 30, 100),
    ("Türk kahvesi", "1 fincan", 10, 0.3, 1.5, 0.2, 60, 65),
    ("Filtre kahve", "1 kupa", 5, 0.3, 0, 0, 120, 240),
    ("Latte", "1 orta boy", 150, 8, 12, 7, 80, 300),
    ("Espresso", "1 shot", 3, 0.1, 0.5, 0, 65, 30),
    ("Americano", "1 orta boy", 10, 0.5, 1.5, 0, 130, 350),
    ("Ayran", "1 bardak", 75, 4, 5, 4, 0, 200),
    ("Süt", "1 bardak", 125, 6.5, 9.5, 6.5, 0, 200),
    ("Portakal suyu", "1 bardak", 90, 1.5, 21, 0.3, 0, 200),
    ("Kola", "1 kutu", 140, 0, 35, 0, 34, 330),
    ("Şekersiz kola", "1 kutu", 1, 0, 0, 0, 34, 330),
    ("Enerji içeceği", "1 kutu", 115, 0, 28, 0, 80, 250),
    ("Soda", "1 şişe", 0, 0, 0, 0, 0, 200),
    ("Bira", "1 şişe", 215, 1.6, 18, 0, 0, 500),
    ("Yeşil çay", "1 kupa", 2, 0, 0, 0, 30, 240),
    ("Bitki çayı", "1 kupa", 2, 0, 0.5, 0, 0, 240),
]

# İngilizce ad ve porsiyon: Türkçe ad -> (ad, porsiyon). Listeye yiyecek eklerken buraya da ekle (test denetler).
EN = {
    "Haşlanmış yumurta": ("Boiled egg", "1 egg"),
    "Sahanda yumurta": ("Fried eggs", "2 eggs"),
    "Menemen": ("Menemen (eggs with tomato and pepper)", "1 serving"),
    "Omlet": ("Omelette", "2 eggs"),
    "Beyaz peynir": ("Feta-style white cheese", "30 g"),
    "Kaşar peyniri": ("Kashar cheese", "30 g"),
    "Zeytin": ("Olives", "5 olives"),
    "Bal": ("Honey", "1 tsp"),
    "Tereyağı": ("Butter", "1 tsp"),
    "Simit": ("Simit (sesame bagel)", "1 piece"),
    "Poğaça": ("Poğaça (savory pastry)", "1 piece"),
    "Börek": ("Börek", "1 slice"),
    "Beyaz ekmek": ("White bread", "1 slice"),
    "Tam buğday ekmeği": ("Whole wheat bread", "1 slice"),
    "Yulaf ezmesi": ("Oatmeal", "40 g dry"),
    "Sucuklu yumurta": ("Eggs with sucuk", "1 serving"),
    "Mercimek çorbası": ("Lentil soup", "1 bowl"),
    "Ezogelin çorbası": ("Ezogelin soup", "1 bowl"),
    "Tavuk suyu çorba": ("Chicken soup", "1 bowl"),
    "Pilav": ("Rice pilaf", "1 serving"),
    "Bulgur pilavı": ("Bulgur pilaf", "1 serving"),
    "Makarna": ("Pasta", "1 serving"),
    "Kuru fasulye": ("White bean stew", "1 serving"),
    "Nohut yemeği": ("Chickpea stew", "1 serving"),
    "Tavuk göğsü ızgara": ("Grilled chicken breast", "150 g"),
    "Tavuk but fırın": ("Roasted chicken leg", "1 piece"),
    "Köfte": ("Köfte (meatballs)", "4 pieces"),
    "Dana ızgara": ("Grilled beef", "150 g"),
    "Balık ızgara": ("Grilled fish", "1 serving"),
    "Ton balığı": ("Tuna", "1 can"),
    "Döner dürüm": ("Döner wrap", "1 wrap"),
    "Tavuk dürüm": ("Chicken wrap", "1 wrap"),
    "Lahmacun": ("Lahmacun", "1 piece"),
    "Pide (kıymalı)": ("Pide (minced meat)", "1 serving"),
    "Adana kebap": ("Adana kebab", "1 serving"),
    "İskender": ("İskender kebab", "1 serving"),
    "Mantı": ("Mantı (Turkish dumplings)", "1 serving"),
    "Karnıyarık": ("Karnıyarık (stuffed eggplant)", "1 piece"),
    "Zeytinyağlı fasulye": ("Green beans in olive oil", "1 serving"),
    "Dolma (yaprak)": ("Stuffed vine leaves", "5 pieces"),
    "Çoban salata": ("Shepherd's salad", "1 serving"),
    "Mevsim salata": ("Green salad", "1 serving"),
    "Yoğurt": ("Yogurt", "1 bowl"),
    "Cacık": ("Cacık (yogurt with cucumber)", "1 bowl"),
    "Hamburger": ("Hamburger", "1 burger"),
    "Pizza": ("Pizza", "1 slice"),
    "Patates kızartması": ("French fries", "1 serving"),
    "Tost (kaşarlı)": ("Grilled cheese sandwich", "1 sandwich"),
    "Elma": ("Apple", "1 medium"),
    "Muz": ("Banana", "1 medium"),
    "Portakal": ("Orange", "1 medium"),
    "Üzüm": ("Grapes", "1 bowl"),
    "Karpuz": ("Watermelon", "1 slice"),
    "Ceviz": ("Walnuts", "3 walnuts"),
    "Badem": ("Almonds", "10 almonds"),
    "Fındık": ("Hazelnuts", "10 hazelnuts"),
    "Fıstık ezmesi": ("Peanut butter", "1 tbsp"),
    "Protein tozu": ("Protein powder", "1 scoop"),
    "Protein bar": ("Protein bar", "1 bar"),
    "Bitter çikolata": ("Dark chocolate", "2 squares"),
    "Sütlü çikolata": ("Milk chocolate", "1 small bar"),
    "Bisküvi": ("Biscuits", "3 biscuits"),
    "Kek": ("Cake", "1 slice"),
    "Baklava": ("Baklava", "2 pieces"),
    "Sütlaç": ("Rice pudding", "1 bowl"),
    "Dondurma": ("Ice cream", "2 scoops"),
    "Cips": ("Potato chips", "1 small bag"),
    "Su": ("Water", "1 glass"),
    "Çay": ("Tea", "1 tea glass"),
    "Çay (şekerli)": ("Tea (with sugar)", "1 tea glass"),
    "Türk kahvesi": ("Turkish coffee", "1 cup"),
    "Filtre kahve": ("Filter coffee", "1 mug"),
    "Latte": ("Latte", "1 medium"),
    "Espresso": ("Espresso", "1 shot"),
    "Americano": ("Americano", "1 medium"),
    "Ayran": ("Ayran", "1 glass"),
    "Süt": ("Milk", "1 glass"),
    "Portakal suyu": ("Orange juice", "1 glass"),
    "Kola": ("Cola", "1 can"),
    "Şekersiz kola": ("Diet cola", "1 can"),
    "Enerji içeceği": ("Energy drink", "1 can"),
    "Soda": ("Sparkling mineral water", "1 bottle"),
    "Bira": ("Beer", "1 bottle"),
    "Yeşil çay": ("Green tea", "1 mug"),
    "Bitki çayı": ("Herbal tea", "1 mug"),
}

_FOLD = str.maketrans("ıİşŞçÇğĞöÖüÜâ", "iisscCggoouua")


def fold(text: str) -> str:
    return text.translate(_FOLD).lower()


def as_dict(row) -> dict:
    """Ad ve porsiyon seçili dildedir (i18n.lang())."""
    name, portion, kcal, p, c, f, caf, ml = row
    if i18n.lang() == "en" and name in EN:
        name, portion = EN[name]
    return {"name": name, "portion": portion, "kcal": kcal, "p": p, "c": c, "f": f, "caf": caf, "ml": ml,
            "kind": "drink" if ml else "food"}


def _names(row) -> tuple[str, ...]:
    """Aramada bakılan adlar: seçili dildeki önce, öteki dildeki sonra."""
    both = (fold(row[0]), fold(EN[row[0]][0])) if row[0] in EN else (fold(row[0]),)
    return both[::-1] if i18n.lang() == "en" else both


def search(query: str, kind: str | None = None, limit: int = 8) -> list[dict]:
    """Ad iki dilde de aranır; sonuçlar seçili dilde döner. Sıra: seçili dilde baştan uyan, öteki dilde baştan uyan, içinde geçen."""
    q = fold(query.strip())
    rows = [r for r in FOODS if not kind or ("drink" if r[7] else "food") == kind]
    if q:
        def rank(r):
            names = _names(r)
            if names[0].startswith(q):
                return 0
            if any(n.startswith(q) for n in names):
                return 1
            return 2 if any(q in n for n in names) else None
        ranked = [(rank(r), r) for r in rows]
        rows = [r for want in (0, 1, 2) for got, r in ranked if got == want]
    return [as_dict(r) for r in rows[:limit]]


def find(name: str) -> dict | None:
    """Türkçe ya da İngilizce adla bulur."""
    key = fold(name.strip())
    return next((as_dict(r) for r in FOODS if key in _names(r)), None)
