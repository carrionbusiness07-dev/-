"""Токен бота не должен попасть в логи.

Токен входит в URL каждого запроса к api.telegram.org, а ошибки aiohttp/httpx иногда
печатают URL целиком (в тексте исключения или в цепочке traceback). Фильтр на обработчиках
вырезает токен из любого сообщения и traceback, а болтливые логгеры HTTP-клиентов
поднимаются до WARNING (как в collector/__main__.py).
"""

from __future__ import annotations

import logging
import re

BOT_URL_TOKEN = re.compile(r"bot\d{5,}:[\w-]{20,}")
MASK = "bot<TOKEN>"


def redact(text: str, token: str | None = None) -> str:
    if token:
        text = text.replace(token, "<TOKEN>")
    return BOT_URL_TOKEN.sub(MASK, text)


class RedactTokenFilter(logging.Filter):
    def __init__(self, token: str | None = None) -> None:
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        clean = redact(message, self.token)
        if clean != message or record.args:
            record.msg, record.args = clean, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self.token)
        if record.stack_info:
            record.stack_info = redact(record.stack_info, self.token)
        return True


def setup_logging(level: str, token: str | None) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    redactor = RedactTokenFilter(token)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
    # INFO этих логгеров пишет URL запросов, а в URL — токен.
    for name in ("httpx", "httpcore", "aiohttp", "aiohttp.client", "aiohttp.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
