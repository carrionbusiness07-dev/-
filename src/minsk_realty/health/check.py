"""Проверка источников по журналу `source_runs` и отправка алертов администратору."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from minsk_realty.db.models import SourceRun

log = logging.getLogger(__name__)

SILENCE_WINDOW = timedelta(hours=24)
FAILURES_IN_A_ROW = 3


def check_sources(
    session: Session, sources: Iterable[str], *, now: datetime | None = None
) -> list[str]:
    """Вернуть тексты алертов. Пустой список — всё в порядке."""
    now = now or datetime.now(UTC)
    alerts = []
    for source in sources:
        recent = session.scalars(
            select(SourceRun)
            .where(SourceRun.source == source, SourceRun.finished_at.is_not(None))
            .order_by(SourceRun.started_at.desc())
            .limit(FAILURES_IN_A_ROW)
        ).all()
        if not recent:
            continue  # источник ещё ни разу не запускался
        latest = recent[0]
        if latest.status == "blocked":
            alerts.append(
                f"{source}: площадка отказывает (403/429), сбор остановлен. {latest.error}"
            )
        elif len(recent) == FAILURES_IN_A_ROW and all(r.status != "ok" for r in recent):
            alerts.append(f"{source}: {FAILURES_IN_A_ROW} прохода подряд с ошибкой. {latest.error}")

        got_data = session.scalar(
            select(SourceRun.id)
            .where(
                SourceRun.source == source,
                SourceRun.status == "ok",
                SourceRun.listings_seen > 0,
                SourceRun.started_at >= now - SILENCE_WINDOW,
            )
            .limit(1)
        )
        oldest_run = session.scalar(
            select(SourceRun.started_at)
            .where(SourceRun.source == source)
            .order_by(SourceRun.started_at)
            .limit(1)
        )
        if got_data is None and oldest_run is not None and oldest_run <= now - SILENCE_WINDOW:
            alerts.append(f"{source}: 0 объявлений за последние сутки — проверьте парсер")
    return alerts


def notify(text: str) -> None:
    """Отправить алерт в Telegram администратору, если заданы TELEGRAM_BOT_TOKEN и ADMIN_CHAT_ID.

    Без них алерт только пишется в лог.
    """
    log.warning("ALERT %s", text)
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("ADMIN_CHAT_ID")
    if not (token and chat_id):
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"⚠️ {text}"},
            timeout=10,
        ).raise_for_status()
    except httpx.HTTPError:
        log.exception("не удалось отправить алерт в Telegram")
