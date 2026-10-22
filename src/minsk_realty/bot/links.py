"""Ссылки из сообщения пользователя: найти, понять площадку, отбросить лишнее.

Площадку и id объявления определяют адаптеры Sam (`resolve_url`) — бот правил разбора
ссылок не дублирует. Здесь только поиск ссылок в тексте и вежливые подсказки.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from urllib.parse import urlsplit, urlunsplit

from minsk_realty.sources.kufar import KufarAdapter
from minsk_realty.sources.realt import RealtAdapter

# Площадки, которые бот уже понимает, и те, что «скоро».
SUPPORTED = ("Kufar", "Realt")
COMING = {"onliner.by": "Onliner", "domovita.by": "Domovita"}

# Ссылка в тексте: со схемой или без («re.kufar.by/vi/123»).
URL_RE = re.compile(r"(?:https?://)?(?:[\w-]+\.)+[a-z]{2,}\b(?:/[^\s<>\"«»]*)?", re.IGNORECASE)
_TRAILING = ".,;:!?)]}…"
# Ищем ссылки только в начале текста: ссылку присылают в начале, а регулярное выражение
# на 4096 символах мусора держит GIL ~0.3 с.
MAX_SCAN = 1000


@cache
def _adapters() -> tuple:
    # resolve_url не ходит в сеть; один экземпляр на процесс.
    return (KufarAdapter(), RealtAdapter())


@dataclass(frozen=True)
class Link:
    url: str  # как прислал пользователь (без хвостовой пунктуации)
    host: str
    source: str | None = None  # kufar / realt, если адаптер понял ссылку
    external_id: str | None = None

    @property
    def supported(self) -> bool:
        return self.source is not None

    @property
    def coming_soon(self) -> str | None:
        """«Onliner», если это площадка, которую мы ещё не собираем."""
        for domain, name in COMING.items():
            if self.host == domain or self.host.endswith("." + domain):
                return name
        return None

    @property
    def clean(self) -> str:
        """Ссылка без параметров и якоря: для журнала checks (utm и метки не храним)."""
        parts = urlsplit(self.url if "://" in self.url else "https://" + self.url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))[:512]


def _host(url: str) -> str:
    parts = urlsplit(url if "://" in url else "https://" + url)
    host = (parts.hostname or "").lower()
    return host.removeprefix("www.")


def resolve(url: str) -> tuple[str, str] | None:
    """→ (площадка, id объявления) или None, если ни один адаптер ссылку не понял."""
    for adapter in _adapters():
        external_id = adapter.resolve_url(url)
        if external_id:
            return adapter.source, external_id
    return None


def find_links(text: str | None) -> list[Link]:
    """Все ссылки из текста по порядку. Поддерживаемые помечены площадкой и id."""
    out = []
    for match in URL_RE.finditer((text or "")[:MAX_SCAN]):
        url = match.group(0).rstrip(_TRAILING)
        host = _host(url)
        if not host or "." not in host:
            continue
        resolved = resolve(url)
        out.append(
            Link(
                url=url,
                host=host,
                source=resolved[0] if resolved else None,
                external_id=resolved[1] if resolved else None,
            )
        )
    return out


def first_supported(links: list[Link]) -> Link | None:
    return next((link for link in links if link.supported), None)
