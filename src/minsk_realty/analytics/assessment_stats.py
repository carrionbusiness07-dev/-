"""Как оценка «цена против похожих» раскладывает весь рынок: для проверки порогов перед выкаткой.

Для каждого активного объекта те же входы, что у отчёта /check (`build_check_report`), но из одного
`load_market` и одного `compute_baseline`, без отчёта на объект.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from minsk_realty.analytics.comparables import find_comparison
from minsk_realty.analytics.config import DEFAULT_ANALYTICS, AnalyticsConfig
from minsk_realty.analytics.data import MarketData
from minsk_realty.analytics.medians import MarketBaseline

GROUPS = ("suspicious", "below", "market", "above", "unknown")
STEPS = ("near", "wide", "district")
FINISHES = ("none", "done", "unknown")
FINISH_LABELS = {"none": "без отделки", "done": "с ремонтом", "unknown": "неизвестна"}


@dataclass(frozen=True)
class Count:
    n: int
    share: float  # доля от всех активных объектов


@dataclass(frozen=True)
class AssessmentStats:
    total: int
    groups: dict[str, Count]  # unknown — сравнения нет
    steps: dict[str, Count]  # только объекты со сравнением
    no_coords: Count
    approx: Count  # координата неточная (центр улицы)
    finishing_known: Count  # отделка известна
    # Раскладка по группам отдельно для отделки none/done/unknown; доля — внутри своей отделки.
    by_finish: dict[str, dict[str, Count]]
    sample: tuple[int, ...]  # object_id для ручного просмотра


def assessment_stats(
    data: MarketData,
    baseline: MarketBaseline,
    at: datetime,
    *,
    seed: int = 1,
    cfg: AnalyticsConfig = DEFAULT_ANALYTICS,
) -> AssessmentStats:
    groups = dict.fromkeys(GROUPS, 0)
    steps = dict.fromkeys(STEPS, 0)
    no_coords = approx = 0
    by_finish = {f: dict.fromkeys(GROUPS, 0) for f in FINISHES}
    active = [oid for oid in data.objects if data.object_active_at(oid, at)]
    for oid in active:
        price = data.object_price(oid, at)
        rooms = (price.rooms if price else None) or data.object_rooms(oid)
        point = data.object_point(oid, cfg.approx_coord_sources)
        no_coords += point is None
        approx += point is not None and point[2]
        comparison = find_comparison(
            baseline.points,
            object_id=oid,
            point=point,
            rooms=rooms,
            year=data.building_years.get(oid),
            finish=data.finishings.get(oid),
            usd_per_m2=price.usd_per_m2 if price else None,
            district_cell=baseline.price_for(data.object_district(oid), rooms),
            now_year=at.year,
            cfg=cfg,
        )
        group = comparison.group if comparison else "unknown"
        groups[group] += 1
        by_finish[data.finishings.get(oid, "unknown")][group] += 1
        if comparison:
            steps[comparison.step] += 1

    total = len(active)

    def count(n: int) -> Count:
        return Count(n, n / total if total else 0.0)

    def share_in(counts: dict[str, int]) -> dict[str, Count]:
        size = sum(counts.values())
        return {k: Count(v, v / size if size else 0.0) for k, v in counts.items()}

    known = sum(sum(g.values()) for f, g in by_finish.items() if f != "unknown")
    sample = random.Random(seed).sample(sorted(active), min(10, total))
    return AssessmentStats(
        total,
        {k: count(v) for k, v in groups.items()},
        {k: count(v) for k, v in steps.items()},
        count(no_coords),
        count(approx),
        count(known),
        {f: share_in(g) for f, g in by_finish.items()},
        tuple(sample),
    )


def render_stats_text(s: AssessmentStats, seconds: float) -> str:
    def line(label: str, c: Count) -> str:
        return f"{label}: {c.n} ({c.share:.1%})"

    lines = [f"всего активных объектов: {s.total}"]
    lines += [line(k, c) for k, c in s.groups.items()]
    lines += [line(k, c) for k, c in s.steps.items()]
    lines += [line("без координат", s.no_coords), line("неточная точка", s.approx)]
    lines.append(line("отделка известна", s.finishing_known))
    for f, groups in s.by_finish.items():
        lines.append(f"{FINISH_LABELS[f]} ({sum(c.n for c in groups.values())}):")
        lines += [f"  {line(k, c)}" for k, c in groups.items()]
    lines += [
        "10 случайных объектов:",
        ", ".join(map(str, s.sample)),
        f"посчитано за {seconds:.1f} с",
    ]
    return "\n".join(lines)
