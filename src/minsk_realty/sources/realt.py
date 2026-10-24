"""Адаптер Realt.by: продажа квартир в Минске из JSON `__NEXT_DATA__` страницы выдачи.

Структура объекта подтверждена по рабочему open-source коду и его фикстуре (аренда),
см. docs/recon/sources.md. Страница продажи realt.by/sale/flats/ отвечает 200 из-за рубежа,
но её JSON живьём ещё не сохранён — первую фикстуру снять `scripts/capture_fixture.py realt`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from minsk_realty.models import ListingSnapshot, RawListing
from minsk_realty.privacy import scrub, seller_key
from minsk_realty.sources.base import PoliteClient

LIST_URL = "https://realt.by/sale/flats/"
OBJECT_URL = "https://realt.by/sale-flats/object/{code}/"
URL_RE = re.compile(r"realt\.by/(?:[a-z-]+-region/)?sale-flats/object/(\d+)")

# Коды валют ISO 4217, как их отдаёт realt.by.
CURRENCY_BY_CODE = {933: "BYN", 840: "USD", 978: "EUR", 643: "RUB"}
USD_CODE = "840"

# Подписи кодов `repairState`: словарь самого Realt из __NEXT_DATA__ страницы
# https://realt.by/sale/flats/ (filters, repairState), снят 24.10.2026, см.
# tests/fixtures/realt/repair_state_options.2026-10-24.json. Не выдумывать, а сверять с ним.
REALT_REPAIR_LABELS: dict[int, str] = {
    0: "евроремонт",
    1: "отличный",
    2: "хороший",
    3: "нормальный",
    4: "удовлетворительный",
    5: "плохое состояние",
    6: "аварийное состояние",
    7: "без отделки",
    8: "строительная отделка",
}


class RealtAdapter:
    source = "realt"

    def __init__(self, client: PoliteClient | None = None, *, town: str = "Минск") -> None:
        self._client = client or PoliteClient()
        self._town = town
        # True, если последний search() дошёл до конца выдачи, а не упёрся в max_pages.
        self.last_search_complete = False

    def search(self, *, max_pages: int = 500) -> Iterator[RawListing]:
        self.last_search_complete = False
        seen = 0
        for page in range(1, max_pages + 1):
            if page > 1:
                self._client.pause()
            html = self._client.get(LIST_URL, params={"page": page}).text
            page_props = extract_page_props(html)
            objects = page_props.get("objects") or []
            for obj in objects:
                if obj.get("townName") == self._town:
                    yield to_raw(obj)
            seen += len(objects)
            total = (page_props.get("pagination") or {}).get("totalCount") or 0
            if not objects or seen >= total:
                self.last_search_complete = True
                return

    def parse(self, raw: RawListing, *, seen_at: datetime | None = None) -> ListingSnapshot:
        obj = raw.payload
        currency = CURRENCY_BY_CODE.get(obj.get("priceCurrency"))
        rates = obj.get("priceRates") or {}
        location = obj.get("location")
        lon, lat = (
            (location[0], location[1])
            if isinstance(location, list) and len(location) == 2
            else (None, None)
        )
        agency = obj.get("agencyName") or obj.get("companyName")
        house = "/".join(str(p) for p in (obj.get("houseNumber"), obj.get("buildingNumber")) if p)

        return ListingSnapshot(
            source="realt",
            external_id=raw.external_id,
            url=OBJECT_URL.format(code=raw.external_id),
            seen_at=seen_at or datetime.now(UTC),
            price_amount=_decimal(obj.get("price")),
            price_currency=currency,
            price_usd=_decimal(rates.get(USD_CODE)),
            seller_type="agency" if agency or obj.get("agencyUuid") else "owner",
            agency_name=agency,
            seller_key=obj.get("_seller_key"),
            rooms=obj.get("rooms"),
            area_total=obj.get("areaTotal"),
            floor=obj.get("storey"),
            floors_total=obj.get("storeys"),
            street=obj.get("streetName"),
            house=house or None,
            address_raw=obj.get("address"),
            town=obj.get("townName"),
            lat=lat,
            lon=lon,
            posted_at=_dt(obj.get("createdAt")),
            updated_at=_dt(obj.get("updatedAt")),
            photo_urls=[
                u for u in obj.get("images") or [] if isinstance(u, str) and u.startswith("http")
            ],
            extra={
                key: obj.get(key)
                for key in (
                    "uuid",
                    "priceChangeDate",
                    "priceChangeDirection",
                    "buildingYear",
                    "metroStationName",
                    "repairState",
                )
                if obj.get(key) is not None
            },
        )

    def close(self) -> None:
        self._client.close()

    def resolve_url(self, url: str) -> str | None:
        match = URL_RE.search(url)
        return match.group(1) if match else None


def extract_page_props(html: str) -> dict[str, Any]:
    node = LexborHTMLParser(html).css_first("script#__NEXT_DATA__")
    if node is None:
        raise ValueError("realt: на странице нет __NEXT_DATA__ — разметка площадки изменилась")
    return json.loads(node.text())["props"]["pageProps"]


def to_raw(obj: dict[str, Any]) -> RawListing:
    payload = dict(obj)
    payload["_seller_key"] = seller_key("realt", obj.get("userUuid"))
    return RawListing(source="realt", external_id=str(obj["code"]), payload=scrub(payload))


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
