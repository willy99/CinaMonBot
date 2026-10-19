"""
Локалізація і форматування грошей.

Використання:
    from app.i18n import t, fmt_money, resolve_lang

    lang = user.lang                      # "uk" | "en"
    text = t(lang, "list_empty")
    text = t(lang, "added_target", price=fmt_money(1234, "UAH", lang))

Правила:
- Ключі однакові для всіх мов. Якщо ключа немає в мові — беремо з "uk".
- Адмінські повідомлення (stats, activate, broadcast, алерти) не локалізуються —
  вони завжди українською і живуть прямо в коді.
- Мова юзера зберігається в User.language_code. Telegram дає туди свій код
  ("uk", "ru", "en", "de", ...) при створенні; /language перезаписує його.
"""
from __future__ import annotations

from decimal import Decimal

DEFAULT_LANG = "uk"

# Мови, які можна вибрати в /language
LANGUAGES: dict[str, str] = {
    "uk": "🇺🇦 Українська",
    "en": "🇬🇧 English",
}


def resolve_lang(code: str | None) -> str:
    """
    Перетворює довільний код мови (з Telegram або з /language) на підтримувану мову.
    uk / ru / be → uk (аудиторія з України), все інше → en.
    """
    if not code:
        return DEFAULT_LANG
    base = code.lower().split("-")[0]
    if base in LANGUAGES:
        return base
    if base in ("ru", "be"):
        return "uk"
    return "en"


# ─── Гроші ───────────────────────────────────────────────────────

# (prefix, suffix) — для валют, що не залежать від мови
_CURRENCY_FORMAT: dict[str, tuple[str, str]] = {
    "USD": ("$", ""),
    "EUR": ("€", ""),
    "GBP": ("£", ""),
    "JPY": ("¥", ""),
    "PLN": ("", " zł"),
    "CZK": ("", " Kč"),
    "TRY": ("₺", ""),
    "KZT": ("", " ₸"),
}

# UAH показуємо по-різному: "1 234 грн" для uk, "₴1,234" для en
_UAH_FORMAT: dict[str, tuple[str, str]] = {
    "uk": ("", " грн"),
    "en": ("₴", ""),
}


def fmt_number(amount: Decimal | float | int, lang: str) -> str:
    """1234.5 → '1 234,50' (uk) або '1,234.50' (en). Цілі — без копійок."""
    amount = Decimal(str(amount))
    is_int = amount == amount.to_integral_value()
    raw = f"{amount:,.0f}" if is_int else f"{amount:,.2f}"
    if lang == "uk":
        # "1,234.50" → "1 234,50"
        return raw.replace(",", "\u0000").replace(".", ",").replace("\u0000", " ")
    return raw


def fmt_money(amount: Decimal | float | int | None, currency: str | None, lang: str) -> str:
    """Форматує суму з валютою для показу юзеру. None → '—'."""
    if amount is None:
        return "—"
    cur = (currency or "UAH").upper()
    number = fmt_number(amount, lang)
    if cur == "UAH":
        prefix, suffix = _UAH_FORMAT.get(lang, _UAH_FORMAT["uk"])
    else:
        prefix, suffix = _CURRENCY_FORMAT.get(cur, ("", f" {cur}"))
    return f"{prefix}{number}{suffix}"


def fmt_date(dt, lang: str) -> str:
    """Дата для юзера за київським часом (у базі зберігається UTC)."""
    if dt is None:
        return "—"
    from app.timeutil import to_kyiv
    return to_kyiv(dt).strftime("%d.%m.%Y")


def fmt_percent(pct: Decimal | float | int | None, lang: str = DEFAULT_LANG) -> str:
    """15 → '15', 15.50 → '15,5' (uk) / '15.5' (en). Без знака і без '%' — їх додає шаблон."""
    if pct is None:
        return "—"
    value = Decimal(str(pct)).normalize()
    if value == value.to_integral_value():
        raw = f"{value:.0f}"
    else:
        raw = f"{value:.2f}".rstrip("0").rstrip(".")
    return raw.replace(".", ",") if lang == "uk" else raw


# ─── Тексти ──────────────────────────────────────────────────────

_T: dict[str, dict[str, str]] = {
    # ── uk ────────────────────────────────────────────────────────
    "uk": {
        # загальне
        "cancelled": "❌ Скасовано.",
        "btn_share_list": "🎁 Поділитися списком",
        "share_send_text": "Мій список бажань у ЦінаБот — тут видно поточні ціни",
        "btn_share_send": "📤 Надіслати друзям",
        "btn_share_rotate": "🔄 Нове посилання",
        "btn_share_off": "🚫 Вимкнути",
        "share_empty": "🎁 Список бажань порожній — спершу додай товари (/add або просто надішли посилання).",
        "share_intro": "🎁 <b>Твій список бажань</b>\n\nПосилання: {link}\n\nХто відкриє, побачить {n} товар(ів): назви, поточні ціни й магазини — і зможе стежити за ними сам. Твої цілі та юзернейм не показуються.\n\n<i>«Нове посилання» вимкне старе. «Вимкнути» — закриє список зовсім.</i>",
        "share_off_done": "🚫 Посилання на список вимкнено. Увімкнути знову — /share.",
        "share_rotated_toast": "Старе посилання більше не працює",
        "shared_invalid": "🤷 Цей список бажань більше недоступний — власник вимкнув або оновив посилання.",
        "shared_own": "🎁 Це твій власний список бажань. Керувати посиланням — /share.",
        "shared_empty": "🎁 У цьому списку бажань поки немає товарів.",
        "shared_friend": "друга",
        "shared_title": "🎁 <b>Список бажань: {name}</b>",
        "shared_hint": "Натисни ➕ з номером, щоб стежити за товаром і отримати сповіщення, коли він подешевшає.",
        "btn_shared_add_all": "➕ Стежити за всіма",
        "shared_invalid_toast": "Список більше недоступний",
        "shared_added_toast": "✅ Стежу: {title}",
        "shared_added_paused_toast": "Додано «{title}», але він на паузі: безкоштовно до ліміту товарів. Деталі — /premium",
        "shared_added_all": "✅ Додано: {added} · вже стежу: {dup}",
        "bulk_busy": "⏳ Ще додаю попередній список — зачекай хвилинку.",
        "bulk_progress": "⏳ Додаю товари: {done} з {total}…",
        "bulk_done_title": "📥 <b>Готово</b>",
        "bulk_added": "✅ Додано: {n}",
        "bulk_duplicates": "♻️ Вже стежу: {n}",
        "bulk_failed": "⚠️ Не вдалося: {n}",
        "bulk_fail_blocked": "сайт блокує автоматичні запити",
        "bulk_fail_not_found": "сторінка не знайдена",
        "bulk_fail_no_price": "не знайшов ціну на сторінці",
        "bulk_fail_forbidden": "це посилання не можна перевіряти",
        "bulk_fail_not_url": "не схоже на посилання",
        "bulk_fail_auction_ended": "торги вже завершились",
        "bulk_fail_network": "сайт не відповів, спробуй пізніше",
        "bulk_skipped": "✋ За раз додаю до {max} товарів — ще {n} не додано, надішли їх окремо.",
        "bulk_paused": "⏳ На паузі через ліміт: {n} (безкоштовно — {limit} активних). Деталі — /premium",
        "bulk_hint": "<i>Для цих товарів напишу про будь-яке помітне зниження ціни. Свою ціль можна поставити, додавши товар окремо через /add.</i>",
        "bulk_file_failed": "❌ Не вдалося прочитати файл. Спробуй ще раз або встав посилання текстом.",
        "bulk_file_empty": "🤷 У файлі не знайшов жодного посилання.",
        "inbox_hint": "🔗 Щоб стежити за товаром, надішли посилання на нього — можна одразу кілька або файл .txt зі списком. Команди — /help.",

        # ціль у відсотках ("скажи, коли впаде на 15%")
        "target_percent_range": "❌ Відсоток має бути від 1 до 90: <code>15%</code>",
        "target_percent_no_price": (
            "❌ Ціна товару зараз невідома, тому ціль у відсотках не підходить. "
            "Введи суму числом: <code>4500</code>"
        ),
        "added_target_percent": "\n🎯 Ціль: −{pct} % (≤ {price})",
        "list_target_percent": " → 🎯−{pct}%",
        "notif_target_percent": (
            "🎯 <b>Ціна впала на {pct} % — твоя ціль досягнута!</b>\n\n"
            "📦 {title}\n"
            "💰 Ціна зараз: <b>{price}</b>\n"
            "🎯 Твоя ціль: −{pct} % від {baseline} (≤ {target})\n\n"
            "🔗 <a href='{url}'>Купити зараз</a>"
        ),

        # щотижневий дайджест "що подешевшало за тиждень"
        "digest_header": "📬 <b>Що подешевшало за тиждень</b>\n",
        "digest_item": "📉 {title}: {old} → {new} (−{pct} %) · мін. за 30 дн.: {min}",
        "digest_ups": "\n📈 Подорожчало товарів: {n}",
        "digest_hint": (
            "\n<i>Дайджест приходить щопонеділка — і лише тоді, коли щось подешевшало. "
            "Вимкнути можна кнопкою нижче або в /settings.</i>"
        ),
        "btn_digest_off": "🔕 Вимкнути дайджест",
        "digest_off_toast": "🔕 Дайджест вимкнено. Увімкнути знову: /settings",
        "digest_on_toast": "📬 Щотижневий дайджест увімкнено",
        "settings_digest": "📬 Щотижневий дайджест",

        "default_name": "друже",
        "untitled": "Без назви",
        "item": "Товар",
        "price_unknown": "невідома",
        "in_stock": "✅",
        "out_of_stock": "❌ немає",
        "error": "Помилка",
        "not_found": "Товар не знайдено",
        "no_access": "❌ Немає доступу",
        "blocked": "❌ Ваш акаунт заблоковано. Ви не можете користуватися ботом.",

        # кнопки
        "btn_add": "➕ Додати товар",
        "btn_add_short": "➕ Додати",
        "btn_list": "📋 Мої товари",
        "btn_list_short": "📋 Список",
        "btn_delete": "🗑 Видалити",
        "btn_buy_premium": "⭐ Купити Premium",
        "btn_activate_premium": "⭐ Активувати Premium",
        "btn_renew_premium": "🔄 Продовжити Premium",
        "btn_pay": "💳 Оплатити {price} грн",
        "btn_language": "🌐 Мова / Language",
        "btn_cancel": "❌ Скасувати",
        "btn_skip": "⏭ Пропустити",
        "btn_back": "◀️ Назад",
        "btn_yes": "✅ Так",
        "btn_no": "❌ Ні",

        # /start
        "start_greeting": "👋 Привіт, {name}!",
        "start_intro": (
            "<b>ЦінаБот</b> — автоматичний моніторинг цін на Rozetka, OLX, Prom.ua "
            "та десятках інших магазинів (/sites) і сповіщення, коли ціна знизилась 📉"
        ),
        "start_premium_active": "⭐ У тебе активний Premium — насолоджуйся!",
        "plans": (
            "🆓 Безкоштовно: до {n} товарів\n"
            "⭐ Premium {price} грн/міс: кількість товарів у списку необмежена"
        ),
        "start_links": (
            "/info — детальна інформація\n"
            "/help — доступні команди\n"
            "/language — мова / language"
        ),
        "contacts": (
            "━━━━━━━━━━━━━━━\n"
            "📞 <b>Контакти для звернень:</b>\n"
            "Email: willy2005@gmail.com\n"
            "Зворотній зв'язок у боті: /feedback\n\n"
            "━━━━━━━━━━━━━━━\n"
            "🏢 <b>Виконавець:</b>\n"
            "👤 Фізична особа Желнов Павло,\n"
            "📍 м. Павлоград, Україна"
        ),

        # /add
        "add_prompt": (
            "🔗 <b>Надішли посилання на товар</b>\n\n"
            "Rozetka, OLX, Prom.ua + десятки інших магазинів (/sites).\n\n"
            "<i>Просто встав посилання на сторінку товару — можна одразу кілька або файл .txt зі списком</i>"
        ),
        "not_a_url": "❌ Це не схоже на посилання. Встав URL сторінки товару (починається з http).",
        "site_needs_js": (
            "⚠️ <b>{name}</b> поки що не підтримується: сторінка підвантажує ціну скриптами, "
            "і я її не бачу. Працюю над цим — спробуй інший магазин (/sites)."
        ),
        "site_blocked": (
            "⛔ <b>{name}</b> блокує автоматичні запити, тому поки що не підтримується. "
            "Спробуй інший магазин (/sites)."
        ),
        "checking": "⏳ Перевіряю товар...",
        "err_blocked": "⛔ Цей сайт блокує автоматичні запити, поки що не підтримується.",
        "err_not_found": "❌ Сторінка не знайдена (404). Перевір посилання.",
        "err_forbidden_target": "❌ Це посилання веде на внутрішню адресу — такі не відстежуємо. Надішли посилання на сторінку магазину.",
        "rate_limited": "⏳ Забагато запитів. Спробуй ще раз за {seconds} с.",
        "err_parse_failed": (
            "🤷 Не знайшов ціну на цій сторінці. Перевір, що це сторінка конкретного товару, "
            "а не каталог чи пошук.\n\n"
            "Якщо цей магазин має бути підтриманий — напиши /feedback, додам."
        ),
        "fetch_failed": "❌ Не вдалося отримати сторінку (мережа або таймаут). Спробуй ще раз трохи пізніше.",
        "ask_target": (
            "📦 <b>{title}</b>\n\n"
            "💰 {price}  {avail}\n\n"
            "Встановити цільову ціну? (сповіщу, коли впаде)\n"
            "Введи суму в {currency} (<code>4500</code>) або відсоток зниження (<code>15%</code>), "
            "або натисни «Пропустити»"
        ),
        "target_invalid": "❌ Введи суму числом (<code>4500</code>) або відсоток (<code>15%</code>)",
        "already_tracked": "⚠️ Цей товар вже відстежується!",
        "added": "✅ <b>Додано!</b>\n\n📦 {title}{target}{slots}",
        "added_target": "\n🎯 Ціль: {price}",
        # ── Аукціони (Violity): напрямок цілі й нагадування перед кінцем торгів ──
        "btn_tdir_above": "📈 Коли ставка перевищить суму",
        "btn_tdir_below": "📉 Коли ціна впаде нижче суми",
        "btn_auction_no_target": "⏰ Без цілі — лише нагадати про кінець",
        "auction_card": (
            "🔨 <b>{title}</b>\n\n"
            "💰 Поточна ставка: <b>{price}</b>\n"
            "⏳ Торги до: {end}\n\n"
            "Коли сповістити?\n"
            "• <b>перевищить</b> — ставки піднялись вище за твою суму;\n"
            "• <b>впаде нижче</b> — для лотів, де ціна знижується.\n\n"
            "Перед кінцем торгів нагадаю один раз — за скільки, обереш кнопками після додавання."
        ),
        "auction_ended_at_add": "🔨 <b>{title}</b>\n\nТорги вже завершились (остання ціна {price}) — стежити нема за чим.",
        "ask_target_above": (
            "📈 Поточна ставка: <b>{price}</b>\n\n"
            "Введи суму в {currency}, вищу за поточну (<code>500000</code>), "
            "або на скільки відсотків має зрости ціна (<code>20%</code>)."
        ),
        "ask_target_below": (
            "📉 Поточна ціна: <b>{price}</b>\n\n"
            "Введи суму в {currency}, нижчу за поточну (<code>300000</code>), "
            "або на скільки відсотків має впасти ціна (<code>15%</code>)."
        ),
        "auction_choose_direction": "Спершу обери, коли сповістити: коли ставка <b>перевищить</b> суму чи ціна <b>впаде нижче</b>.",
        "auction_above_invalid": "❌ Сума має бути більшою за поточну ставку ({price}).",
        "auction_below_invalid": "❌ Сума має бути меншою за поточну ціну ({price}).",
        "added_target_above": "\n📈 Сповіщу, коли ставка перевищить {price}",
        "added_target_below_auction": "\n📉 Сповіщу, коли ціна впаде до {price} або нижче",
        "added_reminder": "\n\n⏰ Торги до {end}. Коли нагадати про кінець? 👇",
        "notif_target_above": (
            "📈 <b>Ставка перевищила твою межу!</b>\n\n"
            "📦 {title}\n"
            "💰 Ставка зараз: <b>{price}</b>\n"
            "🎯 Твоя межа: {target}\n\n"
            "🔗 <a href='{url}'>Відкрити лот</a>"
        ),
        "notif_ending": (
            "⏰ <b>До кінця торгів {left}</b>\n\n"
            "📦 {title}\n"
            "💰 Поточна ставка: <b>{price}</b>\n"
            "🏁 Завершення: {end}\n\n"
            "🔗 <a href='{url}'>Відкрити лот</a>"
        ),
        "minutes_short": "{n} хв",
        "reminder_60": "за 1 год",
        "reminder_30": "за 30 хв",
        "reminder_15": "за 15 хв",
        "reminder_5": "за 5 хв",
        "reminder_off": "вимкнено",
        "rem_btn_60": "1 год",
        "rem_btn_30": "30 хв",
        "rem_btn_15": "15 хв",
        "rem_btn_5": "5 хв",
        "rem_btn_off": "🔕 Не нагадувати про кінець торгів",
        "settings_toast_reminder": "⏰ Нагадування: {value}",
        "list_target_above": " → 📈≥{price}",
        "added_paused": (
            "\n\n⏳ <i>Товар додано, але він на паузі (ліміт {n} активних). "
            "Купіть /premium для розблокування.</i>"
        ),
        "added_slots": "\n\n<i>Слотів використано: {used}/{n}</i>",

        # /list
        "list_empty": "📭 Ти ще нічого не відстежуєш.",
        "list_header": "📋 <b>Твої товари ({n}):</b>\n",
        "list_paused_limit": "(на паузі: ліміт)",
        "list_more": "\n<i>... та ще {n} товарів не помістилося у список.</i>\n",
        "plan_premium": "⭐ <b>Ваш план: Premium (Безліміт)</b>",
        "plan_free": "🆓 <b>Ваш план: Безкоштовний</b>\n<i>Активно: {active}/{total} товарів.</i>",
        "plan_free_limit_note": (
            "\n\n⚠️ Деякі товари призупинені через ліміт. "
            "Активуйте /premium для моніторингу всіх позицій."
        ),

        # /delete
        "delete_empty": "📭 Немає товарів для видалення.",
        "delete_prompt": "🗑 <b>Вибери товар для видалення:</b>",
        "delete_confirm": "❓ <b>Видалити?</b>\n\n📦 {title}",
        "deleted": "✅ <b>Видалено!</b>\n\n📦 {title}\n\n<i>Залишилось: {n} товарів</i>",
        "deleted_toast": "Видалено ✅",
        "deleted_activated": "\n\nЗвільнився слот — знято з паузи:\n{titles}",

        # /premium
        "premium_active": (
            "⭐ <b>У тебе активний Premium!</b>\n\n"
            "📅 Діє до: <b>{until}</b>\n\n"
            "✅ Необмежена кількість товарів\n"
            "✅ Пріоритетна перевірка цін\n\n"
            "Дякуємо за підтримку! 🙏\n\n"
            "<i>Для продовження підписки натисни кнопку нижче після закінчення терміну.</i>"
        ),
        "premium_offer": (
            "⭐ <b>Premium</b> — {price} грн/місяць\n\n"
            "✅ Необмежена кількість товарів\n"
            "✅ Пріоритетна перевірка цін\n\n"
            "<i>Оплата: Visa / Mastercard / Monobank</i>"
        ),
        "already_premium_alert": "У тебе вже є Premium! ⭐",
        "pay_unavailable": "💳 Оплата тимчасово недоступна. Напиши нам через /feedback — активуємо вручну.",
        "pay_instructions": (
            "💳 <b>Оплата Premium — {price} грн</b>\n\n"
            "Переказ на картку Privat:\n\n"
            "<code>{card}</code>\n\n"
            "Отримувач: <b>Желнов Павло</b>\n"
            "Сума: <b>{price} грн</b>\n\n"
            "⚠️ У призначенні платежу обовʼязково вкажи:\n"
            "<code>{ref}</code>\n\n"
            "Після оплати надішли скріншот через /feedback — "
            "активую Premium протягом кількох годин 🙏"
        ),
        "premium_activated": (
            "🎉 <b>Premium активовано!</b>\n\n"
            "📅 Діє до: <b>{until}</b>\n\n"
            "✅ Необмежена кількість товарів\n"
            "✅ Пріоритетна перевірка цін\n\n"
            "Дякуємо за підтримку! 🙏"
        ),
        "premium_expired": (
            "⚠️ <b>Термін Premium закінчився</b>\n\n"
            "Твій план повернувся до безкоштовного ({n} товарів).\n\n"
            "Ці товари призупинені — збережені, але не відстежуються:\n"
            "{paused}\n\n"
            "Що можна зробити:\n"
            "• /delete — видалити зайві товари, щоб активувати інші\n"
            "• /premium — продовжити Premium\n\n"
            "<i>Усі дані збережені — нічого не втрачено 🙏</i>"
        ),

        # /help, /feedback, /language
        "help": (
            "<b>Команди:</b>\n\n"
            "/add — додати товар\n"
            "/list — мої товари\n"
            "/delete — видалити товар\n"
            "/share — поділитися списком бажань\n"
            "/settings — налаштування сповіщень\n"
            "/sites — які магазини підтримую\n"
            "/premium — підписка\n"
            "/language — мова\n\n"
            "/feedback — відгук, ідея\n\n"
            "/info — інфо про сервіс\n\n"
            "<b>Як працює:</b>\n"
            "Надсилай посилання → отримуй сповіщення, коли ціна впаде 📉"
        ),
        "feedback_prompt": (
            "💬 <b>Напиши своє повідомлення</b>\n\n"
            "Ідея, баг, питання — ділись, а я прочитаю і постараюсь відповісти."
        ),
        "feedback_thanks": "✅ Дякую! Я обов'язково прочитаю.",
        "feedback_received": "✅ Отримано, дякую!",
        "feedback_reply_header": "💬 <b>Відповідь від ЦінаБот</b>\n\n{text}",
        "language_prompt": "🌐 <b>Вибери мову / Choose a language:</b>",
        "language_set": "✅ Мова: Українська",

        # /sites
        "sites_header": "🛒 <b>Перевірені магазини</b>\n",
        "sites_country": "{country}: {names}",
        "sites_other": (
            "💡 Будь-який інший магазин зі структурованими даними про товар теж може "
            "працювати — просто спробуй надіслати посилання."
        ),
        "sites_unsupported": (
            "⛔ Поки що не працюють (блокують запити або показують ціну лише через JS): {names}."
        ),
        "sites_unsupported_many": (
            "⛔ Деякі сайти поки що не працюють (блокують запити або показують ціну лише через JS) — "
            "я скажу про це одразу, коли надішлеш посилання."
        ),

        # /info
        "info": (
            "ℹ️ <b>ЦінаБот — моніторинг цін</b>\n\n"
            "Автоматично стежу за цінами на Rozetka, OLX, Prom.ua та десятках інших магазинів (/sites) "
            "і сповіщаю, коли ціна знизилась 📉\n\n"
            "Додай товар → отримай сповіщення, коли ціна впаде 📉\n\n"
            "📍 Де шукаю знижки:\n"
            "- Rozetka — так\n"
            "- OLX — так\n"
            "- Prom.ua — так\n"
            "- Десятки інших магазинів — /sites\n"
            "- У сусіда в гаражі — в стадії розробки 😅\n\n"
            "━━━━━━━━━━━━━━━\n"
            "📦 <b>Послуги та ціни:</b>\n"
            "🆓 Безкоштовний план:\n"
            "  • до {n} товарів одночасно\n"
            "  • перевірка кожні {free_hours} годин\n"
            "  • сповіщення при зниженні ціни\n\n"
            "⭐ Premium — {price} грн/місяць:\n"
            "  • необмежена кількість товарів\n"
            "  • перевірка кожну годину\n\n"
            "💳 <b>Оплата:</b> переказ на картку (Visa / Mastercard / Monobank / ПриватБанк)\n\n"
            "━━━━━━━━━━━━━━━\n"
            "🔄 <b>Умови повернення:</b>\n"
            "Повернення коштів протягом 14 днів з моменту оплати, "
            "якщо послуга не надавалась або не відповідає опису. "
            "Для повернення — звернутись на email нижче.\n\n"
            "{contacts}\n\n"
            "<i>⚠️ Сервіс надається «як є». "
            "Точність цін залежить від зовнішніх майданчиків.\n"
            "Повернення коштів — протягом 14 днів, якщо послуга не надавалась.</i>\n\n"
            "/help — допомога по командах"
        ),

        # сповіщення планувальника
        "notif_restock": (
            "✅ <b>З'явився в наявності!</b>\n\n"
            "📦 {title}\n"
            "💰 Ціна: <b>{price}</b>\n\n"
            "🔗 <a href='{url}'>Перейти до товару</a>"
        ),
        "notif_target": (
            "🎯 <b>Ціна досягла твоєї цілі!</b>\n\n"
            "📦 {title}\n"
            "💰 Ціна зараз: <b>{price}</b>\n"
            "🎯 Твоя ціль: {target}\n\n"
            "🔗 <a href='{url}'>Купити зараз</a>"
        ),
        "notif_decrease": (
            "📉 <b>Ціна знизилась на {pct}%!</b>\n\n"
            "📦 {title}\n"
            "💰 Було: <s>{old}</s>\n"
            "💚 Стало: <b>{new}</b>\n"
            "💾 Економія: {diff}\n\n"
            "🔗 <a href='{url}'>Перейти до товару</a>"
        ),

        # налаштування сповіщень по товару (/settings, кнопки під сповіщенням)
        "btn_mute_restock": "🔕 Не сповіщати про наявність",
        "btn_mute_drop": "🔕 Не сповіщати про знижки",
        "btn_unmute": "🔔 Увімкнути знову",
        "btn_tracker_settings": "⚙️ Налаштування",
        "btn_settings_short": "⚙️",
        "mute_toast_restock": "🔕 Про наявність цього товару більше не сповіщатиму",
        "mute_toast_drop": "🔕 Про знижки на цей товар більше не сповіщатиму",
        "unmute_toast_restock": "🔔 Сповіщення про наявність увімкнено",
        "unmute_toast_drop": "🔔 Сповіщення про знижки увімкнено",
        "settings_prompt": "⚙️ <b>Налаштування сповіщень</b>\n\nВибери товар:",
        "settings_empty": "Товарів ще немає — додай перший: /add",
        "settings_item": (
            "⚙️ <b>{title}</b>\n"
            "💰 {price}\n\n"
            "Які сповіщення надсилати по цьому товару:"
        ),
        "toggle_drop": "Про зниження ціни",
        "toggle_restock": "Про наявність",
        "settings_toast_on": "🔔 Увімкнено",
        "settings_toast_off": "🔕 Вимкнено",
    },

    # ── en ────────────────────────────────────────────────────────
    "en": {
        "cancelled": "❌ Cancelled.",
        "btn_share_list": "🎁 Share my list",
        "share_send_text": "My wishlist in ЦінаБот with current prices",
        "btn_share_send": "📤 Send to friends",
        "btn_share_rotate": "🔄 New link",
        "btn_share_off": "🚫 Turn off",
        "share_empty": "🎁 Your wishlist is empty — add items first (/add or just send a link).",
        "share_intro": "🎁 <b>Your wishlist</b>\n\nLink: {link}\n\nAnyone who opens it sees {n} item(s): titles, current prices and shops — and can track them too. Your targets and username are not shown.\n\n<i>“New link” disables the old one. “Turn off” closes the list completely.</i>",
        "share_off_done": "🚫 The wishlist link is off. Turn it on again with /share.",
        "share_rotated_toast": "The old link no longer works",
        "shared_invalid": "🤷 This wishlist is no longer available — the owner turned off or renewed the link.",
        "shared_own": "🎁 This is your own wishlist. Manage the link with /share.",
        "shared_empty": "🎁 This wishlist has no items yet.",
        "shared_friend": "a friend",
        "shared_title": "🎁 <b>Wishlist: {name}</b>",
        "shared_hint": "Tap ➕ with a number to track an item and get notified when it gets cheaper.",
        "btn_shared_add_all": "➕ Track all",
        "shared_invalid_toast": "This list is no longer available",
        "shared_added_toast": "✅ Tracking: {title}",
        "shared_added_paused_toast": "Added “{title}”, but it’s paused: the free plan has an item limit. See /premium",
        "shared_added_all": "✅ Added: {added} · already tracking: {dup}",
        "bulk_busy": "⏳ Still adding your previous list — give me a minute.",
        "bulk_progress": "⏳ Adding items: {done} of {total}…",
        "bulk_done_title": "📥 <b>Done</b>",
        "bulk_added": "✅ Added: {n}",
        "bulk_duplicates": "♻️ Already tracking: {n}",
        "bulk_failed": "⚠️ Failed: {n}",
        "bulk_fail_blocked": "the site blocks automated requests",
        "bulk_fail_not_found": "page not found",
        "bulk_fail_no_price": "no price found on the page",
        "bulk_fail_forbidden": "this link can’t be checked",
        "bulk_fail_not_url": "doesn’t look like a link",
        "bulk_fail_auction_ended": "bidding has already ended",
        "bulk_fail_network": "the site didn’t respond, try later",
        "bulk_skipped": "✋ I add up to {max} items at a time — {n} more weren’t added, send them separately.",
        "bulk_paused": "⏳ Paused because of the limit: {n} (free plan — {limit} active). See /premium",
        "bulk_hint": "<i>For these items I’ll message you about any noticeable price drop. To set your own target, add an item on its own with /add.</i>",
        "bulk_file_failed": "❌ Couldn’t read the file. Try again or paste the links as text.",
        "bulk_file_empty": "🤷 No links found in the file.",
        "inbox_hint": "🔗 To track an item, send me its link — several at once or a .txt file with a list work too. Commands — /help.",

        # percent target ("tell me when it drops by 15%")
        "target_percent_range": "❌ The percent must be between 1 and 90: <code>15%</code>",
        "target_percent_no_price": (
            "❌ The current price is unknown, so a percent target won't work. "
            "Enter an amount instead: <code>4500</code>"
        ),
        "added_target_percent": "\n🎯 Target: −{pct} % (≤ {price})",
        "list_target_percent": " → 🎯−{pct}%",
        "notif_target_percent": (
            "🎯 <b>Price dropped by {pct} % — your target is reached!</b>\n\n"
            "📦 {title}\n"
            "💰 Price now: <b>{price}</b>\n"
            "🎯 Your target: −{pct} % from {baseline} (≤ {target})\n\n"
            "🔗 <a href='{url}'>Buy now</a>"
        ),

        # weekly digest "what got cheaper this week"
        "digest_header": "📬 <b>What got cheaper this week</b>\n",
        "digest_item": "📉 {title}: {old} → {new} (−{pct} %) · 30-day low: {min}",
        "digest_ups": "\n📈 Items that got pricier: {n}",
        "digest_hint": (
            "\n<i>The digest comes every Monday — and only when something got cheaper. "
            "Turn it off with the button below or in /settings.</i>"
        ),
        "btn_digest_off": "🔕 Turn off the digest",
        "digest_off_toast": "🔕 Digest turned off. Turn it back on in /settings",
        "digest_on_toast": "📬 Weekly digest turned on",
        "settings_digest": "📬 Weekly digest",

        "default_name": "friend",
        "untitled": "Untitled",
        "item": "Item",
        "price_unknown": "unknown",
        "in_stock": "✅",
        "out_of_stock": "❌ out of stock",
        "error": "Error",
        "not_found": "Item not found",
        "no_access": "❌ No access",
        "blocked": "❌ Your account is blocked. You can't use this bot.",

        "btn_add": "➕ Add item",
        "btn_add_short": "➕ Add",
        "btn_list": "📋 My items",
        "btn_list_short": "📋 List",
        "btn_delete": "🗑 Delete",
        "btn_buy_premium": "⭐ Get Premium",
        "btn_activate_premium": "⭐ Activate Premium",
        "btn_renew_premium": "🔄 Renew Premium",
        "btn_pay": "💳 Pay {price} UAH",
        "btn_language": "🌐 Language / Мова",
        "btn_cancel": "❌ Cancel",
        "btn_skip": "⏭ Skip",
        "btn_back": "◀️ Back",
        "btn_yes": "✅ Yes",
        "btn_no": "❌ No",

        "start_greeting": "👋 Hi, {name}!",
        "start_intro": (
            "<b>PriceGuard</b> — automatic price tracking on Rozetka, OLX, Prom.ua "
            "and dozens of other shops (/sites) with alerts when the price drops 📉"
        ),
        "start_premium_active": "⭐ Your Premium is active — enjoy!",
        "plans": (
            "🆓 Free: up to {n} items\n"
            "⭐ Premium {price} UAH/month: unlimited items"
        ),
        "start_links": (
            "/info — service details\n"
            "/help — available commands\n"
            "/language — language / мова"
        ),
        "contacts": (
            "━━━━━━━━━━━━━━━\n"
            "📞 <b>Contacts:</b>\n"
            "Email: willy2005@gmail.com\n"
            "Feedback in the bot: /feedback\n\n"
            "━━━━━━━━━━━━━━━\n"
            "🏢 <b>Service provider:</b>\n"
            "👤 Private individual Pavlo Zhelnov,\n"
            "📍 Pavlohrad, Ukraine"
        ),

        "add_prompt": (
            "🔗 <b>Send a product link</b>\n\n"
            "Rozetka, OLX, Prom.ua + dozens of other shops (/sites).\n\n"
            "<i>Just paste the link to the product page — several links or a .txt file with a list work too</i>"
        ),
        "not_a_url": "❌ That doesn't look like a link. Paste the product page URL (starting with http).",
        "site_needs_js": (
            "⚠️ <b>{name}</b> isn't supported yet: the page loads its price with scripts "
            "that I can't see. Working on it — try another shop (/sites)."
        ),
        "site_blocked": (
            "⛔ <b>{name}</b> blocks automated requests, so it isn't supported yet. "
            "Try another shop (/sites)."
        ),
        "checking": "⏳ Checking the product...",
        "err_blocked": "⛔ This site blocks automated requests — not supported yet.",
        "err_not_found": "❌ Page not found (404). Check the link.",
        "err_forbidden_target": "❌ This link points to an internal address — we don't track those. Send a shop page link.",
        "rate_limited": "⏳ Too many requests. Try again in {seconds} s.",
        "err_parse_failed": (
            "🤷 I couldn't find a price on this page. Make sure it's a page of a specific product, "
            "not a catalogue or search results.\n\n"
            "If this shop should be supported — tell me via /feedback and I'll add it."
        ),
        "fetch_failed": "❌ Couldn't fetch the page (network error or timeout). Try again a bit later.",
        "ask_target": (
            "📦 <b>{title}</b>\n\n"
            "💰 {price}  {avail}\n\n"
            "Set a target price? (I'll notify you when it drops)\n"
            "Enter an amount in {currency} (<code>4500</code>) or a percent drop (<code>15%</code>), "
            "or tap “Skip”"
        ),
        "target_invalid": "❌ Enter a number (<code>4500</code>) or a percent (<code>15%</code>)",
        "already_tracked": "⚠️ You're already tracking this item!",
        "added": "✅ <b>Added!</b>\n\n📦 {title}{target}{slots}",
        "added_target": "\n🎯 Target: {price}",
        # ── Auctions (Violity): target direction and end-of-bidding reminder ──
        "btn_tdir_above": "📈 When the bid goes above an amount",
        "btn_tdir_below": "📉 When the price drops below an amount",
        "btn_auction_no_target": "⏰ No target — just remind me of the end",
        "auction_card": (
            "🔨 <b>{title}</b>\n\n"
            "💰 Current bid: <b>{price}</b>\n"
            "⏳ Bidding ends: {end}\n\n"
            "When should I notify you?\n"
            "• <b>goes above</b> — bids rose past your amount;\n"
            "• <b>drops below</b> — for lots where the price goes down.\n\n"
            "I’ll remind you once before bidding ends — you’ll pick how early with buttons after adding."
        ),
        "auction_ended_at_add": "🔨 <b>{title}</b>\n\nBidding has already ended (last price {price}) — nothing to track.",
        "ask_target_above": (
            "📈 Current bid: <b>{price}</b>\n\n"
            "Enter an amount in {currency} above the current bid (<code>500000</code>), "
            "or by how many percent the price should rise (<code>20%</code>)."
        ),
        "ask_target_below": (
            "📉 Current price: <b>{price}</b>\n\n"
            "Enter an amount in {currency} below the current price (<code>300000</code>), "
            "or by how many percent the price should drop (<code>15%</code>)."
        ),
        "auction_choose_direction": "First choose when to notify: when the bid goes <b>above</b> an amount or the price <b>drops below</b> it.",
        "auction_above_invalid": "❌ The amount must be higher than the current bid ({price}).",
        "auction_below_invalid": "❌ The amount must be lower than the current price ({price}).",
        "added_target_above": "\n📈 I’ll notify you when the bid goes above {price}",
        "added_target_below_auction": "\n📉 I’ll notify you when the price drops to {price} or below",
        "added_reminder": "\n\n⏰ Bidding ends {end}. When should I remind you? 👇",
        "notif_target_above": (
            "📈 <b>The bid went above your limit!</b>\n\n"
            "📦 {title}\n"
            "💰 Bid now: <b>{price}</b>\n"
            "🎯 Your limit: {target}\n\n"
            "🔗 <a href='{url}'>Open the lot</a>"
        ),
        "notif_ending": (
            "⏰ <b>Bidding ends in {left}</b>\n\n"
            "📦 {title}\n"
            "💰 Current bid: <b>{price}</b>\n"
            "🏁 Ends: {end}\n\n"
            "🔗 <a href='{url}'>Open the lot</a>"
        ),
        "minutes_short": "{n} min",
        "reminder_60": "1 h before",
        "reminder_30": "30 min before",
        "reminder_15": "15 min before",
        "reminder_5": "5 min before",
        "reminder_off": "off",
        "rem_btn_60": "1 h",
        "rem_btn_30": "30 min",
        "rem_btn_15": "15 min",
        "rem_btn_5": "5 min",
        "rem_btn_off": "🔕 No end-of-bidding reminder",
        "settings_toast_reminder": "⏰ Reminder: {value}",
        "list_target_above": " → 📈≥{price}",
        "added_paused": (
            "\n\n⏳ <i>Added, but paused (limit of {n} active items). "
            "Get /premium to unlock.</i>"
        ),
        "added_slots": "\n\n<i>Slots used: {used}/{n}</i>",

        "list_empty": "📭 You're not tracking anything yet.",
        "list_header": "📋 <b>Your items ({n}):</b>\n",
        "list_paused_limit": "(paused: limit)",
        "list_more": "\n<i>... and {n} more items not shown.</i>\n",
        "plan_premium": "⭐ <b>Your plan: Premium (unlimited)</b>",
        "plan_free": "🆓 <b>Your plan: Free</b>\n<i>Active: {active}/{total} items.</i>",
        "plan_free_limit_note": (
            "\n\n⚠️ Some items are paused because of the limit. "
            "Activate /premium to track all of them."
        ),

        "delete_empty": "📭 Nothing to delete.",
        "delete_prompt": "🗑 <b>Choose an item to delete:</b>",
        "delete_confirm": "❓ <b>Delete?</b>\n\n📦 {title}",
        "deleted": "✅ <b>Deleted!</b>\n\n📦 {title}\n\n<i>Remaining: {n} items</i>",
        "deleted_toast": "Deleted ✅",
        "deleted_activated": "\n\nA slot freed up — resumed:\n{titles}",

        "premium_active": (
            "⭐ <b>Your Premium is active!</b>\n\n"
            "📅 Valid until: <b>{until}</b>\n\n"
            "✅ Unlimited items\n"
            "✅ Priority price checks\n\n"
            "Thank you for your support! 🙏\n\n"
            "<i>To renew, tap the button below once your subscription expires.</i>"
        ),
        "premium_offer": (
            "⭐ <b>Premium</b> — {price} UAH/month\n\n"
            "✅ Unlimited items\n"
            "✅ Priority price checks\n\n"
            "<i>Payment: Visa / Mastercard / Monobank</i>"
        ),
        "already_premium_alert": "You already have Premium! ⭐",
        "pay_unavailable": "💳 Payment is temporarily unavailable. Message us via /feedback and we'll activate it manually.",
        "pay_instructions": (
            "💳 <b>Premium payment — {price} UAH</b>\n\n"
            "Transfer to a PrivatBank card (Ukraine):\n\n"
            "<code>{card}</code>\n\n"
            "Recipient: <b>Pavlo Zhelnov</b>\n"
            "Amount: <b>{price} UAH</b>\n\n"
            "⚠️ Put this in the payment reference:\n"
            "<code>{ref}</code>\n\n"
            "After paying, send a screenshot via /feedback — "
            "I'll activate Premium within a few hours 🙏"
        ),
        "premium_activated": (
            "🎉 <b>Premium activated!</b>\n\n"
            "📅 Valid until: <b>{until}</b>\n\n"
            "✅ Unlimited items\n"
            "✅ Priority price checks\n\n"
            "Thank you for your support! 🙏"
        ),
        "premium_expired": (
            "⚠️ <b>Your Premium has expired</b>\n\n"
            "Your plan is back to Free ({n} items).\n\n"
            "These items are paused — saved, but not tracked:\n"
            "{paused}\n\n"
            "What you can do:\n"
            "• /delete — remove extra items to activate others\n"
            "• /premium — renew Premium\n\n"
            "<i>All your data is kept — nothing is lost 🙏</i>"
        ),

        "help": (
            "<b>Commands:</b>\n\n"
            "/add — add an item\n"
            "/list — my items\n"
            "/delete — delete an item\n"
            "/share — share my wishlist\n"
            "/settings — notification settings\n"
            "/sites — supported shops\n"
            "/premium — subscription\n"
            "/language — language\n\n"
            "/feedback — feedback, ideas\n\n"
            "/info — about the service\n\n"
            "<b>How it works:</b>\n"
            "Send a link → get notified when the price drops 📉"
        ),
        "feedback_prompt": (
            "💬 <b>Write your message</b>\n\n"
            "An idea, a bug, a question — I'll read it and try to reply."
        ),
        "feedback_thanks": "✅ Thanks! I'll definitely read it.",
        "feedback_received": "✅ Received, thank you!",
        "feedback_reply_header": "💬 <b>Reply from ЦінаБот</b>\n\n{text}",
        "language_prompt": "🌐 <b>Choose a language / Вибери мову:</b>",
        "language_set": "✅ Language: English",

        "sites_header": "🛒 <b>Verified shops</b>\n",
        "sites_country": "{country}: {names}",
        "sites_other": (
            "💡 Any other shop with structured product data may work too — "
            "just try sending a link."
        ),
        "sites_unsupported": (
            "⛔ Not working yet (they block requests or render the price with JS only): {names}."
        ),
        "sites_unsupported_many": (
            "⛔ Some sites don't work yet (they block requests or render the price with JS only) — "
            "I'll tell you right away when you send a link."
        ),

        "info": (
            "ℹ️ <b>PriceGuard — price tracking</b>\n\n"
            "I automatically track prices on Rozetka, OLX, Prom.ua and dozens of other shops (/sites) "
            "and notify you when the price drops 📉\n\n"
            "Add an item → get an alert when the price drops 📉\n\n"
            "📍 Where I look for deals:\n"
            "- Rozetka — yes\n"
            "- OLX — yes\n"
            "- Prom.ua — yes\n"
            "- Dozens of other shops — /sites\n"
            "- Your neighbour's garage — coming soon 😅\n\n"
            "━━━━━━━━━━━━━━━\n"
            "📦 <b>Plans and pricing:</b>\n"
            "🆓 Free plan:\n"
            "  • up to {n} items at a time\n"
            "  • checks every {free_hours} hours\n"
            "  • alerts when the price drops\n\n"
            "⭐ Premium — {price} UAH/month:\n"
            "  • unlimited items\n"
            "  • checks every hour\n\n"
            "💳 <b>Payment:</b> card transfer (Visa / Mastercard / Monobank / PrivatBank)\n\n"
            "━━━━━━━━━━━━━━━\n"
            "🔄 <b>Refund policy:</b>\n"
            "Refunds within 14 days of payment if the service was not provided "
            "or does not match the description. "
            "To request a refund, contact the email below.\n\n"
            "{contacts}\n\n"
            "<i>⚠️ The service is provided “as is”. "
            "Price accuracy depends on the external marketplaces.\n"
            "Refunds within 14 days if the service was not provided.</i>\n\n"
            "/help — command reference"
        ),

        "notif_restock": (
            "✅ <b>Back in stock!</b>\n\n"
            "📦 {title}\n"
            "💰 Price: <b>{price}</b>\n\n"
            "🔗 <a href='{url}'>Open product</a>"
        ),
        "notif_target": (
            "🎯 <b>Price reached your target!</b>\n\n"
            "📦 {title}\n"
            "💰 Price now: <b>{price}</b>\n"
            "🎯 Your target: {target}\n\n"
            "🔗 <a href='{url}'>Buy now</a>"
        ),
        "notif_decrease": (
            "📉 <b>Price dropped by {pct}%!</b>\n\n"
            "📦 {title}\n"
            "💰 Was: <s>{old}</s>\n"
            "💚 Now: <b>{new}</b>\n"
            "💾 You save: {diff}\n\n"
            "🔗 <a href='{url}'>Open product</a>"
        ),

        # per-item notification settings (/settings, buttons under a notification)
        "btn_mute_restock": "🔕 Don't notify about stock",
        "btn_mute_drop": "🔕 Don't notify about price drops",
        "btn_unmute": "🔔 Turn back on",
        "btn_tracker_settings": "⚙️ Settings",
        "btn_settings_short": "⚙️",
        "mute_toast_restock": "🔕 No more stock alerts for this item",
        "mute_toast_drop": "🔕 No more price-drop alerts for this item",
        "unmute_toast_restock": "🔔 Stock alerts are back on",
        "unmute_toast_drop": "🔔 Price-drop alerts are back on",
        "settings_prompt": "⚙️ <b>Notification settings</b>\n\nPick an item:",
        "settings_empty": "No items yet — add your first one: /add",
        "settings_item": (
            "⚙️ <b>{title}</b>\n"
            "💰 {price}\n\n"
            "Which alerts to send for this item:"
        ),
        "toggle_drop": "Price drops",
        "toggle_restock": "Back in stock",
        "settings_toast_on": "🔔 Enabled",
        "settings_toast_off": "🔕 Disabled",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    """Повертає переклад за ключем. Невідома мова або ключ → fallback на uk."""
    table = _T.get(lang) or _T[DEFAULT_LANG]
    template = table.get(key) or _T[DEFAULT_LANG].get(key)
    if template is None:
        raise KeyError(f"i18n: missing key {key!r}")
    return template.format(**kwargs) if kwargs else template


# Назви країн для /sites (код ISO-2 з реєстру платформ → прапор + назва)
_COUNTRY_NAMES: dict[str, dict[str, str]] = {
    "uk": {
        "UA": "Україна", "PL": "Польща", "DE": "Німеччина", "US": "США", "GB": "Велика Британія",
        "CZ": "Чехія", "FR": "Франція", "IT": "Італія", "ES": "Іспанія", "NL": "Нідерланди",
        "TR": "Туреччина", "CN": "Китай", "KZ": "Казахстан", "MD": "Молдова", "RO": "Румунія",
        "LT": "Литва", "LV": "Латвія", "EE": "Естонія", "CA": "Канада", "JP": "Японія",
        "SE": "Швеція", "AT": "Австрія", "CH": "Швейцарія", "BE": "Бельгія", "DK": "Данія", "NO": "Норвегія", "FI": "Фінляндія",
        "GLOBAL": "Міжнародні",
    },
    "en": {
        "UA": "Ukraine", "PL": "Poland", "DE": "Germany", "US": "USA", "GB": "United Kingdom",
        "CZ": "Czechia", "FR": "France", "IT": "Italy", "ES": "Spain", "NL": "Netherlands",
        "TR": "Türkiye", "CN": "China", "KZ": "Kazakhstan", "MD": "Moldova", "RO": "Romania",
        "LT": "Lithuania", "LV": "Latvia", "EE": "Estonia", "CA": "Canada", "JP": "Japan",
        "SE": "Sweden", "AT": "Austria", "CH": "Switzerland", "BE": "Belgium", "DK": "Denmark", "NO": "Norway", "FI": "Finland",
        "GLOBAL": "International",
    },
}


def country_label(code: str, lang: str) -> str:
    """'UA' → '🇺🇦 Україна' / '🇺🇦 Ukraine'. Невідомий код → прапор (якщо ISO-2) + сам код."""
    code = (code or "").upper()
    names = _COUNTRY_NAMES.get(lang) or _COUNTRY_NAMES[DEFAULT_LANG]
    name = names.get(code, code)
    if code == "GLOBAL":
        flag = "🌍"
    elif len(code) == 2 and code.isalpha():
        flag = "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)
    else:
        flag = "🏳️"
    return f"{flag} {name}".strip()


# Описи команд для меню Telegram (set_my_commands)
BOT_COMMANDS: dict[str, list[tuple[str, str]]] = {
    "uk": [
        ("add", "Додати товар"),
        ("list", "Мої товари"),
        ("delete", "Видалити товар"),
        ("share", "Поділитися списком бажань"),
        ("settings", "Налаштування сповіщень"),
        ("sites", "Підтримувані магазини"),
        ("premium", "Підписка Premium"),
        ("language", "Мова / Language"),
        ("feedback", "Відгук або ідея"),
        ("info", "Інфо про сервіс"),
        ("help", "Команди"),
    ],
    "en": [
        ("add", "Add an item"),
        ("list", "My items"),
        ("delete", "Delete an item"),
        ("share", "Share my wishlist"),
        ("settings", "Notification settings"),
        ("sites", "Supported shops"),
        ("premium", "Premium subscription"),
        ("language", "Language / Мова"),
        ("feedback", "Feedback or idea"),
        ("info", "About the service"),
        ("help", "Commands"),
    ],
}
