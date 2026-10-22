"""Срок экспозиции объекта с учётом перевыкладываний.

Формула:
    начало  = min по надёжным (high/medium) объявлениям объекта из
              min(first_seen, posted_at_source), где posted_at_source берётся, только если
              он раньше first_seen и не старше `posted_at_max_age` до него;
    конец   = now, если хоть одно надёжное объявление активно, иначе max(last_seen);
    дней    = целых суток между началом и концом (перерывы между объявлениями входят:
              для покупателя квартира всё это время «продаётся»).
    «как минимум» — если первое объявление мы застали уже висящим (замечено в первые
              `truncation_margin` наблюдения площадки): раньше начала наблюдений мы не видим,
              а дата площадки не знает о прошлых перевыкладываниях.

Перевыкладывания — события `relisted` (тот же продавец снял и выложил заново ≤ 30 дней);
возвращения под новым объявлением — `returned` с `payload.new_listing = true`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from minsk_realty.analytics.config import DEFAULT_ANALYTICS, AnalyticsConfig
from minsk_realty.analytics.data import EventRow, ListingRow
from minsk_realty.matching.config import DEFAULT_CONFIG

ObjectState = Literal["active", "hidden", "removed"]


@dataclass(frozen=True)
class Exposure:
    start: datetime
    start_source: str
    # first_seen — мы сами заметили; posted_at_source — дата публикации площадки.
    start_basis: Literal["first_seen", "posted_at_source"]
    end: datetime
    days: int
    at_least: bool
    state: ObjectState  # hidden — все объявления неактивны, но 3 суток ещё не прошло
    relist_count: int
    returned_new_listing: int
    listings_count: int
    sources: tuple[str, ...]
    observed_since: datetime | None  # с какого момента наблюдаем площадку первого объявления
    # Сколько дней показывает сайт для текущего объявления (по его дате публикации).
    site_days: int | None = None


def listing_start(
    row: ListingRow, cfg: AnalyticsConfig = DEFAULT_ANALYTICS
) -> tuple[datetime, str]:
    posted = row.posted_at
    if posted is not None and posted < row.first_seen <= posted + cfg.posted_at_max_age:
        return posted, "posted_at_source"
    return row.first_seen, "first_seen"


def compute_exposure(
    listings: Sequence[ListingRow],
    events: Sequence[EventRow],
    now: datetime,
    source_starts: dict[str, datetime],
    *,
    current: ListingRow | None = None,
    cfg: AnalyticsConfig = DEFAULT_ANALYTICS,
) -> Exposure | None:
    """`listings` — объявления, по которым считаем (надёжные объявления объекта или одно
    объявление для склейки low). `events` — события объекта (берутся relisted/returned/removed).
    `current` — объявление, которое смотрит пользователь (для «на сайте N дней»)."""
    if not listings:
        return None
    starts = [(listing_start(r, cfg), r) for r in listings]
    (start, basis), first = min(starts, key=lambda item: (item[0][0], item[1].id))
    observed_since = source_starts.get(first.source)
    at_least = observed_since is None or first.first_seen <= observed_since + cfg.truncation_margin

    ids = {r.id for r in listings}
    active = any(r.active for r in listings)
    end = now if active else max(r.last_seen for r in listings)
    state: ObjectState = "active"
    if not active:
        last_state = [e for e in events if e.type in ("removed", "returned")]
        removed = bool(last_state) and last_state[-1].type == "removed"
        # То же правило, что у Emma (3 суток неактивности), — на случай, если событие ещё
        # не записано: события пересчитываются только для недавно виденных объектов.
        removed = removed or now - end >= DEFAULT_CONFIG.removed_after
        state = "removed" if removed else "hidden"

    relists = [e for e in events if e.type == "relisted" and e.listing_id in ids]
    returned_new = [
        e
        for e in events
        if e.type == "returned" and e.payload.get("new_listing") and e.listing_id in ids
    ]
    site_days = None
    if current is not None:
        site_days = max(0, (now - listing_start(current, cfg)[0]).days)
    return Exposure(
        start=start,
        start_source=first.source,
        start_basis=basis,  # type: ignore[arg-type]
        end=end,
        days=max(0, (end - start).days),
        at_least=at_least,
        state=state,
        relist_count=len(relists),
        returned_new_listing=len(returned_new),
        listings_count=len(listings),
        sources=tuple(sorted({r.source for r in listings})),
        observed_since=observed_since,
        site_days=site_days,
    )
