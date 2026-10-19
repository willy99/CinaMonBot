"""
Карусель для LinkedIn (1080×1350): PNG-слайди + один PDF для поста-документа, uk і en.

    venv/bin/python scripts/demo_linkedin.py marketing/linkedin

Бере скріншоти з marketing/screenshots і marketing/bot (scripts/demo_screenshots.py, demo_bot_cards.py).
Цифри — з бази й реєстру станом на дату генерації (див. FACTS).
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "marketing/linkedin")

FACTS = {"shops": 47, "countries": 9, "ua_shops": 26, "render": 19, "tests": "1 783", "tests_en": "1,783"}

TEXT = {
    "uk": {
        "s1_kicker": "Розбір однієї «знижки»",
        "s1_title": "«Знижка −22%»",
        "s1_sub": "За місяць до неї ціну підняли на&nbsp;33%. Я зробив бота, який це бачить.",
        "s1_swipe": "гортай →",
        "s1_tag_note": "а 21 квітня коштувало <b>19 499 грн</b>",
        "s1_was": "26 999 грн", "s1_now": "20 999 грн",
        "s2_title": "Кермо MOZA R5 на Rozetka",
        "s2_sub": "Справжня історія ціни з бази бота",
        "s2_notes": [("21.04", "19 499 грн"), ("19.05", "25 999 грн · +33%"), ("05.06", "20 999 грн · «−22%»"), ("10.10", "26 999 грн")],
        "s2_foot": "Після «знижки» товар усе ще на&nbsp;1&nbsp;500&nbsp;грн дорожчий, ніж у квітні. 3 з&nbsp;12 товарів, за&nbsp;якими я&nbsp;стежив два місяці, мали таку схему.",
        "s3_title": "Як це працює",
        "s3_steps": ["Кидаєш боту посилання на&nbsp;товар", "Ставиш ціль: суму або «−15%»", "Бот пише, коли ціна справді впала"],
        "s4_title": "Тільки те, що ти просив",
        "s4_sub": "Сповіщення — коли дешевше. Щопонеділка — дайджест, і&nbsp;лише якщо щось подешевшало. Набридло «знову в&nbsp;наявності» — одна кнопка.",
        "s5_title": "Під капотом",
        "s5_items": [
            f"<b>{FACTS['shops']} перевірених магазинів</b> у&nbsp;{FACTS['countries']} країнах: Rozetka, Comfy, Notino, Foxtrot, IKEA, Zalando, eBay…",
            "Парсер без селекторів: читає структуровані дані сторінки (JSON-LD, microdata)",
            f"Headless Chromium для {FACTS['render']} сайтів, що віддають ціну лише через JS",
            "Розуміє варіанти: Notino 75&nbsp;мл і&nbsp;100&nbsp;мл — різні ціни",
            f"Python · aiogram · NiceGUI · Playwright · SQLite + Alembic · <b>{FACTS['tests']} автотести</b>",
        ],
        "s6_title": "Чорна п'ятниця — 27&nbsp;листопада",
        "s6_sub": "Додай товари зараз — і&nbsp;побачиш, чи знижка справжня.",
        "s6_free": "Безкоштовно до 5 товарів · українською та англійською",
        "s6_stats": [("47", "перевірених магазинів"), ("9", "країн"), ("5", "товарів безкоштовно")],
        "s6_shops": "Rozetka · Comfy · Notino · Foxtrot · Allo · MOYO · Citrus · Brain · Kasta · JYSK · Prom · OLX · IKEA · Nike · Zalando · OTTO · eBay · Etsy · AliExpress…".replace("AliExpress…", "Apple…"),
    },
    "en": {
        "s1_kicker": "Anatomy of one “discount”",
        "s1_title": "“−22% off”",
        "s1_sub": "A month earlier the price was raised by&nbsp;33%. I&nbsp;built a&nbsp;bot that sees it.",
        "s1_swipe": "swipe →",
        "s1_tag_note": "on April 21 it cost <b>₴19,499</b>",
        "s1_was": "₴26,999", "s1_now": "₴20,999",
        "s2_title": "MOZA R5 wheel on Rozetka",
        "s2_sub": "Real price history from the bot’s database",
        "s2_notes": [("Apr 21", "₴19,499"), ("May 19", "₴25,999 · +33%"), ("Jun 5", "₴20,999 · “−22%”"), ("Oct 10", "₴26,999")],
        "s2_foot": "After the “discount” it still cost ₴1,500 more than in April. 3 of the 12 products I&nbsp;tracked for two months followed the same pattern.",
        "s3_title": "How it works",
        "s3_steps": ["Send the bot a&nbsp;product link", "Set a target: an amount or “−15%”", "Get a message when the price really drops"],
        "s4_title": "Only what you asked for",
        "s4_sub": "An alert when it gets cheaper. A&nbsp;Monday digest — only if something dropped. Tired of “back in stock”? One tap.",
        "s5_title": "Under the hood",
        "s5_items": [
            f"<b>{FACTS['shops']} verified shops</b> in {FACTS['countries']} countries: Rozetka, Comfy, Notino, IKEA, Zalando, eBay…",
            "Selector-free parser: reads the page’s structured data (JSON-LD, microdata)",
            f"Headless Chromium for {FACTS['render']} sites that only show prices via JS",
            "Variant-aware: Notino 75&nbsp;ml and 100&nbsp;ml are different prices",
            f"Python · aiogram · NiceGUI · Playwright · SQLite + Alembic · <b>{FACTS['tests_en']} automated tests</b>",
        ],
        "s6_title": "Black Friday — Nov&nbsp;27",
        "s6_sub": "Add your wishlist now and you’ll see whether the deal is real.",
        "s6_free": "Free for up to 5 items · Ukrainian and English",
        "s6_stats": [("47", "verified shops"), ("9", "countries"), ("5", "items free")],
        "s6_shops": "Rozetka · Comfy · Notino · Foxtrot · Allo · MOYO · Citrus · Brain · Kasta · JYSK · Prom · OLX · IKEA · Nike · Zalando · OTTO · eBay · Etsy · Apple…",
    },
}

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Unbounded:wght@500;700;900&family=Geologica:wght@300;400;500;600&display=swap');
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { background: #0a0d12; }
.slide { width: 1080px; height: 1350px; position: relative; overflow: hidden; padding: 88px 84px;
         font-family: 'Geologica', system-ui, sans-serif; color: #e9eef5; page-break-after: always;
         background: radial-gradient(900px 700px at 85% -10%, rgba(57,135,229,.28), transparent 60%),
                     radial-gradient(800px 700px at -10% 110%, rgba(124,58,237,.22), transparent 60%), #0a0d12; }
.brand { position: absolute; left: 84px; bottom: 64px; display: flex; align-items: center; gap: 14px; color: #93a4b8; font-size: 24px; }
.ava { width: 44px; height: 44px; border-radius: 50%; background: linear-gradient(135deg,#3987e5,#7c3aed); color:#fff;
       display:flex; align-items:center; justify-content:center; font-family:'Unbounded'; font-weight:700; font-size: 20px; }
.page { position: absolute; right: 84px; bottom: 70px; color: #5d6b7c; font-size: 22px; }
.kicker { font-size: 28px; color: #7fb3f0; letter-spacing: .02em; text-transform: uppercase; font-weight: 600; }
h1 { font-family: 'Unbounded', sans-serif; font-weight: 900; font-size: 150px; line-height: 1.02; letter-spacing: -0.02em; margin-top: 40px; }
h2 { font-family: 'Unbounded', sans-serif; font-weight: 700; font-size: 66px; line-height: 1.1; letter-spacing: -0.01em; }
.sub { font-size: 40px; line-height: 1.35; color: #c3cdd9; margin-top: 36px; font-weight: 300; }
.big-sub { font-size: 50px; line-height: 1.3; color: #e9eef5; margin-top: 56px; font-weight: 400; }
.swipe { position: absolute; right: 84px; top: 96px; color: #7fb3f0; font-size: 28px; }
.red { color: #ff8a7a; }
.chart { margin-top: 40px; background: rgba(255,255,255,.035); border: 1px solid rgba(255,255,255,.08); border-radius: 28px; padding: 36px 30px 24px; }
.notes { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 30px; margin-top: 28px; font-size: 30px; }
.notes .d { color: #8796a8; font-size: 24px; } .notes .v { color: #e9eef5; font-weight: 500; }
.foot { margin-top: 30px; font-size: 30px; line-height: 1.4; color: #c3cdd9; font-weight: 300; }
.steps { margin-top: 48px; display: flex; flex-direction: column; gap: 22px; }
.step { display: flex; gap: 22px; align-items: center; font-size: 36px; line-height: 1.25; }
.num { flex: none; width: 62px; height: 62px; border-radius: 50%; background: #3987e5; color: #fff; display:flex;
       align-items:center; justify-content:center; font-family:'Unbounded'; font-weight:700; font-size: 28px; }
.phones { position: absolute; left: 84px; right: 84px; bottom: 150px; display: flex; gap: 28px; justify-content: center; align-items: flex-end; }
.phones img { border-radius: 26px; box-shadow: 0 30px 80px rgba(0,0,0,.55); }
ul.items { list-style: none; margin-top: 44px; display: flex; flex-direction: column; gap: 22px; }
ul.items li { font-size: 31px; line-height: 1.35; color: #d3dbe5; padding-left: 40px; position: relative; font-weight: 300; }
ul.items li b { color: #fff; font-weight: 600; }
ul.items li:before { content: ""; position: absolute; left: 0; top: 15px; width: 16px; height: 16px; border-radius: 4px; background: #3987e5; }
.shot { position: absolute; left: 84px; right: -120px; bottom: 140px; height: 430px; border-radius: 22px; overflow: hidden;
        border: 1px solid rgba(255,255,255,.1); box-shadow: 0 30px 80px rgba(0,0,0,.6); }
.shot img { width: 1300px; display: block; }
.cta { margin-top: 64px; display: flex; gap: 48px; align-items: center; }
.handle { font-family: 'Unbounded'; font-weight: 700; font-size: 62px; color: #fff; }
.free { font-size: 30px; color: #93a4b8; margin-top: 18px; }
#qr { background: #fff; padding: 18px; border-radius: 20px; }
.tag { position: absolute; left: 84px; right: 84px; bottom: 170px; background: rgba(255,255,255,.04);
       border: 1px solid rgba(255,255,255,.09); border-radius: 30px; padding: 44px 48px; }
.tag-row { display: flex; align-items: baseline; gap: 30px; flex-wrap: wrap; }
.was { font-size: 48px; color: #8796a8; text-decoration: line-through; text-decoration-thickness: 3px; }
.now { font-family: 'Unbounded'; font-weight: 700; font-size: 76px; color: #fff; }
.pill { font-family: 'Unbounded'; font-weight: 700; font-size: 34px; color: #fff; background: #d03b3b; padding: 8px 18px; border-radius: 14px; }
.tag-note { margin-top: 26px; font-size: 38px; color: #ffb4a8; font-weight: 400; }
.tag-note b { color: #fff; font-weight: 600; }
.stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 22px; margin-top: 70px; }
.stat { background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.09); border-radius: 24px; padding: 28px 30px; }
.stat .n { font-family: 'Unbounded'; font-weight: 700; font-size: 72px; color: #fff; }
.stat .l { font-size: 26px; color: #93a4b8; margin-top: 6px; line-height: 1.25; }
.shops { margin-top: 40px; font-size: 30px; line-height: 1.5; color: #c3cdd9; font-weight: 300; }
.crop { position: absolute; left: 0; right: 0; bottom: 140px; height: 740px; display: flex; justify-content: center; -webkit-mask-image: linear-gradient(to bottom, transparent 0, #000 110px);
        align-items: flex-end; overflow: hidden; }
.crop img { width: 560px; border-radius: 26px; box-shadow: 0 30px 80px rgba(0,0,0,.55); }
"""


def chart_svg(lang: str) -> str:
    """
    Ступінчаста історія ціни MOZA (справжні виміри з бази), вісь — дні від 21.04.
    28.06–09.10 бот не працював: там не лінія, а позначка «немає даних», щоб не вигадувати ціну.
    """
    obs = [(0, 19499), (28, 25999), (29, 26999), (45, 20999), (68, 20999)]   # до розриву
    after = [(172, 26999), (180, 26999)]                                        # після розриву
    W, H, pad_l, pad_b, pad_t = 852, 380, 92, 30, 30
    x = lambda d: pad_l + (W - pad_l - 20) * d / 180
    y = lambda v: pad_t + (H - pad_t - pad_b) * (27500 - v) / (27500 - 18500)

    def step(points):
        path = f"M{x(points[0][0]):.0f},{y(points[0][1]):.0f}"
        for (_, _), (d1, v1) in zip(points, points[1:]):
            path += f" H{x(d1):.0f} V{y(v1):.0f}"
        return path

    p1, p2 = step(obs), step(after)
    area1 = p1 + f" V{y(18500):.0f} H{x(0):.0f} Z"
    area2 = p2 + f" V{y(18500):.0f} H{x(172):.0f} Z"
    grid = "".join(
        f'<line x1="{pad_l}" x2="{W - 20}" y1="{y(v):.0f}" y2="{y(v):.0f}" stroke="rgba(255,255,255,.08)"/>'
        f'<text x="{pad_l - 14}" y="{y(v) + 8:.0f}" text-anchor="end" fill="#7d8b9c" font-size="22">{v // 1000}k</text>'
        for v in (19000, 21000, 23000, 25000, 27000))
    drop_lbl = "«−22%»" if lang == "uk" else "“−22%”"
    gap_lbl = "немає даних" if lang == "uk" else "no data"
    gx0, gx1 = x(68) + 6, x(172) - 6
    return f"""<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="Geologica">
      {grid}
      <rect x="{gx0:.0f}" y="{pad_t}" width="{gx1 - gx0:.0f}" height="{H - pad_t - pad_b}" rx="10"
            fill="rgba(255,255,255,.03)" stroke="rgba(255,255,255,.12)" stroke-dasharray="6 8"/>
      <text x="{(gx0 + gx1) / 2:.0f}" y="{y(23000):.0f}" text-anchor="middle" fill="#7d8b9c" font-size="24">{gap_lbl}</text>
      <path d="{area1}" fill="rgba(57,135,229,.14)"/><path d="{area2}" fill="rgba(57,135,229,.14)"/>
      <path d="{p1}" fill="none" stroke="#5aa2f0" stroke-width="5" stroke-linejoin="round"/>
      <path d="{p2}" fill="none" stroke="#5aa2f0" stroke-width="5" stroke-linejoin="round"/>
      <circle cx="{x(28):.0f}" cy="{y(25999):.0f}" r="9" fill="#ff8a7a" stroke="#0e131a" stroke-width="4"/>
      <text x="{x(28) - 18:.0f}" y="{y(25999) + 10:.0f}" text-anchor="end" fill="#ff8a7a" font-size="30" font-weight="600">+33%</text>
      <circle cx="{x(45):.0f}" cy="{y(20999):.0f}" r="9" fill="#5aa2f0" stroke="#0e131a" stroke-width="4"/>
      <text x="{x(45) - 4:.0f}" y="{y(20999) + 46:.0f}" fill="#9fc6f5" font-size="30" font-weight="600">{drop_lbl}</text>
      <circle cx="{x(172):.0f}" cy="{y(26999):.0f}" r="9" fill="#5aa2f0" stroke="#0e131a" stroke-width="4"/>
      <line x1="{x(0):.0f}" x2="{x(68):.0f}" y1="{y(19499):.0f}" y2="{y(19499):.0f}" stroke="#7d8b9c" stroke-dasharray="6 8"/>
    </svg>"""


def slides(lang: str) -> list[str]:
    t = TEXT[lang]
    brand = '<div class="brand"><div class="ava">Ц</div>ЦінаБот · @cina_mon_bot</div>'
    page = lambda n: f'<div class="page">{n}/6</div>'
    s = []
    s.append(f'''<section class="slide"><div class="swipe">{t["s1_swipe"]}</div>
        <div class="kicker">{t["s1_kicker"]}</div><h1>{t["s1_title"]}</h1>
        <div class="big-sub">{t["s1_sub"]}</div>
        <div class="tag"><div class="tag-row"><span class="was">{t["s1_was"]}</span><span class="now">{t["s1_now"]}</span>
        <span class="pill">−22%</span></div><div class="tag-note">{t["s1_tag_note"]}</div></div>{brand}{page(1)}</section>''')
    notes = "".join(f'<div><div class="d">{d}</div><div class="v">{v}</div></div>' for d, v in t["s2_notes"])
    s.append(f'''<section class="slide"><h2 style="font-size:56px">{t["s2_title"]}</h2><div class="sub" style="margin-top:18px">{t["s2_sub"]}</div>
        <div class="chart">{chart_svg(lang)}</div><div class="notes">{notes}</div>
        <div class="foot">{t["s2_foot"]}</div>{brand}{page(2)}</section>''')
    steps = "".join(f'<div class="step"><div class="num">{i}</div><div>{st}</div></div>' for i, st in enumerate(t["s3_steps"], 1))
    s.append(f'''<section class="slide"><h2>{t["s3_title"]}</h2><div class="steps">{steps}</div>
        <div class="crop"><img src="../../bot/02-add-{lang}.png"></div>{brand}{page(3)}</section>''')
    s.append(f'''<section class="slide"><h2>{t["s4_title"]}</h2><div class="sub">{t["s4_sub"]}</div>
        <div class="phones"><img src="../../bot/03-price-drop-{lang}.png" style="width:440px">
        <img src="../../bot/04-digest-{lang}.png" style="width:440px"></div>{brand}{page(4)}</section>''')
    items = "".join(f"<li>{i}</li>" for i in t["s5_items"])
    shot = "01-dashboard-uk-dark" if lang == "uk" else "01-dashboard-en-light"
    s.append(f'''<section class="slide"><h2>{t["s5_title"]}</h2><ul class="items">{items}</ul>
        <div class="shot"><img src="../../screenshots/{shot}.png"></div>{brand}{page(5)}</section>''')
    s.append(f'''<section class="slide"><h2>{t["s6_title"]}</h2><div class="big-sub">{t["s6_sub"]}</div>
        <div class="cta"><div id="qr"></div><div><div class="handle">@cina_mon_bot</div>
        <div class="free">{t["s6_free"]}</div></div></div>
        <div class="stats">{"".join(f'<div class="stat"><div class="n">{n}</div><div class="l">{l}</div></div>' for n, l in t["s6_stats"])}</div>
        <div class="shops">{t["s6_shops"]}</div>{brand}{page(6)}</section>''')
    return s


def html(lang: str) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style>"
            "<script src='https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js'></script></head><body>"
            + "".join(slides(lang)) +
            "<script>new QRCode(document.getElementById('qr'), {text: 'https://t.me/cina_mon_bot?start=linkedin', "
            "width: 230, height: 230, colorDark: '#0a0d12', colorLight: '#ffffff'});</script></body></html>")


def main() -> None:
    build = OUT / "_build"
    build.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for lang in ("uk", "en"):
            src = build / f"carousel-{lang}.html"
            src.write_text(html(lang), encoding="utf-8")
            ctx = browser.new_context(viewport={"width": 1080, "height": 1350}, device_scale_factor=1)
            page = ctx.new_page()
            # _build/ лежить у marketing/linkedin → картинки з ../../ ; підміняємо базу шляхів
            page.goto(src.resolve().as_uri())
            page.wait_for_load_state("networkidle")
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(600)
            for i, el in enumerate(page.locator("section.slide").all(), 1):
                path = OUT / f"slide-{i}-{lang}.png"
                el.screenshot(path=str(path))
                print("saved", path)
            pdf = OUT / f"carousel-{lang}.pdf"
            page.pdf(path=str(pdf), width="1080px", height="1350px", print_background=True,
                     margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
            print("saved", pdf)
            ctx.close()
        browser.close()


if __name__ == "__main__":
    main()
