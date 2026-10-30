"""Адаптер Domovita: продажа квартир в Минске из данных страницы выдачи (Next.js).

Структура сверена с живыми ответами 23.10.2026 (tests/fixtures/domovita/, см.
docs/recon/sources.md):
- первая страница — HTML с `__NEXT_DATA__` → props.pageProps.listingCardsFromSSR
  {totalCount, items}, по 20 объявлений; из неё же берётся buildId;
- следующие — `/_next/data/<buildId>/minsk/flats/sale.json?page=N`: те же данные без
  разметки (≈250 КБ вместо 1,3 МБ). После выкладки сайта buildId меняется — тогда
  страница берётся HTML-ом и buildId обновляется.

В карточке объявления лежат телефоны и имя продавца (`contacts`), а ключи `name` есть у
города и района. Поэтому из карточки берутся только нужные поля по белому списку
(`compact`), а не вся карточка через scrub.

Domovita сама отдаёт историю цены объявления (`price_history`, в долларах), поэтому
частый опрос ей не нужен: площадка обходится не чаще раза в `min_interval`.

id объявления — числовой `id` карточки. В ссылке его нет, только адрес страницы
(«1-komnatnaa-kvartira-ul-bratskaa-42-114»), который может измениться при правке объявления.
Поэтому `resolve_url` возвращает адрес страницы, а объявление по нему ищет
`db.repository.find_listing` (по концу сохранённой ссылки).

Название агентства не сохраняем: карточку агентства живьём ещё не видели, и в `agency`
может оказаться риелтор-ИП, то есть имя человека. Хватает признака «агентство».
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from selectolax.lexbor import LexborHTMLParser

from minsk_realty.models import ListingSnapshot, RawListing
from minsk_realty.privacy import scrub, seller_key
from minsk_realty.sources.base import PoliteClient, description_facts

log = logging.getLogger(__name__)

LIST_URL = "https://domovita.by/minsk/flats/sale"
DATA_URL = "https://domovita.by/_next/data/{build}/minsk/flats/sale.json"
DATA_PARAMS = {"city": "minsk", "real_estate_type": "flats"}
URL_RE = re.compile(r"domovita\.by/[a-z-]+/flats/sale/([a-z0-9-]+)")
# Беларусь живёт по UTC+3 без перехода на летнее время; даты площадки — местные.
MINSK_TZ = timezone(timedelta(hours=3))
# Цена в валюте продавца — та, в которой сумма круглая: остальные — пересчёт по курсу.
# Ничья бывает у долларовых цен: BYN = USD × курс с 4 знаками, и «220 000 $» даёт круглые
# «649 946 BYN». Обратный пересчёт BYN → USD круглым почти не бывает, поэтому при ничьей
# побеждает USD, затем EUR.
CURRENCY_ORDER = ("USD", "EUR", "BYN")
# На каждой странице 2–3 места занимают платные объявления, которые повторяются от страницы
# к странице, а пока обходим ~180 страниц, сверху появляются новые и поднятые. Поэтому
# настоящих страниц больше, чем totalCount / 20 (живой проход 23.10: 171 страница при
# расчётных 154). Идём дальше расчётного, пока страницы дают новые объявления, но не больше
# чем в 1,5 раза. Страница за концом выдачи отвечает 404 — это конец, а не ошибка.
# Проход полный, только если увидели почти всю выдачу: иначе пропущенные объявления
# погасли бы как снятые.
MIN_COVERAGE = 0.95
EXTRA_PAGES_SHARE = 0.5
# «9/а» → «9а»: литера через дробь (корпус через дробь — цифра, «10/1» не трогаем).
HOUSE_LETTER_RE = re.compile(r"^(\d+)\s*/\s*([а-яa-z])$", re.IGNORECASE)


class DomovitaAdapter:
    source = "domovita"
    min_interval = timedelta(hours=6)

    def __init__(self, client: PoliteClient | None = None, *, town: str = "Минск") -> None:
        self._client = client or PoliteClient()
        self._town = town
        self._build: str | None = None
        self._html_only = False
        # True, если последний search() дошёл до конца выдачи и увидел почти всю её.
        self.last_search_complete = False

    def search(self, *, max_pages: int = 500) -> Iterator[RawListing]:
        self.last_search_complete = False
        self._html_only = False
        seen: set[str] = set()
        cards = self._html_page(1)
        total = cards.get("totalCount") or 0
        page_size = len(cards.get("items") or []) or 1
        pages = math.ceil(total / page_size)
        reached_end = False
        last_page = pages + max(20, int(pages * EXTRA_PAGES_SHARE))
        for page in range(1, min(max_pages, last_page) + 1):
            if page > 1:
                self._client.pause()
                cards = self._data_page(page)
            items = cards.get("items") or []
            fresh = 0
            for item in items:
                raw = to_raw(item)
                if raw.external_id in seen:
                    continue  # повтор на стыке страниц
                seen.add(raw.external_id)
                fresh += 1
                town = raw.payload.get("town")
                if not town or town == self._town:
                    yield raw
            if not items or (page >= pages and not fresh):
                reached_end = page >= pages
                break
        else:
            reached_end = page >= pages
        coverage = len(seen) / total if total else 0.0
        self.last_search_complete = reached_end and coverage >= MIN_COVERAGE
        log.info(
            "domovita: уникальных %d из %d (%.0f%%), страниц %d из %d, полный проход: %s",
            len(seen),
            total,
            coverage * 100,
            page,
            pages,
            self.last_search_complete,
        )

    def _html_page(self, page: int) -> dict[str, Any]:
        params = {"page": page} if page > 1 else None
        try:
            html = self._client.get(LIST_URL, params=params).text
        except httpx.HTTPStatusError as exc:
            if page > 1 and exc.response.status_code == 404:
                return {"items": []}  # страницы с таким номером нет: выдача кончилась
            raise
        data = extract_next_data(html)
        self._build = data.get("buildId") or self._build
        return listing_cards(data["props"]["pageProps"])

    def _data_page(self, page: int) -> dict[str, Any]:
        if self._build and not self._html_only:
            build = self._build
            url = DATA_URL.format(build=build)
            try:
                response = self._client.get(url, params={**DATA_PARAMS, "page": page})
                return listing_cards(response.json()["pageProps"])
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
            # Сайт выложили заново, старый buildId больше не отвечает: берём страницу HTML-ом.
            self._client.pause()
            cards = self._html_page(page)
            if self._build == build and cards.get("items"):
                # buildId тот же, а лёгкий адрес не отвечает — до конца прохода только HTML,
                # без лишнего запроса-неудачи на каждую страницу.
                log.warning("domovita: /_next/data не отвечает, обход HTML-страницами")
                self._html_only = True
            return cards
        return self._html_page(page)

    def parse(self, raw: RawListing, *, seen_at: datetime | None = None) -> ListingSnapshot:
        item = raw.payload
        currency, amount = original_price(item.get("price") or {})
        usd = _decimal((item.get("price") or {}).get("USD"))
        owner = str(item.get("owner_type") or "").lower()
        if item.get("agency") or "агент" in owner:
            seller_type = "agency"
        elif "собствен" in owner:
            seller_type = "owner"
        else:
            seller_type = "unknown"
        street, house = item.get("street"), item.get("house")
        return ListingSnapshot(
            source="domovita",
            external_id=raw.external_id,
            url=item.get("url") or f"{LIST_URL}/{raw.external_id}",
            seen_at=seen_at or datetime.now(UTC),
            price_amount=amount,
            price_currency=currency,
            price_usd=usd.quantize(Decimal("0.01")) if usd else None,
            seller_type=seller_type,
            seller_key=item.get("_seller_key"),
            # Студию Domovita записывает как 0 комнат, остальные площадки — как 1-комнатную.
            rooms=1 if item.get("rooms") == 0 else item.get("rooms"),
            area_total=item.get("area_total"),
            floor=item.get("storey"),
            floors_total=item.get("storeys"),
            street=street,
            house=HOUSE_LETTER_RE.sub(r"\1\2", house) if house else None,
            address_raw=", ".join(p for p in (item.get("town"), street, house) if p) or None,
            district=item.get("district"),
            town=item.get("town"),
            lat=item.get("lat"),
            lon=item.get("lon"),
            posted_at=_local_dt(item.get("date_reception")),
            updated_at=_local_dt(item.get("date_revision")),
            photo_urls=list(item.get("photos") or []),
            extra={
                **({"studio": True} if item.get("rooms") == 0 else {}),
                **{
                    key: item[key]
                    for key in (
                        "domovita_id",
                        "microdistrict",
                        "metro",
                        "residential_complex",
                        "building_year",
                        "is_new",
                        "price_history",
                    )
                    if item.get(key) is not None
                },
                **({"facts": facts} if (facts := item.get("_facts")) else {}),
            },
        )

    def close(self) -> None:
        self._client.close()

    def resolve_url(self, url: str) -> str | None:
        match = URL_RE.search(url)
        return match.group(1) if match else None


def extract_next_data(html: str) -> dict[str, Any]:
    node = LexborHTMLParser(html).css_first("script#__NEXT_DATA__")
    if node is None:
        raise ValueError("domovita: на странице нет __NEXT_DATA__ — разметка площадки изменилась")
    return json.loads(node.text())


def listing_cards(page_props: dict[str, Any]) -> dict[str, Any]:
    cards = page_props.get("listingCardsFromSSR")
    if not isinstance(cards, dict):
        raise TypeError("domovita: в данных страницы нет listingCardsFromSSR")
    return cards


def compact(item: dict[str, Any]) -> dict[str, Any]:
    """Только нужные поля карточки: без контактов, id пользователя и свободного текста."""
    address = item.get("address") or {}
    district = address.get("district") or {}
    coords = address.get("coords") or {}
    agency = item.get("agency") or {}
    complex_ = item.get("residential_complex") or {}
    history = []
    for row in item.get("price_history") or []:
        usd, when = (row.get("price") or {}).get("USD"), row.get("date")
        if usd is not None and isinstance(when, int | float):
            history.append([datetime.fromtimestamp(when, MINSK_TZ).date().isoformat(), usd])
    return {
        "domovita_id": item.get("id"),
        "url": item.get("url"),
        "town": (address.get("town") or {}).get("name"),
        "district": district.get("name"),
        "microdistrict": (district.get("subDistrict") or {}).get("name"),
        "street": address.get("street_name"),
        "house": address.get("house_number"),
        "lat": coords.get("position_x"),
        "lon": coords.get("position_y"),
        "metro": (address.get("metro") or {}).get("name"),
        "rooms": item.get("rooms"),
        "area_total": (item.get("area") or {}).get("total"),
        "storey": item.get("storey"),
        "storeys": item.get("storeys"),
        "price": {k: v for k, v in (item.get("price") or {}).items() if k in CURRENCY_ORDER},
        "price_history": history or None,
        "owner_type": item.get("owner_type"),
        "agency": bool(agency),
        "residential_complex": complex_.get("name"),
        "building_year": item.get("building_year"),
        "is_new": item.get("is_new"),
        "date_reception": item.get("date_reception"),
        "date_revision": item.get("date_revision"),
        "photos": [p["url"] for p in item.get("photos") or [] if p.get("url")],
        # Текст объявления (`ads_text`) дальше не идёт: остаются только коды фактов.
        "_facts": description_facts(item.get("ads_text")),
    }


def to_raw(item: dict[str, Any]) -> RawListing:
    payload = compact(item)
    payload["_seller_key"] = seller_key("domovita", item.get("user_id"))
    # Числовой id карточки, а не адрес страницы: адрес меняется при правке объявления.
    external_id = str(item["id"])
    return RawListing(source="domovita", external_id=external_id, payload=scrub(payload))


def original_price(price: dict[str, Any]) -> tuple[str | None, Decimal | None]:
    """Цена в валюте продавца. Domovita отдаёт сумму сразу во всех валютах; пересчитанные
    суммы с копейками, а та, что указал продавец, — круглая. Нет круглой — валюта неизвестна
    (тогда изменение цены не считается: иначе курс давал бы ложные «снижения»)."""
    for currency in CURRENCY_ORDER:
        amount = _decimal(price.get(currency))
        if amount and amount == amount.to_integral_value():
            return currency, amount
    return None, None


def _decimal(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return amount if amount > 0 else None


def _local_dt(value: Any) -> datetime | None:
    """«2026-07-30 04:24:53» или «2026-10-06» по минскому времени."""
    try:
        parsed = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=MINSK_TZ)
