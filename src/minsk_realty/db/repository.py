"""Запись результатов сборщика: объявление, его снимок и отметка «ещё висит»."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from minsk_realty.db.models import Listing, Snapshot, SourceRun
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


# Меняйте версию при правке SIGNIFICANT_FIELDS: тогда сравнение идёт только между хешами одной версии
# и правка набора полей не порождает разом новые снимки у всех объявлений.
HASH_VERSION = "v1"


def content_hash(snap: ListingSnapshot) -> str:
    data = {field: _canonical(getattr(snap, field)) for field in SIGNIFICANT_FIELDS}
    raw = json.dumps(data, default=str, sort_keys=True, ensure_ascii=False)
    return f"{HASH_VERSION}:{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


def _canonical(value):
    # 92500, 92500.0 и 92500.00 — одна и та же цена.
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.01")))
    if isinstance(value, float):
        return round(value, 2)
    return value


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
    # Снимки могут прийти не по порядку: last_seen не должен откатываться назад.
    listing.last_seen = max(listing.last_seen, snap.seen_at)
    listing.status = "active"
    # Разовая ошибка разбора не должна стирать уже известные данные о продавце.
    if snap.seller_type != "unknown":
        listing.seller_type = snap.seller_type
    listing.agency_name = snap.agency_name or listing.agency_name
    listing.seller_key = snap.seller_key or listing.seller_key
    listing.posted_at_source = snap.posted_at or listing.posted_at_source
    listing.updated_at_source = snap.updated_at or listing.updated_at_source
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


def touch_listing(session: Session, source: str, external_id: str, seen_at) -> None:
    """Отметить, что объявление есть в выдаче, даже если его не удалось разобрать.

    Иначе нечитаемое объявление погасло бы как снятое, а через 3 дня Вера поставила бы
    ложный `removed`.
    """
    session.execute(
        update(Listing)
        .where(
            Listing.source == source,
            Listing.external_id == external_id,
            Listing.last_seen < seen_at,
        )
        .values(last_seen=seen_at)
    )


def mark_unseen_inactive(session: Session, run: SourceRun) -> int:
    """Пометить неактивными объявления площадки, которых не было в выдаче за этот проход.

    Работает только для успешного и полного прохода: если сборщик упёрся в лимит страниц
    или его остановил 403/429, непросмотренные объявления не гасятся — иначе через 3 суток
    появились бы ложные `removed` (их ставит Вера по неактивности всех объявлений объекта).
    """
    if run.status != "ok" or not run.complete:
        return 0
    result = session.execute(
        update(Listing)
        .where(
            Listing.source == run.source,
            Listing.status == "active",
            Listing.last_seen < run.started_at,
        )
        .values(status="inactive")
    )
    return result.rowcount or 0
