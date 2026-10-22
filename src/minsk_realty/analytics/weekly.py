"""Недельная сводка для канала (docs/02, раздел 1.4) и общие счётчики за период.

Период — [now − 7 дней, now). Все события берутся по `at` (когда случилось на рынке) и только
надёжные (`trusted=True`: без событий цены из склеек low).

- Медиана $/м² — по Минску на `now` и на `now − 7 дней` (см. medians.py), изменение в %.
- Подешевели — объекты с хотя бы одним `price_drop` за период; шаг — медиана `payload.pct`
  событий (медиана, а не среднее: одна исправленная опечатка в цене не сдвигает её).
- Срок экспозиции — медиана по активным объектам на `now`.
- Перевыложено «как новые» — доля надёжных объявлений, впервые замеченных за период, у которых
  есть `relisted` или `returned` с новым объявлением.
- Топ снижений — по объявлению: первая цена до первого события периода → последняя цена после
  последнего, в исходной валюте; на объект — самое сильное снижение. Снижения больше
  `max_public_drop_pct` в топ не попадают (скорее исправленная опечатка).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from minsk_realty.analytics.config import DEFAULT_ANALYTICS, AnalyticsConfig
from minsk_realty.analytics.data import EventRow, MarketData, load_market
from minsk_realty.analytics.fmt import normalize_district, street_label
from minsk_realty.analytics.medians import MarketBaseline, MedianCell, compute_baseline

WEEK = timedelta(days=7)


@dataclass(frozen=True)
class TopDrop:
    object_id: int
    listing_id: int
    source: str
    url: str
    rooms: int | None
    street: str | None
    district: str | None
    old_amount: Decimal
    new_amount: Decimal
    currency: str
    pct: float
    at: datetime


@dataclass(frozen=True)
class PeriodCounts:
    start: datetime
    end: datetime
    new_objects: int
    drop_events: int
    dropped_objects: int
    drop_step_median_pct: float | None
    removed_objects: int
    new_listings: int  # надёжные объявления, впервые замеченные за период
    relisted_listings: int  # из них — перевыложенные «как новые»

    @property
    def relisted_share(self) -> float | None:
        return self.relisted_listings / self.new_listings if self.new_listings else None


@dataclass(frozen=True)
class WeeklySummary:
    year: int
    week: int
    period_start: datetime
    period_end: datetime
    observation_start: datetime | None
    median: MedianCell | None
    median_prev: MedianCell | None
    median_change_pct: float | None
    counts: PeriodCounts
    exposure_median: MedianCell | None
    exposure_truncated_share: float | None
    top_drops: tuple[TopDrop, ...]
    districts: tuple[MedianCell, ...]


def _in(event: EventRow, start: datetime, end: datetime) -> bool:
    return start <= event.at < end


def period_counts(data: MarketData, start: datetime, end: datetime) -> PeriodCounts:
    drops: list[EventRow] = []
    new_objects = removed = 0
    relisted_ids: set[int] = set()
    for object_id in data.objects:
        for e in data.object_events(object_id):
            relist = e.type == "relisted" or (e.type == "returned" and e.payload.get("new_listing"))
            if relist and e.listing_id is not None:
                relisted_ids.add(e.listing_id)
            if not _in(e, start, end):
                continue
            if e.type == "price_drop":
                drops.append(e)
            elif e.type == "new":
                new_objects += 1
            elif e.type == "removed":
                removed += 1
    new_listings = [r for r in data.listings.values() if r.trusted and start <= r.first_seen < end]
    steps = [e.pct for e in drops if e.pct is not None]
    return PeriodCounts(
        start=start,
        end=end,
        new_objects=new_objects,
        drop_events=len(drops),
        dropped_objects=len({e.object_id for e in drops}),
        drop_step_median_pct=statistics.median(steps) if steps else None,
        removed_objects=removed,
        new_listings=len(new_listings),
        relisted_listings=sum(1 for r in new_listings if r.id in relisted_ids),
    )


def top_drops(
    data: MarketData,
    start: datetime,
    end: datetime,
    *,
    cfg: AnalyticsConfig = DEFAULT_ANALYTICS,
    limit: int | None = None,
) -> list[TopDrop]:
    best: dict[int, TopDrop] = {}
    for object_id in data.objects:
        by_listing: dict[int, list[EventRow]] = defaultdict(list)
        for e in data.object_events(object_id):
            if e.type in ("price_drop", "price_up") and e.listing_id and _in(e, start, end):
                by_listing[e.listing_id].append(e)
        for listing_id, events in by_listing.items():
            first, last = events[0], events[-1]
            if first.payload.get("currency") != last.payload.get("currency"):
                continue
            old, new = Decimal(first.payload["old"]), Decimal(last.payload["new"])
            if not old or new >= old:
                continue
            change = float((new - old) / old * 100)
            if abs(change) > cfg.max_public_drop_pct:
                continue
            if object_id in best and best[object_id].pct <= change:
                continue
            best[object_id] = _top_drop(data, object_id, listing_id, old, new, last, change)
    out = sorted(best.values(), key=lambda d: (d.pct, d.object_id))
    return out[:limit] if limit else out


def _top_drop(data, object_id, listing_id, old, new, last, change) -> TopDrop:
    row = data.listings[listing_id]
    if not row.active:  # ссылку даём на живое объявление, если оно есть
        alive = [r for r in data.object_listings(object_id) if r.active]
        row = alive[0] if alive else row
    obj = data.objects.get(object_id)
    snap = data.last_snapshot(row.id)
    return TopDrop(
        object_id=object_id,
        listing_id=row.id,
        source=row.source,
        url=row.url,
        rooms=data.object_rooms(object_id),
        street=street_label(obj.street_norm) if obj else None,
        district=data.object_district(object_id)
        or (normalize_district(snap.district) if snap else None),
        old_amount=old,
        new_amount=new,
        currency=last.payload.get("currency"),
        pct=round(change, 1),
        at=last.at,
    )


def compute_weekly(
    data: MarketData,
    now: datetime,
    *,
    baseline: MarketBaseline | None = None,
    cfg: AnalyticsConfig = DEFAULT_ANALYTICS,
) -> WeeklySummary:
    start = now - WEEK
    baseline = baseline or compute_baseline(data, now, cfg=cfg)
    previous = compute_baseline(data, start, cfg=cfg)
    median, median_prev = baseline.city(), previous.city()
    change = None
    if median and median.enough and median_prev and median_prev.enough:
        change = (median.median / median_prev.median - 1) * 100
    year, week, _ = (now - timedelta(seconds=1)).isocalendar()
    return WeeklySummary(
        year=year,
        week=week,
        period_start=start,
        period_end=now,
        observation_start=data.observation_start,
        median=median,
        median_prev=median_prev,
        median_change_pct=change,
        counts=period_counts(data, start, now),
        exposure_median=baseline.exposure_cell(None),
        exposure_truncated_share=baseline.exposure_truncated_share,
        top_drops=tuple(top_drops(data, start, now, cfg=cfg, limit=cfg.top_drops)),
        districts=tuple(baseline.districts()),
    )


def weekly_summary(
    session: Session,
    now: datetime | None = None,
    *,
    cfg: AnalyticsConfig = DEFAULT_ANALYTICS,
) -> WeeklySummary:
    return compute_weekly(load_market(session), now or datetime.now(UTC), cfg=cfg)
