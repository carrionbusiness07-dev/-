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
from itertools import pairwise
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minsk_realty.analytics.districts import district_at
from minsk_realty.analytics.fmt import normalize_district
from minsk_realty.analytics.geo import valid_point
from minsk_realty.db.models import Event, Listing, RealtyObject, Snapshot, SourceRun
from minsk_realty.matching.config import DEFAULT_CONFIG
from minsk_realty.matching.events import baseline_ends
from minsk_realty.matching.rules import TRUSTED
from minsk_realty.sources.realt import REALT_REPAIR_LABELS


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
    address_raw: str | None = None
    lat: float | None = None
    lon: float | None = None

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
    # Из надёжной склейки (high/medium). notify уже не годится как признак надёжности:
    # старые события и события поздней склейки надёжны, но без уведомления.
    trusted: bool = True

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
    # Объявления, у которых сама площадка показывает снижение цены, в том числе случившееся
    # до начала наших наблюдений (Realt — направление последнего изменения, Domovita —
    # история цены). Только для проверки гипотезы: история до 23.10 у нас обрезана.
    platform_drops: frozenset[int] = frozenset()
    # Конец исходного среза по площадкам (matching.events.baseline_ends): объекты, впервые
    # замеченные позже, мы видим с самого появления на рынке.
    baseline_ends: dict[str, datetime] = field(default_factory=dict)
    # Год постройки по объекту (из самого свежего снимка, где он есть).
    building_years: dict[int, int] = field(default_factory=dict)
    # Отделка по объекту: "none" (без отделки) или "done" (любой ремонт); неизвестная — нет ключа.
    finishings: dict[int, str] = field(default_factory=dict)
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

    def listing_point(
        self, listing_id: int, approx_sources: frozenset[str]
    ) -> tuple[float, float, bool] | None:
        """Координаты по последнему снимку с lat/lon; approx — площадка даёт общую точку двора."""
        row = self.listings.get(listing_id)
        if row is None:
            return None
        found = self._last_point(listing_id)
        return (found[1], found[2], row.source in approx_sources) if found else None

    def object_point(
        self, object_id: int, approx_sources: frozenset[str]
    ) -> tuple[float, float, bool] | None:
        """Координаты объекта по надёжным объявлениям: сначала точные площадки, затем любые;
        среди равных — самый свежий снимок."""
        best: dict[bool, tuple[datetime, float, float]] = {}
        for row in self.object_listings(object_id):
            found = self._last_point(row.id)
            approx = row.source in approx_sources
            if found and (approx not in best or found > best[approx]):
                best[approx] = found
        for approx in (False, True):
            if approx in best:
                return best[approx][1], best[approx][2], approx
        return None

    def _last_point(self, listing_id: int) -> tuple[datetime, float, float] | None:
        for snap in reversed(self.snapshots.get(listing_id, [])):
            if snap.lat is not None and snap.lon is not None:
                # Последняя точка не в Минске — объявление пропускаем целиком.
                if valid_point(snap.lat, snap.lon):
                    return snap.seen_at, snap.lat, snap.lon
                return None
        return None

    def object_events(self, object_id: int, *, trusted_only: bool = True) -> list[EventRow]:
        """События объекта. trusted_only — без событий цены из склеек low (trusted=False)."""
        rows = self.events.get(object_id, [])
        return [e for e in rows if e.trusted] if trusted_only else list(rows)

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
        # Последний запасной вариант — по точке; неточная (двор, улица) для района годится.
        point = self.object_point(object_id, frozenset())
        return district_at(point[0], point[1]) if point else None

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
    # Только нужные колонки, без ORM-объектов: на всём рынке (десятки тысяч строк) это
    # в несколько раз быстрее, а JSON-поля снимков (extra) вообще не читаются.
    obj_q = select(
        RealtyObject.id,
        RealtyObject.street_norm,
        RealtyObject.house,
        RealtyObject.rooms,
        RealtyObject.area_total,
        RealtyObject.floor,
        RealtyObject.floors_total,
        RealtyObject.district,
        RealtyObject.microdistrict,
        RealtyObject.first_seen_at,
    )
    lst_q = select(
        Listing.id,
        Listing.object_id,
        Listing.source,
        Listing.external_id,
        Listing.url,
        Listing.match_confidence,
        Listing.status,
        Listing.first_seen,
        Listing.last_seen,
        Listing.posted_at_source,
        Listing.seller_type,
        Listing.agency_name,
        Listing.seller_key,
    )
    # Отозванные события (объявление отцепили от объекта) — не факты, в историю не идут.
    ev_q = (
        select(
            Event.id,
            Event.object_id,
            Event.listing_id,
            Event.type,
            Event.at,
            Event.created_at,
            Event.notify,
            Event.payload,
            Event.trusted,
        )
        .where(Event.retracted_at.is_(None))
        .order_by(Event.at, Event.id)
    )
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
        for o in session.execute(obj_q)
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
        for r in session.execute(lst_q)
    }
    snapshots: dict[int, list[SnapRow]] = defaultdict(list)
    snap_q = select(
        Snapshot.listing_id,
        Snapshot.seen_at,
        Snapshot.price_amount,
        Snapshot.price_currency,
        Snapshot.price_usd,
        Snapshot.area_total,
        Snapshot.rooms,
        Snapshot.floor,
        Snapshot.floors_total,
        Snapshot.street,
        Snapshot.district,
        Snapshot.address_raw,
        Snapshot.lat,
        Snapshot.lon,
    ).order_by(Snapshot.listing_id, Snapshot.seen_at, Snapshot.id)
    if object_ids is not None or listing_ids is not None:
        snap_q = snap_q.where(Snapshot.listing_id.in_(set(listings)))
    for s in session.execute(snap_q):
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
                address_raw=s.address_raw,
                lat=s.lat,
                lon=s.lon,
            )
        )
    events: dict[int, list[EventRow]] = defaultdict(list)
    for e in session.execute(ev_q):
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
                trusted=e.trusted,
            )
        )
    full = object_ids is None and listing_ids is None
    return MarketData(
        objects=objects,
        listings=listings,
        snapshots=dict(snapshots),
        events=dict(events),
        source_starts=source_starts(session),
        platform_drops=platform_reported_drops(session) if full else frozenset(),
        baseline_ends=baseline_ends(session, DEFAULT_CONFIG) if full else {},
        # Год читает extra, поэтому отдельным лёгким запросом и только по нужным объектам.
        building_years=building_years(
            session, None if full else {r.object_id for r in listings.values() if r.object_id}
        ),
        finishings=finishings(
            session, None if full else {r.object_id for r in listings.values() if r.object_id}
        ),
    )


YEAR_MIN = 1850
YEAR_KEYS = {"kufar": "year_built", "realt": "buildingYear"}


def parse_year(value: Any, now_year: int) -> int | None:
    """Год постройки: число или строка из цифр в пределах [1850, now_year + 10], иначе None."""
    if isinstance(value, str) and value.strip().isdecimal():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if YEAR_MIN <= value <= now_year + 10 else None


def building_years(session: Session, object_ids: Iterable[int] | None = None) -> dict[int, int]:
    """Год постройки по объектам: из самого свежего снимка его объявлений, где год есть."""
    q = (
        select(Listing.object_id, Listing.source, Snapshot.extra)
        .join(Listing, Listing.id == Snapshot.listing_id)
        .where(
            Listing.source.in_(tuple(YEAR_KEYS)),
            Listing.object_id.is_not(None),
            Listing.match_confidence.in_(TRUSTED),
        )
        .order_by(Snapshot.seen_at, Snapshot.id)
    )
    if object_ids is not None:
        q = q.where(Listing.object_id.in_(set(object_ids)))
    now_year = datetime.now(UTC).year
    out: dict[int, int] = {}
    for object_id, source, extra in session.execute(q):
        year = parse_year((extra or {}).get(YEAR_KEYS[source]), now_year)
        if year is not None:
            out[object_id] = year  # порядок по возрастанию seen_at: последний выигрывает
    return out


NO_FINISH_MARKERS = ("без отделки", "строительн", "чернов")


def finishing_kind(label: str | None) -> Literal["none", "done"] | None:
    """Отделка по подписи площадки: без отделки / любой ремонт / неизвестна."""
    text = (label or "").strip().lower()
    if not text:
        return None
    return "none" if any(m in text for m in NO_FINISH_MARKERS) else "done"


def _finishing_label(source: str, extra: dict[str, Any] | None) -> str | None:
    extra = extra or {}
    if source == "kufar":
        label = extra.get("repair")
        return label if isinstance(label, str) else None
    code = extra.get("repairState")  # Realt: код, подпись из словаря самого Realt
    return REALT_REPAIR_LABELS.get(code) if isinstance(code, int) else None


def finishings(session: Session, object_ids: Iterable[int] | None = None) -> dict[int, str]:
    """Отделка по объектам: из самого свежего снимка надёжных объявлений, где она известна."""
    q = (
        select(Listing.object_id, Listing.source, Snapshot.extra)
        .join(Listing, Listing.id == Snapshot.listing_id)
        .where(
            Listing.source.in_(("kufar", "realt")),
            Listing.object_id.is_not(None),
            Listing.match_confidence.in_(TRUSTED),
        )
        .order_by(Snapshot.seen_at, Snapshot.id)
    )
    if object_ids is not None:
        q = q.where(Listing.object_id.in_(set(object_ids)))
    out: dict[int, str] = {}
    for object_id, source, extra in session.execute(q):
        kind = finishing_kind(_finishing_label(source, extra))
        if kind is not None:
            out[object_id] = kind  # порядок по возрастанию seen_at: последний выигрывает
    return out


# Снижение по истории Domovita: история в долларах, а продавец мог ставить цену в рублях —
# меньше процента может оказаться курсом, а не решением продавца.
PLATFORM_DROP_MIN = 0.99


def platform_reported_drops(session: Session) -> frozenset[int]:
    """Объявления, у которых площадка сама показывает снижение цены (последний снимок)."""
    rows = session.execute(
        select(Snapshot.listing_id, Snapshot.extra)
        .join(Listing, Listing.id == Snapshot.listing_id)
        .where(Listing.source.in_(("realt", "domovita")))
        .order_by(Snapshot.listing_id, Snapshot.seen_at, Snapshot.id)
    )
    latest = {listing_id: extra or {} for listing_id, extra in rows}
    return frozenset(lid for lid, extra in latest.items() if _platform_drop(extra))


def _platform_drop(extra: dict[str, Any]) -> bool:
    if extra.get("priceChangeDirection") == -1:  # Realt: последнее изменение цены — снижение
        return True
    usd = [
        row[1]
        for row in extra.get("price_history") or []
        if isinstance(row, list | tuple) and len(row) == 2 and isinstance(row[1], int | float)
    ]
    # Domovita: история от новых к старым.
    return any(newer < older * PLATFORM_DROP_MIN for newer, older in pairwise(usd))


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
