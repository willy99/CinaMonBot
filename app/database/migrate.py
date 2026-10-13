"""
Міграції схеми (Alembic) — логіка в спільному пакеті botkit.db.migrate; тут лише контекст проєкту й CLI.

    python -m app.database.migrate          # ./run.sh migrate; ./run.sh start і dev роблять це самі
    python -m app.database.migrate --check  # 0 — схема актуальна, 1 — треба мігрувати
"""
import app.database.session  # noqa: F401 — налаштовує контекст botkit (app/botkit_setup.py)
from botkit.db.migrate import (  # noqa: F401 — публічне API для процесів, CLI і тестів
    SchemaOutdated,
    alembic_config,
    current_revision,
    ensure_schema,
    head_revision,
    main,
    migrate,
)

if __name__ == "__main__":
    main()
