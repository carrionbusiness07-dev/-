"""Адаптер Domovita для долгосрочной аренды квартир в Минске.

Аренда на Domovita, в отличие от продажи, — старая HTML-вёрстка без `__NEXT_DATA__`
(разведка 31.10.2026, tests/fixtures/domovita/flats_rent_minsk.2026-10-31.html): по 20
карточек `.found_item[data-key]` на странице `?page=N`, в каждой — JSON-LD с адресом,
координатами и площадью и разметка с ценой, этажом и наклейкой «Без посредников».

В JSON-LD карточки лежат телефон и текст продавца: из него берутся только поля по белому
списку, остальное не покидает адаптер.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from selectolax.lexbor import LexborHTMLParser, LexborNode

from minsk_realty.models import ListingSnapshot, RawListing
from minsk_realty.privacy import scrub
from minsk_realty.sources.base import PoliteClient, positive_float
from minsk_realty.sources.domovita import MINSK_TZ

log = logging.getLogger(__name__)

LIST_URL = "https://domovita.by/minsk/flats/rent"
URL_RE = re.compile(r"domovita\.by/[a-z-]+/flats/rent/([a-z0-9-]+)")
PRICE_RE = re.compile(r"([\d\s\xa0]+)\s*(р\.|\$|€|₽)/мес")
CURRENCY_SIGNS = {"р.": "BYN", "$": "USD", "€": "EUR", "₽": "RUB"}
TOTAL_RE = re.compile(r"Найдено\s+([\d\s\xa0]+)")
AREA_RE = re.compile(r"^([\d.,]+)\s*(?:/.*)?м2$")  # «46 / 28 / 7м2»: общая — первая
FLOOR_RE = re.compile(r"(\d+)\s*этаж\s*из\s*(\d+)")
YEAR_RE = re.compile(r"^(\d{4})\s*г\.?$")
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
# Платные карточки повторяются на разных страницах: идём, пока страницы дают новые.
MIN_COVERAGE = 0.95


class DomovitaRentAdapter:
    source = "domovita"

    def __init__(self, client: PoliteClient | None = None) -> None:
        self._client = client or PoliteClient()
        self.last_search_complete = False

    def search(self, *, max_pages: int = 500) -> Iterator[RawListing]:
        self.last_search_complete = False
        seen: set[str] = set()
        total = 0
        for page in range(1, max_pages + 1):
            if page > 1:
                self._client.pause()
            try:
                html = self._client.get(LIST_URL, params={"page": page} if page > 1 else None).text
            except httpx.HTTPStatusError as exc:
                if page > 1 and exc.response.status_code == 404:
                    break  # страницы с таким номером нет: выдача кончилась
                raise
            tree = LexborHTMLParser(html)
            if page == 1:
                found = TOTAL_RE.search(tree.text())
                total = int(re.sub(r"\D", "", found.group(1))) if found else 0
            fresh = 0
            for card in tree.css(".found_item[data-key]"):
                raw = to_raw(card)
                if raw is None or raw.external_id in seen:
                    continue
                seen.add(raw.external_id)
                fresh += 1
                yield raw
            if not fresh:
                break
        coverage = len(seen) / total if total else 0.0
        self.last_search_complete = coverage >= MIN_COVERAGE
        log.info(
            "domovita аренда: уникальных %d из %d (%.0f%%), полный проход: %s",
            len(seen),
            total,
            coverage * 100,
            self.last_search_complete,
        )

    def parse(self, raw: RawListing, *, seen_at: datetime | None = None) -> ListingSnapshot:
        item = raw.payload
        currency, amount = original_price(item.get("prices") or {})
        usd = _decimal((item.get("prices") or {}).get("USD"))
        return ListingSnapshot(
            source="domovita",
            external_id=raw.external_id,
            url=item.get("url") or LIST_URL,
            seen_at=seen_at or datetime.now(UTC),
            price_amount=amount,
            price_currency=currency,
            price_usd=usd,
            # Наклейка «Без посредников» — собственник; без неё Domovita продавца не называет.
            seller_type="owner" if item.get("owner") else "unknown",
            rooms=_int(item.get("rooms")),
            area_total=positive_float(item.get("area_total")),
            floor=item.get("floor"),
            floors_total=item.get("floors_total"),
            street=item.get("street"),
            address_raw=", ".join(p for p in (item.get("town"), item.get("street")) if p) or None,
            district=item.get("district"),
            town=item.get("town"),
            lat=positive_float(item.get("lat")),
            lon=positive_float(item.get("lon")),
            posted_at=_date(item.get("date")),
            photo_urls=list(item.get("photos") or []),
            extra={
                key: item[key]
                for key in ("microdistrict", "metro", "building_year")
                if item.get(key) is not None
            },
        )

    def close(self) -> None:
        self._client.close()

    def resolve_url(self, url: str) -> str | None:
        match = URL_RE.search(url)
        return match.group(1) if match else None


def to_raw(card: LexborNode) -> RawListing | None:
    """Только нужные поля карточки: без телефона и текста продавца из JSON-LD."""
    key = card.attributes.get("data-key")
    ld_node = card.css_first('script[type="application/ld+json"]')
    if not key or ld_node is None:
        return None
    try:
        ld = json.loads(ld_node.text())
    except ValueError:
        ld = {}
    geo, address = ld.get("geo") or {}, ld.get("address") or {}
    prices: dict[str, int] = {}
    price_node = card.css_first(".price")
    for number, sign in PRICE_RE.findall(price_node.text() if price_node else ""):
        prices.setdefault(CURRENCY_SIGNS[sign], int(re.sub(r"\D", "", number)))
    spans = [n.text(strip=True) for n in card.css(".autopaddings span")]
    area = next((m.group(1) for s in spans if (m := AREA_RE.match(s))), None)
    floor = next((m for s in spans if (m := FLOOR_RE.search(s))), None)
    year = next((int(m.group(1)) for s in spans if (m := YEAR_RE.match(s))), None)
    place = card.css_first(".gr.mb-5")
    district, _, micro = (place.text(strip=True).rstrip(";") if place else "").partition(" / ")
    metro = card.css_first(".udg_mark + .gr")
    date = card.css_first(".date")
    payload = {
        "url": ld.get("url"),
        "rooms": ld.get("numberOfRooms"),
        "area_total": area.replace(",", ".") if area else ld.get("floorSize"),
        "floor": int(floor.group(1)) if floor else None,
        "floors_total": int(floor.group(2)) if floor else None,
        "town": address.get("addressLocality"),
        "street": address.get("streetAddress"),
        "district": district.removesuffix(" район") or None,
        "microdistrict": micro or None,
        "metro": metro.text(strip=True) if metro else None,
        "lat": geo.get("latitude"),
        "lon": geo.get("longitude"),
        "building_year": year,
        "prices": prices,
        "owner": card.css_first(".sticker--without-intermediaries") is not None,
        "date": date.text(strip=True) if date else None,
        "photos": [
            src
            for img in card.css("img.img-preview")[:6]
            if (src := img.attributes.get("src") or "").startswith("http")
        ],
    }
    return RawListing(source="domovita", external_id=key, payload=scrub(payload))


def original_price(prices: dict[str, Any]) -> tuple[str | None, Decimal | None]:
    """Цена в валюте продавца. Domovita показывает все валюты целыми числами, поэтому
    круглость не помогает; валюта продавца — та, где сумма кратна 5 (USD при ничьей).
    shortcut: эвристика, при первых ложных «изменениях цены» сверить с карточкой объявления.
    """
    for currency in ("USD", "BYN", "EUR"):
        amount = _decimal(prices.get(currency))
        if amount and amount % 5 == 0:
            return currency, amount
    return None, None


def _decimal(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None
    return amount if amount > 0 else None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> datetime | None:
    match = DATE_RE.search(str(value or ""))
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    return datetime(year, month, day, tzinfo=MINSK_TZ)
