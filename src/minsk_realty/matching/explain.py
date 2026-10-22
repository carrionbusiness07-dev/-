"""Разбор склейки: почему объявления склеились или не склеились.

    from minsk_realty.matching import explain
    print(explain(session, 42))                                  # объект #42
    print(explain(session, "listing:17"))                        # объявление #17
    print(explain(session, "https://re.kufar.by/vi/1000000001"))  # по ссылке

    python -m minsk_realty.matching explain https://realt.by/sale-flats/object/123456/

Вердикты пересчитываются по текущим данным (последний снимок, хеши фото, статусы),
поэтому показывают, как решила бы склейка сейчас. Если вердикт расходится с записанной
уверенностью — значит, данные изменились после склейки; следующий проход это исправит.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from minsk_realty.db.models import Event, Listing, RealtyObject
from minsk_realty.matching.config import DEFAULT_CONFIG, MatchingConfig
from minsk_realty.matching.features import FeatureCache, ListingFeatures
from minsk_realty.matching.matcher import (
    candidate_objects,
    compare_with_object,
    evaluate,
    linked_features,
)
from minsk_realty.sources import kufar, realt

_URL_PATTERNS = {"kufar": kufar.URL_RE, "realt": realt.URL_RE}


def explain(
    session: Session,
    ref: int | str,
    *,
    now: datetime | None = None,
    config: MatchingConfig = DEFAULT_CONFIG,
) -> str:
    """`ref`: id объекта (int или "object:42"), "listing:17" или ссылка на объявление."""
    now = now or datetime.now(UTC)
    if isinstance(ref, int):
        return explain_object(session, ref, now=now, config=config)
    text = str(ref).strip()
    if text.isdigit() or text.startswith("object:"):
        return explain_object(session, int(text.removeprefix("object:")), now=now, config=config)
    if text.startswith("listing:"):
        return explain_listing(session, int(text.removeprefix("listing:")), now=now, config=config)
    listing = find_listing_by_url(session, text)
    if listing is None:
        return f"Объявление по ссылке не найдено в БД: {text}"
    return explain_listing(session, listing.id, now=now, config=config)


def find_listing_by_url(session: Session, url: str) -> Listing | None:
    for source, pattern in _URL_PATTERNS.items():
        match = pattern.search(url)
        if match:
            return session.scalar(
                select(Listing).where(
                    Listing.source == source, Listing.external_id == match.group(1)
                )
            )
    return None


def describe(f: ListingFeatures) -> str:
    floor = f"{f.floor if f.floor is not None else '?'}/{f.floors_total or '?'}"
    coords = f"{f.lat:.5f},{f.lon:.5f}" if f.has_coords else "нет"
    seller = f.seller_type + (f", {f.agency}" if f.agency else "")
    key = f.seller_key[:8] + "…" if f.seller_key else "нет"
    return (
        f"{f.label} [{f.status}, {f.first_seen:%Y-%m-%d %H:%M} — {f.last_seen:%Y-%m-%d %H:%M}]\n"
        f"    продавец: {seller}, псевдоним {key}\n"
        f"    улица: {f.street or '?'}, дом: {f.house or '?'} (источник: {f.address_from}), "
        f"комнат {f.rooms or '?'}, площадь {f.area if f.area else '?'}, этаж {floor}, "
        f"координаты {coords}, фото-хешей {len(f.phashes)}"
    )


def explain_listing(
    session: Session,
    listing_id: int,
    *,
    now: datetime | None = None,
    config: MatchingConfig = DEFAULT_CONFIG,
) -> str:
    now = now or datetime.now(UTC)
    listing = session.get(Listing, listing_id)
    if listing is None:
        return f"Объявления #{listing_id} нет в БД"
    cache = FeatureCache(session)
    f = cache.get(listing)
    lines = ["Объявление " + describe(f)]
    if listing.object_id is None:
        result = evaluate(session, cache, f, config, now)
        lines.append(f"  Объекта нет. Решение сейчас: {result.action}. {result.note}".rstrip())
    else:
        lines.append(f"  Объект #{listing.object_id}, уверенность {listing.match_confidence}")
        trusted, _ = linked_features(session, cache, listing.object_id, exclude=listing.id)
        if trusted:
            verdict = compare_with_object(session, cache, f, listing.object_id, config, now)
            lines.append(f"  Проверка против своего объекта: {verdict.summary()}")
        else:
            lines.append("  Это первое (основное) объявление объекта.")

    candidates = [o for o in candidate_objects(session, f, config) if o.id != listing.object_id]
    if not candidates:
        lines.append("  Других объектов-кандидатов (те же комнаты, площадь, место) нет.")
    for obj in candidates[:10]:
        verdict = compare_with_object(session, cache, f, obj.id, config, now)
        lines.append(f"  Объект #{obj.id} ({_object_label(obj)}): {verdict.summary()}")
    return "\n".join(lines)


def explain_object(
    session: Session,
    object_id: int,
    *,
    now: datetime | None = None,
    config: MatchingConfig = DEFAULT_CONFIG,
) -> str:
    now = now or datetime.now(UTC)
    obj = session.get(RealtyObject, object_id)
    if obj is None:
        return f"Объекта #{object_id} нет в БД"
    cache = FeatureCache(session)
    listings = session.scalars(
        select(Listing)
        .where(Listing.object_id == object_id)
        .order_by(Listing.first_seen, Listing.id)
    ).all()
    lines = [
        f"Объект #{obj.id}: {_object_label(obj)}",
        f"  отпечаток: {obj.fingerprint or 'неполный (нет адреса/этажа)'}",
        f"  объявлений: {len(listings)}",
    ]
    for listing in listings:
        f = cache.get(listing)
        lines.append(f"  [{listing.match_confidence}] " + describe(f))
        trusted, _ = linked_features(session, cache, object_id, exclude=listing.id)
        if not trusted:
            lines.append("      основное объявление: с ним сравниваются остальные")
        else:
            verdict = compare_with_object(session, cache, f, object_id, config, now)
            lines.append("      " + verdict.summary())
    events = session.scalars(
        select(Event).where(Event.object_id == object_id).order_by(Event.at, Event.id)
    ).all()
    lines.append(f"  событий: {len(events)}")
    for event in events:
        mute = "" if event.notify else " (без уведомления)"
        listing_ref = f" #{event.listing_id}" if event.listing_id else ""
        lines.append(
            f"    {event.at:%Y-%m-%d %H:%M} {event.type}{listing_ref}{mute} {event.payload}"
        )
    return "\n".join(lines)


def _object_label(obj: RealtyObject) -> str:
    floor = f"{obj.floor if obj.floor is not None else '?'}/{obj.floors_total or '?'}"
    return (
        f"{obj.street_norm or '?'} {obj.house or '?'}, {obj.rooms or '?'}-комн., "
        f"{obj.area_total or '?'} м², этаж {floor}"
    )
