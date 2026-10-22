"""Недельная сводка Leo в канале: когда публиковать и как не опубликовать дважды.

Сводка считается на границе недели — понедельник 00:00 по Минску, — чтобы номер недели
и период совпадали (неделя 43 = её понедельник–воскресенье). Публикуется в понедельник
в `weekly_hour` по Минску; если бот в это время лежал, догоняет в течение `weekly_catch_up`.
Каждая публикация записывается в `channel_posts` до отправки (уникальная пара kind+period).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from minsk_realty.analytics import WeeklySummary, weekly_summary
from minsk_realty.analytics.fmt import MINSK_TZ
from minsk_realty.bot.config import BotConfig
from minsk_realty.db.models import ChannelPost

KIND = "weekly"


def week_boundary(now: datetime) -> datetime:
    """Понедельник 00:00 по Минску той недели, в которую попадает `now` (в UTC)."""
    local = now.astimezone(MINSK_TZ)
    monday = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday.astimezone(now.tzinfo)


def period_key(summary: WeeklySummary) -> str:
    return f"{summary.year}-W{summary.week:02d}"


def publish_window(now: datetime, cfg: BotConfig) -> datetime | None:
    """Граница недели, сводку за которую пора публиковать, или None (ещё рано / уже поздно)."""
    boundary = week_boundary(now)
    publish_at = (boundary.astimezone(MINSK_TZ) + timedelta(days=cfg.weekly_weekday)).replace(
        hour=cfg.weekly_hour
    )
    if publish_at <= now <= publish_at + cfg.weekly_catch_up:
        return boundary
    return None


def last_week_summary(session: Session, now: datetime) -> WeeklySummary:
    """Сводка за последнюю закончившуюся неделю."""
    return weekly_summary(session, week_boundary(now))


def find_post(session: Session, period: str) -> ChannelPost | None:
    return session.scalar(
        select(ChannelPost).where(ChannelPost.kind == KIND, ChannelPost.period == period)
    )


# Публикация «в процессе» дольше этого — бот упал между записью и отправкой: считаем её
# неудавшейся (повторить может админ), а не «уже опубликованной».
STALE_PENDING = timedelta(minutes=30)


@dataclass(frozen=True)
class Reservation:
    status: Literal["reserved", "sent", "in_progress", "skipped"]
    period: str
    sent_at: datetime | None = None
    text: str | None = None  # текст сводки, если reserved


def reserve_post(
    session: Session, period: str, now: datetime, *, force: bool = False
) -> Reservation:
    """Занять публикацию периода.

    - новой записи нет или прошлая failed (или зависла в pending дольше STALE_PENDING) →
      reserved;
    - уже опубликована → sent (с `force` — reserved, публикуем ещё раз);
    - pending моложе STALE_PENDING → in_progress: публикация идёт прямо сейчас."""
    post = find_post(session, period)
    if post is None:
        session.add(ChannelPost(kind=KIND, period=period, status="pending", created_at=now))
        session.flush()
        return Reservation("reserved", period)
    if post.status == "pending" and now - post.created_at < STALE_PENDING:
        return Reservation("in_progress", period)
    if post.status == "sent" and not force:
        return Reservation("sent", period, sent_at=post.sent_at)
    post.status, post.created_at, post.sent_at = "pending", now, None
    session.flush()
    return Reservation("reserved", period)


def recover_posts(session: Session) -> list[str]:
    """После перезапуска: pending — исход неизвестен (пост мог выйти). Помечаем failed и
    возвращаем периоды, чтобы бот спросил админа, а не публиковал вслепую второй раз."""
    posts = session.scalars(
        select(ChannelPost).where(ChannelPost.kind == KIND, ChannelPost.status == "pending")
    ).all()
    for post in posts:
        post.status = "failed"
    session.flush()
    return [post.period for post in posts]


def finish_post(session: Session, period: str, ok: bool, now: datetime) -> None:
    post = find_post(session, period)
    if post is not None:
        post.status = "sent" if ok else "failed"
        post.sent_at = now if ok else None
        session.flush()
