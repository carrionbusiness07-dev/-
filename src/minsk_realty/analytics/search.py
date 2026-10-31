"""Поиск квартир для бота: индекс того, что сейчас продаётся, фильтры, порядок и подсказки.

    index = build_index(data, baseline, now)      # на всём рынке около минуты: раз на срез
    result = search(index, SearchQuery(price_max=90_000, rooms=frozenset({2})), "best")
    text = summary_line(result, "best")

Индекс — чистая функция среза рынка; `search` только фильтрует и сортирует список в памяти.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from minsk_realty.analytics.comparables import find_comparison
from minsk_realty.analytics.config import DEFAULT_ANALYTICS, AnalyticsConfig
from minsk_realty.analytics.data import FlatDetails, MarketData
from minsk_realty.analytics.exposure import compute_exposure
from minsk_realty.analytics.fmt import money, offer_range, plural, rooms_label, street_label
from minsk_realty.analytics.geo import locate
from minsk_realty.analytics.medians import MarketBaseline
from minsk_realty.analytics.report import housing_label, is_new_building

Sort = Literal["best", "cheap", "fresh"]
BUDGET_STEPS = (60_000, 80_000, 100_000, 130_000)
# «Сначала выгодные»: порядок групп вывода; None — сравнения нет.
GROUP_ORDER: tuple[str | None, ...] = ("below", "market", "suspicious", "above", None)
COUNT_KEYS = ("below", "market", "suspicious", "above", "none")
SORT_WORDS = {"best": "выгодные", "cheap": "дешёвые", "fresh": "свежие"}


@dataclass(frozen=True)
class SearchItem:
    object_id: int
    rooms: int | None
    area: float | None
    floor: int | None
    floors_total: int | None
    district: str | None
    new_building: bool | None  # None — неизвестно (ни новостройка, ни вторичка)
    price_amount: Decimal
    price_currency: str
    price_usd: float | None
    usd_per_m2: float | None
    group: str | None  # "below" | "market" | "suspicious" | "above" | None
    diff_pct: float | None
    metro: str | None  # только при точных координатах
    metro_min: int | None
    days: int | None
    drops: int
    sources: tuple[str, ...]  # площадки, где объект сейчас продаётся
    owner: bool | None  # продавец объявления с ценой: собственник / агентство / неизвестно
    url: str  # объявление с ценой объекта (самое дешёвое)
    url_source: str
    listing_id: int
    checked_at: datetime  # самое позднее last_seen среди объявлений объекта
    first_seen: datetime  # начало экспозиции: первое появление на рынке
    street: str | None
    year: int | None
    housing: str | None
    details: FlatDetails | None


def build_index(
    data: MarketData,
    baseline: MarketBaseline,
    at: datetime,
    cfg: AnalyticsConfig = DEFAULT_ANALYTICS,
) -> tuple[SearchItem, ...]:
    """Объекты в продаже на `at` (тот же набор, что `views._object_rows`), у которых есть цена."""
    items = []
    for oid, obj in data.objects.items():
        listings = data.object_listings(oid)
        events = data.object_events(oid)
        exposure = compute_exposure(listings, events, at, data.source_starts, cfg=cfg)
        if exposure is None or exposure.state == "removed":
            continue
        price = data.object_price(oid, at)
        if price is None:
            continue
        row = data.listings[price.listing_id]
        snap = data.last_snapshot(row.id, at, priced=True)
        rooms = price.rooms or data.object_rooms(oid)
        district = data.object_district(oid)
        year, new_flag = data.building_years.get(oid), data.new_buildings.get(oid)
        housing = housing_label(year, new_flag, at.year)
        new_building = is_new_building(year, new_flag, at.year)
        point = data.object_point(oid, cfg.approx_coord_sources)
        comparison = find_comparison(
            baseline.points,
            object_id=oid,
            point=point,
            rooms=rooms,
            year=year,
            finish=data.finishings.get(oid),
            usd_per_m2=price.usd_per_m2,
            district_cell=baseline.price_for(district, rooms),
            now_year=at.year,
            cfg=cfg,
            new_building=new_building,
        )
        # Метро и минуты — только по точной точке, как в карточке (спека, раздел 6).
        location = locate(point[0], point[1], approx=False) if point and not point[2] else None
        station = location.station if location else None
        items.append(
            SearchItem(
                object_id=oid,
                rooms=rooms,
                area=price.area,
                floor=snap.floor if snap.floor is not None else obj.floor,
                floors_total=snap.floors_total or obj.floors_total,
                district=district,
                new_building=new_building if housing else None,
                price_amount=price.amount,
                price_currency=price.currency,
                price_usd=float(price.usd),
                usd_per_m2=price.usd_per_m2,
                group=comparison.group if comparison else None,
                diff_pct=comparison.diff_pct if comparison else None,
                metro=station.name if station else None,
                metro_min=location.walk_min if station else None,
                days=exposure.days,
                drops=sum(1 for e in events if e.type == "price_drop"),
                sources=tuple(sorted({r.source for r in listings if r.active_at(at)})),
                owner={"owner": True, "agency": False}.get(row.seller_type),
                url=row.url,
                url_source=row.source,
                listing_id=row.id,
                checked_at=max(r.last_seen for r in listings),
                first_seen=exposure.start,
                street=street_label(obj.street_norm),
                year=year,
                housing=housing,
                details=data.details.get(oid),
            )
        )
    return tuple(items)


# --- Запрос ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchQuery:
    price_min: int | None = None
    price_max: int | None = None
    rooms: frozenset[int] = frozenset()  # 4 — «4+»; пусто — любые
    districts: frozenset[str] = frozenset()  # названия MINSK_DISTRICTS; пусто — любой
    area_min: int | None = None
    housing: Literal["new", "old"] | None = None
    not_first: bool = False
    not_last: bool = False

    def to_json(self) -> dict[str, Any]:
        """JSON-совместимый dict (для таблицы «последний поиск»)."""
        return {
            "price_min": self.price_min,
            "price_max": self.price_max,
            "rooms": sorted(self.rooms),
            "districts": sorted(self.districts),
            "area_min": self.area_min,
            "housing": self.housing,
            "not_first": self.not_first,
            "not_last": self.not_last,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SearchQuery:
        return cls(
            price_min=data.get("price_min"),
            price_max=data.get("price_max"),
            rooms=frozenset(data.get("rooms") or ()),
            districts=frozenset(data.get("districts") or ()),
            area_min=data.get("area_min"),
            housing=data.get("housing"),
            not_first=bool(data.get("not_first")),
            not_last=bool(data.get("not_last")),
        )

    def label(self) -> str:
        """«До $90 000 · 2-комн. · Фрунзенский, Центральный · от 50 м²»."""
        parts = []
        if self.price_min is not None and self.price_max is not None:
            parts.append(offer_range(Decimal(self.price_min), Decimal(self.price_max), "USD"))
        elif self.price_max is not None:
            parts.append(f"До {money(self.price_max, 'USD')}")
        elif self.price_min is not None:
            parts.append(f"От {money(self.price_min, 'USD')}")
        if len(self.rooms) == 1:
            parts.append(rooms_label(next(iter(self.rooms))))
        elif self.rooms:
            parts.append(
                ", ".join("4+" if r >= 4 else str(r) for r in sorted(self.rooms)) + " комн."
            )
        if self.districts:
            parts.append(", ".join(sorted(self.districts)))
        if self.area_min is not None:
            parts.append(f"от {self.area_min} м²")
        if self.housing:
            parts.append("новостройка" if self.housing == "new" else "вторичка")
        if self.not_first:
            parts.append("не первый этаж")
        if self.not_last:
            parts.append("не последний этаж")
        return " · ".join(parts) or "Любые квартиры"


def _matches(item: SearchItem, q: SearchQuery) -> bool:
    if q.price_min is not None or q.price_max is not None:
        if item.price_usd is None:  # цену в USD не знаем — в бюджет не попадает никогда
            return False
        if q.price_min is not None and item.price_usd < q.price_min:
            return False
        if q.price_max is not None and item.price_usd > q.price_max:
            return False
    if q.rooms and (
        item.rooms is None or not (item.rooms in q.rooms or (4 in q.rooms and item.rooms >= 4))
    ):
        return False
    if q.districts and item.district not in q.districts:
        return False
    if q.area_min is not None and (item.area is None or item.area < q.area_min):
        return False
    # Неизвестно (None) не проходит ни «новостройку», ни «вторичку».
    if q.housing is not None and item.new_building is not (q.housing == "new"):
        return False
    if q.not_first and (item.floor is None or item.floor <= 1):
        return False
    return not (
        q.not_last
        and (item.floor is None or item.floors_total is None or item.floor >= item.floors_total)
    )


# --- Выдача ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchResult:
    items: tuple[SearchItem, ...]
    counts: dict[str, int] = field(default_factory=dict)  # below/market/suspicious/above/none


def _last_if_none(value: float | None) -> tuple[bool, float]:
    return value is None, value or 0.0


_SORT_KEYS = {
    # В группе — по отклонению от похожих; без сравнения — по цене за м².
    "best": lambda i: (
        GROUP_ORDER.index(i.group),
        *_last_if_none(i.diff_pct if i.group else i.usd_per_m2),
        i.object_id,
    ),
    "cheap": lambda i: (*_last_if_none(i.price_usd), i.object_id),
    "fresh": lambda i: (-i.first_seen.timestamp(), i.object_id),
}


def search(index: tuple[SearchItem, ...], query: SearchQuery, sort: Sort = "best") -> SearchResult:
    found = sorted((i for i in index if _matches(i, query)), key=_SORT_KEYS[sort])
    groups = Counter(i.group or "none" for i in found)
    return SearchResult(tuple(found), {k: groups[k] for k in COUNT_KEYS})


def _flats(n: int) -> str:
    return f"{n} {plural(n, 'квартира', 'квартиры', 'квартир')}"


def _relaxed(q: SearchQuery) -> list[tuple[str, SearchQuery]]:
    """Запрос, ослабленный по одному условию (спека, раздел 3, «Пустая выдача»)."""
    out = []
    step = next((s for s in BUDGET_STEPS if q.price_max is not None and s > q.price_max), None)
    if step is not None:
        out.append((f"Если поднять бюджет до {money(step, 'USD')}", replace(q, price_max=step)))
    if q.districts:
        out.append(("Если любой район", replace(q, districts=frozenset())))
    if q.rooms:
        out.append(("Если любые комнаты", replace(q, rooms=frozenset())))
    if q.area_min is not None:
        out.append(("Если площадь неважна", replace(q, area_min=None)))
    if q.not_first or q.not_last:
        out.append(("Если этаж неважен", replace(q, not_first=False, not_last=False)))
    if q.housing is not None:
        out.append(("Если неважно, новостройка или вторичка", replace(q, housing=None)))
    return out


def suggestions(
    index: tuple[SearchItem, ...], query: SearchQuery, limit: int = 2
) -> list[tuple[str, SearchQuery, int]]:
    """«Если поднять бюджет до $100 000 — 14 квартир»: только ненулевые, по убыванию числа."""
    found = []
    for text, relaxed in _relaxed(query):
        n = sum(1 for i in index if _matches(i, relaxed))
        if n:
            found.append((f"{text} — {_flats(n)}", relaxed, n))
    found.sort(key=lambda s: -s[2])
    return found[:limit]


def summary_line(result: SearchResult, sort: Sort = "best") -> str:
    """«Нашёл 23 квартиры: 5 дешевле похожих, 12 в рынке, 6 дороже. Показываю сначала выгодные.»"""
    n = len(result.items)
    if not n:
        return "Ничего не нашёл."
    c = result.counts
    parts = [
        (c["below"], "дешевле похожих"),
        (c["market"], "в рынке"),
        (c["suspicious"], "подозрительно дёшево"),
        (c["above"], "дороже"),
        (c["none"], "без сравнения"),
    ]
    found = plural(n, "квартиру", "квартиры", "квартир")
    groups = ", ".join(f"{k} {text}" for k, text in parts if k)
    return f"Нашёл {n} {found}: {groups}. Показываю сначала {SORT_WORDS[sort]}."


_BUDGET_NOISE = re.compile(r"тыс\.?|usd|[$кk\s]")
_BUDGET = re.compile(r"(\d+)(?:-(\d+))?")


def parse_budget(text: str) -> tuple[int | None, int | None] | None:
    """«85000», «85», «85 тыс», «$85 000» → (None, 85000); «70-90» → (70000, 90000); иначе None.
    Числа меньше 1000 — тысячи; одно число — верхняя граница."""
    cleaned = _BUDGET_NOISE.sub("", text.lower()).replace("–", "-").replace("—", "-")
    match = _BUDGET.fullmatch(cleaned)
    if match is None:
        return None
    values = [int(v) * 1000 if int(v) < 1000 else int(v) for v in match.groups() if v]
    if not all(values):
        return None
    if len(values) == 1:
        return None, values[0]
    low, high = sorted(values)
    return low, high
