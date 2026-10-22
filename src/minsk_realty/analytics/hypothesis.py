"""Проверка гипотезы на 6-й неделе (docs/01-idea.md, раздел 9).

Единица — объект (квартира) с хотя бы одним надёжным объявлением. События — только надёжные
(`trusted=True`).

Метрики:
- relisted         — доля объектов с событием `relisted` или `returned` под новым объявлением;
- cross_listed     — доля объектов с `cross_listed` (2+ площадки или 2+ продавца одновременно);
- relisted_or_cross — объединение двух предыдущих (это и есть «≥10%» из идеи);
- with_drops       — доля объектов с хотя бы одним `price_drop`;
- with_insight     — доля объектов, где /check покажет то, чего нет на сайте: перевыкладывание,
                     дубль, снижение цены или срок на рынке больше счётчика сайта на 7+ дней;
- те же доли среди «зрелых» объектов (видим ≥ 14 дней): новым объектам ещё некогда было
  перевыложиться, поэтому общая доля на старте занижена;
- распределение срока экспозиции (все объекты, для снятых — до снятия) и доля «как минимум».

Решение:
- too_early  — меньше 6 недель наблюдения или меньше 200 объектов (метрики показываем);
- continue   — relisted_or_cross ≥ 10%, with_drops ≥ 10%, with_insight ≥ 50%;
- stop       — relisted_or_cross < 5% и with_insight < 50%;
- borderline — остальное: решает пользователь, причины перечислены.
Пороги кроме «≥10%» — допущения (см. config.py), их стоит подтвердить с пользователем.
"""

from __future__ import annotations

import random
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.orm import Session

from minsk_realty.analytics.config import DEFAULT_ANALYTICS, AnalyticsConfig
from minsk_realty.analytics.data import MarketData, load_market
from minsk_realty.analytics.exposure import compute_exposure

Decision = Literal["continue", "stop", "borderline", "too_early"]

BUCKETS = ((0, 7, "до 7 дней"), (7, 30, "7–30"), (30, 90, "30–90"), (90, 180, "90–180"))
BUCKET_LAST = (180, None, "180+")


@dataclass(frozen=True)
class Share:
    count: int
    total: int

    @property
    def share(self) -> float | None:
        return self.count / self.total if self.total else None


@dataclass(frozen=True)
class Bucket:
    label: str
    lo: int
    hi: int | None
    count: int
    share: float | None


@dataclass(frozen=True)
class Distribution:
    n: int
    p25: float | None
    p50: float | None
    p75: float | None
    p90: float | None
    buckets: tuple[Bucket, ...]
    at_least_share: float | None


@dataclass(frozen=True)
class Criterion:
    key: str
    label: str
    value: float | None
    threshold: float
    passed: bool


@dataclass(frozen=True)
class HypothesisReport:
    at: datetime
    observation_start: datetime | None
    observation_days: int
    objects: int
    mature_objects: int
    listings_by_source: tuple[tuple[str, int], ...]
    low_listings: Share  # объявления со склейкой low среди привязанных
    relisted: Share
    cross_listed: Share
    relisted_or_cross: Share
    with_drops: Share
    with_insight: Share
    returned: Share
    removed: Share
    relisted_or_cross_mature: Share
    with_drops_mature: Share
    exposure: Distribution
    sample_object_ids: tuple[int, ...]  # для ручной проверки через /check
    decision: Decision
    criteria: tuple[Criterion, ...]
    reasons: tuple[str, ...]


def distribution(values: list[int], at_least: list[bool]) -> Distribution:
    n = len(values)
    if n == 0:
        return Distribution(0, None, None, None, None, (), None)
    if n >= 2:
        qs = statistics.quantiles(values, n=20, method="inclusive")
        p25, p50, p75, p90 = qs[4], qs[9], qs[14], qs[17]
    else:
        p25 = p50 = p75 = p90 = float(values[0])
    buckets = []
    for lo, hi, label in (*BUCKETS, BUCKET_LAST):
        count = sum(1 for v in values if v >= lo and (hi is None or v < hi))
        buckets.append(Bucket(label, lo, hi, count, count / n))
    return Distribution(n, p25, p50, p75, p90, tuple(buckets), sum(at_least) / n)


def compute_hypothesis(
    data: MarketData, now: datetime, *, cfg: AnalyticsConfig = DEFAULT_ANALYTICS
) -> HypothesisReport:
    objects = [oid for oid in data.objects if data.object_listings(oid)]
    flags: dict[str, set[int]] = {k: set() for k in ("relisted", "cross", "drops", "insight")}
    flags |= {"returned": set(), "removed": set()}
    mature: set[int] = set()
    days: list[int] = []
    at_least: list[bool] = []
    for oid in objects:
        listings = data.object_listings(oid)
        events = data.object_events(oid)
        types = Counter(e.type for e in events)
        current = max(listings, key=lambda r: (r.active, r.first_seen, r.id))
        exp = compute_exposure(listings, events, now, data.source_starts, current=current, cfg=cfg)
        days.append(exp.days)
        at_least.append(exp.at_least)
        if min(r.first_seen for r in listings) <= now - cfg.mature_after:
            mature.add(oid)
        if types["relisted"] or exp.returned_new_listing:
            flags["relisted"].add(oid)
        if types["cross_listed"]:
            flags["cross"].add(oid)
        if types["price_drop"]:
            flags["drops"].add(oid)
        if types["returned"]:
            flags["returned"].add(oid)
        if types["removed"] and exp.state == "removed":
            flags["removed"].add(oid)
        hidden_age = (
            exp.site_days is not None and exp.days - exp.site_days >= cfg.site_counter_gap_days
        )
        if oid in flags["relisted"] | flags["cross"] | flags["drops"] or hidden_age:
            flags["insight"].add(oid)

    total = len(objects)
    either = flags["relisted"] | flags["cross"]
    linked = [r for r in data.listings.values() if r.object_id is not None]
    obs_start = data.observation_start
    obs_days = (now - obs_start).days if obs_start else 0
    rnd = random.Random(f"{now.date()}")
    sample = tuple(sorted(rnd.sample(objects, min(cfg.sample_size, total)))) if total else ()

    report = {
        "at": now,
        "observation_start": obs_start,
        "observation_days": obs_days,
        "objects": total,
        "mature_objects": len(mature),
        "listings_by_source": tuple(
            sorted(Counter(r.source for r in data.listings.values()).items())
        ),
        "low_listings": Share(sum(1 for r in linked if r.confidence == "low"), len(linked)),
        "relisted": Share(len(flags["relisted"]), total),
        "cross_listed": Share(len(flags["cross"]), total),
        "relisted_or_cross": Share(len(either), total),
        "with_drops": Share(len(flags["drops"]), total),
        "with_insight": Share(len(flags["insight"]), total),
        "returned": Share(len(flags["returned"]), total),
        "removed": Share(len(flags["removed"]), total),
        "relisted_or_cross_mature": Share(len(either & mature), len(mature)),
        "with_drops_mature": Share(len(flags["drops"] & mature), len(mature)),
        "exposure": distribution(days, at_least),
        "sample_object_ids": sample,
    }
    decision, criteria, reasons = decide(report, cfg)
    return HypothesisReport(**report, decision=decision, criteria=criteria, reasons=reasons)


def decide(
    m: dict, cfg: AnalyticsConfig
) -> tuple[Decision, tuple[Criterion, ...], tuple[str, ...]]:
    def crit(key: str, label: str, share: Share, threshold: float) -> Criterion:
        value = share.share
        return Criterion(key, label, value, threshold, value is not None and value >= threshold)

    criteria = (
        crit(
            "relisted_or_cross",
            "перевыкладывались или есть на нескольких площадках/у нескольких продавцов",
            m["relisted_or_cross"],
            cfg.relist_or_cross_min,
        ),
        crit("with_drops", "есть снижения цены", m["with_drops"], cfg.drops_min),
        crit(
            "with_insight",
            "отчёт показывает то, чего нет на сайте",
            m["with_insight"],
            cfg.insight_min,
        ),
    )
    reasons: list[str] = []
    early = m["observation_days"] < cfg.hypothesis_weeks * 7 or m["objects"] < cfg.min_objects
    if m["observation_days"] < cfg.hypothesis_weeks * 7:
        reasons.append(
            f"наблюдаем {m['observation_days']} дн., для решения нужно {cfg.hypothesis_weeks * 7}"
        )
    if m["objects"] < cfg.min_objects:
        reasons.append(f"объектов {m['objects']}, для решения нужно хотя бы {cfg.min_objects}")
    sources = [s for s, n in m["listings_by_source"] if n]
    if len(sources) < 2:
        reasons.append(
            "собираем одну площадку: дубли между площадками не видны, доля cross_listed занижена"
        )
    low = m["low_listings"].share
    if low and low >= 0.1:
        reasons.append(
            f"{low:.0%} привязанных объявлений склеены с низкой уверенностью — их перевыкладывания "
            "и дубли не считаются, метрики занижены (вопрос к Вере)"
        )
    for c in criteria:
        if not c.passed:
            value = "нет данных" if c.value is None else f"{c.value:.1%}"
            reasons.append(f"не выполнено: {c.label} — {value}, порог {c.threshold:.0%}")

    either = m["relisted_or_cross"].share or 0.0
    insight = m["with_insight"].share or 0.0
    if early:
        decision: Decision = "too_early"
    elif all(c.passed for c in criteria):
        decision = "continue"
    elif either < cfg.stop_max and insight < cfg.insight_min:
        decision = "stop"
    else:
        decision = "borderline"
    return decision, criteria, tuple(reasons)


def hypothesis_report(
    session: Session, now: datetime | None = None, *, cfg: AnalyticsConfig = DEFAULT_ANALYTICS
) -> HypothesisReport:
    return compute_hypothesis(load_market(session), now or datetime.now(UTC), cfg=cfg)
