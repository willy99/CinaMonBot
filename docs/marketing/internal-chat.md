# Повідомлення для внутрішніх чатів

Коротко, без маркетингу — для колег у Slack / Telegram / Teams. До повідомлення варто
прикріпити 2 картинки: `bot/03-price-drop-uk.png` (як виглядає сповіщення) і
`linkedin/slide-2-uk.png` (історія ціни MOZA). Для технічних каналів —
`screenshots/01-dashboard-uk-dark.png`.

---

## Загальний чат (українською)

Привіт! Зробив у вільний час Telegram-бота, який стежить за цінами: кидаєш посилання на товар — він пише, коли ціна реально впала.

Працює з Rozetka, Comfy, Notino, Foxtrot, Allo, MOYO, IKEA, Zalando, eBay і ще ~40 магазинами. Можна поставити ціль сумою або відсотком («−15%»), щопонеділка приходить дайджест «що подешевшало».

Перед Чорною п'ятницею буде корисно: за два місяці я бачив, як «знижку −22%» робили після підняття ціни на 33%.

Безкоштовно до 5 товарів 👉 @cina_mon_bot
Буду радий фідбеку — прямо в боті, команда /feedback 🙏

---

## Технічний канал (українською)

Pet project, яким поділюсь: @cina_mon_bot — Telegram-бот моніторингу цін.

Що може бути цікаво технічно:
• універсальний парсер без селекторів — JSON-LD → microdata → OpenGraph → евристика; 47 перевірених магазинів у 9 країнах, новий сайт часто працює без коду;
• Playwright для 19 сайтів, що віддають ціну лише через JS; детектор Cloudflare/Akamai/DataDome-заглушок;
• Notino: JSON-LD завжди описує дефолтний варіант, тож ціна береться з Apollo state сторінки по id варіанта;
• aiogram + APScheduler, SQLite + Alembic, NiceGUI-адмінка (uk/en, dark/light, 2FA через самого бота), 1 783 тести, статичний тест, що бот не має «зайвих» місць масової розсилки.

Скріншоти адмінки — на демо-даних. Питання й критика вітаються.

---

## General chat (English)

Hi all! I built a small Telegram bot in my spare time that watches prices for you: send it a product link and it messages you when the price actually drops.

It works with Rozetka, Notino, IKEA, Zalando, eBay, Nike, Apple and ~40 other shops. You can set a target as an amount or a percent ("−15%"), and on Mondays it sends a short "what got cheaper" digest.

Handy before Black Friday: over two months I saw a "−22% off" that came right after a +33% price hike.

Free for up to 5 items 👉 @cina_mon_bot
Feedback is very welcome, right in the bot via /feedback 🙏

---

## Tech channel (English)

Sharing a side project: @cina_mon_bot, a Telegram price-tracking bot.

Possibly interesting bits:
• a selector-free parser: JSON-LD → microdata → OpenGraph → heuristics; 47 verified shops in 9 countries, new sites often work with zero code
• Playwright for 19 sites that only render prices via JS, plus a detector for Cloudflare / Akamai / DataDome walls
• Notino's JSON-LD always describes the default variant, so prices come from the page's Apollo state by variant id
• aiogram + APScheduler, SQLite + Alembic, a NiceGUI admin (uk/en, dark/light, 2FA through the bot itself), 1,783 tests, including a static test that guards against unintended mass-messaging code paths

Admin screenshots use demo data. Questions and critique welcome.
