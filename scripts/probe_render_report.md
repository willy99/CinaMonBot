# Проба через headless Chromium: що рятує Playwright

Дата проби: **2026-10-11**. Інструмент: `scripts/probe_render.py` → `app/services/scrapers/render.py`
(той самий рендерер, що й у боті): Playwright 1.63.0, Chromium 153 у новому headless-режимі
(`channel="chromium"`), `--disable-blink-features=AutomationControlled`, контекст на домен із локаллю/часовим
поясом за TLD (uk-UA/Europe/Kyiv, pl-PL, de-DE, en-US …), viewport 1366×768, картинки/шрифти/медіа заблоковані,
`domcontentloaded` → очікування JSON-LD Product / `[itemprop=price]` / OG-ціни до 10 с → `networkidle` до 8 с;
Cloudflare «Just a moment» — до 12 с на зміну title. Один рендер на магазин, 2 воркери, 3 с пауза, IP — Україна.
Без проксі та платних API. Увесь прогін 39 магазинів — **216 с**.

Пробувалися всі записи реєстру зі статусом `needs_js` або `blocked` після проби 2026-10-10
(`scripts/probe_report.md`). Результат: **ok** — назва і додатна ціна через `extract_product`;
**blocked** — бот-стіна і після рендеру; **timeout/failed** — не вклалися / нічого не витягли.

## Таблиця

| domain | було | рендер | стратегія | назва / ціна | с | нотатки |
|---|---|---|---|---|---|---|
| comfy.ua | blocked | **ok** | json-ld | Смартфон Apple iPhone 16 128Gb Black / 40 499 UAH | 3 | Cloudflare пропускає headless Chromium |
| eldorado.ua | needs_js | **ok** | microdata | Навушники PANASONIC RP-HJE125E-W / 169 UAH | 2 | старий sample (iPhone 14 Pro) знято з продажу → price=0 було справжнім; новий URL із sitemap |
| epicentrk.ua | needs_js | failed | — | (title є) / — | 11 | Nuxt: і після рендеру ні JSON-LD/microdata, ні «грн» у DOM |
| makeup.com.ua | blocked | failed | — | (h1 є) / — | 11 | HTTP 202, назва є, але ціну вантажить API, який AWS WAF не пускає; евристика дала сміття — фікстуру видалено |
| intertop.ua | blocked | **ok** | json-ld | Кросівки NIKE Air Force 1 / 4 334 UAH | 2 | Cloudflare пропускає |
| tabletki.ua | blocked | **ok** | json-ld | Нурофєн таблетки 200 мг №12 / 107.5 UAH | 2 | Cloudflare пропускає |
| sportmaster.ua | blocked | failed | — | — | 0 | NXDOMAIN |
| answear.ua | blocked | **ok** | json-ld | Gestuz пальто жіноче / 21 299 UAH | 2 | deny-сторінку браузер проходить; старий sample → 404, товар знайдено з /c/vona |
| mobilluck.com.ua | blocked | **ok** | json-ld | Samsung Galaxy S10 Duos / 12 403 UAH | 4 | httpx висів 20 с, браузер — 4 с |
| allegro.pl | blocked | blocked | — | — | 11 | DataDome 403 + капча і в браузері |
| ceneo.pl | needs_js | blocked | — | — | 10 | та сама JS-оболонка без title |
| x-kom.pl | blocked | blocked | — | — | 11 | Cloudflare-челендж **пройдено**, але далі власна заглушка «Cierpliwości…» |
| mediaexpert.pl | blocked | **ok** | json-ld | Smartfon APPLE iPhone 16 128GB / 3 999 PLN | 2 | власну перевірку трафіку браузер проходить |
| zalando.de | blocked | **ok** | json-ld | AIR FORCE 1 '07 / 83.95 EUR | 5 | Akamai пропускає повний Chromium (headless shell — таймаут) |
| otto.de | blocked | **ok** | json-ld | BOSCH Waschmaschine Serie 6 / 649 EUR | 3 | Kasada: перша відповідь 400, JS проходить челендж |
| mediamarkt.de | blocked | blocked | — | — | 10 | Cloudflare «Nur einen Moment…» (Turnstile) |
| saturn.de | blocked | blocked | — | — | 10 | те саме |
| idealo.de | blocked | **ok** | json-ld | Citizen Promaster Mechanical GMT / 636 EUR | 3 | товар визначає id у URL (slug ігнорується) |
| alza.cz | blocked | **ok** | json-ld | iPhone 16 128GB černá / 22 990 CZK | 2 | Cloudflare пропускає |
| bol.com | blocked | blocked | — | — | 11 | заглушка «bol» і в браузері |
| cdiscount.com | blocked | **ok** | json-ld | APPLE iPhone 16 128GB White / 999 EUR | 2 | JS-челендж проходить |
| fnac.com | blocked | blocked | — | — | 10 | 403 без контенту |
| elgiganten.se | blocked | **ok** | json-ld | iPhone 16 128GB Black / 10 990 SEK | 4 | Vercel Security Checkpoint (429) проходить |
| walmart.com | blocked | blocked | — | — | 11 | F5 «Request Rejected» (444) |
| target.com | blocked | blocked | — | — | 10 | 429, порожня сторінка |
| bestbuy.com | blocked | blocked | — | — | 10 | Akamai «Access Denied» |
| ebay.com | blocked | **ok** | json-ld | Apple iPhone 15 128GB Unlocked / 422.95 USD | 3 | |
| etsy.com | blocked | **ok** | json-ld | Handmade Ceramic Mug / 46 USD | 2 | DataDome тут пропускає (на відміну від allegro) |
| newegg.com | blocked | blocked | — | — | 11 | Cloudflare «Just a moment» не проходить |
| bhphotovideo.com | blocked | blocked | — | — | 12 | інтерактивний Cloudflare-челендж не розв'язується |
| argos.co.uk | blocked | blocked | — | — | 11 | Akamai «Access Denied» |
| currys.co.uk | blocked | **ok** | json-ld | APPLE Refurbished AirPods 4 / 139 GBP | 3 | Cloudflare пропускає; старий sample → редірект на категорію (евристика дала ціну зі списку — фікстуру замінено) |
| johnlewis.com | blocked | **ok** | json-ld | Apple AirPods Pro 3 / 219 GBP | 3 | httpx висів, браузер — 3 с |
| asos.com | blocked | **ok** | json-ld | Nike Air Force 1 '07 / 174 USD | 3 | Akamai пропускає; валюта за гео |
| adidas.com | blocked | blocked | — | — | 19 | Akamai: 403-заглушка «adidas» |
| aliexpress.com | needs_js | **ok** | json-ld | INMAS Q26s Earbuds / 695.24 UAH | 5 | ціна/валюта за гео |
| amazon.com | needs_js | failed | — | — | 12 | порожня сторінка без title (robot check) |
| hillary.ua (Horoshop) | blocked | (ok) | heuristic | головна/категорія | 11–13 | JS-челендж Horoshop **проходить**, але URL товару не знайдено — лишено experimental |
| quality.in.ua (Horoshop) | blocked | **ok** | microdata | Телевізор TCL 98P745 / 95 000 UAH | 4 | JS-челендж проходить; товар `/televizor-tcl-98p745/` |

## Підсумок

| результат | кількість |
|---|---|
| **ok → verified, render_js=True** | **20**: comfy, eldorado, intertop, tabletki, answear, mobilluck, mediaexpert, zalando, otto, idealo, alza, cdiscount, elgiganten, ebay, etsy, currys, johnlewis, asos, aliexpress, quality.in.ua |
| blocked і в браузері | 15: allegro, ceneo, x-kom, mediamarkt, saturn, bol, fnac, walmart, target, bestbuy, newegg, bhphotovideo, argos, adidas, amazon |
| сторінка є, ціни немає | 2: epicentrk (needs_js), makeup (blocked: WAF ріже API ціни) |
| інше | sportmaster (NXDOMAIN), hillary (experimental — товар не підтверджено) |

Фікстури: `tests/fixtures/<domain>.render.html` + `.render.json` для всіх 20 (скорочені: `<head>` без
скриптів/стилів + ld+json + microdata). Вони проходять і `tests/test_extract.py::test_real_fixture_pages`,
і детектор бот-стін (`test_bot_wall_detector_no_false_positives`).

## Антибот-вендори: хто пропускає headless Chromium

| вендор | проходить? | приклади |
|---|---|---|
| Cloudflare, звичайний JS-челендж («Just a moment») | **так** (comfy, intertop, tabletki, alza, currys) | — |
| Cloudflare з інтерактивним Turnstile/капчею | **ні** | mediamarkt, saturn, newegg, bhphotovideo |
| Akamai Bot Manager | **залежить від конфігурації**: zalando, asos — так; bestbuy, argos, adidas — ні | |
| Kasada | так (otto.de — 400 → JS-челендж → сторінка) | |
| DataDome | залежить: etsy — так; allegro — капча | |
| F5 (Request Rejected) | ні | walmart |
| Vercel Security Checkpoint | так | elgiganten |
| AWS WAF (CloudFront challenge) | сторінка — так, API ціни — ні | makeup |
| Horoshop JS-челендж (cookie `challenge_passed`) | так | quality.in.ua, hillary |
| Власні заглушки (x-kom «Cierpliwości», bol, fnac, target 429, amazon robot check) | ні | |

Висновок: де є **лише JS-челендж** (перевірка, що це браузер) — Chromium проходить; де є **аналіз поведінки /
репутації IP / інтерактивна капча** — ні. Жодних «стелс»-плагінів не використано; хто не пройшов, тому
потрібен житловий проксі або розв'язувач капч (поза межами задачі).

## Повний Chromium чи headless shell

Playwright ставить обидва. Порівняння на тих самих URL:

| | `channel="chromium"` (новий headless, **за замовчуванням**) | headless shell (`RENDER_BROWSER_CHANNEL=`) |
|---|---|---|
| пам'ять після 1 сторінки (footprint `top`) | ~735 MB, 9 процесів | ~380 MB, 4 процеси |
| comfy.ua (Cloudflare) | ok | ok |
| zalando.de (Akamai) | ok, 5 с | таймаут 30 с |
| otto.de (Kasada) | ok | blocked |
| etsy.com (DataDome) | ok | blocked |

Тобто економити пам'ять headless-shell'ом не варто: він губить половину виграшу.

## Ресурси (Mac, Chromium 153)

- Запуск браузера: ~2.5–3 с (один раз; далі живе між рендерами, закривається через
  `RENDER_IDLE_CONTEXT_MINUTES` простою разом із контекстами).
- Пам'ять: ~735 MB фізично після першого рендеру, ~840 MB із трьома контекстами доменів;
  `ps rss` показує 1.1–1.2 GB, бо рахує спільні сторінки кілька разів. Під час проби з 2 воркерами й 16
  процесами сума RSS сягала 3 GB (реально ~1.5 GB). Після `close_renderer()` — 0.
- CPU: 7–10 % у середньому під час проби (пікові сплески на рендері).
- Час: типовий успішний рендер 2–5 с; невдалий — 10–12 с (вичерпали очікування даних і networkidle);
  жорстка межа `RENDER_TIMEOUT_SECONDS` (35) + 10 с.

## Що змінено в коді

- `app/services/scrapers/render.py` — `BrowserRenderer` (singleton `get_renderer()`, `close_renderer()`).
- `registry.Platform.render_js` — 20 доменів позначено; `scraper.py` для них одразу йде через браузер, для
  решти після `blocked`/`parse_failed`/товару без ціни робить одну спробу рендеру (`RENDER_FALLBACK=true`)
  і пише `render_fallback_success` у лог. Стратегія в `scrape_log` отримує суфікс `+render`.
- Налаштування: `RENDER_MAX_CONCURRENCY=2`, `RENDER_TIMEOUT_SECONDS=35`, `RENDER_FALLBACK=true`,
  `RENDER_IDLE_CONTEXT_MINUTES=10`, `RENDER_BROWSER_CHANNEL=chromium` (env або `Settings`).
- Повторна проба: `venv/bin/python scripts/probe_render.py [--only d1,d2] [--url domain=URL] [--status blocked]`.
