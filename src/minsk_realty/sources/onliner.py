"""Адаптер Onliner (r.onliner.by/pk): продажа квартир в Минске через поисковый JSON-API.

Эндпоинт и поля сверены с живым ответом 23.10.2026
(tests/fixtures/onliner/search_sale_minsk.2026-10-23.json, см. docs/recon/sources.md).

Onliner показывает у себя и объявления Realt: у такой копии фото лежит на files.realt.by
(тот же идентификатор снимка, та же цена и адрес, что на Realt). Копии не собираем: они
ничего не добавляют к истории, а каждая дала бы ложный «дубль на второй площадке».
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from minsk_realty.models import ListingSnapshot, RawListing
from minsk_realty.privacy import scrub, seller_key
from minsk_realty.sources.base import PoliteClient

log = logging.getLogger(__name__)

SEARCH_URL = "https://pk.api.onliner.by/search/apartments"
# Прямоугольник вокруг Минска; пригороды из выдачи отсекаются по адресу (`is_minsk`).
SEARCH_PARAMS = {
    "bounds[lb][lat]": "53.7",
    "bounds[lb][long]": "27.3",
    "bounds[rt][lat]": "54.1",
    "bounds[rt][long]": "27.8",
}
ITEM_URL = "https://r.onliner.by/pk/apartments/{id}"
URL_RE = re.compile(r"r\.onliner\.by/pk/apartments/(\d+)")

CURRENCIES = {"USD": "USD", "BYN": "BYN", "EUR": "EUR", "RUB": "RUB"}
MINSK = frozenset({"минск", "minsk"})
# Хосты фото, по которым видно, что объявление — копия с другой площадки.
MIRROR_HOSTS = {"realt.by": "realt"}


class OnlinerAdapter:
    source = "onliner"

    def __init__(self, client: PoliteClient | None = None) -> None:
        self._client = client or PoliteClient()
        # True, если последний search() дошёл до конца выдачи, а не упёрся в max_pages.
        self.last_search_complete = False
        self.mirrors_skipped = 0

    def search(self, *, max_pages: int = 500) -> Iterator[RawListing]:
        self.last_search_complete = False
        self.mirrors_skipped = 0
        native = 0
        for page in range(1, max_pages + 1):
            if page > 1:
                self._client.pause()
            data = self._client.get(SEARCH_URL, params={**SEARCH_PARAMS, "page": page}).json()
            items = data.get("apartments") or []
            for item in items:
                if not is_minsk(item):
                    continue
                if mirror_of(item):
                    self.mirrors_skipped += 1
                    continue
                native += 1
                yield to_raw(item)
            last = int((data.get("page") or {}).get("last") or 0)
            if page >= last or not items:
                # Полный проход — только дошли до последней страницы, которую назвала площадка.
                # Пустая страница раньше неё (сбой, мягкая блокировка) — обрыв: иначе все
                # объявления дальше погасли бы как снятые.
                self.last_search_complete = last > 0 and page >= last
                break
        share = self.mirrors_skipped / (self.mirrors_skipped + native or 1)
        log.info(
            "onliner: своих %d, копий Realt пропущено %d (%.0f%%), полный проход: %s",
            native,
            self.mirrors_skipped,
            share * 100,
            self.last_search_complete,
        )
        if self.last_search_complete and native > 500 and not self.mirrors_skipped:
            # Обычно копий около половины. Ноль — скорее всего, изменился формат ссылок на фото,
            # и копии Realt пошли в базу как объявления Onliner (ложные «дубли»).
            log.warning("onliner: ни одной копии Realt — проверьте разбор фото (mirror_of)")

    def parse(self, raw: RawListing, *, seen_at: datetime | None = None) -> ListingSnapshot:
        item = raw.payload
        location = item.get("location") or {}
        price = item.get("price") or {}
        usd = ((price.get("converted") or {}).get("USD") or {}).get("amount")
        area = item.get("area") or {}
        seller = (item.get("seller") or {}).get("type")
        photo = original_photo_url(item.get("photo"))
        return ListingSnapshot(
            source="onliner",
            external_id=raw.external_id,
            url=item.get("url") or ITEM_URL.format(id=raw.external_id),
            seen_at=seen_at or datetime.now(UTC),
            price_amount=_decimal(price.get("amount")),
            price_currency=CURRENCIES.get(str(price.get("currency") or "").upper()),
            price_usd=_decimal(usd),
            seller_type={"agent": "agency", "owner": "owner"}.get(seller, "unknown"),
            seller_key=item.get("_seller_key"),
            rooms=item.get("number_of_rooms"),
            area_total=area.get("total"),
            floor=item.get("floor"),
            floors_total=item.get("number_of_floors"),
            address_raw=location.get("address") or location.get("user_address"),
            town="Минск",
            lat=location.get("latitude"),
            lon=location.get("longitude"),
            posted_at=_dt(item.get("created_at")),
            updated_at=_dt(item.get("last_time_up")),
            photo_urls=[photo] if photo else [],
            extra={
                key: item.get(key)
                for key in ("resale", "last_time_up")
                if item.get(key) is not None
            },
        )

    def close(self) -> None:
        self._client.close()

    def resolve_url(self, url: str) -> str | None:
        match = URL_RE.search(url)
        return match.group(1) if match else None


def is_minsk(item: dict[str, Any]) -> bool:
    """«Минск, …», «Беларусь, Минск, …», «Minsk, …» — да; пригороды и пустой адрес — нет."""
    address = (item.get("location") or {}).get("address") or ""
    parts = [p.strip().lower() for p in address.split(",")[:2]]
    return any(p in MINSK for p in parts)


def original_photo_url(url: str | None) -> str | None:
    """Исходный адрес фото из ссылки imgproxy.onliner.by (он закодирован в base64 в пути)."""
    if not url:
        return None
    if "imgproxy." not in url:
        return url
    segments = urlsplit(url).path.split("/")[2:]  # первый сегмент — подпись imgproxy
    encoded = "".join(s for s in segments if s and ":" not in s)
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return url
    return decoded if decoded.startswith("http") else url


def mirror_of(item: dict[str, Any]) -> str | None:
    """Площадка, копию объявления которой показывает Onliner (по хосту фото), или None."""
    photo = original_photo_url(item.get("photo"))
    host = (urlsplit(photo).hostname or "") if photo else ""
    for domain, source in MIRROR_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return source
    return None


def to_raw(item: dict[str, Any]) -> RawListing:
    """Псевдоним продавца и вырезание персональных данных до того, как item покинет адаптер."""
    payload = dict(item)
    payload["_seller_key"] = seller_key("onliner", item.get("author_id"))
    payload.pop("author_id", None)
    return RawListing(source="onliner", external_id=str(item["id"]), payload=scrub(payload))


def _decimal(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None
    return amount if amount > 0 else None


def _dt(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
