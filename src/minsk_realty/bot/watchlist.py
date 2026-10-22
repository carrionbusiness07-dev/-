"""Пользователи и список «Мои квартиры»: синхронные функции над сессией SQLAlchemy.

О пользователе храним только telegram_id (таблица users). Пользователь появляется в БД
при первом действии, которое что-то сохраняет (/check, «Следить»), а не на /start.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from minsk_realty.analytics import compute_exposure, load_market
from minsk_realty.analytics.data import MarketData
from minsk_realty.analytics.fmt import street_label
from minsk_realty.db.models import Listing, User, Watch
from minsk_realty.matching.rules import TRUSTED

WatchStatus = Literal["added", "already", "limit", "not_found", "not_matched"]


def find_user(session: Session, telegram_id: int) -> User | None:
    return session.scalar(select(User).where(User.telegram_id == telegram_id))


def get_or_create_user(session: Session, telegram_id: int, now: datetime) -> User:
    user = find_user(session, telegram_id)
    if user is None:
        user = User(telegram_id=telegram_id, created_at=now)
        session.add(user)
        session.flush()
    return user


def watch_count(session: Session, user_id: int) -> int:
    return session.scalar(select(func.count()).where(Watch.user_id == user_id)) or 0


def is_watching(session: Session, telegram_id: int, object_id: int | None) -> bool:
    if object_id is None:
        return False
    return (
        session.scalar(
            select(Watch.object_id)
            .join(User, User.id == Watch.user_id)
            .where(User.telegram_id == telegram_id, Watch.object_id == object_id)
        )
        is not None
    )


def watchable_object(listing: Listing | None) -> int | None:
    """Объект, за которым можно следить по этому объявлению.

    Только надёжная склейка (high/medium): при low объект может оказаться другой квартирой,
    и пользователь получал бы новости не о своей квартире.
    """
    if listing is None or listing.object_id is None:
        return None
    return listing.object_id if listing.match_confidence in TRUSTED else None


@dataclass(frozen=True)
class WatchResult:
    status: WatchStatus
    object_id: int | None = None
    count: int = 0  # сколько квартир в списке после операции
    limit: int = 0


def add_watch(
    session: Session, telegram_id: int, listing: Listing | None, now: datetime, limit: int
) -> WatchResult:
    if listing is None:
        return WatchResult("not_found", limit=limit)
    object_id = watchable_object(listing)
    if object_id is None:
        return WatchResult("not_matched", limit=limit)
    user = get_or_create_user(session, telegram_id, now)
    count = watch_count(session, user.id)
    if session.get(Watch, (user.id, object_id)) is not None:
        return WatchResult("already", object_id, count, limit)
    if count >= limit:
        return WatchResult("limit", object_id, count, limit)
    session.add(Watch(user_id=user.id, object_id=object_id, created_at=now))
    session.flush()
    return WatchResult("added", object_id, count + 1, limit)


def remove_watch(session: Session, telegram_id: int, object_id: int) -> bool:
    user = find_user(session, telegram_id)
    if user is None:
        return False
    result = session.execute(
        delete(Watch).where(Watch.user_id == user.id, Watch.object_id == object_id)
    )
    return bool(result.rowcount)


def forget_user(session: Session, telegram_id: int) -> bool:
    """Удалить пользователя: список, журнал проверок и уведомлений уходят каскадом."""
    user = find_user(session, telegram_id)
    if user is None:
        return False
    session.delete(user)
    session.flush()
    return True


# --- Как назвать квартиру и что о ней показать в списке -----------------------------------


@dataclass(frozen=True)
class FlatName:
    street: str | None  # «ул. Притыцкого»
    rooms: int | None
    area: float | None
    district: str | None  # «Фрунзенский»

    @property
    def where(self) -> str | None:
        """«Притыцкого» для улиц, «пр-т Независимости» для остального (как в docs 1.3)."""
        if not self.street:
            return None
        return self.street.removeprefix("ул. ")

    @property
    def brief(self) -> str:
        parts = []
        if self.rooms:
            parts.append(f"{self.rooms}-комн.")
        if self.area:
            parts.append(f"{self.area:g} м²")
        if not self.street and self.district:
            parts.append(f"{self.district} р-н")
        return ", ".join(parts)

    @property
    def subject(self) -> str:
        """«Квартира на Притыцкого (2-комн., 54 м²)»."""
        text = f"Квартира на {self.where}" if self.where else "Квартира"
        return f"{text} ({self.brief})" if self.brief else text


def flat_name(data: MarketData, object_id: int) -> FlatName:
    obj = data.objects.get(object_id)
    if obj is None:
        return FlatName(None, None, None, None)
    district = data.object_district(object_id)
    return FlatName(
        street=street_label(obj.street_norm),
        rooms=obj.rooms,
        area=obj.area,
        district=district,
    )


@dataclass(frozen=True)
class WatchItem:
    object_id: int
    name: FlatName
    state: str  # active / hidden / removed / unknown
    amount: Decimal | None
    currency: str | None
    source: str | None
    url: str | None  # объявление, которое открывать: активное и самое дешёвое
    days: int | None
    at_least: bool
    drops: int
    last_seen: datetime | None


def current_listing(data: MarketData, object_id: int):
    """Объявление объекта «сейчас»: активное, иначе последнее виденное (как в кабинете)."""
    rows = data.object_listings(object_id)
    return max(rows, key=lambda r: (r.active, r.last_seen, r.id)) if rows else None


def watch_items(session: Session, telegram_id: int, now: datetime) -> list[WatchItem]:
    user = find_user(session, telegram_id)
    if user is None:
        return []
    watches = session.scalars(
        select(Watch).where(Watch.user_id == user.id).order_by(Watch.created_at, Watch.object_id)
    ).all()
    if not watches:
        return []
    data = load_market(session, object_ids=[w.object_id for w in watches])
    return [watch_item(data, w.object_id, now) for w in watches]


def watch_item(data: MarketData, object_id: int, now: datetime) -> WatchItem:
    rows = data.object_listings(object_id)
    events = data.object_events(object_id)
    current = current_listing(data, object_id)
    exposure = compute_exposure(rows, events, now, data.source_starts, current=current)
    price = data.object_price(object_id, now)
    url = source = None
    if price is not None:
        listing = data.listings.get(price.listing_id)
        url, source = (listing.url, listing.source) if listing else (None, None)
    elif current is not None:
        url, source = current.url, current.source
    return WatchItem(
        object_id=object_id,
        name=flat_name(data, object_id),
        state=exposure.state if exposure else "unknown",
        amount=price.amount if price else None,
        currency=price.currency if price else None,
        source=source,
        url=url,
        days=exposure.days if exposure else None,
        at_least=exposure.at_least if exposure else False,
        drops=sum(1 for e in events if e.type == "price_drop"),
        last_seen=exposure.end if exposure else None,
    )
