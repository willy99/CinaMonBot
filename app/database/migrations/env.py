"""Alembic env: спільна логіка — botkit/db/alembic_env.py."""
from app.config import settings
from app.database.models import Base
from botkit.db.alembic_env import run

run(Base.metadata, settings.DATABASE_URL)
