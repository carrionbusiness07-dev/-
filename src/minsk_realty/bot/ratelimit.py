"""Ограничение частоты запросов одного пользователя (сообщения и кнопки).

Корзина токенов в памяти: `burst` запросов подряд, дальше — `per_minute` в минуту.
Защищает от флуда и перебора кнопок; после перезапуска бота счётчики обнуляются —
для защиты от флуда этого достаточно. Сверх лимита: на кнопку — всплывающее «слишком часто»
(иначе крутится индикатор), на сообщение — один ответ, дальше молча.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from minsk_realty.bot.texts import TOO_OFTEN, esc

MAX_USERS = 10_000  # сколько корзин держать в памяти, прежде чем выбросить полные


class TokenBucket:
    def __init__(
        self, burst: int, per_minute: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.burst, self.rate, self.clock = burst, per_minute / 60, clock
        self._state: dict[int, tuple[float, float]] = {}

    def hit(self, key: int) -> bool:
        """True — запрос пропускаем (и тратим токен), False — слишком часто."""
        now = self.clock()
        tokens, last = self._state.get(key, (float(self.burst), now))
        tokens = min(float(self.burst), tokens + (now - last) * self.rate)
        allowed = tokens >= 1
        self._state[key] = (tokens - 1 if allowed else tokens, now)
        if len(self._state) > MAX_USERS:
            self._prune(now)
        return allowed

    def _prune(self, now: float) -> None:
        full = [
            k
            for k, (tokens, last) in self._state.items()
            if tokens + (now - last) * self.rate >= self.burst
        ]
        for k in full:
            del self._state[k]


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, bucket: TokenBucket) -> None:
        self.bucket = bucket
        self._warned: set[int] = set()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        if self.bucket.hit(user.id):
            self._warned.discard(user.id)
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            with suppress(Exception):
                await event.answer(TOO_OFTEN)
        elif isinstance(event, Message) and user.id not in self._warned:
            self._warned.add(user.id)
            with suppress(Exception):
                await event.answer(esc(TOO_OFTEN))
        return None
