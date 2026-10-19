# Пост для LinkedIn

Що публікувати: **документ-карусель** `carousel-uk.pdf` (або `carousel-en.pdf`) — LinkedIn показує
його як гортальні слайди, це найбільший формат у стрічці. Альтернатива — 6 картинок `slide-*-uk.png`.

Посилання на бота LinkedIn у тексті поста "ріже" в охопленні, тож воно в першому коментарі
(готовий текст нижче) і на останньому слайді (QR).

---

## Українською

Бачите на сайті «−22%» — і хочеться купити, поки не закінчилось.

Я два місяці записував ціни кількох товарів на Rozetka. Кермо MOZA R5:
21 квітня — 19 499 грн,
19 травня — 25 999 грн (+33% за місяць),
5 червня — «знижка −22%», 20 999 грн.

Після «знижки» кермо все ще коштувало на 1 500 грн більше, ніж у квітні. У 3 з 12 товарів, за якими я стежив, була та сама схема: спершу підняти ціну, потім «знизити».

Тому я зробив ЦінаБот — Telegram-бота, який стежить за ціною замість вас:
→ кидаєш посилання на товар;
→ ставиш ціль: суму або «−15%»;
→ бот пише, коли ціна справді впала, і раз на тиждень — що подешевшало.

Працює з 47 магазинами в 9 країнах: Rozetka, Comfy, Notino, Foxtrot, Allo, MOYO, IKEA, Zalando, eBay та іншими.

Під капотом:
• парсер без CSS-селекторів, який читає структуровані дані сторінки (JSON-LD, microdata), тож новий магазин часто працює без жодного рядка коду;
• headless Chromium для сайтів, що показують ціну лише через JavaScript;
• розуміє варіанти товару: на Notino 75 мл і 100 мл — різні ціни, і бот стежить саме за вашим;
• Python, aiogram, NiceGUI-адмінка з 2FA, Playwright, SQLite + Alembic, 1 783 автотести.

Найбільше часу пішло не на парсинг, а на те, щоб бот не писав зайвого: окремі галочки «про знижку» і «про наявність» для кожного товару, антиспам для товарів, що зникають і з'являються, дайджест лише тоді, коли є що сказати.

Чорна п'ятниця — 27 листопада. Якщо додати товари вже зараз, до неї в боті буде місяць історії цін, і ви побачите, чи знижка справжня.

Безкоштовно до 5 товарів. Посилання — у першому коментарі. Буду вдячний за фідбек і за магазини, які варто додати 🙏

#pricetracking #telegrambot #python #blackfriday #sideproject #ukraine

**Перший коментар:**
👉 Бот: https://t.me/cina_mon_bot?start=linkedin
Працює українською та англійською. Ідеї та баги — прямо в боті через /feedback.

---

## English

You see “−22% off” and feel you should buy before it’s gone.

For two months I logged prices of a few products on Rozetka, Ukraine’s largest marketplace. A MOZA R5 racing wheel:
Apr 21 — ₴19,499
May 19 — ₴25,999 (+33% in a month)
Jun 5 — “−22% off”, ₴20,999

After the “discount” it still cost ₴1,500 more than in April. 3 of the 12 products I tracked showed the same pattern: raise the price first, then “cut” it.

So I built ЦінаБот, a Telegram bot that watches prices for you:
→ send a product link
→ set a target: an amount or “−15%”
→ get a message when the price really drops, plus a weekly “what got cheaper” digest

It works with 47 shops in 9 countries, including Rozetka, Notino, IKEA, Zalando, eBay, Nike and Apple.

Under the hood:
• a selector-free parser that reads a page’s structured data (JSON-LD, microdata), so a new shop often works with zero new code
• headless Chromium for sites that only render prices with JavaScript
• variant-aware: on Notino the 75 ml and 100 ml are different prices, and the bot tracks yours
• Python, aiogram, a NiceGUI admin with 2FA, Playwright, SQLite + Alembic, 1,783 automated tests

The hardest part wasn’t scraping. It was making sure the bot never sends anything you didn’t ask for: per-item toggles for “price drop” and “back in stock”, a cooldown for items that flicker in and out of stock, and a digest only when there’s something to say.

Black Friday is November 27. Add your wishlist now and you’ll have a month of price history by then.

Free for up to 5 items. Link in the first comment. Feedback and shop requests are very welcome 🙏

#pricetracking #telegrambot #python #blackfriday #sideproject

**First comment:**
👉 The bot: https://t.me/cina_mon_bot?start=linkedin
Works in English and Ukrainian. Ideas and bugs go straight to /feedback in the bot.
