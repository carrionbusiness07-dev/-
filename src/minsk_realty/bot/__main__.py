"""Запуск бота.

    python -m minsk_realty.bot

Нужны DATABASE_URL (миграции применены: `alembic upgrade head`) и TELEGRAM_BOT_TOKEN.
Необязательно: ADMIN_CHAT_ID (команда /weekly), CHANNEL_ID (недельная сводка в канал),
BOT_WATCH_LIMIT (квартир в списке, по умолчанию 10), BOT_NOTIFY_EVERY_MIN (по умолчанию 10).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError, validate_token

from minsk_realty.bot.config import BotConfig, ConfigError
from minsk_realty.bot.logsafe import setup_logging


def main() -> int:
    try:
        cfg = BotConfig.from_env()
        validate_token(cfg.token)
    except ConfigError as exc:
        sys.exit(str(exc))
    except TokenValidationError:
        sys.exit("TELEGRAM_BOT_TOKEN выглядит неправильно: нужен токен от @BotFather вида 123:ABC…")
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"), cfg.token)

    from minsk_realty.bot.app import run

    # Ошибки — через logging (фильтр вырезает токен), а не стандартным traceback Python.
    log = logging.getLogger("minsk_realty.bot")
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass
    except TelegramUnauthorizedError:
        log.error("Telegram не принял TELEGRAM_BOT_TOKEN: проверьте токен у @BotFather")
        return 1
    except TelegramNetworkError as exc:
        log.error("нет связи с api.telegram.org (%s), перезапуск через Docker", exc.message)
        return 1
    except Exception:
        log.exception("бот остановился с ошибкой")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
