"""История цены объекта: точки из снимков, по всем надёжным объявлениям, по времени.

Точка — первая цена объявления или её изменение. Изменение считаем так же, как Вера считает
события цены (matching/events.py): в исходной валюте, от последней показанной точки, не меньше
`price_change_min_pct` (0.5%); смена валюты — новая точка без процента. USD — только для показа.
Число снижений и повышений берём из событий (`price_drop` / `price_up`), а не из точек, чтобы
отчёт и уведомления не расходились.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from minsk_realty.analytics.data import EventRow, ListingRow, SnapRow
from minsk_realty.matching.config import DEFAULT_CONFIG

# first — первая цена объекта; change — изменение цены; currency — смена валюты;
# new_listing — новое объявление вместо снятого (перевыкладывание, возвращение);
# parallel — ещё одно объявление, пока висят другие (другая площадка или продавец).
PointKind = Literal["first", "change", "new_listing", "parallel", "currency"]


@dataclass(frozen=True)
class PricePoint:
    at: datetime
    amount: Decimal
    currency: str
    usd: Decimal | None
    source: str
    listing_id: int
    seller_type: str
    agency_name: str | None
    kind: PointKind
    # К предыдущей точке того же объявления (для new_listing — к последней цене объявления,
    # которое этот продавец снял перед перевыкладыванием). В исходной валюте, %.
    change_pct: float | None
    is_current_listing: bool


def build_price_history(
    listings: Sequence[ListingRow],
    snapshots: dict[int, list[SnapRow]],
    events: Sequence[EventRow] = (),
    *,
    current_listing_id: int | None = None,
    min_pct: Decimal = DEFAULT_CONFIG.price_change_min_pct,
) -> list[PricePoint]:
    previous_of = {
        e.listing_id: e.payload.get("previous_listing_id")
        for e in events
        if e.type == "relisted" and e.listing_id is not None
    }
    last_price: dict[int, SnapRow] = {}
    points: list[PricePoint] = []
    ordered = sorted(listings, key=lambda r: (r.first_seen, r.id))
    for index, row in enumerate(ordered):
        base: SnapRow | None = None
        for snap in snapshots.get(row.id, []):
            if not snap.priced:
                continue
            kind: PointKind
            change: float | None = None
            if base is None:
                kind = "first" if index == 0 else _appearance(row, ordered[:index], previous_of)
                prev = last_price.get(previous_of.get(row.id))  # type: ignore[arg-type]
                if prev is not None and prev.currency == snap.currency:
                    change = _pct(prev.amount, snap.amount)
            elif snap.currency != base.currency:
                kind = "currency"
            else:
                raw = (snap.amount - base.amount) / base.amount * 100
                if abs(raw) < min_pct:
                    continue
                kind, change = "change", round(float(raw), 2)
            base = snap
            points.append(
                PricePoint(
                    at=snap.seen_at,
                    amount=snap.amount,
                    currency=snap.currency,
                    usd=snap.price_usd,
                    source=row.source,
                    listing_id=row.id,
                    seller_type=row.seller_type,
                    agency_name=row.agency_name,
                    kind=kind,
                    change_pct=change,
                    is_current_listing=row.id == current_listing_id,
                )
            )
        if base is not None:
            last_price[row.id] = base
    points.sort(key=lambda p: (p.at, p.listing_id))
    return points


def _appearance(row: ListingRow, earlier: Sequence[ListingRow], previous_of: dict) -> PointKind:
    if row.id in previous_of:
        return "new_listing"
    overlapping = any(r.first_seen <= row.first_seen <= r.last_seen for r in earlier)
    return "parallel" if overlapping else "new_listing"


def _pct(old: Decimal, new: Decimal) -> float:
    return round(float((new - old) / old * 100), 2)


@dataclass(frozen=True)
class PriceMoves:
    drops: int
    ups: int
    last_drop_at: datetime | None
    # Суммарное изменение от первой цены до текущей по цепочке, если валюта не менялась, %.
    total_change_pct: float | None


def price_moves(
    events: Sequence[EventRow], listing_ids: set[int], history: Sequence[PricePoint]
) -> PriceMoves:
    drops = [e for e in events if e.type == "price_drop" and e.listing_id in listing_ids]
    ups = [e for e in events if e.type == "price_up" and e.listing_id in listing_ids]
    total = None
    if history:
        first, last = history[0], _current_point(history)
        if first.currency == last.currency and first.amount:
            total = _pct(first.amount, last.amount)
    return PriceMoves(
        drops=len(drops),
        ups=len(ups),
        last_drop_at=max((e.at for e in drops), default=None),
        total_change_pct=total,
    )


def _current_point(history: Sequence[PricePoint]) -> PricePoint:
    current = [p for p in history if p.is_current_listing]
    return current[-1] if current else history[-1]
