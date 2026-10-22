"""Запись результатов сборщика: объявление, его снимок и отметка «ещё висит»."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from minsk_realty.db.models import Listing, Snapshot
from minsk_realty.models import ListingSnapshot

# Поля, изменение которых даёт новую строку в snapshots. Порядок важен для хеша.
SIGNIFICANT_FIELDS = (
    "price_amount",
    "price_currency",
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


def content_hash(snap: ListingSnapshot) -> str:
    data = {field: getattr(snap, field) for field in SIGNIFICANT_FIELDS}
    raw = json.dumps(data, default=str, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def save_snapshot(session: Session, snap: ListingSnapshot) -> tuple[Listing, Snapshot | None]:
    """Обновить объявление и записать снимок, если значимые поля изменились.

    Возвращает объявление и новый снимок (или None, если ничего не поменялось).
    Цена сравнивается в исходной валюте объявления: курс в хеш не входит.
    """
    listing = session.scalar(
        select(Listing).where(
            Listing.source == snap.source, Listing.external_id == snap.external_id
        )
    )
    if listing is None:
        listing = Listing(
            source=snap.source,
            external_id=snap.external_id,
            url=snap.url,
            first_seen=snap.seen_at,
            last_seen=snap.seen_at,
        )
        session.add(listing)

    listing.url = snap.url
    listing.last_seen = snap.seen_at
    listing.status = "active"
    listing.seller_type = snap.seller_type
    listing.agency_name = snap.agency_name
    listing.seller_key = snap.seller_key
    listing.posted_at_source = snap.posted_at
    listing.updated_at_source = snap.updated_at
    session.flush()

    digest = content_hash(snap)
    last = session.scalar(
        select(Snapshot)
        .where(Snapshot.listing_id == listing.id)
        .order_by(Snapshot.seen_at.desc(), Snapshot.id.desc())
        .limit(1)
    )
    if last is not None and last.content_hash == digest:
        return listing, None

    snapshot = Snapshot(
        listing_id=listing.id,
        seen_at=snap.seen_at,
        content_hash=digest,
        price_amount=snap.price_amount,
        price_currency=snap.price_currency,
        price_usd=snap.price_usd,
        rooms=snap.rooms,
        area_total=snap.area_total,
        floor=snap.floor,
        floors_total=snap.floors_total,
        street=snap.street,
        house=snap.house,
        address_raw=snap.address_raw,
        district=snap.district,
        town=snap.town,
        lat=snap.lat,
        lon=snap.lon,
        extra=snap.extra,
    )
    session.add(snapshot)
    session.flush()
    return listing, snapshot


def mark_unseen_inactive(session: Session, source: str, run_started_at: datetime) -> int:
    """После полного прохода по площадке пометить неактивными объявления, которых не было в выдаче.

    Событие `removed` из этого не следует напрямую: Вера ставит его только после 3 суток
    неактивности всех объявлений объекта.
    """
    result = session.execute(
        update(Listing)
        .where(
            Listing.source == source,
            Listing.status == "active",
            Listing.last_seen < run_started_at,
        )
        .values(status="inactive")
    )
    return result.rowcount or 0
