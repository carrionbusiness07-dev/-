"""Признаки объявления для склейки: последний снимок + нормализованный адрес + хеши фото."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from minsk_realty.db.models import Listing, PhotoHash, Snapshot
from minsk_realty.matching.address import (
    Street,
    normalize_house,
    normalize_street,
    parse_address,
    streets_match,
)
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

    @property
    def label(self) -> str:
        return f"#{self.listing_id} {self.source}/{self.external_id}"

    @property
    def has_coords(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def active(self) -> bool:
        return self.status == "active"


def latest_snapshot(session: Session, listing_id: int) -> Snapshot | None:
    return session.scalar(
        select(Snapshot)
        .where(Snapshot.listing_id == listing_id)
        .order_by(Snapshot.seen_at.desc(), Snapshot.id.desc())
        .limit(1)
    )


def _norm_text(value: str | None) -> str | None:
    if not value:
        return None
    return " ".join(value.lower().replace("ё", "е").split()) or None


def build_features(listing: Listing, snap: Snapshot | None, phashes: set[str]) -> ListingFeatures:
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
        phashes=frozenset(h for h in phashes if is_informative(h)),
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
    snap = latest_snapshot(session, listing.id)
    hashes = set(session.scalars(select(PhotoHash.phash).where(PhotoHash.listing_id == listing.id)))
    return build_features(listing, snap, hashes)


class FeatureCache:
    """Признаки объявлений в пределах одной обработки: не читать снимки по многу раз."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._cache: dict[int, ListingFeatures] = {}

    def get(self, listing: Listing) -> ListingFeatures:
        f = self._cache.get(listing.id)
        if f is None:
            f = self._cache[listing.id] = load_features(self._session, listing)
        # Привязка могла измениться в этой же обработке.
        f.object_id, f.confidence, f.status = (
            listing.object_id,
            listing.match_confidence,
            listing.status,
        )
        return f

    def forget(self, listing_id: int) -> None:
        self._cache.pop(listing_id, None)


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
