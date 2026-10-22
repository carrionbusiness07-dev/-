"""Срез БД для аналитики: объекты, объявления, снимки и события в памяти.

Аналитика читает БД одним набором запросов и дальше считает чистыми функциями — так каждую
цифру можно проверить тестом без БД, а сама выборка воспроизводится четырьмя SELECT-ами.

Состояние «на момент `at`» (для сравнения недель):
- объявление активно в `at`, если `first_seen ≤ at` и (оно активно сейчас или `last_seen ≥ at`);
- цена в `at` — последний снимок с ценой, у которого `seen_at ≤ at`.
Временные скрытия объявления внутри этого интервала не учитываются.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minsk_realty.analytics.fmt import normalize_district
from minsk_realty.db.models import Event, Listing, RealtyObject, Snapshot, SourceRun
from minsk_realty.matching.rules import TRUSTED


@dataclass(frozen=True)
class ListingRow:
    id: int
    object_id: int | None
    source: str
    external_id: str
    url: str
    confidence: str | None
    status: str
    first_seen: datetime
    last_seen: datetime
    posted_at: datetime | None
    seller_type: str
    agency_name: str | None
    seller_key: str | None

    @property
    def trusted(self) -> bool:
        return self.confidence in TRUSTED

    @property
    def active(self) -> bool:
        return self.status == "active"

    def active_at(self, at: datetime) -> bool:
        return self.first_seen <= at and (self.active or self.last_seen >= at)

    @property
    def seller_id(self) -> str:
        """Кто продаёт: агентство (юрлицо) или псевдоним продавца на площадке."""
        if self.agency_name:
            return "agency:" + " ".join(self.agency_name.lower().replace("ё", "е").split())
        if self.seller_key:
            return f"{self.source}:{self.seller_key}"
        return f"listing:{self.id}"


@dataclass(frozen=True)
class SnapRow:
    listing_id: int
    seen_at: datetime
    amount: Decimal | None
    currency: str | None
    usd: Decimal | None
    area: float | None
    rooms: int | None
    floor: int | None
    floors_total: int | None
    street: str | None
    district: str | None

    @property
    def priced(self) -> bool:
        return self.amount is not None and self.currency is not None

    @property
    def price_usd(self) -> Decimal | None:
        """USD для показа: пересчёт площадки, а для цены в долларах — сама цена."""
        if self.usd is not None:
            return self.usd
        return self.amount if self.currency == "USD" else None


@dataclass(frozen=True)
class ObjectRow:
    id: int
    street_norm: str | None
    house: str | None
    rooms: int | None
    area: float | None
    floor: int | None
    floors_total: int | None
    district: str | None
    microdistrict: str | None
    first_seen_at: datetime


@dataclass(frozen=True)
class EventRow:
    id: int
    object_id: int
    listing_id: int | None
    type: str
    at: datetime
    created_at: datetime
    notify: bool
    payload: dict[str, Any]

    @property
    def pct(self) -> float | None:
        value = self.payload.get("pct")
        return float(value) if value is not None else None


@dataclass(frozen=True)
class ObjectPrice:
    """Текущая цена объекта: одна точка на объект для медиан."""

    listing_id: int
    amount: Decimal
    currency: str
    usd: Decimal
    area: float | None
    rooms: int | None

    @property
    def usd_per_m2(self) -> float | None:
        return float(self.usd) / self.area if self.area else None


@dataclass
class MarketData:
    objects: dict[int, ObjectRow] = field(default_factory=dict)
    listings: dict[int, ListingRow] = field(default_factory=dict)
    snapshots: dict[int, list[SnapRow]] = field(default_factory=dict)  # по seen_at
    events: dict[int, list[EventRow]] = field(default_factory=dict)  # по объекту, по at
    source_starts: dict[str, datetime] = field(default_factory=dict)
    _by_object: dict[int, list[ListingRow]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.reindex()

    def reindex(self) -> None:
        by_object: dict[int, list[ListingRow]] = defaultdict(list)
        for row in self.listings.values():
            if row.object_id is not None:
                by_object[row.object_id].append(row)
        for rows in by_object.values():
            rows.sort(key=lambda r: (r.first_seen, r.id))
        self._by_object = dict(by_object)

    # --- Наблюдение ---

    @property
    def observation_start(self) -> datetime | None:
        return min(self.source_starts.values()) if self.source_starts else None

    # --- Объявления и снимки ---

    def object_listings(self, object_id: int, *, trusted_only: bool = True) -> list[ListingRow]:
        rows = self._by_object.get(object_id, [])
        return [r for r in rows if r.trusted] if trusted_only else list(rows)

    def last_snapshot(
        self, listing_id: int, at: datetime | None = None, *, priced: bool = False
    ) -> SnapRow | None:
        snaps = self.snapshots.get(listing_id, [])
        if at is not None:
            snaps = snaps[: bisect_right([s.seen_at for s in snaps], at)]
        for snap in reversed(snaps):
            if not priced or snap.priced:
                return snap
        return None

    def object_events(self, object_id: int, *, trusted_only: bool = True) -> list[EventRow]:
        """События объекта. trusted_only — без событий цены из склеек low (notify=False)."""
        rows = self.events.get(object_id, [])
        return [e for e in rows if e.notify] if trusted_only else list(rows)

    # --- Объект ---

    def object_active_at(self, object_id: int, at: datetime) -> bool:
        return any(r.active_at(at) for r in self.object_listings(object_id))

    def object_district(self, object_id: int) -> str | None:
        obj = self.objects.get(object_id)
        district = normalize_district(obj.district) if obj else None
        if district:
            return district
        # Realt района не отдаёт: берём из снимка любого надёжного объявления (Kufar).
        for row in self.object_listings(object_id):
            snap = self.last_snapshot(row.id)
            district = normalize_district(snap.district) if snap else None
            if district:
                return district
        return None

    def object_rooms(self, object_id: int) -> int | None:
        obj = self.objects.get(object_id)
        if obj and obj.rooms:
            return obj.rooms
        for row in self.object_listings(object_id):
            snap = self.last_snapshot(row.id)
            if snap and snap.rooms:
                return snap.rooms
        return None

    def object_price(self, object_id: int, at: datetime) -> ObjectPrice | None:
        """Цена объекта в `at`: минимальная последняя цена среди его активных надёжных
        объявлений (столько квартира стоит для покупателя). Только то, что переводится в USD."""
        obj = self.objects.get(object_id)
        best: ObjectPrice | None = None
        for row in self.object_listings(object_id):
            if not row.active_at(at):
                continue
            snap = self.last_snapshot(row.id, at, priced=True)
            if snap is None or snap.price_usd is None:
                continue
            price = ObjectPrice(
                listing_id=row.id,
                amount=snap.amount,
                currency=snap.currency,
                usd=snap.price_usd,
                area=snap.area or (obj.area if obj else None),
                rooms=(obj.rooms if obj and obj.rooms else snap.rooms),
            )
            if best is None or price.usd < best.usd:
                best = price
        return best


def load_market(
    session: Session,
    *,
    object_ids: Iterable[int] | None = None,
    listing_ids: Iterable[int] | None = None,
) -> MarketData:
    """Загрузить срез. Без фильтров — весь рынок; с фильтрами — объекты и/или объявления."""
    obj_q = select(RealtyObject)
    lst_q = select(Listing)
    ev_q = select(Event).order_by(Event.at, Event.id)
    if object_ids is not None or listing_ids is not None:
        oids = set(object_ids or ())
        lids = set(listing_ids or ())
        obj_q = obj_q.where(RealtyObject.id.in_(oids))
        lst_q = lst_q.where(Listing.object_id.in_(oids) | Listing.id.in_(lids))
        ev_q = ev_q.where(Event.object_id.in_(oids))

    objects = {
        o.id: ObjectRow(
            id=o.id,
            street_norm=o.street_norm,
            house=o.house,
            rooms=o.rooms,
            area=o.area_total,
            floor=o.floor,
            floors_total=o.floors_total,
            district=o.district,
            microdistrict=o.microdistrict,
            first_seen_at=o.first_seen_at,
        )
        for o in session.scalars(obj_q)
    }
    listings = {
        r.id: ListingRow(
            id=r.id,
            object_id=r.object_id,
            source=r.source,
            external_id=r.external_id,
            url=r.url,
            confidence=r.match_confidence,
            status=r.status,
            first_seen=r.first_seen,
            last_seen=r.last_seen,
            posted_at=r.posted_at_source,
            seller_type=r.seller_type,
            agency_name=r.agency_name,
            seller_key=r.seller_key,
        )
        for r in session.scalars(lst_q)
    }
    snapshots: dict[int, list[SnapRow]] = defaultdict(list)
    snap_q = select(Snapshot).order_by(Snapshot.listing_id, Snapshot.seen_at, Snapshot.id)
    if object_ids is not None or listing_ids is not None:
        snap_q = snap_q.where(Snapshot.listing_id.in_(set(listings)))
    for s in session.scalars(snap_q):
        snapshots[s.listing_id].append(
            SnapRow(
                listing_id=s.listing_id,
                seen_at=s.seen_at,
                amount=s.price_amount,
                currency=s.price_currency,
                usd=s.price_usd,
                area=s.area_total if s.area_total and s.area_total > 0 else None,
                rooms=s.rooms if s.rooms and s.rooms > 0 else None,
                floor=s.floor,
                floors_total=s.floors_total,
                street=s.street,
                district=s.district,
            )
        )
    events: dict[int, list[EventRow]] = defaultdict(list)
    for e in session.scalars(ev_q):
        events[e.object_id].append(
            EventRow(
                id=e.id,
                object_id=e.object_id,
                listing_id=e.listing_id,
                type=e.type,
                at=e.at,
                created_at=e.created_at,
                notify=e.notify,
                payload=dict(e.payload or {}),
            )
        )
    return MarketData(
        objects=objects,
        listings=listings,
        snapshots=dict(snapshots),
        events=dict(events),
        source_starts=source_starts(session),
    )


def source_starts(session: Session) -> dict[str, datetime]:
    """С какого момента мы наблюдаем каждую площадку.

    Первый успешный проход сборщика, а если журнала нет (импорт, тесты) — первое замеченное
    объявление площадки. Берём более раннее из двух.
    """
    out: dict[str, datetime] = {}
    runs = session.execute(
        select(SourceRun.source, func.min(SourceRun.started_at))
        .where(SourceRun.status == "ok")
        .group_by(SourceRun.source)
    ).all()
    seen = session.execute(
        select(Listing.source, func.min(Listing.first_seen)).group_by(Listing.source)
    ).all()
    for source, at in [*runs, *seen]:
        if at is None:
            continue
        at = as_utc(at)
        out[source] = min(out.get(source, at), at)
    return out


def as_utc(value: datetime | str) -> datetime:
    # func.min() возвращает «сырое» значение без TypeDecorator: в SQLite оно наивное и бывает
    # строкой.
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
