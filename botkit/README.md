# botkit

Спільний код Snipket (`~/work/snipket`) і ЦінаБот / PriceGuard (`~/work/priceguard`). Джерело правди —
цей репозиторій (GitHub `willy99/botkit`, приватний; локально `~/work/botkit`); у проєкти він вбудований
через **git subtree** у папку `botkit/` (імпорт: `import botkit`).

| Модуль | Що робить |
|---|---|
| `botkit.context` | `setup(Context(...))` — проєкт передає налаштування, сесію БД, моделі, i18n, тему адмінки |
| `botkit.admin.auth` | паролі (scrypt), 2FA-код у Telegram, сесії з `session_version`, ролі owner/admin, блокування за логіном |
| `botkit.admin.queries` | запити про адмінів і журнал дій |
| `botkit.admin.pages` | сторінки `/login` і `/admins` (`register()`) |
| `botkit.admin.telegram` | медіа з Telegram для адмінки: inline лише растрові картинки, nosniff + CSP |
| `botkit.bot.middlewares` | заблоковані адміном; ліміт `USER_REQUESTS_PER_MINUTE` на користувача |
| `botkit.db.migrate` | Alembic: `migrate()`, `ensure_schema()`, legacy-шлях; `migration_helpers`, `alembic_env` |
| `botkit.ops` | `backup`, `healthcheck` (дебаунс, лише адміну), `heartbeat` |
| `botkit.runtime_settings` | налаштування «на льоту»: перекриття `.env` у таблиці `app_settings`, набір параметрів реєструє проєкт; сторінка `/settings` — `pages.register(with_settings=True)` |
| `botkit.logs` | логування з маскуванням токена бота |
| `botkit.ratelimit`, `botkit.breaker`, `botkit.netguard` | ковзне вікно, запобіжник, захист від SSRF |

Пакет **нічого не імпортує з проєкту** (`app.*`). Усе специфічне — скрапери, перевізники, тема, i18n,
CLI — лишається в проєктах.

## Підключення в проєкт

```python
# app/botkit_setup.py — імпортується з app/config.py, тож контекст є в кожному процесі
import botkit
botkit.setup(botkit.Context(app_name="Snipket", settings=settings, get_session=get_session,
                            models=botkit.Models(Admin, AdminAction, User, Heartbeat), root=ROOT, ...))
```

Моделі мають поля, які читає пакет:
- `Admin`: `id`, `username`, `password_hash`, `telegram_id`, `is_active`, `last_login_at`, `role`, `session_version`,
  `failed_logins`, `locked_until`, `created_at`;
- `AdminAction`: `admin_id`, `admin_username`, `action`, `target`, `details`, `created_at`;
- `User`: `telegram_id`, `is_blocked`;
- `Heartbeat`: `name`, `at`, `info`;
- `AppSetting` (для `/settings`): `key`, `value`, `updated_by`, `updated_at`.

## Оновлення в проєктах

```bash
# у ~/work/botkit: зміни + тести + коміт + push
cd ~/work/botkit && PYTHONPATH=.. ../snipket/venv/bin/python -m pytest -q && git push
# у кожному проєкті: підтягнути з GitHub (робоче дерево проєкту має бути закомічене)
cd ~/work/snipket && ./run.sh botkit-pull
```

Джерело для `botkit-pull` — GitHub `willy99/botkit` (за замовчуванням у `run.sh`). Щоб підтягнути ще не
запушену локальну версію: `BOTKIT_REPO=~/work/botkit ./run.sh botkit-pull`.

Правка прямо в `botkit/` усередині проєкту теж можлива. Тоді треба віддати її назад:
`git subtree push --prefix=botkit ~/work/botkit <гілка>`.

Тести пакета (`botkit/tests`) проганяються і в CI кожного проєкту.
