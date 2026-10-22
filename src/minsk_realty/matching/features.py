"""Признаки объявления для склейки: последние известные поля снимков + нормализованный адрес
+ хеши фото (без «типовых»)."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minsk_realty.db.models import Listing, PhotoHash, Snapshot
from minsk_realty.matching.address import (
    Street,
    normalize_house,
    normalize_street,
    parse_address,
    streets_match,
)
from minsk_realty.matching.config import MatchingConfig
from minsk_realty.matching.photos import is_informative


@dataclass
class ListingFeatures:
    listing_id: int
    source: str
    external_id: str
    status: str
    first_seen: datetime
    last_seen: datetime
    seller_type: str
    seller_key: str | None
    agency: str | None
    object_id: int | None
    confidence: str | None

    rooms: int | None = None
    area: float | None = None
    floor: int | None = None
    floors_total: int | None = None
    street: Street | None = None
    house: str | None = None
    town: str | None = None
    district: str | None = None
    lat: float | None = None
    lon: float | None = None
    # Откуда взят адрес: поля площадки или разбор свободной строки (для разбора склейки).
    address_from: str = "нет"
    phashes: frozenset[str] = field(default_factory=frozenset)
    # Сколько хешей отброшено как «типовые» (фасад, заглушка) — для разбора склейки.
    popular_phashes: int = 0

    @property
    def label(self) -> str:
        return f"#{self.listing_id} {self.source}/{self.external_id}"

    @property
    def has_coords(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def active(self) -> bool:
        return self.status == "active"


# Поля снимка, из которых строятся признаки. Пустое значение в свежем снимке не стирает
# известное: разовый сбой разбора (нет площади, нет адреса) не должен отцеплять объявление.
_FIELDS = (
    "rooms",
    "area_total",
    "floor",
    "floors_total",
    "street",
    "house",
    "address_raw",
    "district",
    "town",
)
_CHUNK = 500  # параметров в одном IN (SQLite ограничивает число переменных)


@dataclass
class KnownFields:
    """Последние известные непустые значения полей по всем снимкам объявления."""

    rooms: int | None = None
    area_total: float | None = None
    floor: int | None = None
    floors_total: int | None = None
    street: str | None = None
    house: str | None = None
    address_raw: str | None = None
    district: str | None = None
    town: str | None = None
    lat: float | None = None
    lon: float | None = None

    def update(self, snap: Any) -> None:
        """Наложить более свежий снимок: непустые значения заменяют известные."""
        for name in _FIELDS:
            value = getattr(snap, name)
            if name in ("rooms", "area_total") and value is not None and value <= 0:
                value = None
            if isinstance(value, str) and not value.strip():
                value = None
            if value is not None:
                setattr(self, name, value)
        if snap.lat is not None and snap.lon is not None and _plausible(snap.lat, snap.lon):
            self.lat, self.lon = snap.lat, snap.lon


def latest_snapshot(session: Session, listing_id: int) -> Snapshot | None:
    return session.scalar(
        select(Snapshot)
        .where(Snapshot.listing_id == listing_id)
        .order_by(Snapshot.seen_at.desc(), Snapshot.id.desc())
        .limit(1)
    )


def known_fields(session: Session, listing_ids: Iterable[int]) -> dict[int, KnownFields]:
    """Последние известные значения полей для многих объявлений (пакетами, без N+1)."""
    ids = sorted(set(listing_ids))
    columns = [getattr(Snapshot, name) for name in _FIELDS]
    out: dict[int, KnownFields] = {}
    for i in range(0, len(ids), _CHUNK):
        rows = session.execute(
            select(Snapshot.listing_id, *columns, Snapshot.lat, Snapshot.lon)
            .where(Snapshot.listing_id.in_(ids[i : i + _CHUNK]))
            .order_by(Snapshot.listing_id, Snapshot.seen_at, Snapshot.id)
        )
        for row in rows:
            out.setdefault(row.listing_id, KnownFields()).update(row)
    return out


def photo_hashes(session: Session, listing_ids: Iterable[int]) -> dict[int, set[str]]:
    ids = sorted(set(listing_ids))
    out: dict[int, set[str]] = {}
    for i in range(0, len(ids), _CHUNK):
        rows = session.execute(
            select(PhotoHash.listing_id, PhotoHash.phash).where(
                PhotoHash.listing_id.in_(ids[i : i + _CHUNK])
            )
        )
        for listing_id, phash in rows:
            out.setdefault(listing_id, set()).add(phash)
    return out


def _norm_text(value: str | None) -> str | None:
    if not value:
        return None
    return " ".join(value.lower().replace("ё", "е").split()) or None


def build_features(
    listing: Listing,
    snap: Snapshot | KnownFields | None,
    phashes: set[str],
    popular: frozenset[str] | set[str] = frozenset(),
) -> ListingFeatures:
    """`snap` — известные поля объявления (`KnownFields`) или один снимок."""
    f = ListingFeatures(
        listing_id=listing.id,
        source=listing.source,
        external_id=listing.external_id,
        status=listing.status,
        first_seen=listing.first_seen,
        last_seen=listing.last_seen,
        seller_type=listing.seller_type,
        seller_key=listing.seller_key,
        agency=_norm_text(listing.agency_name),
        object_id=listing.object_id,
        confidence=listing.match_confidence,
        phashes=frozenset(h for h in phashes if is_informative(h) and h not in popular),
        popular_phashes=len({h for h in phashes if h in popular}),
    )
    if snap is None:
        return f
    f.rooms = snap.rooms if snap.rooms and snap.rooms > 0 else None
    f.area = snap.area_total if snap.area_total and snap.area_total > 0 else None
    f.floor = snap.floor
    f.floors_total = snap.floors_total
    f.town = _norm_text(snap.town)
    f.district = _norm_text(snap.district)
    if snap.lat is not None and snap.lon is not None and _plausible(snap.lat, snap.lon):
        f.lat, f.lon = snap.lat, snap.lon

    f.street = normalize_street(snap.street)
    f.house = normalize_house(snap.house)
    if f.street or f.house:
        f.address_from = "поля площадки"
    if (f.street is None or f.house is None) and snap.address_raw:
        parsed = parse_address(snap.address_raw)
        # Дом из строки берём, только если улица в строке та же (или улицы в полях нет).
        same_street = f.street is None or streets_match(f.street, parsed.street) is True
        if same_street:
            if f.street is None and parsed.street is not None:
                f.street = parsed.street
                f.address_from = "строка адреса"
            if f.house is None and parsed.house is not None:
                f.house = parsed.house
                f.address_from = (
                    "строка адреса" if f.address_from == "нет" else "поля + строка адреса"
                )
    return f


def _plausible(lat: float, lon: float) -> bool:
    # Минск и окрестности. Перепутанные lat/lon или (0, 0) не должны участвовать в сравнении.
    return 53.6 <= lat <= 54.2 and 27.2 <= lon <= 27.9


def load_features(session: Session, listing: Listing) -> ListingFeatures:
    """Признаки одного объявления без учёта «типовых» фото (для них нужен `FeatureCache`)."""
    known = known_fields(session, [listing.id]).get(listing.id)
    hashes = set(session.scalars(select(PhotoHash.phash).where(PhotoHash.listing_id == listing.id)))
    return build_features(listing, known, hashes)


def popular_hashes(
    session: Session, cfg: MatchingConfig, hashes: Iterable[str] | None = None
) -> set[str]:
    """«Типовые» фото: один и тот же хеш у объявлений заведомо разных квартир.

    Разные квартиры — жёсткое противоречие признаков (комнаты, площадь вне допуска, этаж,
    этажность, улица, дом, координаты дальше радиуса противоречия). Так ловятся фасад и двор,
    которыми агентство иллюстрирует все квартиры дома, и заглушки «фото скоро будет». Одна
    квартира на нескольких площадках или перевыложенная с теми же фото противоречий не даёт
    и типовой не становится. `hashes=None` — проверить все хеши в БД.
    """
    from minsk_realty.matching.rules import hard_conflicts  # rules импортирует features

    shared = select(PhotoHash.phash).group_by(PhotoHash.phash).having(func.count() > 1)
    if hashes is not None:
        wanted = sorted(set(hashes))
        if not wanted:
            return set()
        chunks = [wanted[i : i + _CHUNK] for i in range(0, len(wanted), _CHUNK)]
    else:
        chunks = [None]
    groups: dict[str, set[int]] = {}
    for chunk in chunks:
        sub = shared if chunk is None else shared.where(PhotoHash.phash.in_(chunk))
        rows = session.execute(
            select(PhotoHash.phash, PhotoHash.listing_id).where(PhotoHash.phash.in_(sub))
        )
        for phash, listing_id in rows:
            groups.setdefault(phash, set()).add(listing_id)
    if not groups:
        return set()
    all_ids = sorted(set().union(*groups.values()))
    known = known_fields(session, all_ids)
    listings = {
        item.id: item
        for i in range(0, len(all_ids), _CHUNK)
        for item in session.scalars(select(Listing).where(Listing.id.in_(all_ids[i : i + _CHUNK])))
    }
    attrs = {
        lid: build_features(listings[lid], known.get(lid), set())
        for lid in all_ids
        if lid in listings
    }
    out = set()
    for phash, ids_ in groups.items():
        feats = [attrs[i] for i in sorted(ids_) if i in attrs]
        if len(feats) > cfg.phash_popular_check_limit:
            if _spread_conflict(feats, cfg):
                out.add(phash)
            continue
        if any(hard_conflicts(a, b, cfg) for j, a in enumerate(feats) for b in feats[j + 1 :]):
            out.add(phash)
    return out


def _spread_conflict(feats: list[ListingFeatures], cfg: MatchingConfig) -> bool:
    """Без попарного сравнения: разброс комнат, этажей или площади больше допуска."""
    rooms = {f.rooms for f in feats if f.rooms}
    floors = {f.floor for f in feats if f.floor is not None}
    areas = [f.area for f in feats if f.area]
    spread = max(areas) - min(areas) if areas else 0.0
    return len(rooms) > 1 or len(floors) > 1 or spread > cfg.area_tolerance_m2 + 1e-9


class FeatureCache:
    """Признаки объявлений и состав объектов в пределах одной обработки (или одного отчёта).

    `preload` читает снимки и хеши пачкой; без него признаки грузятся по одному (для разбора
    и отчёта этого хватает). «Типовые» фото исключаются из признаков всегда: в проходе — по
    общему списку, посчитанному один раз, в разборе — по хешам конкретного объявления.
    """

    def __init__(self, session: Session, config: MatchingConfig | None = None) -> None:
        self._session = session
        self._cfg = config or MatchingConfig()
        self._cache: dict[int, ListingFeatures] = {}
        self._members: dict[int, list[Listing]] = {}
        self._popular: dict[str, bool] = {}
        self._popular_complete = False

    # --- «Типовые» фото ---

    def load_all_popular(self) -> int:
        """Посчитать «типовые» хеши по всей БД один раз (в начале прохода)."""
        found = popular_hashes(self._session, self._cfg)
        self._popular = dict.fromkeys(found, True)
        self._popular_complete = True
        return len(found)

    def _popular_among(self, hashes: set[str]) -> set[str]:
        if not self._popular_complete:
            unknown = {h for h in hashes if h not in self._popular}
            if unknown:
                found = popular_hashes(self._session, self._cfg, unknown)
                self._popular.update({h: h in found for h in unknown})
        return {h for h in hashes if self._popular.get(h)}

    # --- Признаки ---

    def preload(self, listings: Iterable[Listing]) -> None:
        todo = [item for item in listings if item.id not in self._cache]
        if not todo:
            return
        ids = [item.id for item in todo]
        known = known_fields(self._session, ids)
        hashes = photo_hashes(self._session, ids)
        for item in todo:
            phashes = hashes.get(item.id, set())
            self._cache[item.id] = build_features(
                item, known.get(item.id), phashes, self._popular_among(phashes)
            )

    def get(self, listing: Listing) -> ListingFeatures:
        f = self._cache.get(listing.id)
        if f is None:
            self.preload([listing])
            f = self._cache[listing.id]
        # Привязка могла измениться в этой же обработке.
        f.object_id, f.confidence, f.status = (
            listing.object_id,
            listing.match_confidence,
            listing.status,
        )
        return f

    def forget(self, listing_id: int) -> None:
        self._cache.pop(listing_id, None)

    # --- Состав объектов ---

    def members(self, object_id: int) -> list[Listing]:
        """Объявления объекта по (first_seen, id). Читается из БД один раз за обработку."""
        rows = self._members.get(object_id)
        if rows is None:
            rows = self._members[object_id] = list(
                self._session.scalars(
                    select(Listing)
                    .where(Listing.object_id == object_id)
                    .order_by(Listing.first_seen, Listing.id)
                )
            )
        return rows

    def preload_members(self, object_ids: Iterable[int]) -> None:
        ids = sorted({i for i in object_ids if i not in self._members})
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i : i + _CHUNK]
            for object_id in chunk:
                self._members[object_id] = []
            for item in self._session.scalars(
                select(Listing)
                .where(Listing.object_id.in_(chunk))
                .order_by(Listing.first_seen, Listing.id)
            ):
                self._members[item.object_id].append(item)

    def new_object(self, object_id: int, founder: Listing, old_object_id: int | None) -> None:
        """Объект создан в этой обработке: кроме основного объявления в нём никого нет."""
        self.moved(founder, old_object_id)
        self._members[object_id] = [founder]

    def members_map(self, object_ids: Iterable[int]) -> dict[int, list[Listing]]:
        ids = set(object_ids)
        self.preload_members(ids)
        return {object_id: self.members(object_id) for object_id in ids}

    def moved(self, listing: Listing, old_object_id: int | None) -> None:
        """Объявление перешло из `old_object_id` в `listing.object_id` (None — отцеплено)."""
        if old_object_id is not None and old_object_id in self._members:
            self._members[old_object_id] = [
                item for item in self._members[old_object_id] if item.id != listing.id
            ]
        new = listing.object_id
        if new is not None and new in self._members:
            rows = [item for item in self._members[new] if item.id != listing.id]
            rows.append(listing)
            rows.sort(key=lambda item: (item.first_seen, item.id))
            self._members[new] = rows


def distance_m(a: ListingFeatures, b: ListingFeatures) -> float | None:
    if not (a.has_coords and b.has_coords):
        return None
    return haversine_m(a.lat, a.lon, b.lat, b.lon)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))
