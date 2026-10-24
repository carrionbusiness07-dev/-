"""Данные для страниц кабинета: лента «Что изменилось», пульс рынка, список объектов.

Слой поверх аналитики Leo (`minsk_realty.analytics`): здесь только сборка того, что
показывает страница, без новых расчётов рынка.
"""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minsk_realty.analytics import (
    MarketData,
    build_price_history,
    compute_baseline,
    compute_exposure,
    compute_hypothesis,
    compute_overview,
    load_market,
)
from minsk_realty.analytics.config import DEFAULT_ANALYTICS
from minsk_realty.analytics.fmt import (
    MINSK_TZ,
    MONTHS_GEN,
    district_label,
    local,
    money,
    ppm,
    rooms_label,
    street_label,
)
from minsk_realty.analytics.fmt import pct as _pct
from minsk_realty.collector.run import INTERRUPTED, STALE_RUN_AFTER
from minsk_realty.db.models import Event, RealtyObject, SourceRun
from minsk_realty.db.models import Snapshot as DbSnapshot

DEMO_MARKER = "demo"


def pct(value: float) -> str:
    """−4,1%: в интерфейсе десятичная запятая, как принято в русском."""
    return _pct(value).replace(".", ",")


def share(value: float | None) -> str:
    """Доля в процентах: 0,4% · 12% · —."""
    if value is None:
        return "—"
    text = f"{value * 100:.1f}" if value < 0.1 else f"{value * 100:.0f}"
    return text.removesuffix(".0").replace(".", ",") + "%"


SOURCE_NAMES = {"kufar": "Kufar", "realt": "Realt", "onliner": "Onliner", "domovita": "Domovita"}
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

# Рубрики ленты: какое событие главное, если в один день у квартиры их несколько.
KINDS = {
    "removed": "Снята",
    "relisted": "Перевыложена",
    "price_drop": "Подешевела",
    "returned": "Вернулась",
    "new": "Новая",
    "price_up": "Подорожала",
    "cross_listed": "На двух площадках",
}
FILTERS = [
    ("all", "Все"),
    ("price_drop", "Подешевели"),
    ("relisted", "Перевыложены"),
    ("removed", "Сняты"),
    ("new", "Новые"),
    ("returned", "Вернулись"),
]


# --- Источник данных -----------------------------------------------------------------------


@dataclass
class Snapshot:
    data: MarketData
    now: datetime
    demo: bool
    loaded_at: float
    version: tuple = ()
    # Расчёты поверх среза (лента, пульс, список объектов): считаются один раз на срез.
    memo: dict = field(default_factory=dict, repr=False)

    @property
    def issue(self) -> int | None:
        """Номер «выпуска» — день наблюдений по счёту."""
        start = self.data.observation_start
        return (local(self.now).date() - local(start).date()).days + 1 if start else None

    def cached(self, key, compute):
        if key not in self.memo:
            self.memo[key] = compute()
        return self.memo[key]


_cache: dict[str, Snapshot] = {}
_load_lock = threading.Lock()
# Срез перечитывается, когда в БД что-то поменялось (закончился проход, склейка записала
# события). Срок жизни — подстраховка на изменения, которых версия не видит.
CACHE_SECONDS = 15 * 60


def data_version(session: Session) -> tuple:
    """Дешёвый отпечаток состояния БД: меняется после каждого прохода и склейки."""
    row = session.execute(
        select(
            select(func.max(SourceRun.id)).scalar_subquery(),
            select(func.max(SourceRun.finished_at)).scalar_subquery(),
            select(func.max(Event.id)).scalar_subquery(),
            select(func.max(Event.retracted_at)).scalar_subquery(),
            select(func.max(RealtyObject.id)).scalar_subquery(),
        )
    ).one()
    return tuple(str(value) for value in row)


def market_snapshot(session: Session, key: str) -> Snapshot:
    """Срез рынка для страниц. Загрузка всего рынка — около секунды, поэтому срез живёт,
    пока данные не изменились: сборщик пишет раз в 30–60 минут."""
    version = data_version(session)
    cached = _fresh(key, version)
    if cached:
        return cached
    with _load_lock:  # фоновый прогрев и запрос не грузят рынок дважды одновременно
        return _fresh(key, version) or _load(session, key, version)


def _fresh(key: str, version: tuple) -> Snapshot | None:
    cached = _cache.get(key)
    if cached and cached.version == version and time.monotonic() - cached.loaded_at < CACHE_SECONDS:
        return cached
    return None


def _load(session: Session, key: str, version: tuple) -> Snapshot:
    demo = bool(session.scalar(select(SourceRun.id).where(SourceRun.error == DEMO_MARKER).limit(1)))
    now = datetime.now(UTC)
    if demo:
        # Демо-база заканчивается в прошлом: «сейчас» — вскоре после её последнего прохода.
        last = session.scalar(select(func.max(SourceRun.finished_at)))
        now = (last or now) + timedelta(hours=2)
    snap = Snapshot(load_market(session), now, demo, time.monotonic(), version)
    _cache[key] = snap
    return snap


def warm(snap: Snapshot) -> None:
    """Посчитать заранее то, что нужно страницам по умолчанию (фоновый прогрев кабинета)."""
    feed_groups(snap, 7)
    overview(snap, 5)
    observation_metrics(snap)
    city_series(snap)
    all_object_rows(snap)
    overview(snap, 10)  # «Рынок»
    city_series(snap, 12)


def overview(snap: Snapshot, top: int):
    return snap.cached(("overview", top), lambda: compute_overview(snap.data, snap.now, top=top))


# --- Ход наблюдения ------------------------------------------------------------------------


@dataclass
class Gauge:
    label: str
    value: float | None
    threshold: float | None
    passed: bool


@dataclass
class Observation:
    """Полоса над лентой: идёт ли эксперимент на 6 недель и копятся ли чистые данные."""

    day: int | None
    total_days: int
    gauges: list[Gauge]
    address: list[tuple[str, float]]  # площадка → доля активных объявлений с адресом
    full_passes: list[tuple[str, int]]  # площадка → полных проходов за сутки


def observation_metrics(snap: Snapshot) -> tuple[list[Gauge], dict[str, float]]:
    """Метрики гипотезы (Leo) и доля активных объявлений с адресом по площадкам."""

    def compute() -> tuple[list[Gauge], dict[str, float]]:
        report = compute_hypothesis(snap.data, snap.now)
        labels = {"relisted_or_cross": "Дубли и перевыкладывания", "with_drops": "Подешевели"}
        gauges = [
            Gauge(labels[c.key], c.value, c.threshold, c.passed)
            for c in report.criteria
            if c.key in labels
        ]
        # По самому объявлению, а не по объекту: Kufar без адреса, склеенный с объектом Realt,
        # не должен считаться «с адресом».
        counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for row in snap.data.listings.values():
            if not row.active:
                continue
            last = snap.data.last_snapshot(row.id)
            counts[row.source][0] += bool(last and (last.street or last.address_raw))
            counts[row.source][1] += 1
        return gauges, {src: have / total for src, (have, total) in counts.items() if total}

    return snap.cached("observation", compute)


def observation(snap: Snapshot, session: Session) -> Observation:
    gauges, address = observation_metrics(snap)
    passes = dict(
        session.execute(
            select(SourceRun.source, func.count())
            .where(
                SourceRun.status == "ok",
                SourceRun.complete.is_(True),
                SourceRun.started_at >= snap.now - timedelta(hours=24),
            )
            .group_by(SourceRun.source)
        ).all()
    )
    sources = [s for s in SOURCE_NAMES if s in address or s in passes]
    return Observation(
        day=snap.issue,
        total_days=DEFAULT_ANALYTICS.hypothesis_weeks * 7,
        gauges=gauges,
        address=[(SOURCE_NAMES[s], address[s]) for s in sources if s in address],
        full_passes=[(SOURCE_NAMES[s], int(passes.get(s, 0))) for s in sources],
    )


# --- Лента «Что изменилось» ----------------------------------------------------------------


@dataclass
class PriceStep:
    text: str
    struck: bool


@dataclass
class Entry:
    object_id: int
    kind: str
    rubric: str
    tags: list[str]
    at: datetime
    title: str
    address: str
    district: str
    steps: list[PriceStep]
    delta: str | None
    delta_dir: str | None  # down / up
    note: str | None
    source: str
    seller: str
    url: str | None
    ppm: str | None


@dataclass
class Day:
    day: date
    label: str
    entries: list[Entry] = field(default_factory=list)


def day_label(d: date, today: date) -> str:
    words = f"{d.day} {MONTHS_GEN[d.month - 1]}, {WEEKDAYS[d.weekday()]}"
    if d == today:
        return f"Сегодня · {words}"
    if d == today - timedelta(days=1):
        return f"Вчера · {words}"
    return words[0].upper() + words[1:]


FEED_PAGE = 50
FEED_MAX = 2000  # дальше листать ленту бессмысленно: для поиска есть «Объекты»


@dataclass
class FeedGroup:
    """События одной квартиры за один день — одна строка ленты."""

    object_id: int
    events: list  # главное — первым
    kind: str
    tags: set[str]
    at: datetime


def feed_groups(snap: Snapshot, days: int) -> list[FeedGroup]:
    """Строки ленты за `days` дней, свежие первыми. Дорогая часть строки (история цены,
    срок на рынке) здесь не считается — только для показанной страницы ленты."""

    def compute() -> list[FeedGroup]:
        data, now = snap.data, snap.now
        since = now - timedelta(days=days)
        grouped: dict[tuple[int, date], list] = defaultdict(list)
        for object_id, events in data.events.items():
            for event in events:
                if event.trusted and since <= event.at <= now and event.type in KINDS:
                    grouped[(object_id, local(event.at).date())].append(event)
        order = list(KINDS)
        groups = []
        for (object_id, _), events in grouped.items():
            if object_id not in data.objects or not data.object_listings(object_id):
                continue
            events = sorted(events, key=lambda e: order.index(e.type))
            kind = events[0].type
            tags = {e.type for e in events} - {kind}
            groups.append(FeedGroup(object_id, events, kind, tags, max(e.at for e in events)))
        groups.sort(key=lambda g: g.at, reverse=True)
        return groups

    return snap.cached(("feed", days), compute)


@dataclass
class Feed:
    days: list[Day]
    counts: dict[str, int]
    total: int  # строк под фильтром
    shown: int


def build_feed(snap: Snapshot, *, days: int = 7, kind: str = "all", limit: int = FEED_PAGE) -> Feed:
    data, now = snap.data, snap.now
    groups = feed_groups(snap, days)

    # Счётчики — по квартирам, а не по записям ленты: квартира, подешевевшая дважды за
    # неделю, считается один раз (как «Подешевели» в пульсе рынка).
    objects_by_tag: dict[str, set[int]] = {key: set() for key, _ in FILTERS}
    for group in groups:
        objects_by_tag["all"].add(group.object_id)
        for tag in {group.kind, *group.tags}:
            if tag in objects_by_tag:
                objects_by_tag[tag].add(group.object_id)
    counts = {key: len(ids) for key, ids in objects_by_tag.items()}

    if kind != "all":
        groups = [g for g in groups if g.kind == kind or kind in g.tags]
    entries = [_entry(data, now, g.object_id, g.events) for g in groups[:limit]]
    today = local(now).date()
    by_day: dict[date, Day] = {}
    for entry in entries:
        if entry is None:
            continue
        d = local(entry.at).date()
        by_day.setdefault(d, Day(d, day_label(d, today))).entries.append(entry)
    return Feed(list(by_day.values()), counts, len(groups), min(limit, len(groups)))


def _entry(data: MarketData, now: datetime, object_id: int, events: list) -> Entry | None:
    obj = data.objects.get(object_id)
    listings = data.object_listings(object_id)
    if obj is None or not listings:
        return None
    order = list(KINDS)
    events = sorted(events, key=lambda e: order.index(e.type))
    main = events[0]
    kinds = {e.type for e in events}
    listing = data.listings.get(main.listing_id) if main.listing_id else None
    listing = listing or max(listings, key=lambda r: (r.last_seen, r.id))

    history = build_price_history(listings, data.snapshots, data.object_events(object_id))
    amounts: list[tuple[Decimal, str]] = []
    for point in history:
        if not amounts or amounts[-1] != (point.amount, point.currency):
            amounts.append((point.amount, point.currency))
    # В строке — текущая цена и до двух предыдущих, отличных от неё (цена могла вернуться
    # к прежней: зачёркнутая «$81 000» рядом с текущей «$81 000» читается как ошибка).
    current = amounts[-1] if amounts else None
    previous: list[tuple[Decimal, str]] = []
    for item in reversed(amounts[:-1]):
        # Только в валюте текущей цены: «$102 338 → €91 219» не сравнить глазами (и это не
        # изменение цены — продавец сменил валюту).
        if item[1] != current[1]:
            break
        if item != current and item not in previous:
            previous.append(item)
        if len(previous) == 2:
            break
    shown = [*reversed(previous), *([current] if current else [])]
    steps = [PriceStep(money(a, c), i < len(shown) - 1) for i, (a, c) in enumerate(shown)]

    delta = delta_dir = None
    change = next((e for e in events if e.type in ("price_drop", "price_up")), None)
    if change is not None and change.pct is not None:
        delta, delta_dir = pct(change.pct), "down" if change.pct < 0 else "up"

    exposure = compute_exposure(
        listings, data.object_events(object_id), now, data.source_starts, current=listing
    )
    note = _note(main, exposure, data, object_id, now)
    price = data.object_price(object_id, now)
    area = obj.area or (price.area if price else None)
    title = ", ".join(
        part
        for part in (
            rooms_label(obj.rooms),
            f"{_num(area)} м²" if area else None,
            f"{obj.floor}/{obj.floors_total} эт." if obj.floor and obj.floors_total else None,
        )
        if part
    )
    street = street_label(obj.street_norm)
    address = f"{street}, {obj.house}" if street and obj.house else street or "адрес не указан"
    return Entry(
        object_id=object_id,
        kind=main.type,
        rubric=KINDS[main.type],
        tags=[k for k in order if k in kinds and k != main.type],
        at=max(e.at for e in events),
        title=title,
        address=address,
        district=district_label(data.object_district(object_id)),
        steps=steps,
        delta=delta,
        delta_dir=delta_dir,
        note=note,
        source=SOURCE_NAMES.get(listing.source, listing.source),
        seller=seller_label(listing),
        url=listing.url,
        ppm=ppm(price.usd_per_m2) if price and price.usd_per_m2 else None,
    )


def seller_label(listing) -> str:
    if listing.agency_name:
        return f"агентство «{listing.agency_name}»"
    # Kufar отдаёт только признак «агентство», без названия.
    return {"owner": "собственник", "agency": "агентство"}.get(
        listing.seller_type, "продавец не указан"
    )


def _note(event, exposure, data: MarketData, object_id: int, now: datetime) -> str | None:
    days = exposure.days if exposure else None
    at_least = "как минимум " if exposure and exposure.at_least else ""
    if days == 0 and exposure.at_least and event.type in ("price_drop", "price_up"):
        return "впервые замечена сегодня"
    if event.type == "relisted" and exposure is not None:
        site = exposure.site_days or 0
        return f"на сайте {site} дн. · на деле {at_least}{days} дн."
    if event.type == "removed" and days is not None:
        return f"снята после {at_least}{days} дн. на рынке"
    if event.type == "returned":
        away = event.payload.get("away_days")
        return f"не было {int(away)} дн." if away else "снова в продаже"
    if event.type == "cross_listed":
        sources = [SOURCE_NAMES.get(s, s) for s in event.payload.get("sources", [])]
        return "теперь на " + " и ".join(sources) if sources else None
    if event.type == "new":
        return "впервые на рынке"
    if days is not None:
        drops = sum(1 for e in data.object_events(object_id) if e.type == "price_drop")
        return f"на рынке {at_least}{days} дн. · снижений: {drops}"
    return None


def _num(value: float) -> str:
    return f"{value:.1f}".replace(".0", "").replace(".", ",")


# --- Пульс рынка ---------------------------------------------------------------------------


@dataclass
class WeekPoint:
    label: str
    value: float | None


def city_series(snap: Snapshot, weeks: int = 8) -> list[WeekPoint]:
    """Медиана $/м² по Минску на конец каждой из последних недель (из истории)."""
    return snap.cached(("series", weeks), lambda: _city_series(snap, weeks))


def _city_series(snap: Snapshot, weeks: int) -> list[WeekPoint]:
    points = []
    for k in range(weeks - 1, -1, -1):
        at = snap.now - timedelta(weeks=k)
        cell = compute_baseline(snap.data, at).city()
        value = cell.median if cell and cell.enough else None
        points.append(WeekPoint(f"{local(at):%d.%m}", value))
    return points


# --- Список объектов -----------------------------------------------------------------------


@dataclass
class ObjectRow:
    object_id: int
    title: str
    address: str
    district: str
    district_raw: str | None
    price: str
    price_value: float
    ppm: str
    ppm_value: float
    days: int
    at_least: bool
    drops: int
    relists: int
    sources: str
    state: str  # active / hidden / removed


OBJECTS_PAGE = 100


def object_rows(snap: Snapshot, *, district: str | None = None) -> list[ObjectRow]:
    rows = all_object_rows(snap)
    return [r for r in rows if r.district_raw == district] if district else list(rows)


def all_object_rows(snap: Snapshot) -> list[ObjectRow]:
    return snap.cached("objects", lambda: _object_rows(snap))


def _object_rows(snap: Snapshot) -> list[ObjectRow]:
    data, now = snap.data, snap.now
    rows = []
    for object_id, obj in data.objects.items():
        listings = data.object_listings(object_id)
        if not listings:
            continue
        raw_district = data.object_district(object_id)
        events = data.object_events(object_id)
        exposure = compute_exposure(listings, events, now, data.source_starts)
        if exposure is None or exposure.state == "removed":
            continue
        price = data.object_price(object_id, now)
        if price is None:
            last = max(listings, key=lambda r: r.last_seen)
            snapshot = data.last_snapshot(last.id, priced=True)
            if snapshot is None:
                continue
            amount, currency, usd, area = (
                snapshot.amount,
                snapshot.currency,
                snapshot.price_usd,
                snapshot.area,
            )
        else:
            amount, currency, usd, area = price.amount, price.currency, price.usd, price.area
        per_m2 = float(usd) / area if usd and area else 0.0
        street = street_label(obj.street_norm)
        rows.append(
            ObjectRow(
                object_id=object_id,
                title=", ".join(
                    p for p in (rooms_label(obj.rooms), f"{_num(area)} м²" if area else None) if p
                ),
                address=f"{street}, {obj.house}" if street and obj.house else street or "—",
                district=district_label(raw_district),
                district_raw=raw_district,
                price=money(amount, currency),
                price_value=float(usd or 0),
                ppm=ppm(per_m2) if per_m2 else "—",
                ppm_value=per_m2,
                days=exposure.days,
                at_least=exposure.at_least,
                drops=sum(1 for e in events if e.type == "price_drop"),
                relists=exposure.relist_count,
                sources=" · ".join(SOURCE_NAMES.get(s, s) for s in exposure.sources),
                state=exposure.state,
            )
        )
    return rows


SORTS = {
    "days": (lambda r: r.days, True),
    "drops": (lambda r: (r.drops, r.days), True),
    "price": (lambda r: r.price_value, False),
    "ppm": (lambda r: r.ppm_value, False),
    "relists": (lambda r: (r.relists, r.days), True),
}


def sort_rows(rows: list[ObjectRow], sort: str) -> list[ObjectRow]:
    key, reverse = SORTS.get(sort, SORTS["days"])
    return sorted(rows, key=key, reverse=reverse)


# --- Хроника объекта: график цены и события ------------------------------------------------


@dataclass
class Mark:
    x: float  # 0..100, % ширины
    label: str
    date: str
    kind: str


@dataclass
class Timeline:
    path: str  # SVG path ступенчатой цены, координаты viewBox 0 0 1000 200
    y_labels: list[tuple[float, str]]
    x_labels: list[tuple[float, str]]
    dots: list[tuple[float, float, str, str]]  # x, y, kind, подпись
    marks: list[Mark]


MAP_GROUP_LIMIT = 10
MAP_GROUPS_MAX = 300


def map_data(report, data: MarketData) -> dict | None:
    """Данные для карты на странице квартиры; None — координаты неточные или их нет.

    Похожие с одинаковыми координатами (4 знака) сливаются в одну группу: в новостройках сотни
    квартир сидят в одной точке. Цвета ярлыков считает JS по ppm, в данных их нет."""
    loc = report.location
    if loc is None or loc.approx:
        return None
    st = loc.station
    price = report.listing.price if report.listing else None
    cmp = report.comparison
    groups: dict[tuple[float, float], list[dict]] = {}
    for p in cmp.points if cmp else ():
        obj = data.objects.get(p.object_id)
        groups.setdefault((round(p.lat, 4), round(p.lon, 4)), []).append(
            {
                "id": p.object_id,
                "url": f"/objects/{p.object_id}",
                "ppm": round(p.usd_per_m2),
                "rooms": obj.rooms if obj and obj.rooms else p.rooms,
                "area": obj.area if obj else None,
                "floor": obj.floor if obj else None,
                "floors_total": obj.floors_total if obj else None,
                "finish": p.finish,
            }
        )
    similar = []
    # shortcut: не больше 300 групп, оставляем ближайшие к квартире (не самые дешёвые, чтобы
    # карта не искажалась); поднять, если на карте начнёт не хватать точек.
    near = sorted(groups.items(), key=lambda g: (g[0][0] - loc.lat) ** 2 + (g[0][1] - loc.lon) ** 2)
    for (lat, lon), items in near[:MAP_GROUPS_MAX]:
        items.sort(key=lambda i: i["ppm"])
        similar.append(
            {"lat": lat, "lon": lon, "items": items[:MAP_GROUP_LIMIT],
             "more": max(len(items) - MAP_GROUP_LIMIT, 0)}
        )
    return {
        "flat": {
            "lat": loc.lat,
            "lon": loc.lon,
            "ppm": round(price.usd_per_m2) if price and price.usd_per_m2 else None,
        },
        "radius_km": cmp.radius_km if cmp else None,
        "station": {"name": st.name, "lat": st.lat, "lon": st.lon} if st else None,
        "similar": similar,
        "capped": len(near) > MAP_GROUPS_MAX,
        "max_groups": MAP_GROUPS_MAX,
    }


PHOTO_LIMIT = 6


def _latest_extra(session: Session, listing_id: int) -> dict:
    extra = session.scalar(
        select(DbSnapshot.extra)
        .where(DbSnapshot.listing_id == listing_id)
        .order_by(DbSnapshot.seen_at.desc(), DbSnapshot.id.desc())
        .limit(1)
    )
    return extra if isinstance(extra, dict) else {}


def _photo_urls(session: Session, listing_ids: list[int]) -> dict[int, list[str]]:
    """Ссылки на фото из последнего снимка каждого объявления (extra читаем только у них).

    Только https://: ссылка с площадки идёт в src без прокси, чужую схему не пропускаем."""
    out: dict[int, list[str]] = {}
    for lid in listing_ids:
        urls = _latest_extra(session, lid).get("photo_urls")
        urls = urls if isinstance(urls, list) else []
        out[lid] = [u for u in urls if isinstance(u, str) and u.startswith("https://")]
    return out


def photos(session: Session, report) -> list[dict]:
    """До 6 фото квартиры: из проверяемого объявления, иначе из первого другого надёжного,
    где они есть. Фото не храним и не проксируем — браузер берёт их с площадки."""
    if report.listing is None:
        return []
    trusted = [o for o in report.others if o.confidence in ("high", "medium")]
    ids = [report.listing.listing_id, *(o.listing_id for o in trusted)]
    urls = _photo_urls(session, ids)
    sources = {report.listing.listing_id: report.listing.source}
    sources.update({o.listing_id: o.source for o in trusted})
    for lid in ids:
        if urls[lid]:
            return [{"url": u, "source": sources[lid]} for u in urls[lid][:PHOTO_LIMIT]]
    return []


def other_rows(session: Session, data: MarketData, report) -> list[dict]:
    """Строки таблицы «на других площадках»: объявление + данные его последнего снимка."""
    approx = DEFAULT_ANALYTICS.approx_coord_sources
    thumbs = _photo_urls(session, [o.listing_id for o in report.others])
    rows = []
    for o in report.others:
        snap = data.last_snapshot(o.listing_id)
        point = data.listing_point(o.listing_id, approx)
        rows.append(
            {
                "o": o,
                "thumb": thumbs[o.listing_id][0] if thumbs[o.listing_id] else None,
                "address": snap.address_raw if snap else None,
                "floor": snap.floor if snap else None,
                "floors_total": snap.floors_total if snap else None,
                "area": snap.area if snap else None,
                # Точка неточной площадки — общая для двора, на карте её не показываем.
                "map_url": (
                    f"https://yandex.by/maps/?pt={point[1]},{point[0]}&z=17"
                    if point and not point[2]
                    else None
                ),
            }
        )
    return rows


@dataclass
class PlatformEvent:
    """Изменение цены со слов площадки: мы его не наблюдали, в наши проценты оно не входит."""

    at: datetime
    source: str
    usd: float | None  # Realt цену не отдаёт — только факт и направление
    direction: int  # -1 снижение, 1 повышение, 0 просто цена на дату
    change_pct: float | None  # к предыдущей цене той же площадки, если она известна
    platform = True  # строка таблицы истории: со слов площадки (у HistoryRow — False)


PLATFORM_EVENTS_MAX = 12
# Площадка может помнить цену за годы; такая точка растянет ось так, что наши дни сольются.
PLATFORM_EVENTS_DAYS = 365


def _platform_when(value) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if len(str(value)) <= 10:  # Domovita отдаёт только дату
        when = when.replace(hour=12)
    return when if when.tzinfo else when.replace(tzinfo=MINSK_TZ)


def platform_events(
    session: Session, report, now: datetime | None = None
) -> list[PlatformEvent]:
    """События цены со слов площадок (Domovita `price_history`, Realt `priceChangeDate`) из
    последних снимков надёжных объявлений объекта. Читаем только их, от старых к новым."""
    if report.listing is None:
        return []
    oldest = (now or datetime.now(UTC)) - timedelta(days=PLATFORM_EVENTS_DAYS)
    trusted = [o for o in report.others if o.confidence in ("high", "medium")]
    found: dict[tuple, PlatformEvent] = {}
    for lid, source in [(report.listing.listing_id, report.listing.source)] + [
        (o.listing_id, o.source) for o in trusted
    ]:
        extra = _latest_extra(session, lid)
        pairs = []  # Domovita: [[дата, USD], ...] от новых к старым
        for row in extra.get("price_history") or []:
            if (
                isinstance(row, list | tuple)
                and len(row) == 2
                and isinstance(row[1], int | float)
                and not isinstance(row[1], bool)
                and math.isfinite(row[1])
                and row[1] > 0
            ):
                when = _platform_when(row[0])
                if when:
                    pairs.append((when, float(row[1])))
        pairs.sort()
        for i, (when, usd) in enumerate(pairs):
            change = (usd - pairs[i - 1][1]) / pairs[i - 1][1] * 100 if i and pairs[i - 1][1] else None
            direction = 0 if not change else (-1 if change < 0 else 1)
            found[(source, when.date(), usd)] = PlatformEvent(when, source, usd, direction, change)
        direction = extra.get("priceChangeDirection")  # Realt: -1 снижение, 1 повышение
        when = _platform_when(extra.get("priceChangeDate")) if direction in (-1, 1) else None
        if when:
            found[(source, when.date(), None)] = PlatformEvent(when, source, None, direction, None)
    recent = [e for e in found.values() if e.at >= oldest]
    return sorted(recent, key=lambda e: e.at)[-PLATFORM_EVENTS_MAX:]


# Короче — вместо графика лента: две точки на пустой оси ничего не показывают.
CHART_MIN_POINTS = 3
CHART_MIN_DAYS = 7


def timeline(snap: Snapshot, report, events: list[PlatformEvent] = ()) -> Timeline | None:
    points = [p for p in report.history if p.usd is not None]
    if not points or report.exposure is None:
        return None
    end = snap.now if report.exposure.state == "active" else report.exposure.end
    if len(points) < CHART_MIN_POINTS or end - points[0].at < timedelta(days=CHART_MIN_DAYS):
        return None
    priced = [e for e in events if e.usd is not None]
    # Ось начинается с самой ранней точки (нашей или площадки), а не с даты публикации.
    start = min([points[0].at, *(e.at for e in events)])
    span = max((end - start).total_seconds(), 1)

    def x(at: datetime) -> float:
        return max(0.0, min(1000.0, (at - start).total_seconds() / span * 1000))

    values = [float(p.usd) for p in points] + [e.usd for e in priced]
    low, high = min(values), max(values)
    pad = max((high - low) * 0.25, high * 0.01)
    low, high = low - pad, high + pad

    def y(v: float) -> float:
        return 180 - (v - low) / (high - low) * 160

    path = []
    # Площадка — полые точки, рисуются первыми: наши лежат поверх.
    dots = [
        (x(e.at), y(e.usd), "platform", f"{SOURCE_NAMES.get(e.source, e.source)}, со слов площадки: {money(e.usd, 'USD')}")
        for e in priced
    ]
    for i, point in enumerate(points):
        px, py = x(point.at), y(float(point.usd))
        # Ступенька: цена держится до следующего изменения, потом скачком меняется.
        path.append(f"M{px:.1f} {py:.1f}" if i == 0 else f"H{px:.1f} V{py:.1f}")
        change = point.change_pct or 0
        kind = "first" if i == 0 else "down" if change < 0 else "up" if change > 0 else "same"
        dots.append((px, py, kind, money(point.amount, point.currency)))
    path.append(f"H{x(end):.1f}")

    ticks = [low + (high - low) * f for f in (0.15, 0.5, 0.85)]
    y_labels = [(y(v), f"${round(v / 1000, 1):g}k".replace(".", ",")) for v in ticks]
    x_labels = []
    month = local(start).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while month <= local(end):
        if month >= local(start):
            x_labels.append((x(month), f"{MONTHS_GEN[month.month - 1][:3]}"))
        month = (month + timedelta(days=32)).replace(day=1)

    names = {
        "relisted": "перевыложена",
        "removed": "снята",
        "returned": "вернулась",
        "cross_listed": "вторая площадка",
    }
    by_day: dict[str, Mark] = {}
    for event in snap.data.object_events(report.object_id) if report.object_id else []:
        if event.type not in names:
            continue
        if event.type == "cross_listed" and event.at - start < timedelta(days=1):
            continue  # квартира сразу появилась на двух площадках — это не событие истории
        day = f"{local(event.at):%d.%m}"
        if day in by_day:
            if names[event.type] not in by_day[day].label:
                by_day[day].label += f", {names[event.type]}"
        else:
            by_day[day] = Mark(x(event.at) / 10, names[event.type], day, event.type)
    marks = list(by_day.values())
    for e in events:
        if e.usd is None:  # цены нет — нечего ставить на линию, остаётся риска внизу
            word = "снижение" if e.direction < 0 else "повышение"
            marks.append(
                Mark(x(e.at) / 10, f"{SOURCE_NAMES.get(e.source, e.source)}: {word} цены (со слов площадки)", f"{local(e.at):%d.%m}", "platform")
            )
    return Timeline(" ".join(path), y_labels, x_labels, dots, marks)


@dataclass
class FeedItem:
    at: datetime
    text: str  # цена или «цену снижали»
    change_pct: float | None
    note: str
    platform: bool


def price_feed(history: list[HistoryRow], events: list[PlatformEvent]) -> list[FeedItem]:
    """Лента вместо графика: наши точки истории и события со слов площадки, новые сверху."""
    items = [
        FeedItem(
            h.at,
            money(h.amount, h.currency),
            h.change_pct,
            h.sources + (" · первая цена" if h.kind == "first" else ""),
            False,
        )
        for h in history
    ]
    for e in events:
        name = SOURCE_NAMES.get(e.source, e.source)
        text = money(e.usd, "USD") if e.usd is not None else (
            "цену снижали" if e.direction < 0 else "цену повышали"
        )
        note = f"{name} · со слов площадки" + (" · в долларах" if e.usd is not None else "")
        items.append(FeedItem(e.at, text, e.change_pct, note, True))
    return sorted(items, key=lambda i: i.at, reverse=True)


@dataclass
class HistoryRow:
    platform = False
    at: datetime
    amount: Decimal
    currency: str
    change_pct: float | None
    kind: str
    sources: str
    seller: str


def history_rows(report) -> list[HistoryRow]:
    """История цены без дублей: одна строка на дату и цену, площадки — через запятую."""
    rows: list[HistoryRow] = []
    for point in report.history:
        seller = point.agency_name or {"owner": "собственник", "agency": "агентство"}.get(
            point.seller_type, "—"
        )
        source = SOURCE_NAMES.get(point.source, point.source)
        last = rows[-1] if rows else None
        if (
            last
            and local(last.at).date() == local(point.at).date()
            and (last.amount, last.currency) == (point.amount, point.currency)
        ):
            if source not in last.sources:
                last.sources += f", {source}"
            if seller not in last.seller:
                last.seller += f", {seller}"
            continue
        rows.append(
            HistoryRow(
                point.at, point.amount, point.currency, point.change_pct, point.kind, source, seller
            )
        )
    return rows


def history_table(history: list[HistoryRow], events: list[PlatformEvent]) -> list:
    """Строки таблицы под графиком: наши точки и (приглушённо) события со слов площадки."""
    return sorted([*history, *events], key=lambda r: r.at)


# --- Источники -----------------------------------------------------------------------------


@dataclass
class RunRow:
    run: SourceRun
    label: str
    dot: str
    reason: str | None


RUN_LABELS = {
    "ok": ("успешно", "ok"),
    "running": ("идёт", "warning"),
    "failed": ("ошибка", "failing"),
    "blocked": ("отказ площадки", "blocked"),
}


def run_row(run: SourceRun, now: datetime) -> RunRow:
    label, dot = RUN_LABELS.get(run.status, (run.status, "warning"))
    reason = run.error if run.status in ("failed", "blocked") else None
    stale = run.status == "running" and now - run.started_at > STALE_RUN_AFTER
    if stale or (run.status == "failed" and run.error == INTERRUPTED):
        # Процесс остановили посреди прохода (выключили компьютер, перезапустили Docker).
        label, dot, reason = "прерван", "never", "сборщик остановили посреди прохода"
    return RunRow(run, label, dot, reason[:160] if reason else None)


def recent_runs(session: Session, now: datetime, limit: int = 40) -> list[RunRow]:
    runs = session.scalars(
        select(SourceRun)
        .where(SourceRun.started_at >= now - timedelta(days=7))
        .order_by(SourceRun.started_at.desc())
        .limit(limit)
    )
    return [run_row(run, now) for run in runs]
