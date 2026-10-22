"""Данные для страниц кабинета: лента «Что изменилось», пульс рынка, список объектов.

Слой поверх аналитики Льва (`minsk_realty.analytics`): здесь только сборка того, что
показывает страница, без новых расчётов рынка.
"""

from __future__ import annotations

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
    load_market,
)
from minsk_realty.analytics.fmt import (
    MONTHS_GEN,
    district_label,
    local,
    money,
    ppm,
    rooms_label,
    street_label,
)
from minsk_realty.analytics.fmt import pct as _pct
from minsk_realty.db.models import SourceRun

DEMO_MARKER = "demo"


def pct(value: float) -> str:
    """−4,1%: в интерфейсе десятичная запятая, как принято в русском."""
    return _pct(value).replace(".", ",")


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

    @property
    def issue(self) -> int | None:
        """Номер «выпуска» — день наблюдений по счёту."""
        start = self.data.observation_start
        return (local(self.now).date() - local(start).date()).days + 1 if start else None


_cache: dict[str, Snapshot] = {}
CACHE_SECONDS = 60


def market_snapshot(session: Session, key: str) -> Snapshot:
    """Срез рынка для страниц. Кэш на минуту: сборщик пишет раз в 30–60 минут."""
    cached = _cache.get(key)
    if cached and time.monotonic() - cached.loaded_at < CACHE_SECONDS:
        return cached
    demo = bool(session.scalar(select(SourceRun.id).where(SourceRun.error == DEMO_MARKER).limit(1)))
    now = datetime.now(UTC)
    if demo:
        # Демо-база заканчивается в прошлом: «сейчас» — вскоре после её последнего прохода.
        last = session.scalar(select(func.max(SourceRun.finished_at)))
        now = (last or now) + timedelta(hours=2)
    snap = Snapshot(load_market(session), now, demo, time.monotonic())
    _cache[key] = snap
    return snap


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


def build_feed(
    snap: Snapshot, *, days: int = 7, kind: str = "all"
) -> tuple[list[Day], dict[str, int]]:
    data, now = snap.data, snap.now
    since = now - timedelta(days=days)
    grouped: dict[tuple[int, date], list] = defaultdict(list)
    for object_id, events in data.events.items():
        for event in events:
            if event.trusted and since <= event.at <= now and event.type in KINDS:
                grouped[(object_id, local(event.at).date())].append(event)

    entries: list[Entry] = []
    for (object_id, _), events in grouped.items():
        entry = _entry(data, now, object_id, events)
        if entry is not None:
            entries.append(entry)

    # Счётчики — по квартирам, а не по записям ленты: квартира, подешевевшая дважды за
    # неделю, считается один раз (как «Подешевели» в пульсе рынка).
    objects_by_tag: dict[str, set[int]] = {key: set() for key, _ in FILTERS}
    for entry in entries:
        objects_by_tag["all"].add(entry.object_id)
        for tag in {entry.kind, *entry.tags}:
            if tag in objects_by_tag:
                objects_by_tag[tag].add(entry.object_id)
    counts = {key: len(ids) for key, ids in objects_by_tag.items()}

    if kind != "all":
        entries = [e for e in entries if e.kind == kind or kind in e.tags]
    entries.sort(key=lambda e: e.at, reverse=True)
    today = local(now).date()
    by_day: dict[date, Day] = {}
    for entry in entries:
        d = local(entry.at).date()
        by_day.setdefault(d, Day(d, day_label(d, today))).entries.append(entry)
    return list(by_day.values()), counts


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
        seller=f"агентство «{listing.agency_name}»"
        if listing.agency_name
        else ("собственник" if listing.seller_type == "owner" else "продавец не указан"),
        url=listing.url,
        ppm=ppm(price.usd_per_m2) if price and price.usd_per_m2 else None,
    )


def _note(event, exposure, data: MarketData, object_id: int, now: datetime) -> str | None:
    days = exposure.days if exposure else None
    at_least = "как минимум " if exposure and exposure.at_least else ""
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


def object_rows(snap: Snapshot, *, district: str | None = None) -> list[ObjectRow]:
    data, now = snap.data, snap.now
    rows = []
    for object_id, obj in data.objects.items():
        listings = data.object_listings(object_id)
        if not listings:
            continue
        raw_district = data.object_district(object_id)
        if district and raw_district != district:
            continue
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


def timeline(snap: Snapshot, report) -> Timeline | None:
    points = [p for p in report.history if p.usd is not None]
    if not points or report.exposure is None:
        return None
    start = min(report.exposure.start, points[0].at)
    end = snap.now if report.exposure.state == "active" else report.exposure.end
    span = max((end - start).total_seconds(), 1)

    def x(at: datetime) -> float:
        return max(0.0, min(1000.0, (at - start).total_seconds() / span * 1000))

    values = [float(p.usd) for p in points]
    low, high = min(values), max(values)
    pad = max((high - low) * 0.25, high * 0.01)
    low, high = low - pad, high + pad

    def y(v: float) -> float:
        return 180 - (v - low) / (high - low) * 160

    path, dots = [], []
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
    return Timeline(" ".join(path), y_labels, x_labels, dots, list(by_day.values()))


@dataclass
class HistoryRow:
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
        seller = point.agency_name or ("собственник" if point.seller_type == "owner" else "—")
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


# --- Источники -----------------------------------------------------------------------------


def recent_runs(session: Session, now: datetime, limit: int = 40) -> list[SourceRun]:
    return list(
        session.scalars(
            select(SourceRun)
            .where(SourceRun.started_at >= now - timedelta(days=7))
            .order_by(SourceRun.started_at.desc())
            .limit(limit)
        )
    )
