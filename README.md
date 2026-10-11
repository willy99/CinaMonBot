# PriceGuard 🛡️

Telegram-бот для моніторингу цін. Без Docker, без Redis, без зайвого.

## Запуск за 3 хвилини

```bash
# 1. Встановлюємо залежності
pip install -r requirements.txt

# 2. Налаштовуємо
cp .env.example .env
# Відкрий .env і встав свій BOT_TOKEN

# 3. Запускаємо
python bot.py            # або ./run.sh — бот + адмінка через pm2
```

Все. База даних (SQLite файл) створюється автоматично.

---

## Структура

```
priceguard/
├── bot.py                           # Telegram-бот + планувальник (точка входу)
├── admin.py                         # веб-адмінка (NiceGUI), окремий процес
├── run.sh                           # запуск/зупинка обох через pm2
├── ecosystem.config.js              # опис процесів для pm2
├── requirements.txt
├── .env.example
└── app/
    ├── config.py                    # всі налаштування
    ├── bot/
    │   └── handlers/
    │       └── main.py              # команди бота (/start /add /list)
    ├── admin/                       # сторінки, запити й auth адмінки
    ├── i18n.py                      # тексти uk/en + форматування валют
    ├── database/
    │   ├── models.py                # таблиці БД
    │   └── session.py               # підключення до БД
    ├── scheduler/
    │   └── price_checker.py         # перевірка цін (APScheduler)
    └── services/
        └── scrapers/
            └── scraper.py           # парсери Rozetka / OLX / Prom
```

## Один процес — все всередині

```
python bot.py
    │
    ├── Bot (aiogram) — обробляє команди юзерів
    │
    └── Scheduler (APScheduler) — кожні 15 хв перевіряє ціни
            │
            └── Scrapers — парсять Rozetka / OLX / Prom
```

## SQLite → PostgreSQL (коли буде потрібно)

Просто змінюємо один рядок у `.env`:

```
# Було
DATABASE_URL=sqlite+aiosqlite:///./priceguard.db

# Стало
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/priceguard
```

Більше нічого змінювати не треба — код однаковий.

## Коли переходити на PostgreSQL?

Залишайся на SQLite поки:
- менше 1000 активних трекерів
- менше 500 юзерів
- один сервер

Переходь на PostgreSQL коли:
- треба кілька серверів одночасно
- бачиш помилки "database is locked"
- трекерів стає більше 5000

## Адмін-дашборд (`python admin.py`)

Веб-панель на NiceGUI поруч із ботом, спільна база. Порт 8088, слухає лише `127.0.0.1`.

```bash
pip install -r requirements.txt

# 1. Секрет для сесій (один раз, у .env)
python -c "import secrets; print('ADMIN_STORAGE_SECRET=' + secrets.token_hex(32))" >> .env

# 2. Перший адмін. Telegram ID потрібен для 2FA — на нього приходить код входу.
python -m app.admin.cli create-admin pavel --telegram-id 431742835

# 3. Запуск: або окремо, або все разом
python admin.py
./run.sh
```

Що є: дашборд зі статистикою, користувачі (пошук, фільтри, картка з товарами,
історія цін, активація/деактивація Premium, блокування), усі трекери, адміни
(додати, вимкнути, змінити пароль/Telegram ID), журнал дій.

Безпека: пароль (scrypt) + код у Telegram через бота; 5 невдалих спроб → блок на
15 хв; сесія 12 год. Доступ ззовні — через Tailscale або SSH-тунель
(`ssh -L 8088:127.0.0.1:8088 mac`), не виставляйте порт в інтернет без HTTPS.
`ADMIN_2FA_ENABLED=false` вимикає другий фактор (тільки для локальної розробки).

## Рендеринг через браузер (Playwright, необов'язково)

Частина магазинів не віддає ціну без JavaScript (Eldorado, Епіцентр, Ceneo, AliExpress) або ховається за
Cloudflare/DataDome/Akamai (Comfy, Intertop, Tabletki, Allegro, Zalando, OTTO, Alza …). Для них є
`app/services/scrapers/render.py`: один headless Chromium на процес бота, окремий контекст на домен
(локаль/часовий пояс за TLD, картинки й шрифти не вантажаться), ліміт одночасних сторінок.

```bash
venv/bin/pip install -r requirements.txt          # містить playwright
venv/bin/python -m playwright install chromium    # ~100 MB; один раз на машину
pm2 restart priceguard                            # або як ви перезапускаєте бота
```

Без Playwright/Chromium бот працює як раніше (httpx) і один раз пише `render_unavailable` у лог.

Як це вмикається:
- домени з `render_js=True` у `app/services/scrapers/registry.py` одразу парсяться через браузер;
- для решти, якщо httpx дав `blocked`/`parse_failed` або товар без ціни, робиться одна спроба рендеру
  (`RENDER_FALLBACK=true`); успіх пишеться в лог як `render_fallback_success` — це сигнал поставити домену
  `render_js=True`;
- у `scrape_log` стратегія таких рядків має суфікс `+render` (`json-ld+render`), тож на дашборді видно,
  які магазини тримаються на браузері.

Налаштування (env або `.env`): `RENDER_MAX_CONCURRENCY=2`, `RENDER_TIMEOUT_SECONDS=35`,
`RENDER_FALLBACK=true`, `RENDER_IDLE_CONTEXT_MINUTES=10` (простій → контекст, а потім і Chromium закриваються),
`RENDER_BROWSER_CHANNEL=chromium` (повний Chromium у новому headless-режимі; `""` — легший headless shell,
але його частіше ловлять антиботи).

Перевірка магазинів: `venv/bin/python scripts/probe_render.py` (звіт у `scripts/probe_render_report.md`,
фікстури `tests/fixtures/<domain>.render.html`). Тести: `venv/bin/python -m pytest tests/test_playwright_render.py`;
зі справжнім Chromium — `RUN_BROWSER_TESTS=1`.
