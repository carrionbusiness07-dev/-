"""Привязка объявлений к объектам: поиск кандидатов, решение, пересмотр склеек."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from minsk_realty.db.models import Listing, RealtyObject
from minsk_realty.matching.address import normalize_street, streets_match
from minsk_realty.matching.config import MatchingConfig
from minsk_realty.matching.features import FeatureCache, ListingFeatures, haversine_m
from minsk_realty.matching.rules import CONFIDENCE_RANK, TRUSTED, Verdict, compare

log = logging.getLogger(__name__)

Action = Literal["link", "new", "defer"]

FOUNDER_BASIS: dict[str, Any] = {"rule": "founder"}
_M_PER_DEG_LAT = 111_320.0


@dataclass
class MatchResult:
    action: Action
    object_id: int | None = None
    confidence: str | None = None
    verdicts: list[Verdict] = field(default_factory=list)
    note: str = ""

    @property
    def verdict(self) -> Verdict | None:
        """Вердикт выбранного объекта (для основания склейки)."""
        return next((v for v in self.verdicts if v.object_id == self.object_id), None)


def candidate_objects(
    session: Session, f: ListingFeatures, cfg: MatchingConfig
) -> list[RealtyObject]:
    """Объекты, с которыми объявление вообще имеет смысл сравнивать.

    Отбор в БД — по комнатам, площади (с запасом: площадь объекта — от основного объявления)
    и месту: та же улица (по последнему слову названия, индекс), объекты без улицы рядом
    (квадрат радиуса противоречия) и, если у объявления есть фото, объекты без места вовсе.
    Объект на другой улице или дальше радиуса противоречия склеить нельзя, поэтому он
    даже не читается. Дальше в Python — точная проверка улицы, дома и расстояния.
    """
    if not (f.rooms and f.area):
        return []
    has_place = bool((f.street and f.house) or f.has_coords)
    if not has_place and not f.phashes:
        return []  # без места и без фото никакая улика невозможна
    margin = 2 * cfg.area_tolerance_m2
    base = select(RealtyObject).where(
        RealtyObject.rooms == f.rooms,
        RealtyObject.area_total.between(f.area - margin, f.area + margin),
    )
    queries = []
    if f.street is not None:
        same_street = base.where(RealtyObject.street_tail == f.street.tail)
        if f.house and not f.phashes and not f.has_coords:
            # Другой дом той же улицы — жёсткое противоречие; без фото и координат других
            # путей к объекту нет.
            same_street = same_street.where(
                (RealtyObject.house == f.house) | RealtyObject.house.is_(None)
            )
        queries.append(same_street)
        no_street = base.where(RealtyObject.street_tail.is_(None))
    else:
        no_street = base
    if f.has_coords:
        dlat = cfg.geo_contradiction_m / _M_PER_DEG_LAT
        dlon = dlat / max(math.cos(math.radians(f.lat)), 0.1)
        box = RealtyObject.lat.between(f.lat - dlat, f.lat + dlat) & RealtyObject.lon.between(
            f.lon - dlon, f.lon + dlon
        )
        queries.append(no_street.where(box))
        if f.phashes:
            queries.append(no_street.where(RealtyObject.lat.is_(None)))
    elif f.phashes:
        queries.append(no_street)

    seen: dict[int, RealtyObject] = {}
    for query in queries:
        for obj in session.scalars(query):
            seen.setdefault(obj.id, obj)
    out = []
    for obj in sorted(seen.values(), key=lambda o: o.id):
        same = streets_match(f.street, normalize_street(obj.street_norm))
        if same and f.house and obj.house and f.house != obj.house:
            continue
        dist = (
            haversine_m(f.lat, f.lon, obj.lat, obj.lon)
            if f.has_coords and obj.lat is not None and obj.lon is not None
            else None
        )
        if same is False or (dist is not None and dist > cfg.geo_contradiction_m):
            continue
        if same or dist is not None or f.phashes:
            out.append(obj)
    return out


def is_founder(listing: Listing, members: list[Listing]) -> bool:
    """Основное объявление объекта: с него объект создан, с ним сравниваются остальные.

    Записано в match_basis при создании объекта. Для склеек до миграции 0003 основания нет —
    тогда основное объявление — самое раннее с уверенностью high.
    """
    basis = listing.match_basis or {}
    if basis.get("rule") == "founder":
        return True
    if any((item.match_basis or {}).get("rule") == "founder" for item in members):
        return False
    first_high = next((item for item in members if item.match_confidence == "high"), None)
    return first_high is not None and first_high.id == listing.id


def linked_features(
    session: Session, cache: FeatureCache, object_id: int, *, exclude: int | None = None
) -> tuple[list[ListingFeatures], list[ListingFeatures]]:
    """Объявления объекта: (надёжные high/medium, слабые low), без `exclude`."""
    listings = cache.members(object_id)
    cache.preload(listings)
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
    if not (f.rooms and f.area):
        # Комнат или площади нет ни в одном снимке. Сначала ждём: часто это разовый сбой
        # разбора, и объект-дубль, созданный сейчас, потом уже ни с чем не склеится.
        if now - f.first_seen <= cfg.missing_data_grace:
            return MatchResult("defer", note="нет комнат или площади — ждём данных")
        return MatchResult("new", note="нет комнат или площади — сравнивать не по чему")
    candidates = candidate_objects(session, f, cfg)
    cache.preload_members(obj.id for obj in candidates)
    cache.preload([item for obj in candidates for item in cache.members(obj.id)])
    verdicts = [compare_with_object(session, cache, f, obj.id, cfg, now) for obj in candidates]
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


def _object_fields(f: ListingFeatures) -> dict[str, Any]:
    return {
        "street_norm": str(f.street) if f.street else None,
        "street_tail": f.street.tail if f.street else None,
        "house": f.house,
        "rooms": f.rooms,
        "area_total": f.area,
        "floor": f.floor,
        "floors_total": f.floors_total,
        "town": f.town,
        "district": f.district,
        "lat": f.lat,
        "lon": f.lon,
    }


def create_object(
    session: Session,
    listing: Listing,
    f: ListingFeatures,
    cache: FeatureCache | None = None,
) -> RealtyObject:
    obj = RealtyObject(
        fingerprint=fingerprint(f), first_seen_at=listing.first_seen, **_object_fields(f)
    )
    session.add(obj)
    session.flush()
    # Первое объявление объекта и есть объект: склейки нет, сомневаться не в чем.
    old = listing.object_id
    listing.object_id, listing.match_confidence = obj.id, "high"
    listing.match_basis = dict(FOUNDER_BASIS)
    if cache is not None:
        cache.new_object(obj.id, listing, old)
    return obj


def link(
    session: Session,
    listing: Listing,
    f: ListingFeatures,
    object_id: int,
    conf: str,
    *,
    basis: dict[str, Any] | None = None,
    cache: FeatureCache | None = None,
) -> None:
    old = listing.object_id
    listing.object_id, listing.match_confidence = object_id, conf
    listing.match_basis = basis
    if cache is not None:
        cache.moved(listing, old)
    if conf in TRUSTED:
        obj = session.get(RealtyObject, object_id)
        # Дополняем объект недостающими полями (этаж с Realt к объекту, открытому Kufar).
        for attr, value in _object_fields(f).items():
            if getattr(obj, attr) is None and value is not None:
                setattr(obj, attr, value)
        if obj.fingerprint is None:
            obj.fingerprint = fingerprint(f)


def refresh_object(session: Session, cache: FeatureCache, object_id: int) -> bool:
    """Обновить поля объекта по текущим данным его надёжных объявлений.

    Источник каждого поля — основное объявление; если у него поля нет, — первое надёжное
    объявление, где оно есть (по уверенности, затем по времени). Надёжные объявления объекта
    друг другу не противоречат (иначе их отцепили бы), поэтому это безопасно: объект лишь
    следует за исправлениями продавца (уточнил площадь, появился этаж). Слабые (low)
    объявления в поля объекта не попадают. Возвращает True, если что-то поменялось.
    """
    obj = session.get(RealtyObject, object_id)
    members = cache.members(object_id)
    trusted = [item for item in members if item.match_confidence in TRUSTED]
    if obj is None or not trusted:
        return False
    cache.preload(trusted)
    founder = next((item for item in trusted if is_founder(item, members)), None)
    ordered = sorted(
        trusted,
        key=lambda item: (
            item is not founder,
            -CONFIDENCE_RANK.get(item.match_confidence or "", 0),
            item.first_seen,
            item.id,
        ),
    )
    feats = [cache.get(item) for item in ordered]
    changed = False
    values: dict[str, Any] = {}
    for f in feats:
        for attr, value in _object_fields(f).items():
            if values.get(attr) is None and value is not None:
                values[attr] = value
    # Улица и её ключ — из одного источника.
    if values.get("street_norm") is not None:
        src = next(f for f in feats if f.street is not None)
        values["street_norm"], values["street_tail"] = str(src.street), src.street.tail
    lat_src = next((f for f in feats if f.has_coords), None)
    if lat_src is not None:
        values["lat"], values["lon"] = lat_src.lat, lat_src.lon
    values["fingerprint"] = next((fp for f in feats if (fp := fingerprint(f))), None)
    values["first_seen_at"] = min(item.first_seen for item in trusted)
    for attr, value in values.items():
        if value is not None and getattr(obj, attr) != value:
            setattr(obj, attr, value)
            changed = True
    return changed


def detach(session: Session, listing: Listing, cache: FeatureCache | None = None) -> int | None:
    """Отцепить объявление от объекта (склейка оказалась ложной). Возвращает id объекта.

    События объекта, которые ссылаются на это объявление, не удаляются здесь: при следующей
    генерации событий объекта они будут отозваны (retracted_at, notify=False) —
    `events.reconcile`. Объявление становится «без объекта» и проходит склейку заново.
    """
    object_id = listing.object_id
    if object_id is None:
        return None
    listing.object_id, listing.match_confidence, listing.match_basis = None, None, None
    if cache is not None:
        cache.moved(listing, object_id)
    return object_id


def fingerprint(f: ListingFeatures) -> str | None:
    """Отпечаток уровня 1 без площади (она сравнивается с допуском). None, если он неполный."""
    if not (f.street and f.house and f.rooms and f.floor is not None and f.floors_total):
        return None
    return f"{f.street.key}|{f.house}|{f.rooms}|{f.floor}/{f.floors_total}"[:128]
