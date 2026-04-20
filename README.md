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
python main.py
```

Все. База даних (SQLite файл) створюється автоматично.

---

## Структура

```
priceguard/
├── main.py                          # точка входу — запускаємо це
├── requirements.txt
├── .env.example
└── app/
    ├── config.py                    # всі налаштування
    ├── bot/
    │   └── handlers/
    │       └── main.py              # команди бота (/start /add /list)
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
python main.py
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
