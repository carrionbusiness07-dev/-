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
import signal

from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError, validate_token

from minsk_realty.bot.config import BotConfig, ConfigError
from minsk_realty.bot.logsafe import setup_logging


def wait_for_stop() -> None:
    """Ждать SIGTERM/SIGINT (docker stop, Ctrl+C), не тратя ресурсы."""
    stop = {signal.SIGINT, signal.SIGTERM}
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_BLOCK, stop)
        signal.sigwait(stop)
    else:  # Windows без docker: просто выходим, перезапускать некому
        return


def idle(reason: str) -> int:
    """Настройки не позволяют работать: одно понятное сообщение и ожидание остановки.

    Выход с ошибкой при `restart: unless-stopped` превратился бы в перезапуск раз в минуту
    с тем же сообщением в логе. Ожидание — сервис bot «спит», пока токен не вписан;
    после правки .env: `docker compose up -d --force-recreate bot`."""
    logging.getLogger("minsk_realty.bot").error("%s Бот не работает и ждёт остановки.", reason)
    wait_for_stop()
    return 0


def main() -> int:
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"), os.environ.get("TELEGRAM_BOT_TOKEN"))
    try:
        cfg = BotConfig.from_env()
        validate_token(cfg.token)
    except ConfigError as exc:
        return idle(str(exc))
    except TokenValidationError:
        return idle(
            "TELEGRAM_BOT_TOKEN выглядит неправильно: нужен токен от @BotFather вида 123:ABC…."
        )

    from minsk_realty.bot.app import run

    # Ошибки — через logging (фильтр вырезает токен), а не стандартным traceback Python.
    log = logging.getLogger("minsk_realty.bot")
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass
    except TelegramUnauthorizedError:
        # Токен отозван или с опечаткой: перезапуск не поможет — ждём, пока человек поправит.
        return idle("Telegram не принял TELEGRAM_BOT_TOKEN: проверьте токен у @BotFather.")
    except TelegramNetworkError as exc:
        log.error("нет связи с api.telegram.org (%s), перезапуск через Docker", exc.message)
        return 1
    except Exception:
        log.exception("бот остановился с ошибкой")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
