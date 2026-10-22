"""Одна точка входа после прохода сборщика: пересмотр склеек, склейка новых, события."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from minsk_realty.db.models import Listing
from minsk_realty.matching.config import DEFAULT_CONFIG, MatchingConfig
from minsk_realty.matching.events import generate_events
from minsk_realty.matching.features import FeatureCache
from minsk_realty.matching.matcher import (
    create_object,
    detach,
    evaluate,
    link,
    linked_features,
)
from minsk_realty.matching.rules import compare

log = logging.getLogger(__name__)


@dataclass
class ProcessStats:
    linked: Counter[str] = field(default_factory=Counter)  # по уверенности
    created: int = 0
    deferred: int = 0
    ambiguous: int = 0
    regraded: int = 0
    detached: int = 0
    events: Counter[str] = field(default_factory=Counter)

    def __str__(self) -> str:
        linked = ", ".join(f"{k}={v}" for k, v in sorted(self.linked.items())) or "0"
        events = ", ".join(f"{k}={v}" for k, v in sorted(self.events.items())) or "0"
        return (
            f"склеено: {linked}; новых объектов: {self.created} (неоднозначных {self.ambiguous}); "
            f"отложено: {self.deferred}; пересмотрено: {self.regraded}; "
            f"отцеплено: {self.detached}; событий: {events}"
        )


def process_after_run(
    session: Session,
    source: str | None = None,
    now: datetime | None = None,
    *,
    config: MatchingConfig = DEFAULT_CONFIG,
    full: bool = False,
) -> ProcessStats:
    """Обработать БД после прохода сборщика. Коммит — на стороне вызывающего.

    1. Пересмотреть склейки medium/low (могли появиться фото или этаж): поднять или понизить
       уверенность, при противоречии — отцепить объявление.
    2. Склеить объявления без объекта (новые, отложенные, отцеплённые) — по всем площадкам,
       `source` нужен только для журнала.
    3. Сгенерировать события для объектов, которые недавно были на рынке или изменились.

    Повторный вызов с теми же данными ничего не меняет. `full=True` — пересмотреть всё,
    а не только объявления, виденные за `config.events_lookback`.
    """
    now = now or datetime.now(UTC)
    cfg = config
    stats = ProcessStats()
    cache = FeatureCache(session)
    touched: set[int] = set()
    since = None if full else now - cfg.events_lookback

    # 1. Пересмотр слабых и средних склеек.
    query = select(Listing).where(Listing.match_confidence.in_(("medium", "low")))
    if since is not None:
        query = query.where(Listing.last_seen >= since)
    for listing in session.scalars(query.order_by(Listing.id)).all():
        f = cache.get(listing)
        trusted, weak = linked_features(session, cache, listing.object_id, exclude=listing.id)
        if not trusted:
            continue
        verdict = compare(f, listing.object_id, trusted, cfg, now, weak=weak)
        if verdict.outcome == "match" and verdict.confidence != listing.match_confidence:
            log.info(
                "склейка %s → объект #%d: %s → %s",
                f.label,
                listing.object_id,
                listing.match_confidence,
                verdict.confidence,
            )
            listing.match_confidence = verdict.confidence
            touched.add(listing.object_id)
            stats.regraded += 1
        elif verdict.outcome == "reject":
            log.warning(
                "отцепляю %s от объекта #%d: %s", f.label, listing.object_id, verdict.summary()
            )
            touched.add(listing.object_id)
            detach(session, listing)
            stats.detached += 1
    session.flush()

    # 2. Объявления без объекта — по порядку появления: старые основывают объекты первыми.
    pending = session.scalars(
        select(Listing).where(Listing.object_id.is_(None)).order_by(Listing.first_seen, Listing.id)
    ).all()
    for listing in pending:
        f = cache.get(listing)
        result = evaluate(session, cache, f, cfg, now)
        if result.action == "defer":
            stats.deferred += 1
            continue
        if result.action == "link":
            link(session, listing, f, result.object_id, result.confidence)
            stats.linked[result.confidence] += 1
        else:
            create_object(session, listing, f)
            stats.created += 1
            stats.ambiguous += result.note.startswith("неоднозначно")
        touched.add(listing.object_id)
    session.flush()

    # 3. События.
    scope = set(touched)
    recent = select(Listing.object_id).where(Listing.object_id.is_not(None))
    if since is not None:
        recent = recent.where(Listing.last_seen >= since)
    scope.update(session.scalars(recent.distinct()))
    for object_id in sorted(scope):
        stats.events.update(generate_events(session, object_id, cfg, now))
    session.flush()

    log.info("склейка после прохода %s: %s", source or "-", stats)
    return stats
