"""
Налаштування stdlib-логування для всіх процесів (bot.py, admin.py).

- httpx/httpcore на INFO пишуть повний URL запиту, а в URL Bot API сидить токен
  (https://api.telegram.org/bot<TOKEN>/sendMessage) — тому їх піднімаємо до WARNING.
- Форматер додатково маскує будь-який токен бота в тексті й traceback'ах (напр., HTTPStatusError з URL).
"""
from __future__ import annotations

import logging
import re
import sys

_BOT_TOKEN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")
QUIET_LOGGERS = ("httpx", "httpcore")


def redact(text: str) -> str:
    return _BOT_TOKEN.sub("<BOT_TOKEN>", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, stream=sys.stdout)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter(handler.formatter._fmt if handler.formatter else None))
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
