"""Общий HTTP-клиент с бережным режимом и контракт адаптера площадки."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator
from typing import Any, Protocol

import httpx

from minsk_realty.models import ListingSnapshot, RawListing

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; KvartirnayaIstoriya/0.1)",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


class SourceBlocked(RuntimeError):
    """Площадка трижды подряд ответила 403/429: источник останавливается до разбора человеком."""


class PoliteClient:
    """HTTP-клиент, который не давит на площадку.

    - пауза 2–5 с между страницами выдачи (`pause()`);
    - на 403/429 — экспоненциальный backoff, после трёх подряд — `SourceBlocked`;
    - никакой ротации прокси и подмены отпечатков: если площадка закрылась, это повод
      для разговора о партнёрстве, а не для обхода.
    """

    max_consecutive_refusals = 3

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        page_pause: tuple[float, float] = (2.0, 5.0),
        backoff_base: float = 30.0,
        timeout: float = 30.0,
    ) -> None:
        self._client = httpx.Client(
            headers=DEFAULT_HEADERS, transport=transport, timeout=timeout, follow_redirects=True
        )
        self._sleep = sleep
        self._page_pause = page_pause
        self._backoff_base = backoff_base
        self._refusals = 0

    def get(
        self, url: str, *, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> httpx.Response:
        while True:
            response = self._client.get(url, params=params, **kwargs)
            if response.status_code not in (403, 429):
                self._refusals = 0
                response.raise_for_status()
                return response
            self._refusals += 1
            if self._refusals >= self.max_consecutive_refusals:
                raise SourceBlocked(f"{url}: {response.status_code} {self._refusals} раза подряд")
            self._sleep(self._backoff_base * 2 ** (self._refusals - 1))

    def pause(self) -> None:
        self._sleep(random.uniform(*self._page_pause))

    def close(self) -> None:
        self._client.close()


class SourceAdapter(Protocol):
    """Контракт адаптера площадки (см. .claude/skills/minsk-realty/SKILL.md)."""

    source: str

    # True, если последний search() дошёл до конца выдачи. Без этого сборщик не гасит
    # пропавшие объявления (см. db.repository.mark_unseen_inactive).
    last_search_complete: bool

    def search(self, *, max_pages: int = 500) -> Iterator[RawListing]: ...

    def parse(self, raw: RawListing) -> ListingSnapshot: ...

    def resolve_url(self, url: str) -> str | None: ...
