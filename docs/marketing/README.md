# Матеріали для просування ЦінаБот

Що де лежить і для чого. Скріншоти адмінки зроблені на **демо-базі** з вигаданими
користувачами (`scripts/demo_db.py`) — справжні імена й Telegram ID нікуди не потрапляють.

| Папка / файл | Що це | Куди |
|---|---|---|
| `linkedin/carousel-uk.pdf`, `carousel-en.pdf` | Карусель із 6 слайдів 1080×1350 | LinkedIn → «Документ» |
| `linkedin/slide-*-uk.png`, `-en.png` | Ті самі слайди окремими картинками | LinkedIn, Instagram, чати |
| `linkedin/post.md` | Текст поста uk/en + перший коментар із посиланням | LinkedIn |
| `internal-chat.md` | Короткі повідомлення: загальний і технічний чат, uk/en | Slack / Telegram / Teams |
| `bot/*.png` | Повідомлення бота (справжні тексти з коду), стиль Telegram | будь-де |
| `screenshots/*.png` | Адмінка @2x: дашборд, платформи, картка юзера, історія ціни, сповіщення, фідбек, система; uk-dark і en-light | презентації, технічні чати |
| `data-story.md`, `posts.md`, `plan.md` | Дата-історія для медіа, пости для каналів, план до Чорної п'ятниці | — |

Сторінка-демо з усім цим (посилання для чатів): https://claude.ai/artifact/W7eau17QZhpvj8cRfMp35X

## Як перезняти

```bash
# 1. Демо-база (вигадані юзери, справжні магазини)
DATABASE_URL=sqlite+aiosqlite:////tmp/demo/demo.db DEMO_ADMIN_PASSWORD=<будь-який> venv/bin/python scripts/demo_db.py

# 2. Адмінка на демо-базі — НЕ на 8088 (там живий сервіс)
DATABASE_URL=sqlite+aiosqlite:////tmp/demo/demo.db ADMIN_2FA_ENABLED=false ADMIN_PORT=8093 venv/bin/python admin.py

# 3. Скріншоти адмінки, повідомлення бота, карусель
DEMO_URL=http://127.0.0.1:8093 DEMO_ADMIN_PASSWORD=<той самий> DEMO_USER_ID=<id юзера з MOZA> venv/bin/python scripts/demo_screenshots.py marketing/screenshots
DATABASE_URL=sqlite+aiosqlite:////tmp/demo/bot.db venv/bin/python scripts/demo_bot_messages.py /tmp/demo/bot.json
venv/bin/python scripts/demo_bot_cards.py /tmp/demo/bot.json marketing/bot
venv/bin/python scripts/demo_linkedin.py marketing/linkedin
```

Цифри на слайдах (47 магазинів, 9 країн, 19 через браузер, кількість тестів) — у `FACTS` в
`scripts/demo_linkedin.py`; оновлюй перед новою публікацією.
