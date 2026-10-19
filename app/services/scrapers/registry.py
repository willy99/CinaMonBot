"""
Реєстр платформ (магазинів), які бот уміє парсити.

Це довідник для людей і дашборду, а не механізм роботи парсера: універсальний
парсер приймає будь-який http(s) URL і шукає структуровані дані (JSON-LD,
microdata, OpenGraph). Реєстр каже, які магазини перевірені вручну, які
потребують JS-рендерингу, а які блокують запити.

Статуси:
    verified      — перевірено на реальній сторінці: парситься без JS або (render_js=True) через
                    headless Chromium (app/services/scrapers/render.py)
    experimental  — має працювати (є структуровані дані), але не перевірено
    needs_js      — сторінка рендериться на клієнті, httpx не бачить даних, і рендер теж не допоміг
    blocked       — сайт блокує і httpx, і headless Chromium (капча/403)

Доступ:
    platform_for("www.comfy.ua")  → Platform або None
    platform_name("comfy.ua")     → "Comfy"  (для невідомих — сам домен)
    verified_platforms()          → список для /sites і дашборду
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

PlatformStatus = Literal["verified", "experimental", "needs_js", "blocked"]


@dataclass(frozen=True)
class Platform:
    domain: str                 # базовий домен без www: "comfy.ua"
    name: str                   # як показувати людям: "Comfy"
    country: str                # ISO-2: "UA", "US", "PL", "DE", "GLOBAL"
    status: PlatformStatus = "experimental"
    # Яка стратегія реально спрацювала при перевірці: "json-ld" | "microdata" | "opengraph" | "selectors"
    strategy: str = ""
    notes: str = ""             # людською: особливості, дата перевірки, що не так
    sample_url: str = ""        # сторінка товару, на якій перевіряли
    aliases: tuple[str, ...] = field(default_factory=tuple)  # інші домени того ж магазину: ("comfy.com.ua",)
    # Парсити через headless Chromium (app/services/scrapers/render.py), а не httpx:
    # для магазинів, що без JS не віддають ціну або ховаються за Cloudflare. Див. scripts/probe_render_report.md
    render_js: bool = False
    # Аукціон: ціна рухається ставками; при додаванні питаємо напрямок цілі (перевищить / впаде нижче),
    # загальних сповіщень «ціна впала на 3%» не шлемо, нагадуємо перед кінцем торгів
    auction: bool = False


# Заповнено за результатами проби 2026-10-10 (scripts/probe_platforms.py, звіт у
# scripts/probe_report.md): один GET на сторінку товару заголовками бота, без JS.
# Доповнено пробою 2026-10-11 через Playwright (scripts/probe_render.py, звіт у
# scripts/probe_render_report.md): магазини з render_js=True парсяться лише через браузер.
# Спеціальні парсери (rozetka/olx/prom) теж тут, щоб дашборд бачив усе в одному місці.
# Порядок: UA → ЄС → США/глобальні → приклади платформ (Shopify/WooCommerce/Horoshop/PrestaShop/Magento).
PLATFORMS: list[Platform] = [
    # ── Україна: власні парсери ──
    Platform("rozetka.com.ua", "Rozetka", "UA", "verified", "selectors", "власний парсер + JSON-LD fallback"),
    Platform("olx.ua", "OLX", "UA", "verified", "json-ld", "власний парсер"),
    Platform("prom.ua", "Prom.ua", "UA", "verified", "selectors", "власний парсер"),
    Platform("violity.com", "Violity", "UA", "verified", "violity-lot",
             "власний парсер, перевірено 2026-10-19: аукціон — ціна = поточна ставка в грн (метатеги дають USD), "
             "кінець торгів → «немає в наявності». Ціна на аукціоні росте, тож сповіщення про зниження тут рідкісні.",
             "https://violity.com/ua/124306466-breguet-marine-chronograph-5827br-z2-5zu", auction=True),
    Platform("notino.ua", "Notino", "UA", "verified", "notino-state",
             "власний парсер, перевірено 2026-10-13: ціна варіанта (об'єм/відтінок із /p-<id>/) зі стану сторінки "
             "__APOLLO_STATE__ — JSON-LD завжди описує варіант за замовчуванням. httpx, без браузера.",
             "https://www.notino.ua/biorepair/total-protective-repair-zmicnjujucha-zubna-pasta/"),
    # ── Україна: універсальний парсер ──
    Platform("comfy.ua", "Comfy", "UA", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: Cloudflare пропускає headless Chromium, json-ld за ~3 с "
             "(2026-10-10 httpx: «Just a moment» 403; 11.10 httpx теж проходив, але покладатись не варто)",
             "https://comfy.ua/ua/smartfon-apple-iphone-16-128gb-black.html", render_js=True),
    Platform("foxtrot.com.ua", "Foxtrot", "UA", "verified", "json-ld",
             "2026-10-10: один Product, offers.price, UAH",
             "https://www.foxtrot.com.ua/uk/shop/smartfoniy-i-mobilniye-telefoniy-apple-smartfon-iphone-16-plus-128gb-blue.html"),
    Platform("allo.ua", "Allo", "UA", "verified", "json-ld",
             "2026-10-10: Product з offers.price; сторінка 1.6 MB — парсити лише head/ld+json",
             "https://allo.ua/ua/televizory/televizor-xiaomi-tv-a-pro-43-2026.html"),
    Platform("eldorado.ua", "Eldorado", "UA", "verified", "microdata",
             "через Playwright, перевірено 2026-10-11: після рендеру microdata з ціною > 0 (~2 с). "
             "httpx бачить microdata з price=0.00 — ціну домальовує JS; старий sample (iPhone 14 Pro) знято з продажу",
             "https://eldorado.ua/uk/offer_1226888/p71094589/", render_js=True),
    Platform("moyo.ua", "MOYO", "UA", "verified", "microdata",
             "2026-10-10: JSON-LD без Product; microdata — name брати з власного scope Product (у вкладеному Brand name=«ACER»)",
             "https://www.moyo.ua/ua/noutbuk_acer_extensa_exo15-41_nx_elheu_002/853624.html"),
    Platform("epicentrk.ua", "Епіцентр", "UA", "needs_js", "",
             "2026-10-10: Nuxt SSR без JSON-LD/microdata/OG-ціни, у стані price:0. "
             "Рендер 2026-10-11: сторінка відкривається, але ні розмітки, ні «грн» у DOM — не парситься й через Playwright",
             "https://epicentrk.ua/ua/shop/smartfon-samsung-galaxy-a56-5g-8-256gb-awesome-graphite-sm-a566bzkceuc.html"),
    Platform("citrus.ua", "Citrus", "UA", "verified", "json-ld",
             "2026-10-10: ctrs.com.ua → 301 citrus.ua; Product в окремому ld+json-блоці",
             "https://citrus.ua/zmishuvach-dlya-bide/zmishuvach-dlya-bide-deante-silia-z-gigenichnim-dushem-titan-bqs-d34m-760004.html",
             aliases=("ctrs.com.ua",)),
    Platform("brain.com.ua", "Brain", "UA", "verified", "json-ld",
             "2026-10-10: Product з offers.price; сторінка 2.9 MB",
             "https://brain.com.ua/ukr/Mobilniy_telefon_Samsung_Galaxy_A27_5G_6_128Gb_Black_SM-A276BZKBEUC-p1353675.html"),
    Platform("stls.store", "Stylus", "UA", "verified", "json-ld",
             "2026-10-10: stylus.ua → 301 stls.store; og:type нестандартний («og:product»)",
             "https://stls.store/apple-iphone-16-pro-max-256gb-natural-titanium-mywy3-approved-vitrinnyy-obrazec-p1428052c13877.html",
             aliases=("stylus.ua",)),
    Platform("telemart.ua", "Telemart", "UA", "verified", "json-ld",
             "2026-10-10: offers — масив Offer, брати перший з price",
             "https://telemart.ua/ua/products/asus-rt-ax52/"),
    Platform("kasta.ua", "Kasta", "UA", "verified", "json-ld",
             "2026-10-10: ціна лише в offers.priceSpecification.price (UnitPriceSpecification)",
             "https://kasta.ua/uk/product/18797508:33126731/"),
    Platform("makeup.com.ua", "MAKEUP", "UA", "blocked", "",
             "2026-10-10: HTTP 202 з порожнім тілом (CloudFront/AWS WAF challenge). "
             "Рендер 2026-10-11: HTML з назвою є (202), але ціну вантажить API, який WAF не пускає — ціни в DOM немає",
             "https://makeup.com.ua/ua/product/919818/"),
    Platform("zakupka.com", "Zakupka", "UA", "verified", "json-ld",
             "2026-10-10: канонічні товари zakupka.com/uk/p/<id>-slug/; дзеркала <shop>.zakupka.com/p/… часто 404",
             "https://zakupka.com/uk/p/1646193193-detskiy-muzykalnyy-telefon-elza/"),
    Platform("bigl.ua", "Bigl", "UA", "verified", "json-ld",
             "2026-10-10: як prom.ua; чистити ?click_track_data= з посилань",
             "https://bigl.ua/ua/p2787356605-udarnyj-akkumulyatornyj-shurupovert"),
    Platform("intertop.ua", "Intertop", "UA", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: Cloudflare пропускає headless Chromium, json-ld за ~2 с "
             "(httpx: «Just a moment» 403)",
             "https://intertop.ua/uk-ua/product/sneakers-nike-9662581", render_js=True),
    Platform("apteka911.ua", "Аптека 911", "UA", "verified", "json-ld",
             "2026-10-10: 2 Product, перший головний; ціна в offers.lowPrice (AggregateOffer)",
             "https://apteka911.ua/ua/shop/mukaltin-tabl-50mg-30-p12645"),
    Platform("tabletki.ua", "Tabletki.ua", "UA", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: Cloudflare пропускає headless Chromium, json-ld за ~2 с "
             "(httpx: «Just a moment» 403)",
             "https://tabletki.ua/uk/Nurofen/13347/", render_js=True),
    Platform("jysk.ua", "JYSK", "UA", "verified", "json-ld",
             "2026-10-10: ProductGroup + hasVariant; поточний товар = hasVariant[0] (name=h1); у решти price «18999 грн.»; зниклий товар → 301 на категорію з ItemList",
             "https://jysk.ua/spalnya/matraci/pruzhinni-matraci/matrats-pruzhynnyy-80x200sm-tenna-vysokyy-rp"),
    Platform("sportmaster.ua", "Sportmaster", "UA", "blocked", "",
             "2026-10-10: домен не резолвиться (NXDOMAIN) — магазин, схоже, закрито; рендер 2026-10-11: те саме",
             "https://sportmaster.ua/"),
    Platform("answear.ua", "Answear", "UA", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: deny-сторінку антибота браузер проходить, json-ld за ~2 с "
             "(httpx: 403 «Site Maintenance»); старий sample → 404, товар зник",
             "https://answear.ua/p/gestuz-palto-zhinoche-z-dodavannyam-vovny-gzavic-sire-10911241-1799266", render_js=True),
    Platform("mobilluck.com.ua", "Mobilluck", "UA", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: у браузері відповідає за ~4 с, json-ld "
             "(httpx: ReadTimeout 20 с — тихий бан неброузерних клієнтів)",
             "https://www.mobilluck.com.ua/katalog/mobila/samsung/samsung-G973FD_Galaxy_S10_Duos_128GB_Green-1853535.html",
             render_js=True),
    Platform("yakaboo.ua", "Yakaboo", "UA", "verified", "json-ld",
             "2026-10-10: 2 Product, перший головний; сторінка 1.45 MB",
             "https://www.yakaboo.ua/ua/alhimizovani-3452388.html"),
    Platform("aromateque.com.ua", "Aromateque", "UA", "verified", "json-ld",
             "2026-10-10: Product з offers.price; назва без бренду (brand окремо)",
             "https://aromateque.com.ua/maison-francis-kurkdjian-oud-velvet-mood-ra141681-comb-70-ml"),
    Platform("deshevshe.net.ua", "Deshevshe", "UA", "experimental", "",
             "2026-10-10: каталог віддає 404/410, сайт став купонним агрегатором — не перевірено",
             "https://deshevshe.net.ua/laptop-apple/apple_a1342_macbook_mc516rsa.html"),
    # ── Польща / ЄС ──
    Platform("allegro.pl", "Allegro", "PL", "blocked", "",
             "2026-10-10: DataDome 403; Accept-Language pl-PL не допомагає. "
             "Рендер 2026-10-11: DataDome 403 і в headless Chromium (капча)",
             "https://allegro.pl/produkt/smartfon-apple-iphone-15-6-gb-128-gb-5g-czarny-2c471a7b-e083-42cc-b811-faffef64ef77"),
    Platform("ceneo.pl", "Ceneo", "PL", "blocked", "",
             "2026-10-10: 21 KB JS-оболонка «Przejdź do Ceneo» без даних; pl-PL не допомагає. "
             "Рендер 2026-10-11: та сама заглушка без title — антибот не пускає й браузер",
             "https://www.ceneo.pl/157123293"),
    Platform("x-kom.pl", "x-kom", "PL", "blocked", "",
             "2026-10-10: Cloudflare «Just a moment» (403); pl-PL не допомагає. "
             "Рендер 2026-10-11: Cloudflare-челендж проходить, але далі власна заглушка «Cierpliwości…»",
             "https://www.x-kom.pl/p/1203206-laptop-15-16-lenovo-ideapad-slim-3-15-i5-12450h-16gb-512-win11.html"),
    Platform("mediaexpert.pl", "Media Expert", "PL", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: власну перевірку трафіку браузер проходить, json-ld за ~2 с, PLN "
             "(httpx: 403, 530 KB сторінка перевірки)",
             "https://www.mediaexpert.pl/smartfony-i-zegarki/smartfony/smartfon-apple-iphone-16-5g-black-128gb", render_js=True),
    Platform("morele.net", "Morele", "PL", "verified", "json-ld",
             "2026-10-10: 2 Product — перший ProductGroup без ціни, ціна у другому; є й product:price:amount; PLN",
             "https://www.morele.net/dysk-zewnetrzny-hdd-toshiba-canvio-basics-exclusive-1tb-czarny-hdtb510mk3aa-600016107/"),
    Platform("zalando.de", "Zalando", "DE", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: Akamai пропускає headless Chromium, json-ld за ~5 с, EUR "
             "(httpx: 403 «strona niedostępna»)",
             "https://www.zalando.de/nike-sportswear-air-force-1-07-unisex-sneaker-low-blackwhite-ni115o070-q11.html",
             aliases=("zalando.pl", "zalando.co.uk", "zalando.fr", "zalando.it", "zalando.es", "zalando.nl"), render_js=True),
    Platform("otto.de", "OTTO", "DE", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: Kasada віддає 400, але JS проходить челендж і сторінка з json-ld "
             "вантажиться за ~3 с (httpx: HTTP 400 KPSDK)",
             "https://www.otto.de/p/bosch-waschmaschine-serie-6-wgg256z41-10-kg-1600-u-min-2043083677/", render_js=True),
    Platform("mediamarkt.de", "MediaMarkt", "DE", "blocked", "",
             "2026-10-10: Cloudflare captcha («Das ging uns leider zu schnell») 403. "
             "Рендер 2026-10-11: «Nur einen Moment…» (Turnstile) не проходить і в браузері",
             "https://www.mediamarkt.de/de/product/_samsung-galaxy-s25-5g-256-gb-navy-dual-sim-2972620.html"),
    Platform("saturn.de", "Saturn", "DE", "blocked", "",
             "2026-10-10: Cloudflare challenge 403 (та ж інфраструктура, що MediaMarkt). "
             "Рендер 2026-10-11: «Nur einen Moment…» не проходить",
             "https://www.saturn.de/de/product/_apple-iphone-16-5g-128-gb-schwarz-dual-sim-2954263.html"),
    Platform("idealo.de", "idealo", "DE", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: у браузері 200 і json-ld за ~3 с (httpx: 403). "
             "Товар визначає id у URL (204004898 — годинник Citizen, не iPhone зі slug)",
             "https://www.idealo.de/preisvergleich/OffersOfProduct/204004898_-iphone-16-128gb-schwarz-apple.html", render_js=True),
    Platform("alza.cz", "Alza", "CZ", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: Cloudflare пропускає headless Chromium, json-ld за ~2 с, CZK "
             "(httpx: «Please confirm you are human» 403)",
             "https://www.alza.cz/iphone-16-128gb-cerna-d12541617.htm",
             aliases=("alza.sk", "alza.de"), render_js=True),
    Platform("bol.com", "bol", "NL", "blocked", "",
             "2026-10-10: 403 (10 KB заглушка). Рендер 2026-10-11: та сама заглушка «bol» і в браузері",
             "https://www.bol.com/nl/nl/p/apple-iphone-16-128gb-wit/9300000189439845/"),
    Platform("cdiscount.com", "Cdiscount", "FR", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: JS-челендж проходить, json-ld за ~2 с, EUR "
             "(httpx: 14 KB «Le JavaScript n'est pas activé»)",
             "https://www.cdiscount.com/telephonie/telephone-mobile/apple-iphone-16-128gb-white/f-14404-ip16128white.html",
             render_js=True),
    Platform("fnac.com", "Fnac", "FR", "blocked", "",
             "2026-10-10: 403 «FNAC DARTY - Maintenance» (deny-сторінка). Рендер 2026-10-11: 403 без контенту і в браузері",
             "https://www.fnac.com/Apple-iPhone-16-6-1-5G-128-Go-Double-SIM-Noir/a19813594/w-4"),
    Platform("elgiganten.se", "Elgiganten", "SE", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: Vercel Security Checkpoint (429) браузер проходить, json-ld за ~4 с, SEK",
             "https://www.elgiganten.se/product/mobiler-tablets-smartklockor/mobiltelefon/iphone-16-5g-smartphone-128gb-black/825094",
             render_js=True),
    # ── США / Британія / глобальні ──
    Platform("walmart.com", "Walmart", "US", "blocked", "",
             "2026-10-10: HTTP 444 «Request Rejected» (F5); en-US не допомагає. Рендер 2026-10-11: те саме і в браузері",
             "https://www.walmart.com/ip/Apple-AirPods-Pro-with-MagSafe-USB-C-Charging-Case-2nd-Gen/15191113204"),
    Platform("target.com", "Target", "US", "blocked", "",
             "2026-10-10: 429 на будь-який запит. Рендер 2026-10-11: 429 і в браузері (порожня сторінка «Target»)",
             "https://www.target.com/p/airpods-pro-2nd-generation-with-magsafe-case-usb-c/-/A-85978622"),
    Platform("bestbuy.com", "Best Buy", "US", "blocked", "",
             "2026-10-10: Akamai «Access Denied» 403; ?skuId= у URL обов'язковий. Рендер 2026-10-11: Access Denied і в браузері",
             "https://www.bestbuy.com/site/apple-airpods-pro-2nd-generation-with-magsafe-case-usbc-white/6447382.p?skuId=6447382"),
    Platform("ebay.com", "eBay", "US", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: у браузері 200 і json-ld за ~3 с, USD (httpx: 403 «Error Page»)",
             "https://www.ebay.com/itm/296110312917",
             aliases=("ebay.co.uk", "ebay.de", "ebay.pl"), render_js=True),
    Platform("etsy.com", "Etsy", "US", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: DataDome тут пропускає headless Chromium, json-ld за ~2 с, USD "
             "(httpx: DataDome 403)",
             "https://www.etsy.com/listing/1756455865/handmade-ceramic-mug-with-woman-line-art", render_js=True),
    Platform("newegg.com", "Newegg", "US", "blocked", "",
             "2026-10-10: 403 «CAPTCHA page». Рендер 2026-10-11: Cloudflare «Just a moment» не проходить",
             "https://www.newegg.com/samsung-2tb-990-pro-w-heatsink-normal-package-nvme-2-0/p/N82E16820147863"),
    Platform("bhphotovideo.com", "B&H Photo", "US", "blocked", "",
             "2026-10-10: Cloudflare «Just a moment» 403. Рендер 2026-10-11: інтерактивний челендж не розв'язується за 12 с",
             "https://www.bhphotovideo.com/c/product/1667800-REG/sony_ilce_7m4_b_alpha_a7_iv_mirrorless.html"),
    Platform("argos.co.uk", "Argos", "GB", "blocked", "",
             "2026-10-10: Akamai «Access Denied» 403. Рендер 2026-10-11: Access Denied і в браузері",
             "https://www.argos.co.uk/product/4190356"),
    Platform("currys.co.uk", "Currys", "GB", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: Cloudflare пропускає headless Chromium, json-ld за ~3 с, GBP "
             "(httpx: 403 «checking your connection»); старий sample → редірект на категорію refurbished-headphones",
             "https://www.currys.co.uk/products/apple-refurbished-airpods-4-with-active-noise-cancellation-white-very-good-condition-10287145.html",
             render_js=True),
    Platform("johnlewis.com", "John Lewis", "GB", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: у браузері відповідає за ~3 с, json-ld, GBP "
             "(httpx: ReadTimeout 20 с — з'єднання тримають без відповіді)",
             "https://www.johnlewis.com/apple-airpods-pro-3-with-magsafe-charging-case-usb-c-white/p114325329", render_js=True),
    Platform("asos.com", "ASOS", "GB", "verified", "json-ld",
             "через Playwright, перевірено 2026-10-11: Akamai пропускає headless Chromium, json-ld за ~3 с; "
             "валюта за гео (з UA — USD) (httpx: Access Denied 403)",
             "https://www.asos.com/nike/nike-air-force-1-07-trainers-in-triple-black/prd/202329454", render_js=True),
    Platform("ikea.com", "IKEA", "GLOBAL", "verified", "json-ld",
             "2026-10-10: Product з offers.price; ціна/валюта залежать від країни в шляху (/us/en/); сторінка 1.1 MB",
             "https://www.ikea.com/us/en/p/raskog-utility-cart-white-30586783/"),
    Platform("apple.com", "Apple", "GLOBAL", "verified", "json-ld",
             "2026-10-10: сторінка buy = Product з AggregateOffer, ціна «від» (lowPrice), не конкретної конфігурації",
             "https://www.apple.com/shop/buy-iphone/iphone-17"),
    Platform("nike.com", "Nike", "GLOBAL", "verified", "json-ld",
             "2026-10-10: ProductGroup + 18 варіантів; ціна в hasVariant[].offers.price; сторінка 1.2 MB",
             "https://www.nike.com/t/pegasus-premium-mens-road-running-shoes-kWXqW9yR/HQ2592-023"),
    Platform("adidas.com", "adidas", "GLOBAL", "blocked", "",
             "2026-10-10: Akamai 403 «security issue was automatically identified». "
             "Рендер 2026-10-11: 403 заглушка «adidas» і в браузері (19 с)",
             "https://www.adidas.com/us/samba-og-shoes/B75806.html"),
    Platform("aliexpress.com", "AliExpress", "GLOBAL", "blocked", "",
             "2026-10-11: один раз пройшло через Playwright (json-ld), але при реальному використанні API ціни "
             "(mtop.aliexpress.pdp.pc.query) перенаправляється на капчу Alibaba (_____tmd_____/punish, baxia) — "
             "і з прогрітою сесією, і на m./.us. Сторінка приходить без ціни. Офіційний шлях — AliExpress Affiliate API.",
             "https://www.aliexpress.com/item/1005007432415523.html",
             aliases=("aliexpress.us", "aliexpress.ru", "m.aliexpress.com")),
    Platform("amazon.com", "Amazon", "US", "blocked", "",
             "2026-10-10: товар віддав 200 і HTML з #productTitle, але без ціни/JSON-LD/OG (buybox окремо); головна — robot check 202. "
             "Рендер 2026-10-11: порожня сторінка без title (robot check) — не покладатися",
             "https://www.amazon.com/dp/B0D1XD1ZV3",
             aliases=("amazon.de", "amazon.co.uk", "amazon.pl")),
    # ── Приклади платформ (SaaS/движки) ──
    Platform("gymshark.com", "Gymshark (Shopify)", "GLOBAL", "verified", "json-ld",
             "2026-10-10: Shopify; ProductGroup + 8 варіантів, ціна в hasVariant; сторінка 2.4 MB",
             "https://www.gymshark.com/products/gymshark-adapt-x-whitney-leggings-leggings-green-aw26"),
    Platform("allbirds.com", "Allbirds (Shopify)", "US", "verified", "json-ld",
             "2026-10-10: Shopify; 54 Product у JSON-LD (усі кольори), перший — поточний; є og:price:amount",
             "https://www.allbirds.com/products/mens-runner-nz-slip-on-mushroom"),
    Platform("novritsch.com", "Novritsch (WooCommerce)", "GLOBAL", "verified", "json-ld",
             "2026-10-10: WooCommerce; novritsch.com → eu.novritsch.com; AggregateOffer lowPrice; EUR",
             "https://eu.novritsch.com/product/bbs-bags/",
             aliases=("eu.novritsch.com",)),
    Platform("galaxystore.com.ua", "Galaxy Store (Horoshop)", "UA", "verified", "microdata",
             "2026-10-10: Horoshop без JS-челенджу; JSON-LD немає, лише microdata Product",
             "https://galaxystore.com.ua/ua/wall-chargers/setevoe-zaryadnoe-ustroystvo-essager-grace-100w-2type-c-2usb-ect2ca-qyb01-z/"),
    Platform("hillary.ua", "Hillary (Horoshop)", "UA", "experimental", "",
             "2026-10-10: Horoshop з 518-байтним JS-челенджем (cookie challenge_passed=<hash зі сторінки>, reload). "
             "Рендер 2026-10-11: челендж браузер проходить (головна 500 KB), але URL товару не підтверджено — "
             "має працювати як quality.in.ua через RENDER_FALLBACK",
             "https://hillary.ua/"),
    Platform("quality.in.ua", "quality.in.ua (Horoshop)", "UA", "verified", "microdata",
             "через Playwright, перевірено 2026-10-11: Horoshop-челендж браузер проходить, microdata Product за ~4 с "
             "(httpx: 518-байтний JS-челендж; обхід через cookie challenge_passed можливий, але це вже не plain httpx)",
             "https://quality.in.ua/televizor-tcl-98p745/", render_js=True),
    Platform("bleu-de-chauffe.com", "Bleu de Chauffe (PrestaShop)", "FR", "verified", "microdata",
             "2026-10-10: PrestaShop 1.7; лише microdata Product, JSON-LD немає; EUR",
             "https://www.bleu-de-chauffe.com/en/messenger-bag/1948-leon-messenger-bag-camel-3700716022378.html"),
    Platform("oliverbonas.com", "Oliver Bonas (Magento 2)", "GB", "verified", "json-ld",
             "2026-10-10: Magento 2; один Product, offers.price; GBP",
             "https://www.oliverbonas.com/homeware/polka-dot-bed-linen-410829"),
]

# Перевизначення заголовків для окремих доменів (поверх get_http_client()).
# Проба 2026-10-10: Accept-Language pl-PL (allegro, ceneo, x-kom, mediaexpert), de-DE (zalando, otto,
# mediamarkt, idealo), en-US (walmart, target, ebay, argos, currys) НІЧОГО не змінив — блокують за
# TLS/JS-відбитком, а не за локаллю. Тому поки порожньо; заповнювати лише перевіреними записами.
DOMAIN_HEADERS: dict[str, dict[str, str]] = {}


def _norm(host: str) -> str:
    host = (host or "").lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


def platform_for(host: str) -> Platform | None:
    """Платформа для хоста (або повного домену з піддоменами). None — невідомий сайт."""
    h = _norm(host)
    for p in PLATFORMS:
        for d in (p.domain, *p.aliases):
            if h == d or h.endswith("." + d):
                return p
    return None


def platform_name(host: str) -> str:
    p = platform_for(host)
    return p.name if p else _norm(host)


def verified_platforms() -> list[Platform]:
    return [p for p in PLATFORMS if p.status == "verified"]


def platforms_by_country() -> dict[str, list[Platform]]:
    out: dict[str, list[Platform]] = {}
    for p in PLATFORMS:
        out.setdefault(p.country, []).append(p)
    return out
