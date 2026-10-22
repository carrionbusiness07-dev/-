"""Обзор рынка и здоровье источников — для веб-кабинета (дашборда).

`market_overview` — цифры на `now`: активные объекты, новые/снижения/снятия за 7 дней,
медианы $/м² (Минск, районы, комнатность), медиана срока, топ снижений недели.
`source_health_summary` — по журналу `source_runs`: последний проход, статус, сколько
объявлений, ошибки, плюс активные алерты из `health.check_sources`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minsk_realty.analytics.config import DEFAULT_ANALYTICS, AnalyticsConfig
from minsk_realty.analytics.data import MarketData, as_utc, load_market
from minsk_realty.analytics.medians import MarketBaseline, MedianCell, compute_baseline
from minsk_realty.analytics.weekly import WEEK, PeriodCounts, TopDrop, period_counts, top_drops
from minsk_realty.db.models import SOURCES, Listing, SourceRun
from minsk_realty.health.check import check_sources


@dataclass(frozen=True)
class MarketOverview:
    at: datetime
    observation_start: datetime | None
    active_objects: int
    active_listings: int
    last_7d: PeriodCounts
    city: MedianCell | None
    by_district: tuple[MedianCell, ...]
    by_rooms: tuple[MedianCell, ...]  # по Минску: 1, 2, 3, 4+
    exposure_median: MedianCell | None
    exposure_truncated_share: float | None
    top_drops: tuple[TopDrop, ...]


def compute_overview(
    data: MarketData,
    now: datetime,
    *,
    baseline: MarketBaseline | None = None,
    cfg: AnalyticsConfig = DEFAULT_ANALYTICS,
    top: int = 10,
) -> MarketOverview:
    baseline = baseline or compute_baseline(data, now, cfg=cfg)
    rooms = tuple(c for c in baseline.price_cells if c.district is None and c.rooms is not None)
    return MarketOverview(
        at=now,
        observation_start=data.observation_start,
        active_objects=baseline.active_objects,
        active_listings=sum(1 for r in data.listings.values() if r.active),
        last_7d=period_counts(data, now - WEEK, now),
        city=baseline.city(),
        by_district=tuple(baseline.districts()),
        by_rooms=rooms,
        exposure_median=baseline.exposure_cell(None),
        exposure_truncated_share=baseline.exposure_truncated_share,
        top_drops=tuple(top_drops(data, now - WEEK, now, cfg=cfg, limit=top)),
    )


def market_overview(
    session: Session,
    now: datetime | None = None,
    *,
    cfg: AnalyticsConfig = DEFAULT_ANALYTICS,
    top: int = 10,
) -> MarketOverview:
    return compute_overview(load_market(session), now or datetime.now(UTC), cfg=cfg, top=top)


# --- Источники ---------------------------------------------------------------------------

HealthState = Literal["ok", "warning", "failing", "blocked", "never_run"]
STALE_AFTER = timedelta(hours=3)  # опрос раз в 30–60 мин: 3 часа без успешного прохода — повод


@dataclass(frozen=True)
class SourceHealth:
    source: str
    state: HealthState
    last_run_at: datetime | None
    last_run_status: str | None
    last_run_complete: bool | None
    last_run_listings: int | None
    last_run_parse_errors: int | None
    last_run_error: str | None
    last_ok_at: datetime | None
    runs_24h: int
    failed_24h: int
    active_listings: int
    alerts: tuple[str, ...]


def source_health_summary(
    session: Session, now: datetime | None = None, *, sources: tuple[str, ...] = SOURCES
) -> tuple[SourceHealth, ...]:
    now = now or datetime.now(UTC)
    alerts: dict[str, list[str]] = {}
    for alert in check_sources(session, sources, now=now):
        alerts.setdefault(alert.key.split(":")[0], []).append(alert.text)
    active = dict(
        session.execute(
            select(Listing.source, func.count())
            .where(Listing.status == "active")
            .group_by(Listing.source)
        ).all()
    )
    out = []
    for source in sources:
        last = session.scalar(
            select(SourceRun)
            .where(SourceRun.source == source)
            .order_by(SourceRun.started_at.desc(), SourceRun.id.desc())
            .limit(1)
        )
        last_ok = session.scalar(
            select(func.max(SourceRun.started_at)).where(
                SourceRun.source == source, SourceRun.status == "ok"
            )
        )
        recent = (
            session.execute(
                select(SourceRun.status).where(
                    SourceRun.source == source, SourceRun.started_at >= now - timedelta(hours=24)
                )
            )
            .scalars()
            .all()
        )
        source_alerts = tuple(alerts.get(source, ()))
        last_ok_at = as_utc(last_ok) if last_ok is not None else None
        if last is None:
            state: HealthState = "never_run"
        elif last.status == "blocked":
            state = "blocked"
        elif source_alerts and any("подряд" in a or "0 записанных" in a for a in source_alerts):
            state = "failing"
        elif source_alerts or last_ok_at is None or now - last_ok_at > STALE_AFTER:
            state = "warning"
        else:
            state = "ok"
        out.append(
            SourceHealth(
                source=source,
                state=state,
                last_run_at=last.started_at if last else None,
                last_run_status=last.status if last else None,
                last_run_complete=last.complete if last else None,
                last_run_listings=last.listings_seen if last else None,
                last_run_parse_errors=last.parse_errors if last else None,
                last_run_error=last.error if last else None,
                last_ok_at=last_ok_at,
                runs_24h=len(recent),
                failed_24h=sum(1 for s in recent if s in ("failed", "blocked")),
                active_listings=int(active.get(source, 0)),
                alerts=source_alerts,
            )
        )
    return tuple(out)
