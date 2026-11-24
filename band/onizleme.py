"""Uygulamanın uzaktan açılabilen, dondurulmuş bir kopyasını üretir.

Çalışan sunucudan (localhost:8377) o anki verileri okur ve web/index.html'i,
ağ çağrılarını bu verilerle yanıtlayan tek bir sayfaya çevirir. Kopya salt
okunurdur: saate komut gitmez, girilen hiçbir şey kaydedilmez.

Kullanım: python onizleme.py <çıktı.html>
"""
import datetime
import json
import re
import sys
import urllib.request
from pathlib import Path

import foods

import os

BASE = os.environ.get("ONIZLEME_BASE", "http://localhost:8377")   # telefondaki uygulama için adb forward adresi
HERE = Path(__file__).parent


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.load(r)


SHIM = r"""
<script>
// uzaktan önizleme: ağ çağrıları sayfaya gömülü anlık verilerle yanıtlanır
(() => {
  const SNAP = __SNAP__;
  const fold = s => s.replace(/[ıİ]/g, 'i').replace(/[şŞ]/g, 's').replace(/[çÇ]/g, 'c').replace(/[ğĞ]/g, 'g').replace(/[öÖ]/g, 'o').replace(/[üÜ]/g, 'u').toLowerCase();
  let tip, timer;
  const toast = () => {
    if (!tip) { tip = document.createElement('div'); tip.setAttribute('role', 'status'); tip.textContent = 'Uzaktan önizleme: bu işlem yalnızca evdeki uygulamada çalışır.';
      tip.style.cssText = 'position:fixed;left:50%;translate:-50% 0;bottom:96px;z-index:9;max-width:calc(100% - 32px);padding:10px 16px;border-radius:16px;background:var(--ink);color:var(--bg);font-size:13px;font-weight:600;text-align:center;transition:opacity .3s'; document.body.append(tip); }
    tip.style.opacity = 1; clearTimeout(timer); timer = setTimeout(() => tip.style.opacity = 0, 2600);
  };
  window.addEventListener('unhandledrejection', e => e.preventDefault());
  const reply = body => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(JSON.parse(JSON.stringify(body))) });
  window.fetch = (url, opt) => {
    const u = new URL(url, 'http://x'), q = u.searchParams;
    if (opt && opt.method === 'POST') { toast(); return Promise.resolve({ ok: false, status: 403, json: () => Promise.resolve({ error: 'Bu sayfa uzaktan önizleme; değişiklikler evdeki uygulamadan yapılır.' }) }); }
    if (u.pathname === '/api/foods') {
      let rows = SNAP.foods.filter(f => !q.get('kind') || f.kind === q.get('kind'));
      const s = fold((q.get('q') || '').trim());
      if (s) rows = [...rows.filter(f => fold(f.name).startsWith(s)), ...rows.filter(f => !fold(f.name).startsWith(s) && fold(f.name).includes(s))];
      return reply(rows.slice(0, q.get('all') ? 30 : 8));
    }
    if (SNAP.real) {  // gerçek uyku, antrenman ve elle ölçüm kayıtları
      if (u.pathname === '/api/workout') return reply(SNAP.real.workout[q.get('id')] || null);
      if (u.pathname === '/api/sleep') return reply(SNAP.real.sleep[q.get('d')] || SNAP.real.sleep['']);
    }
    if (SNAP.demo) {
      if (u.pathname === '/api/sleep') return reply(SNAP.demo.sleep);
      if (u.pathname === '/api/workouts') return reply(SNAP.demo.workouts);
      if (u.pathname === '/api/workout') return reply(SNAP.demo['workout_' + q.get('id')] || null);
      if (u.pathname === '/api/manual') return reply(SNAP.demo.manual);
    }
    if (u.pathname === '/api/minutes' || u.pathname === '/api/report') {
      const part = SNAP[u.pathname.slice(5)];
      return reply(part[q.get('d')] || part.empty);
    }
    return reply(SNAP[u.pathname.slice(5)] ?? {});
  };
  try { localStorage.removeItem('layout'); } catch (e) {}  // önizleme uygulamanın şimdiki düzeniyle açılır
  window.EventSource = function () { this.close = () => {}; this.addEventListener = () => {}; };
  // "bugün", verinin alındığı gün olarak kalır
  const T0 = SNAP.taken * 1000, RealDate = Date;
  window.Date = class extends RealDate { constructor(...a) { if (a.length) super(...a); else super(T0); } static now() { return T0; } };
})();
</script>
"""

BANNER = (
    '<p class="note demo" style="padding-bottom:12px">Uzaktan önizleme: veriler __WHEN__ tarihinde alındı. '
    "Saate komut gitmez, girilenler kaydedilmez.</p>"
    '<div class="t w" style="margin-bottom:12px"><h3>Renk modunu dene</h3><p class="hint" style="margin:0 0 4px">Yeni, sade modlar</p><div class="chips" style="margin:0 0 10px">'
    + "".join(f'<button onclick="theme(\'{k}\', true)">{n}</button>' for k, n in (("kul", "Kül"), ("komur", "Kömür"), ("tas", "Taş"), ("kagit", "Kâğıt")))
    + '</div><p class="hint" style="margin:0 0 4px">Beğendiğin modlar</p><div class="chips" style="margin:0">'
    + "".join(f'<button onclick="theme(\'{k}\', true)">{n}</button>' for k, n in (("duman", "Duman"), ("gece", "Gece"), ("bej", "Bej"), ("krem", "Krem")))
    + "</div></div>"
)


def build():
    state = get("/api/state")
    state["updated"] = state["updated"] or int(datetime.datetime.now().timestamp())
    days = get("/api/days")
    known = [d["date"] for d in days if d.get("steps") is not None]
    today = datetime.date.today().isoformat()
    if today not in known:
        known.append(today)
    empty_day = (datetime.date.today() - datetime.timedelta(days=400)).isoformat()
    snap = {
        "taken": state["updated"],
        "state": state,
        "days": days,
        "week": get("/api/week"),
        "foods": [foods.as_dict(r) for r in foods.FOODS],
        "minutes": {d: get("/api/minutes?d=" + d) for d in known} | {"empty": get("/api/minutes?d=" + empty_day)},
        "report": {d: get("/api/report?d=" + d) for d in known} | {"empty": get("/api/report?d=" + empty_day)},
    }
    for key in ("body", "workouts", "manual", "eventlog"):
        try:
            snap[key] = get("/api/" + key)
        except Exception:
            pass
    try:  # gerçek kayıtlar: her antrenmanın ayrıntısı, bilinen günlerin uykusu
        snap["real"] = {"workout": {str(w["id"]): get("/api/workout?id=%s" % w["id"]) for w in snap.get("workouts", [])[:12]},
                        "sleep": {d: get("/api/sleep?d=" + d) for d in known[-10:]} | {"": get("/api/sleep")}}
    except Exception:
        pass
    if len(sys.argv) > 2:  # uyku ve antrenman ekranları için deneme dosyalarından üretilmiş veri
        snap["demo"] = json.loads(Path(sys.argv[2]).read_text())
    html = (HERE / "web" / "index.html").read_text()
    html = re.sub(
        r'<!doctype[^>]*>|</?html[^>]*>|</?head>|</body>|<meta charset[^>]*>|<meta name="viewport"[^>]*>'
        r'|<link rel="(?:preconnect|manifest|icon)"[^>]*>',
        "",
        html,
        flags=re.I,
    )
    html = re.sub(r"<title>.*?</title>", "<title>Smart Band</title>", html)
    html = re.sub(r"<body[^>]*>", "", html, count=1)
    when = datetime.datetime.fromtimestamp(state["updated"]).strftime("%d.%m.%Y %H:%M")
    html = html.replace(' download>', ' hidden>')
    html = html.replace('<section class="screen on" id="s-ozet">', '<section class="screen on" id="s-ozet">\n' + BANNER.replace("__WHEN__", when), 1)
    data = json.dumps(snap, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    boot = "<script>document.body.classList.add('boot');</script>"
    html = html.replace("<script>\nconst $", boot + SHIM.replace("__SNAP__", data) + "<script>\nconst $", 1)
    return html.lstrip()


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.write_text(build())
    print(out, out.stat().st_size, "bayt")
