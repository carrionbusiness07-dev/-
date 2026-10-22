"""Привязка объявлений к объектам: поиск кандидатов, решение, пересмотр слабых склеек."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from minsk_realty.db.models import Event, Listing, RealtyObject
from minsk_realty.matching.address import normalize_street, streets_match
from minsk_realty.matching.config import MatchingConfig
from minsk_realty.matching.features import FeatureCache, ListingFeatures, haversine_m
from minsk_realty.matching.rules import TRUSTED, Verdict, compare

log = logging.getLogger(__name__)

Action = Literal["link", "new", "defer"]


@dataclass
class MatchResult:
    action: Action
    object_id: int | None = None
    confidence: str | None = None
    verdicts: list[Verdict] = field(default_factory=list)
    note: str = ""


def candidate_objects(
    session: Session, f: ListingFeatures, cfg: MatchingConfig
) -> list[RealtyObject]:
    """Объекты, с которыми объявление вообще имеет смысл сравнивать.

    Отбор в БД — по комнатам и площади (с запасом: площадь объекта — от первого объявления),
    дальше в Python отбрасываются объекты на другой улице или дальше радиуса противоречия.
    Объекты без адреса и координат остаются: их может склеить только фото.
    """
    if not (f.rooms and f.area):
        return []
    has_place = bool((f.street and f.house) or f.has_coords)
    if not has_place and not f.phashes:
        return []  # без места и без фото никакая улика невозможна
    margin = 2 * cfg.area_tolerance_m2
    objects = session.scalars(
        select(RealtyObject)
        .where(
            RealtyObject.rooms == f.rooms,
            RealtyObject.area_total.between(f.area - margin, f.area + margin),
        )
        .order_by(RealtyObject.id)
    ).all()
    out = []
    for obj in objects:
        same_street = streets_match(f.street, normalize_street(obj.street_norm))
        dist = (
            haversine_m(f.lat, f.lon, obj.lat, obj.lon)
            if f.has_coords and obj.lat is not None and obj.lon is not None
            else None
        )
        if same_street is False or (dist is not None and dist > cfg.geo_contradiction_m):
            continue
        if same_street or dist is not None or f.phashes:
            out.append(obj)
    return out


def linked_features(
    session: Session, cache: FeatureCache, object_id: int, *, exclude: int | None = None
) -> tuple[list[ListingFeatures], list[ListingFeatures]]:
    """Объявления объекта: (надёжные high/medium, слабые low), без `exclude`."""
    listings = session.scalars(
        select(Listing)
        .where(Listing.object_id == object_id)
        .order_by(Listing.first_seen, Listing.id)
    ).all()
    feats = [cache.get(item) for item in listings if item.id != exclude]
    return (
        [f for f in feats if f.confidence in TRUSTED],
        [f for f in feats if f.confidence not in TRUSTED],
    )


def compare_with_object(
    session: Session,
    cache: FeatureCache,
    f: ListingFeatures,
    object_id: int,
    cfg: MatchingConfig,
    now: datetime,
) -> Verdict:
    trusted, weak = linked_features(session, cache, object_id, exclude=f.listing_id)
    return compare(f, object_id, trusted, cfg, now, weak=weak)


def evaluate(
    session: Session,
    cache: FeatureCache,
    f: ListingFeatures,
    cfg: MatchingConfig,
    now: datetime,
) -> MatchResult:
    """Решить судьбу объявления без привязки: склеить, создать объект или подождать."""
    verdicts = [
        compare_with_object(session, cache, f, obj.id, cfg, now)
        for obj in candidate_objects(session, f, cfg)
    ]
    if any(v.outcome == "defer" for v in verdicts):
        return MatchResult("defer", verdicts=verdicts, note="ждём решения продавца")
    matches = [v for v in verdicts if v.outcome == "match"]
    if not matches:
        return MatchResult("new", verdicts=verdicts, note="подходящих объектов нет")
    best = max(v.rank for v in matches)
    top = [v for v in matches if v.rank == best]
    if len(top) > 1:
        ids = ", ".join(f"#{v.object_id}" for v in top)
        return MatchResult(
            "new",
            verdicts=verdicts,
            note=f"неоднозначно: объекты {ids} подходят одинаково ({top[0].confidence}) — "
            "не выбираем наугад, создаём отдельный объект",
        )
    return MatchResult("link", top[0].object_id, top[0].confidence, verdicts)


def create_object(session: Session, listing: Listing, f: ListingFeatures) -> RealtyObject:
    obj = RealtyObject(
        fingerprint=fingerprint(f),
        street_norm=str(f.street) if f.street else None,
        house=f.house,
        rooms=f.rooms,
        area_total=f.area,
        floor=f.floor,
        floors_total=f.floors_total,
        town=f.town,
        district=f.district,
        lat=f.lat,
        lon=f.lon,
        first_seen_at=listing.first_seen,
    )
    session.add(obj)
    session.flush()
    # Первое объявление объекта и есть объект: склейки нет, сомневаться не в чем.
    listing.object_id, listing.match_confidence = obj.id, "high"
    return obj


def link(session: Session, listing: Listing, f: ListingFeatures, object_id: int, conf: str) -> None:
    listing.object_id, listing.match_confidence = object_id, conf
    if conf in TRUSTED:
        obj = session.get(RealtyObject, object_id)
        # Дополняем объект недостающими полями (этаж с Realt к объекту, открытому Kufar).
        for attr, value in (
            ("street_norm", str(f.street) if f.street else None),
            ("house", f.house),
            ("floor", f.floor),
            ("floors_total", f.floors_total),
            ("town", f.town),
            ("district", f.district),
            ("lat", f.lat),
            ("lon", f.lon),
        ):
            if getattr(obj, attr) is None and value is not None:
                setattr(obj, attr, value)
        if obj.fingerprint is None:
            obj.fingerprint = fingerprint(f)
    session.flush()


def detach(session: Session, listing: Listing) -> None:
    """Отцепить объявление от объекта (склейка оказалась ложной) и убрать его события там.

    Объявление становится «без объекта» и проходит склейку заново.
    """
    object_id = listing.object_id
    if object_id is None:
        return
    session.execute(
        delete(Event).where(Event.object_id == object_id, Event.listing_id == listing.id)
    )
    for event in session.scalars(
        select(Event).where(Event.object_id == object_id, Event.type == "cross_listed")
    ):
        if listing.id in (event.payload or {}).get("listing_ids", []):
            session.delete(event)
    listing.object_id, listing.match_confidence = None, None
    session.flush()


def fingerprint(f: ListingFeatures) -> str | None:
    """Отпечаток уровня 1 без площади (она сравнивается с допуском). None, если он неполный."""
    if not (f.street and f.house and f.rooms and f.floor is not None and f.floors_total):
        return None
    return f"{f.street.key}|{f.house}|{f.rooms}|{f.floor}/{f.floors_total}"[:128]
