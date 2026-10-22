"""Уведомления по списку «Мои квартиры»: что разослать, кому и как не прислать дважды.

Правила (договорённость с Emma и Leo):
- только события `notify AND trusted AND retracted_at IS NULL` (Emma ставит notify лишь
  свежим событиям надёжной склейки high/medium);
- новизна — по `created_at` (когда мы событие заметили), а не по `at`: события из истории
  приходят с прошлым `at`. Событие должно быть замечено после того, как пользователь начал
  следить, и не раньше `notify_max_age` назад;
- одно событие одному пользователю — один раз: таблица `notifications`, уникальная пара
  (user_id, event_id). Строка пишется и коммитится ДО отправки; если процесс упал между
  записью и отправкой, `recover_pending` помечает её failed, и событие больше не уходит
  (лучше потерять уведомление, чем прислать дважды);
- не чаще одного сообщения в час по одному объекту одному пользователю: новые события
  ждут и уходят следующим сообщением, все вместе.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import delete, exists, select, update
from sqlalchemy.orm import Session

from minsk_realty.analytics import compute_exposure, load_market
from minsk_realty.analytics.data import MarketData
from minsk_realty.bot.config import BotConfig
from minsk_realty.bot.texts import NOTIFY_TYPES, EventNews, ObjectNews, news_text
from minsk_realty.bot.watchlist import current_listing, flat_name
from minsk_realty.db.models import Event, Notification, User, Watch

Outcome = Literal["sent", "failed", "release"]


@dataclass(frozen=True)
class Outgoing:
    user_id: int
    telegram_id: int
    object_id: int
    event_ids: tuple[int, ...]
    text: str
    url: str | None  # объявление для кнопки «Открыть объявление»


def pending_events(
    session: Session, now: datetime, cfg: BotConfig
) -> dict[tuple[int, int, int], list[Event]]:
    """(user_id, telegram_id, object_id) → новые события, которые этому пользователю ещё не
    отправлялись. Без учёта лимита «раз в час»."""
    already = exists().where(
        Notification.user_id == Watch.user_id, Notification.event_id == Event.id
    )
    rows = session.execute(
        select(Event, Watch.user_id, User.telegram_id)
        .join(Watch, Watch.object_id == Event.object_id)
        .join(User, User.id == Watch.user_id)
        .where(
            Event.notify.is_(True),
            Event.trusted.is_(True),
            Event.retracted_at.is_(None),
            Event.type.in_(NOTIFY_TYPES),
            Event.created_at >= Watch.created_at,
            Event.created_at >= now - cfg.notify_max_age,
            Event.created_at <= now,
            ~already,
        )
        .order_by(Event.created_at, Event.id)
    ).all()
    out: dict[tuple[int, int, int], list[Event]] = defaultdict(list)
    for event, user_id, telegram_id in rows:
        out[(user_id, telegram_id, event.object_id)].append(event)
    return dict(out)


def recently_notified(
    session: Session, user_ids: Iterable[int], now: datetime, cfg: BotConfig
) -> set[tuple[int, int]]:
    """(user_id, object_id), по которым сообщение уходило меньше `notify_min_gap` назад."""
    ids = set(user_ids)
    if not ids:
        return set()
    rows = session.execute(
        select(Notification.user_id, Notification.object_id)
        .where(
            Notification.user_id.in_(ids),
            Notification.created_at > now - cfg.notify_min_gap,
        )
        .distinct()
    ).all()
    return {(u, o) for u, o in rows}


def due_notifications(session: Session, now: datetime, cfg: BotConfig) -> list[Outgoing]:
    """Сообщения, которые пора отправить: по одному на (пользователь, квартира)."""
    pending = pending_events(session, now, cfg)
    busy = recently_notified(session, {u for u, _, _ in pending}, now, cfg)
    groups = [(key, events) for key, events in pending.items() if (key[0], key[2]) not in busy]
    groups = groups[: cfg.notify_batch]
    if not groups:
        return []
    data = load_market(session, object_ids={key[2] for key, _ in groups})
    out = []
    for (user_id, telegram_id, object_id), events in groups:
        news, url = object_news(data, object_id, events, now)
        out.append(
            Outgoing(
                user_id=user_id,
                telegram_id=telegram_id,
                object_id=object_id,
                event_ids=tuple(e.id for e in events),
                text=news_text(news),
                url=url,
            )
        )
    return out


def object_news(
    data: MarketData, object_id: int, events: list[Event], now: datetime
) -> tuple[ObjectNews, str | None]:
    """Собрать данные для текста уведомления из событий Emma и среза аналитики."""
    rows = data.object_listings(object_id)
    history = data.object_events(object_id)
    current = current_listing(data, object_id)
    exposure = compute_exposure(rows, history, now, data.source_starts, current=current)
    drops = [e.id for e in history if e.type == "price_drop"]
    relist_prices = {
        e.listing_id
        for e in history
        if e.type in ("price_drop", "price_up") and e.payload.get("via") == "relisted"
    }
    items = []
    for event in sorted(events, key=lambda e: (e.at, e.id)):
        listing = data.listings.get(event.listing_id) if event.listing_id else None
        snap = data.last_snapshot(listing.id, priced=True) if listing else None
        items.append(
            EventNews(
                type=event.type,
                payload=dict(event.payload or {}),
                source=listing.source if listing else None,
                amount=snap.amount if snap and event.type not in ("removed",) else None,
                currency=snap.currency if snap else None,
                drop_number=drops.index(event.id) + 1 if event.id in drops else 0,
                relist_price_changed=event.listing_id in relist_prices,
            )
        )
    state = exposure.state if exposure else "unknown"
    url = None
    if state == "active":
        price = data.object_price(object_id, now)
        best = data.listings.get(price.listing_id) if price else None
        url = best.url if best else (current.url if current and current.active else None)
    news = ObjectNews(
        name=flat_name(data, object_id),
        events=tuple(items),
        days=exposure.days if exposure else None,
        at_least=exposure.at_least if exposure else False,
        site_days=exposure.site_days if exposure else None,
        drops=len(drops),
        state=state,
    )
    return news, url


def reserve(session: Session, outgoing: Iterable[Outgoing], now: datetime) -> None:
    """Записать «отправляю» до отправки. Вызывающий коммитит сразу после."""
    for o in outgoing:
        for event_id in o.event_ids:
            session.add(
                Notification(
                    user_id=o.user_id,
                    object_id=o.object_id,
                    event_id=event_id,
                    status="pending",
                    created_at=now,
                )
            )
    session.flush()


def finish(session: Session, results: Iterable[tuple[Outgoing, Outcome]], now: datetime) -> None:
    """sent / failed — оставить запись (больше не отправлять); release — Telegram попросил
    подождать (сообщение точно не ушло): удалить запись, событие уйдёт в следующий проход."""
    for o, outcome in results:
        where = (Notification.user_id == o.user_id, Notification.event_id.in_(o.event_ids))
        if outcome == "release":
            session.execute(delete(Notification).where(*where))
        else:
            session.execute(
                update(Notification)
                .where(*where)
                .values(status=outcome, sent_at=now if outcome == "sent" else None)
            )
    session.flush()


def recover_pending(session: Session) -> int:
    """После перезапуска: «pending» мог уйти, а мог и нет — повторно не отправляем."""
    result = session.execute(
        update(Notification).where(Notification.status == "pending").values(status="failed")
    )
    return result.rowcount or 0
