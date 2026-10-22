"""Индекс торга: простое правило из баллов, у каждого балла — причина словами.

| Сигнал                                              | Баллы |
|-----------------------------------------------------|-------|
| срок ≥ 2× медианы срока по району (или ≥ 90 дней)    | +2    |
| срок ≥ 1.25× медианы (или ≥ 45 дней)                 | +1    |
| срок < 14 дней                                      | −1    |
| снижений ≥ 2 / одно                                 | +2/+1 |
| перевыкладывали (relisted, возвращение под новым ID) | +1    |
| на другой площадке / у другого продавца дешевле      | +1    |
| цена за м² ≥ медианы + 10%                           | +1    |
| цена за м² ≤ медианы − 10%                           | −1    |
| цену поднимали                                      | −1    |

Уровень: ≥ 4 — высокий, 2–3 — средний, ≤ 1 — низкий. Если объект видим впервые и истории нет,
уровень «мало данных». Ориентир первого предложения: высокий — 3–5%, средний — 1.5–3%
ниже самой низкой цены этой квартиры (с учётом более дешёвого объявления на другой площадке).
Это эвристика, не гарантия: пороги не откалиброваны (калибруем на 6-й неделе по объектам,
которые ушли с рынка).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from minsk_realty.analytics.config import DEFAULT_ANALYTICS, AnalyticsConfig
from minsk_realty.analytics.fmt import (
    SOURCE_NAMES,
    days_text,
    money,
    offer_range,
    pct,
    times_text,
)

Level = Literal["low", "medium", "high", "unknown"]


@dataclass(frozen=True)
class CheaperOffer:
    """То же объявление дешевле: другая площадка или другой продавец (только high/medium)."""

    listing_id: int
    source: str
    amount: Decimal
    currency: str
    diff: Decimal  # насколько дешевле, в той же валюте
    url: str
    other_seller: bool


@dataclass(frozen=True)
class BargainInputs:
    has_history: bool
    days_on_market: int | None = None
    at_least: bool = False
    exposure_median_days: float | None = None
    exposure_scope: str = "по району"
    drops: int = 0
    ups: int = 0
    relists: int = 0
    cheaper: CheaperOffer | None = None
    vs_median_pct: float | None = None
    median_scope: str = "района"
    current_amount: Decimal | None = None
    currency: str | None = None


@dataclass(frozen=True)
class BargainReason:
    code: str
    weight: int
    short: str  # для строки «Вывод: …»
    text: str  # подробное объяснение (кабинет, кнопка «почему»)


@dataclass(frozen=True)
class Bargaining:
    level: Level
    score: int
    reasons: tuple[BargainReason, ...]
    recommendation: str
    offer_low: Decimal | None = None
    offer_high: Decimal | None = None
    offer_currency: str | None = None
    disclaimer: str = field(default="Это рекомендация по истории объявлений, а не гарантия скидки.")


def bargaining_index(inp: BargainInputs, cfg: AnalyticsConfig = DEFAULT_ANALYTICS) -> Bargaining:
    reasons: list[BargainReason] = []
    if not inp.has_history:
        return Bargaining(
            level="unknown",
            score=0,
            reasons=(),
            recommendation=(
                "Истории пока мало, чтобы судить о торге. Нажмите «Следить» — сообщу, "
                "если цена изменится или объявление снимут."
            ),
        )

    days = inp.days_on_market
    if days is not None:
        at_least = "как минимум " if inp.at_least else ""
        med = inp.exposure_median_days
        if med:
            ratio = days / med
            versus = f"медиана {inp.exposure_scope} — {days_text(round(med))}"
            if ratio >= cfg.long_ratio_high:
                reasons.append(
                    BargainReason(
                        "long_exposure",
                        2,
                        f"висит {at_least}{days_text(days)}",
                        f"на рынке {at_least}{days_text(days)}, в {ratio:.1f} раза дольше "
                        f"обычного ({versus})",
                    )
                )
            elif ratio >= cfg.long_ratio_mid:
                reasons.append(
                    BargainReason(
                        "long_exposure",
                        1,
                        f"висит {at_least}{days_text(days)}",
                        f"на рынке {at_least}{days_text(days)}, дольше обычного ({versus})",
                    )
                )
        elif days >= cfg.long_days_mid:
            weight = 2 if days >= cfg.long_days_high else 1
            reasons.append(
                BargainReason(
                    "long_exposure",
                    weight,
                    f"висит {at_least}{days_text(days)}",
                    f"на рынке {at_least}{days_text(days)} (медианы срока по району пока нет)",
                )
            )
        if days < cfg.fresh_days and not inp.at_least:
            reasons.append(
                BargainReason(
                    "fresh",
                    -1,
                    f"на рынке всего {days_text(days)}",
                    f"объявлению {days_text(days)}: продавец обычно ещё не готов уступать",
                )
            )

    if inp.drops:
        words = {1: "один раз снижалась", 2: "дважды снижалась", 3: "трижды снижалась"}
        short = words.get(inp.drops, f"снижалась {times_text(inp.drops)}")
        reasons.append(
            BargainReason(
                "price_drops",
                2 if inp.drops >= 2 else 1,
                short,
                f"цену снижали {times_text(inp.drops)}: продавец уже идёт навстречу",
            )
        )
    if inp.relists:
        reasons.append(
            BargainReason(
                "relisted",
                1,
                f"перевыкладывалась {times_text(inp.relists)}",
                f"объявление перевыкладывали {times_text(inp.relists)} — с первого раза "
                "квартира не продалась, а счётчик дней на сайте обнулялся",
            )
        )
    if inp.cheaper is not None:
        c = inp.cheaper
        where = SOURCE_NAMES.get(c.source, c.source)
        who = " у другого продавца" if c.other_seller else ""
        reasons.append(
            BargainReason(
                "cheaper_elsewhere",
                1,
                f"на {where}{who} дешевле",
                f"эта же квартира на {where}{who} стоит {money(c.amount, c.currency)} — "
                f"дешевле на {money(c.diff, c.currency)}",
            )
        )
    if inp.vs_median_pct is not None:
        if inp.vs_median_pct >= cfg.median_gap_pct:
            reasons.append(
                BargainReason(
                    "above_median",
                    1,
                    f"на {pct(inp.vs_median_pct, signed=False)} дороже медианы",
                    f"цена за м² на {pct(inp.vs_median_pct, signed=False)} выше медианы "
                    f"{inp.median_scope}",
                )
            )
        elif inp.vs_median_pct <= -cfg.median_gap_pct:
            reasons.append(
                BargainReason(
                    "below_median",
                    -1,
                    f"уже на {pct(inp.vs_median_pct, signed=False)} ниже медианы",
                    f"цена за м² уже на {pct(inp.vs_median_pct, signed=False)} ниже медианы "
                    f"{inp.median_scope} — запас для торга меньше",
                )
            )
    if inp.ups:
        reasons.append(
            BargainReason(
                "price_up",
                -1,
                "цену поднимали",
                f"цену поднимали {times_text(inp.ups)}: продавец уверен в спросе",
            )
        )

    score = sum(r.weight for r in reasons)
    level: Level = (
        "high" if score >= cfg.level_high else "medium" if score >= cfg.level_medium else "low"
    )
    low = high = None
    recommendation = _recommendation(level, inp, reasons)
    band = {"high": cfg.offer_pct_high, "medium": cfg.offer_pct_medium}.get(level)
    if band and inp.current_amount and inp.currency:
        low, high = _offer(inp.current_amount, inp.currency, band, inp.cheaper)
        recommendation += (
            f" Ориентир для первого предложения — {offer_range(low, high, inp.currency)}"
        )
        if inp.cheaper is not None:
            where = SOURCE_NAMES.get(inp.cheaper.source, inp.cheaper.source)
            recommendation += f", сошлитесь на цену на {where}."
        else:
            recommendation += "."
    return Bargaining(
        level=level,
        score=score,
        reasons=tuple(reasons),
        recommendation=recommendation,
        offer_low=low,
        offer_high=high,
        offer_currency=inp.currency if low is not None else None,
    )


def _recommendation(level: Level, inp: BargainInputs, reasons: list[BargainReason]) -> str:
    if level == "high":
        return "Торг, скорее всего, реален."
    if level == "medium":
        return "Торг возможен, но небольшой."
    negative = [r for r in reasons if r.weight < 0]
    if negative:
        return f"Поводов для торга мало ({negative[0].short}): большую скидку вряд ли дадут."
    if any(r.weight > 0 for r in reasons):
        return (
            "Сильных поводов для торга нет: спросить о скидке можно, "
            "но на большую не рассчитывайте."
        )
    return (
        "Явных поводов для торга не видно: спросить о скидке можно, "
        "но рассчитывать на неё не стоит."
    )


def _offer(
    amount: Decimal, currency: str, band: tuple[float, float], cheaper: CheaperOffer | None
) -> tuple[Decimal, Decimal]:
    """Диапазон ниже цены на `band` %. База — самая низкая цена этой квартиры: если на
    другой площадке дешевле, торговаться имеет смысл от той цены."""
    step = Decimal(500) if currency in ("USD", "EUR") else Decimal(1000)
    if cheaper is not None and cheaper.currency == currency:
        amount = min(amount, cheaper.amount)
    top = amount * (1 - Decimal(str(band[0])) / 100)
    bottom = amount * (1 - Decimal(str(band[1])) / 100)
    return _round(bottom, step), _round(top, step)


def _round(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step
