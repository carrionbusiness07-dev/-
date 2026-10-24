"""Адаптер Kufar: продажа квартир в Минске через поисковый JSON-API.

Поля сверены с живой выдачей 23.10.2026 (tests/fixtures/kufar/search_sell_minsk.2026-10-23.json).
Kufar отвечает «Доступ ограничен» на запросы не из Беларуси: сборщик работает из Минска.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from minsk_realty.models import ListingSnapshot, RawListing
from minsk_realty.privacy import scrub, seller_key
from minsk_realty.sources.base import PoliteClient

SEARCH_URL = "https://api.kufar.by/search-api/v2/search/rendered-paginated"
SEARCH_PARAMS = {
    "cat": "1010",  # квартиры
    "typ": "sell",  # продажа
    "cur": "USD",
    "gtsy": "country-belarus~province-minsk~locality-minsk",
    "lang": "ru",
    "size": "200",
    "sort": "lst.d",
}
ITEM_URL = "https://re.kufar.by/vi/{ad_id}"

# Ссылки вида re.kufar.by/vi/123456789, kufar.by/item/123456789 (+ возможный путь перед id).
URL_RE = re.compile(r"kufar\.by/(?:vi|item)/(?:[^?#\s]*/)?(\d{6,})")

# Белый список параметров объявления, которые уходят в snapshots.extra. Новые ключи площадки
# в БД не попадают, пока их явно не добавят сюда (защита от новых полей с персональными данными).
EXTRA_PARAMS = frozenset(
    {
        "type",
        "category",
        "rooms",
        "size",
        "region",
        "area",
        "coordinates",
        "metro",
        "re_district",
        "year_built",
        "house_type",
        "flat_new_building",
        "new_buildings_apartment_complex",
        "flat_repair",
    }
)

# В живой выдаче `currency` бывает USD, BYR и EUR. Цена в валюте объявления лежит в
# `calculator` (в центах); price_usd/price_byn — пересчёт площадки по курсу дня.
CURRENCY_MAP = {"USD": "USD", "BYR": "BYN", "BYN": "BYN", "EUR": "EUR"}
CALCULATOR_CURRENCY = {"USD": "USD", "BYN": "BYN", "EUR": "EUR"}


class KufarAdapter:
    source = "kufar"

    def __init__(self, client: PoliteClient | None = None) -> None:
        self._client = client or PoliteClient()
        # True, если последний search() дошёл до конца выдачи, а не упёрся в max_pages.
        self.last_search_complete = False

    def search(self, *, max_pages: int = 500) -> Iterator[RawListing]:
        self.last_search_complete = False
        params = dict(SEARCH_PARAMS)
        for page in range(max_pages):
            if page:
                self._client.pause()
            data = self._client.get(SEARCH_URL, params=params).json()
            for ad in data.get("ads") or []:
                yield to_raw(ad)
            token = next_cursor(data)
            if not token:
                self.last_search_complete = True
                return
            params["cursor"] = token

    def parse(self, raw: RawListing, *, seen_at: datetime | None = None) -> ListingSnapshot:
        ad = raw.payload
        params = {p["p"]: p for p in ad.get("ad_parameters") or [] if p.get("p")}
        account = {p["p"]: p for p in ad.get("account_parameters") or [] if p.get("p")}

        currency, amount = original_price(ad)
        price_usd = _from_cents(ad.get("price_usd"))

        coords = _value(params, "coordinates")
        lon, lat = (
            (coords[0], coords[1])
            if isinstance(coords, list) and len(coords) == 2
            else (None, None)
        )

        return ListingSnapshot(
            source="kufar",
            external_id=raw.external_id,
            url=ad.get("ad_link") or ITEM_URL.format(ad_id=raw.external_id),
            seen_at=seen_at or datetime.now(UTC),
            price_amount=amount,
            price_currency=currency,
            price_usd=price_usd,
            seller_type="agency" if ad.get("company_ad") else "owner",
            seller_key=ad.get("_seller_key"),
            rooms=_int(_value(params, "rooms")),
            area_total=_float(_value(params, "size")),
            floor=_first_int(_value(params, "floor")),
            floors_total=_first_int(_value(params, "re_number_floors")),
            address_raw=_text(account, "address"),
            district=_label(params, "area"),
            town=_label(params, "region"),
            lat=_float(lat),
            lon=_float(lon),
            posted_at=_dt(ad.get("list_time")),
            photo_urls=[
                f"https://rms.kufar.by/v1/gallery/{img['path']}"
                for img in ad.get("images") or []
                if img.get("path")
            ],
            extra={
                **{key: p.get("v") for key, p in params.items() if key in EXTRA_PARAMS},
                **({"microdistrict": md} if (md := _label(params, "re_district")) else {}),
                **({"repair": rp} if (rp := _label(params, "flat_repair")) else {}),
            },
        )

    def close(self) -> None:
        self._client.close()

    def resolve_url(self, url: str) -> str | None:
        match = URL_RE.search(url)
        return match.group(1) if match else None


def original_price(ad: dict[str, Any]) -> tuple[str | None, Decimal | None]:
    """Цена в валюте, в которой её указал продавец.

    Изменение цены считается в исходной валюте (SKILL.md): для объявления в евро пересчёт
    площадки в доллары менялся бы каждый день вместе с курсом и давал ложные «снижения».
    """
    currency = CURRENCY_MAP.get(str(ad.get("currency") or "").upper())
    if currency is None:
        usd = _from_cents(ad.get("price_usd"))
        return ("USD", usd) if usd else (None, None)
    for row in ad.get("calculator") or []:
        if CALCULATOR_CURRENCY.get(str(row.get("currency")).upper()) == currency:
            amount = _from_cents(row.get("price"))
            if amount:
                return currency, amount
    fallback = {"USD": "price_usd", "BYN": "price_byn"}.get(currency)
    amount = _from_cents(ad.get(fallback)) if fallback else None
    return (currency, amount) if amount else (None, None)


def to_raw(ad: dict[str, Any]) -> RawListing:
    """Посчитать псевдоним продавца и вырезать персональные данные до того, как ad покинет адаптер."""
    ad_id = str(ad.get("ad_id") or ad.get("list_id"))
    payload = dict(ad)
    payload["_seller_key"] = seller_key("kufar", ad.get("account_id"))
    return RawListing(source="kufar", external_id=ad_id, payload=scrub(payload))


def next_cursor(data: dict[str, Any]) -> str | None:
    pages = (data.get("pagination") or {}).get("pages") or []
    return next(
        (p.get("token") for p in pages if p.get("label") == "next" and p.get("token")), None
    )


def _value(params: dict[str, dict], key: str) -> Any:
    return params.get(key, {}).get("v")


def _label(params: dict[str, dict], key: str) -> str | None:
    value = params.get(key, {}).get("vl")
    return value if isinstance(value, str) and value else None


def _text(params: dict[str, dict], key: str) -> str | None:
    """Текстовый параметр. Адрес Kufar лежит в `v` («Мира пр, 8, Минск»), а `vl` у него пустой."""
    for field in ("v", "vl"):
        value = params.get(key, {}).get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _from_cents(value: Any) -> Decimal | None:
    """Kufar отдаёт цены строкой в центах/копейках: "9250000" -> 92500. Ноль — «договорная»."""
    try:
        amount = Decimal(str(value)) / 100
    except (ArithmeticError, ValueError, TypeError):
        return None
    return amount if amount > 0 else None


def _first_int(value: Any) -> int | None:
    """Kufar отдаёт этаж и этажность списком: {"p": "floor", "v": [8]}."""
    if isinstance(value, list):
        value = value[0] if value else None
    return _int(value)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
