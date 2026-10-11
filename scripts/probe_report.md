# Проба платформ: що бачить `httpx` на сторінках товарів

Дата проби: **2026-10-10**. Інструмент: `scripts/probe_platforms.py` (один GET на сторінку
товару тими ж заголовками, що й бот — desktop Chrome UA + `Accept-Language: uk-UA,uk;q=0.9,en;q=0.8`,
`follow_redirects`, таймаут 20 с, без Playwright/проксі/платних API). IP — Україна (109.200.x.x).

Статуси: **verified** — назва і додатна ціна витягнуті без JS; **needs_js** — HTML є, але ціни/розмітки
в ньому немає; **blocked** — бот-стіна/403/429/таймаут; **experimental** — не перевірено.

Фікстури: `tests/fixtures/<domain>.html` + `<domain>.json` для verified; `<domain>.blocked.html` /
`<domain>.needs_js.html` для малих (< 200 KB) сторінок-заглушок — для тестів детектора бот-стін.

## Україна

| domain | status | стратегія | назва / ціна | нотатки |
|---|---|---|---|---|
| comfy.ua | blocked | — | — | Cloudflare "Just a moment" (403) уже на головній і на товарі |
| foxtrot.com.ua | verified | json-ld | Смартфон APPLE iPhone 16 Plus 128GB … / 45 999 UAH | Один Product, `offers.price`; сторінка 650 KB |
| allo.ua | verified | json-ld | Телевізор Xiaomi TV A Pro 43 2026 / 14 499 UAH | Сторінка 1.6 MB → фікстура скорочена (head + ld+json) |
| eldorado.ua | needs_js | (opengraph) | Смартфон APPLE iPhone 14 Pro … / **0** | microdata Product і `product:price:amount` є, але `price=0.00` — ціну підвантажує API. Детектор має відкидати ціну 0 |
| moyo.ua | verified | microdata | Ноутбук ACER Extensa 15 EXO15-41 / 28 999 UAH | JSON-LD лише Organization/WebSite. У microdata перший `itemprop=name` — усередині вкладеного `Brand` ("ACER"); брати name з власного scope Product (h1) |
| epicentrk.ua | needs_js | — | (title є) / — | Nuxt SSR без JSON-LD/microdata/OG-ціни; у стані `"price":0`. Повний HTML 350 KB, але ціни немає |
| citrus.ua (ctrs.com.ua) | verified | json-ld | Змішувач для біде Deante Silia / 8 436 UAH | ctrs.com.ua → 301 citrus.ua. 4 ld+json-блоки, Product окремо |
| brain.com.ua | verified | json-ld | Samsung Galaxy A27 5G 6/128Gb / 14 299 UAH | Сторінка 2.9 MB → фікстура скорочена |
| stls.store (stylus.ua) | verified | json-ld | Apple iPhone 16 Pro Max 256GB / 53 698 UAH | stylus.ua → 301 stls.store; `og:type=og:product` (нестандартно) |
| telemart.ua | verified | json-ld | Wi-Fi роутер Asus RT-AX52 / 2 699 UAH | `offers` — масив Offer; брати перший з price |
| kasta.ua | verified | json-ld | Вологі серветки XXL 100 шт / 175 UAH | Ціна лише в `offers.priceSpecification.price` (UnitPriceSpecification), не в `offers.price` |
| makeup.com.ua | blocked | — | — | HTTP 202 з порожнім тілом (CloudFront/AWS WAF challenge) |
| zakupka.com | verified | json-ld | Дитячий музичний телефон Ельза / 85 UAH | Канонічні товари `zakupka.com/uk/p/<id>-slug/`; дзеркала `<shop>.zakupka.com/p/…` часто 404 |
| bigl.ua | verified | json-ld | Ударний акумуляторний шуруповерт Makita / 1 099 UAH | Як prom.ua; URL із `?click_track_data=` треба чистити |
| intertop.ua | blocked | — | — | Cloudflare "Just a moment" (403) |
| apteka911.ua | verified | json-ld | Мукалтин табл. 50мг №30 / 70.5 UAH | 2 Product у JSON-LD, перший — головний; ціна в `offers.lowPrice` (AggregateOffer) |
| tabletki.ua | blocked | — | — | Cloudflare "Just a moment" (403) |
| jysk.ua | verified | json-ld | Матрац пружинний 80x200см TENNA / 16 999 UAH | JSON-LD = **ProductGroup** з 9 hasVariant; поточний товар — `hasVariant[0]` (name = h1), у решти `price: "18999 грн."` (рядок із валютою). Зниклий товар → 301 на категорію з ItemList із 12 Product — перевіряти, що canonical не категорія |
| sportmaster.ua | blocked | — | — | Домен не резолвиться (NXDOMAIN); магазин, схоже, закрито |
| answear.ua | blocked | — | — | 403 "ANSWEAR - Site Maintenance … Loading deny page content" — deny-сторінка антибота |
| mobilluck.com.ua | blocked | — | — | ReadTimeout 20 с двічі (з'єднання висить) — ймовірно тихий бан неброузерних клієнтів |
| yakaboo.ua | verified | json-ld | Алхімізовані / 449 UAH | 2 Product (другий — рекомендація/інший формат); перший головний. Сторінка 1.45 MB |
| aromateque.com.ua | verified | json-ld | Oud Velvet Mood 70мл / 18 890 UAH | Назва без бренду (бренд окремо в `brand`) |
| deshevshe.net.ua | experimental | — | — | Каталог віддає 404/410, сайт перетворився на купонний агрегатор — не перевірено |

## Польща / ЄС

| domain | status | стратегія | назва / ціна | нотатки |
|---|---|---|---|---|
| allegro.pl | blocked | — | — | DataDome 403 ("Please enable JS and disable any ad blocker"); `Accept-Language: pl-PL` не допомагає |
| ceneo.pl | needs_js | — | — | 21 KB JS-оболонка "Przejdź do Ceneo" без даних; `pl-PL` не допомагає |
| x-kom.pl | blocked | — | — | Cloudflare "Just a moment" (403); `pl-PL` не допомагає |
| mediaexpert.pl | blocked | — | — | 403 із власною сторінкою перевірки трафіку (530 KB, "mechanizmy sprawdzające ruch"); `pl-PL` не допомагає |
| morele.net | verified | json-ld | Dysk zewnętrzny HDD Toshiba / 369.99 PLN | 2 Product: перший — **ProductGroup без ціни**, ціна у 2-му; також є `product:price:amount` (OG) |
| zalando.de (.pl/.co.uk …) | blocked | — | — | 403 з HTML-заглушкою "strona niedostępna" (Akamai); `de-DE` не допомагає |
| otto.de | blocked | — | — | HTTP 400, тіло — Kasada (`window.KPSDK`, `x-kpsdk-*`) |
| mediamarkt.de | blocked | — | — | Cloudflare captcha ("Das ging uns leider zu schnell") 403 |
| saturn.de | blocked | — | — | Cloudflare challenge 403 (та ж інфраструктура, що MediaMarkt) |
| idealo.de | blocked | — | — | 403 (URL товару не підтверджено — idealo блокує і пошуковий краулер) |
| alza.cz | blocked | — | — | Cloudflare "Please confirm you are human" 403 |
| bol.com | blocked | — | — | 403 (10 KB заглушка) |
| cdiscount.com | blocked | — | — | HTTP 200, але 14 KB "Le JavaScript n'est pas activé" — JS-челендж |
| fnac.com | blocked | — | — | 403 "FNAC DARTY - Maintenance" (65 KB deny-сторінка) |
| elgiganten.se | blocked | — | — | 429 "Vercel Security Checkpoint" |

## США / Британія / глобальні

| domain | status | стратегія | назва / ціна | нотатки |
|---|---|---|---|---|
| walmart.com | blocked | — | — | HTTP 444 "Request Rejected" (F5); `en-US` не допомагає |
| target.com | blocked | — | — | 429 на будь-який запит |
| bestbuy.com | blocked | — | — | Akamai "Access Denied" 403 |
| ebay.com | blocked | — | — | 403 "Error Page" на /itm/ (і на головній) |
| etsy.com | blocked | — | — | DataDome 403 |
| newegg.com | blocked | — | — | 403 "CAPTCHA page" (680 KB) |
| bhphotovideo.com | blocked | — | — | Cloudflare "Just a moment" 403 |
| argos.co.uk | blocked | — | — | Akamai "Access Denied" 403 |
| currys.co.uk | blocked | — | — | 403 "We're just checking your connection is secure" (Cloudflare custom) |
| johnlewis.com | blocked | — | — | ReadTimeout 20 с двічі — з'єднання тримають і не відповідають |
| asos.com | blocked | — | — | Akamai "Access Denied" 403 (на головній — обрив з'єднання) |
| ikea.com | verified | json-ld | RÅSKOG Utility cart - white / 39.99 USD | Ціна/валюта залежать від країни в шляху (`/us/en/`); сторінка 1.1 MB |
| apple.com | verified | json-ld | iPhone 17 / 899 USD | Сторінка "buy" = Product з **AggregateOffer**, `lowPrice` ("від"), не ціна конкретної конфігурації |
| nike.com | verified | json-ld | Nike Pegasus Premium … / 220 USD | **ProductGroup** + 18 Product-варіантів; ціна в `hasVariant[].offers.price` |
| adidas.com | blocked | — | — | Akamai 403 "security issue was automatically identified" |
| aliexpress.com | needs_js | — | — | 77 KB "Your browser does not support JavaScript!"; є лише `og:type=product` |
| amazon.com | needs_js | — | — | Несподівано віддало 200 і 1.5–1.8 MB HTML з `#productTitle`, але **без ціни** (buybox вантажиться окремо) і без JSON-LD/OG. За іншими умовами — "robot check" (202 на головній). Не покладатися |

## Платформи (приклади магазинів на SaaS/движках)

| domain | платформа | status | стратегія | назва / ціна | нотатки |
|---|---|---|---|---|---|
| gymshark.com | Shopify | verified | json-ld | Adapt Solid Leggings / 60 USD | ProductGroup + 8 варіантів; сторінка 2.4 MB → фікстура скорочена |
| allbirds.com | Shopify | verified | json-ld | Men's Runner NZ Slip On / 105 USD | **54 Product** у JSON-LD (усі кольори); перший — поточний; є й `og:price:amount` |
| novritsch.com (eu.) | WooCommerce | verified | json-ld | Premium Novritsch BBs / 8 EUR | novritsch.com → eu.novritsch.com; AggregateOffer `lowPrice` для варіативного товару |
| galaxystore.com.ua | Horoshop | verified | microdata | Мережевий зарядний пристрій ESSAGER / 1 349 UAH | Horoshop без JS-челенджу; JSON-LD немає, лише microdata |
| hillary.ua | Horoshop | blocked | — | — | 518-байтний JS-челендж (рахує хеш, ставить cookie `challenge_passed=<hash>`, reload) |
| quality.in.ua | Horoshop | blocked | — | — | Той самий челендж. Хеш лежить у самій сторінці: виставивши cookie `challenge_passed=<defaultHash>`, httpx отримує звичайний HTML (перевірено) — обхід без JS можливий, але це вже не "plain httpx" |
| bleu-de-chauffe.com | PrestaShop | verified | microdata | Léon messenger bag - Camel / 235 EUR | PrestaShop 1.7: microdata Product, JSON-LD немає |
| oliverbonas.com | Magento 2 | verified | json-ld | Charcoal Polka Dot … Duvet Cover Set / 85 GBP | Один Product, `offers.price` |

Відкинуті кандидати (не той движок або недоступні): xeroshoes.com та archiduchesse.com — уже Shopify;
jmbullion.com — 403; mebli-shop.in.ua — Next.js, не Horoshop; demo2.cart2quote.com — самопідписаний
сертифікат. lanet.click — не магазин (маркетингова агенція), у реєстр не внесено.

## Підсумок

| status | кількість |
|---|---|
| verified | 24 (UA 14, PL 1, US/глобальні 3, платформи 6) |
| needs_js | 5 (eldorado, epicentrk, ceneo, aliexpress, amazon) |
| blocked | 34 (UA 7, ЄС 13, США/GB/глобальні 12, Horoshop-челендж 2) |
| experimental | 1 (deshevshe) |

Разом 64 пробованих магазини; у `registry.py` — 67 записів (плюс rozetka/olx/prom із власними парсерами).
Фікстури: 24 verified (`<domain>.html` + `.json`; allo, brain, gymshark скорочені до head+ld+json),
27 `*.blocked.html`, 2 `*.needs_js.html` (ceneo, aliexpress). lanet.click у реєстр не внесено.

**Що реально працює без JS:** майже всі великі українські рітейлери, крім Comfy/Intertop/Tabletki
(Cloudflare), Makeup (AWS WAF), Eldorado/Епіцентр (ціна через API). З ЄС — лише morele.net. США/глобальні —
лише бренди з власними сайтами (IKEA, Apple, Nike) та Shopify/WooCommerce/Magento/PrestaShop-магазини;
усі великі маркетплейси й мережі (Walmart, Target, Best Buy, eBay, Etsy, Newegg, B&H, Argos, Currys,
John Lewis, ASOS, Amazon) — бот-стіни.

**Заголовки по країнах.** Перевірено `Accept-Language` pl-PL (allegro, ceneo, x-kom, mediaexpert),
de-DE (zalando, otto, mediamarkt, idealo), en-US (walmart, target, ebay, argos, currys) — **нічого не
змінилось** (ті самі стіни й статуси). Тож `DOMAIN_HEADERS` у registry.py порожній. Блокування —
TLS/JS-фінгерпринт (Cloudflare, DataDome, Akamai, Kasada, F5), а не локаль. Для них допоможе лише
headless-браузер або проксі з браузерним TLS-відбитком — поза межами цієї задачі.

## Квірки для автора екстрактора

1. **ProductGroup / hasVariant** (nike, gymshark, jysk, morele): ціна не в кореневому вузлі, а в
   `hasVariant[].offers`. Поточний товар — варіант, чий `url`/`sku` збігається з canonical, інакше
   `hasVariant[0]` (jysk: name варіанта = h1, у ProductGroup лише назва серії).
2. **Кілька Product в одному документі** (allbirds 54, nike 18, yakaboo 2, apteka911 2, morele 2):
   перший Product з ціною — головний; у morele перший вузол — ProductGroup **без** ціни, брати другий.
3. **Ціна в нестандартних полях**: `offers.priceSpecification.price` (kasta); `offers.lowPrice`
   AggregateOffer (apple, apteka911, novritsch) — це ціна "від"; `offers` як масив (telemart).
4. **Ціна як рядок із валютою/пробілами**: jysk `"18999 грн."`, формати `"1 299,00"` — нормалізувати
   (прибрати все, крім цифр і роздільника; визначати десятковий роздільник за останнім символом).
5. **price = 0** (eldorado microdata/OG, epicentrk state) означає "ціну підвантажує JS" — не вважати
   успіхом, повертати needs_js.
6. **microdata з вкладеними scope** (moyo): `itemprop=name` усередині `Brand` дає "ACER"; брати
   `name`, чий найближчий `itemscope` — сам Product. Horoshop/PrestaShop дають **лише** microdata.
7. **Нестандартний og:type** (`og:product` у stls.store, `website` на товарних сторінках citrus/bigl) —
   не фільтрувати за `og:type=product`.
8. **Редірект на категорію** замість 404 (jysk): після редіректу перевіряти canonical ≠ початковий
   шлях-категорія та наявність `ItemList`/`CollectionPage` — тоді товар зник.
9. **Трекінг-параметри** у посиланнях (`?click_track_data=` на bigl, `?conditionGroupCode=` на walmart,
   `?skuId=` на bestbuy — цей треба **залишити**): чистити лише відомі трекери, не всі query-параметри.
10. **Редіректи домену**: ctrs.com.ua → citrus.ua, stylus.ua → stls.store, novritsch.com → eu.novritsch.com —
    `platform_for` має знаходити платформу і за початковим, і за кінцевим хостом (aliases).
11. **Бот-стіни з HTTP 200**: cdiscount (JS-required), Horoshop-челендж (518 байт), DataDome/Cloudflare
    іноді 200. Детектор: малий розмір (< ~100 KB) + маркери (`Just a moment`, `Enable JavaScript and
    cookies`, `defaultHash = "<64 hex>"`, `Request Rejected`, `KPSDK`, `Access Denied … edgesuite`).
    Слово "captcha" зустрічається у формах відгуків нормальних сторінок (jysk, allbirds) — слабкий маркер.
12. **Великі сторінки** (allo 1.6 MB, brain 2.9 MB, gymshark 2.4 MB, yakaboo 1.45 MB): парсити лише
    `<head>` + `ld+json` + microdata-вузли, не весь DOM.
