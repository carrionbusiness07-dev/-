"""Выводы из истории объявлений (зона Leo): срок экспозиции, история цены, медианы,
индекс торга, отчёт /check, недельная сводка, обзор рынка, метрики гипотезы.

Функции возвращают структуры (frozen dataclass) — их используют и бот, и веб-кабинет.
Тексты — отдельно, в `render`. `to_jsonable` превращает любую структуру в JSON-совместимый вид.

    baseline = market_baseline(session, now)                 # кэшировать на час
    report = build_check_report(session, url, now, baseline=baseline)
    text = render_check_text(report)
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from minsk_realty.analytics.assessment import Assessment
from minsk_realty.analytics.bargaining import Bargaining, BargainInputs, bargaining_index
from minsk_realty.analytics.comparables import Comparison
from minsk_realty.analytics.config import DEFAULT_ANALYTICS, AnalyticsConfig
from minsk_realty.analytics.data import MarketData, load_market
from minsk_realty.analytics.exposure import Exposure, compute_exposure
from minsk_realty.analytics.geo import Location
from minsk_realty.analytics.history import PricePoint, build_price_history
from minsk_realty.analytics.hypothesis import (
    HypothesisReport,
    compute_hypothesis,
    hypothesis_report,
)
from minsk_realty.analytics.medians import MarketBaseline, MedianCell, compute_baseline
from minsk_realty.analytics.overview import (
    MarketOverview,
    SourceHealth,
    compute_overview,
    market_overview,
    source_health_summary,
)
from minsk_realty.analytics.render import (
    render_check_text,
    render_hypothesis_text,
    render_weekly_text,
)
from minsk_realty.analytics.report import CheckReport, build_check_report
from minsk_realty.analytics.weekly import WeeklySummary, compute_weekly, weekly_summary


def market_baseline(
    session: Session, now: datetime | None = None, *, cfg: AnalyticsConfig = DEFAULT_ANALYTICS
) -> MarketBaseline:
    """Медианы рынка на `now` по всей БД (для кэша перед серией /check)."""
    return compute_baseline(load_market(session), now or datetime.now(UTC), cfg=cfg)


def object_exposure(
    session: Session, object_id: int, now: datetime | None = None
) -> Exposure | None:
    """Срок экспозиции объекта по надёжным объявлениям."""
    data = load_market(session, object_ids=[object_id])
    return compute_exposure(
        data.object_listings(object_id),
        data.object_events(object_id),
        now or datetime.now(UTC),
        data.source_starts,
    )


def object_price_history(session: Session, object_id: int) -> list[PricePoint]:
    """История цены объекта по надёжным объявлениям."""
    data = load_market(session, object_ids=[object_id])
    return build_price_history(
        data.object_listings(object_id), data.snapshots, data.object_events(object_id)
    )


def to_jsonable(value: Any) -> Any:
    """dataclass/Decimal/datetime/tuple → dict/str/list. Для API кабинета."""
    if is_dataclass(value) and not isinstance(value, type):
        out = {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
        for name in ("enough", "share", "relisted_share", "usd_per_m2"):
            attr = getattr(type(value), name, None)
            if isinstance(attr, property):
                out[name] = to_jsonable(getattr(value, name))
        return out
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]
    return value


__all__ = [
    "DEFAULT_ANALYTICS",
    "AnalyticsConfig",
    "Assessment",
    "BargainInputs",
    "Bargaining",
    "CheckReport",
    "Comparison",
    "Exposure",
    "HypothesisReport",
    "Location",
    "MarketBaseline",
    "MarketData",
    "MarketOverview",
    "MedianCell",
    "PricePoint",
    "SourceHealth",
    "WeeklySummary",
    "bargaining_index",
    "build_check_report",
    "build_price_history",
    "compute_baseline",
    "compute_exposure",
    "compute_hypothesis",
    "compute_overview",
    "compute_weekly",
    "hypothesis_report",
    "load_market",
    "market_baseline",
    "market_overview",
    "object_exposure",
    "object_price_history",
    "render_check_text",
    "render_hypothesis_text",
    "render_weekly_text",
    "source_health_summary",
    "to_jsonable",
    "weekly_summary",
]
