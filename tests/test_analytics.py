"""Аналитика: срок экспозиции, история цены, медианы, индекс торга, отчёт /check, сводки.

Сети нет. Сценарии /check проходят через настоящую склейку (tests/analytics_data.py),
чистые функции проверяются на данных в памяти.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from analytics_data import (
    DAY,
    HOUR,
    KUFAR_URL,
    NOW,
    OBS_START,
    REALT_URL,
    add_background,
    build_docs_scenario,
    kufar,
    listing,
    observe,
    realt,
    start_observation,
)

from minsk_realty.analytics import (
    DEFAULT_ANALYTICS,
    BargainInputs,
    bargaining_index,
    build_check_report,
    build_price_history,
    compute_baseline,
    compute_exposure,
    compute_hypothesis,
    compute_overview,
    compute_weekly,
    load_market,
    market_baseline,
    market_overview,
    object_exposure,
    object_price_history,
    render_check_text,
    render_hypothesis_text,
    render_weekly_text,
    source_health_summary,
    to_jsonable,
)
from minsk_realty.analytics.bargaining import CheaperOffer
from minsk_realty.analytics.data import EventRow, ListingRow, MarketData, ObjectRow, SnapRow
from minsk_realty.analytics.fmt import (
    days_text,
    money,
    normalize_district,
    offer_range,
    pct,
    street_label,
)
from minsk_realty.analytics.medians import robust_summary
from minsk_realty.db.models import Base, SourceRun
from minsk_realty.db.session import make_engine, make_session_factory
from minsk_realty.matching import process_after_run

# Фон Фрунзенского р-на, 2-комн.: разброс как у живого рынка + два выброса.
FRUNZ_PPM = [1650, 1700, 1720, 1760, 1800, 1820, 1850, 1880, 1900, 1950, 2000, 2050]
OUTLIERS = [9000, 150]  # 9000 срежет IQR, 150 — грубая граница


@pytest.fixture
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'analytics.db'}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        yield session


@pytest.fixture
def docs(session):
    """Сценарий из docs/02 (1.2) + 12 объектов фона и 2 выброса во Фрунзенском районе."""
    build_docs_scenario(session)
    add_background(session, len(FRUNZ_PPM), ppm_values=FRUNZ_PPM)
    add_background(session, 2, ppm_values=OUTLIERS, prefix="out")
    process_after_run(session, None, now=NOW, full=True)
    return session


# --- Форматирование ---------------------------------------------------------------------


def test_formatting_helpers():
    assert money(Decimal("92500.00"), "USD") == "$92 500"
    assert money(300000, "BYN") == "300 000 BYN"
    assert [days_text(n) for n in (1, 3, 11, 21, 94)] == [
        "1 день",
        "3 дня",
        "11 дней",
        "21 день",
        "94 дня",
    ]
    assert offer_range(Decimal(88000), Decimal(89000), "USD") == "$88 000–89 000"
    assert offer_range(Decimal(86500), Decimal(88500), "USD") == "$86 500–88 500"
    assert offer_range(Decimal(300000), Decimal(310000), "BYN") == "300 000–310 000 BYN"
    assert pct(-2.94) == "−2.9%" and pct(0.4) == "+0.4%" and pct(12.3, signed=False) == "12%"
    for raw in ("фрунзенский", "Фрунзенский р-н", "Фрунзенский район"):
        assert normalize_district(raw) == "Фрунзенский"
    assert normalize_district("Минский район") is None
    assert street_label("ул притыцкого") == "ул. Притыцкого"
    assert street_label("пр-т независимости") == "пр-т Независимости"


# --- Медианы ----------------------------------------------------------------------------


def test_robust_summary_cuts_outliers_and_requires_ten_points():
    s = robust_summary([*FRUNZ_PPM, 1685, *OUTLIERS], bounds=(300, 10_000))
    assert s.n_raw == 15 and len(s.kept) == 13 and s.median == 1820
    # Девять точек — медианы нет, но точки посчитаны (для «мало данных: 9 объектов»).
    small = robust_summary(FRUNZ_PPM[:9], bounds=(300, 10_000))
    assert small.median is None and len(small.kept) == 9


def test_baseline_one_object_is_one_point(docs):
    baseline = market_baseline(docs, NOW)
    cell = baseline.price_cell("Фрунзенский", 2)
    # 12 фона + квартира сценария (три объявления — одна точка) + 2 выброса.
    assert (cell.n_raw, cell.n, cell.median) == (15, 13, 1820)
    # Точка квартиры — самое дешёвое активное надёжное объявление: Onliner, $91 000 / 54 м².
    data = load_market(docs)
    oid = listing(docs, "kufar", "1000201").object_id
    price = data.object_price(oid, NOW)
    assert (price.amount, round(price.usd_per_m2)) == (Decimal("91000.00"), 1685)
    # Медиана срока экспозиции по району тоже есть (≥ 10 активных объектов).
    assert baseline.exposure_for("Фрунзенский").enough


def test_baseline_small_sample_and_city_fallback(session):
    start_observation(session)
    add_background(session, 9, ppm_values=FRUNZ_PPM[:9])
    add_background(session, 3, district=None, ppm_values=[1500, 1600, 1700], prefix="nod")
    baseline = market_baseline(session, NOW)
    district = baseline.price_cell("Фрунзенский", 2)
    assert district.n == 9 and not district.enough
    # По району мало — берём Минск (12 объектов вместе с объектами без района).
    chosen = baseline.price_for("Фрунзенский", 2)
    assert chosen.district is None and chosen.enough and chosen.n == 12


# --- Срок экспозиции ---------------------------------------------------------------------


def _row(id_, *, first, last=NOW, status="active", posted=None, source="realt", **kw):
    data = {
        "id": id_,
        "object_id": kw.pop("object_id", 1),
        "source": source,
        "external_id": str(id_),
        "url": f"https://example.invalid/{id_}",
        "confidence": "high",
        "status": status,
        "first_seen": first,
        "last_seen": last,
        "posted_at": posted,
        "seller_type": "agency",
        "agency_name": None,
        "seller_key": f"seller-{id_}",
    }
    return ListingRow(**(data | kw))


def _event(id_, type_, at, listing_id=None, object_id=1, notify=True, **payload):
    # Событие склейки low: и без уведомления, и не надёжное (trusted=False).
    return EventRow(id_, object_id, listing_id, type_, at, at, notify, payload, trusted=notify)


STARTS = {"realt": OBS_START, "kufar": OBS_START}


def test_exposure_counts_from_first_listing_across_relists():
    first = _row(1, first=datetime(2026, 7, 20, tzinfo=UTC), last=datetime(2026, 9, 29, tzinfo=UTC))
    first = replace(first, status="inactive")
    second = _row(2, first=datetime(2026, 10, 3, tzinfo=UTC), source="kufar")
    events = [_event(1, "relisted", second.first_seen, listing_id=2)]
    e = compute_exposure([first, second], events, NOW, STARTS, current=second)
    assert (e.start, e.start_source, e.start_basis) == (first.first_seen, "realt", "first_seen")
    assert e.days == 94 and not e.at_least and e.state == "active"
    assert e.relist_count == 1 and e.site_days == 19


def test_exposure_uses_platform_date_and_marks_truncated_history():
    # Застали уже висящим в первый день наблюдения: срок — «как минимум».
    seen = _row(1, first=OBS_START + HOUR)
    e = compute_exposure([seen], [], NOW, STARTS)
    assert e.at_least and e.start_basis == "first_seen"
    # Площадка знает дату публикации раньше нашего первого замера — берём её.
    posted = replace(seen, posted_at=OBS_START - 30 * DAY)
    e = compute_exposure([posted], [], NOW, STARTS)
    assert e.start == OBS_START - 30 * DAY and e.start_basis == "posted_at_source" and e.at_least
    # Дата публикации пятилетней давности — мусор, не используем.
    junk = replace(seen, posted_at=OBS_START - 5 * 365 * DAY)
    assert compute_exposure([junk], [], NOW, STARTS).start_basis == "first_seen"


def test_exposure_of_removed_object_ends_when_last_seen():
    last = datetime(2026, 9, 1, tzinfo=UTC)
    row = _row(1, first=datetime(2026, 8, 1, tzinfo=UTC), last=last, status="inactive")
    e = compute_exposure([row], [_event(1, "removed", last)], NOW, STARTS)
    assert (e.state, e.end, e.days) == ("removed", last, 31)
    # Событие ещё не записано, но объявлений нет больше 3 суток — тоже снята.
    assert compute_exposure([row], [], NOW, STARTS).state == "removed"
    hidden = compute_exposure([row], [], last + 2 * DAY, STARTS)
    assert hidden.state == "hidden"


# --- История цены -------------------------------------------------------------------------


def _snap(listing_id, at, amount, currency="USD", usd=None, **kw):
    data = {
        "listing_id": listing_id,
        "seen_at": at,
        "amount": Decimal(amount) if amount is not None else None,
        "currency": currency,
        "usd": Decimal(usd) if usd is not None else None,
        "area": 50.0,
        "rooms": 2,
        "floor": None,
        "floors_total": None,
        "street": None,
        "district": "Фрунзенский",
    }
    return SnapRow(**(data | kw))


def test_price_history_in_original_currency():
    t = datetime(2026, 9, 1, tzinfo=UTC)
    row = _row(1, first=t)
    snaps = {
        1: [
            _snap(1, t, 300000, "BYN", usd=100000),
            _snap(1, t + DAY, 299000, "BYN", usd=95000),  # −0.33%: шум, не точка
            _snap(1, t + 2 * DAY, 300000, "BYN", usd=90000),  # курс прыгнул — цена та же
            _snap(1, t + 3 * DAY, 290000, "BYN", usd=88000),  # −3.3% от 300 000
            _snap(1, t + 4 * DAY, 88000, "USD"),  # сменили валюту
            _snap(1, t + 5 * DAY, None, None),  # снимок без цены
        ]
    }
    points = build_price_history([row], snaps)
    assert [(p.kind, p.amount, p.currency, p.change_pct) for p in points] == [
        ("first", Decimal(300000), "BYN", None),
        ("change", Decimal(290000), "BYN", -3.33),
        ("currency", Decimal(88000), "USD", None),
    ]


# --- Индекс торга ---------------------------------------------------------------------------


def test_bargaining_unknown_without_history():
    b = bargaining_index(BargainInputs(has_history=False))
    assert b.level == "unknown" and not b.reasons and "Следить" in b.recommendation


def test_bargaining_high_explains_every_point():
    cheaper = CheaperOffer(3, "onliner", Decimal(91000), "USD", Decimal(1500), "u", True)
    b = bargaining_index(
        BargainInputs(
            has_history=True,
            days_on_market=94,
            exposure_median_days=41,
            drops=3,
            relists=2,
            cheaper=cheaper,
            vs_median_pct=-6.4,
            current_amount=Decimal(92500),
            currency="USD",
        )
    )
    codes = {r.code: r.weight for r in b.reasons}
    assert codes == {"long_exposure": 2, "price_drops": 2, "relisted": 1, "cheaper_elsewhere": 1}
    assert b.level == "high" and b.score == 6
    assert [r.short for r in b.reasons] == [
        "висит 94 дня",
        "трижды снижалась",
        "перевыкладывалась 2 раза",
        "на Onliner у другого продавца дешевле",
    ]
    assert "в 2.3 раза дольше обычного" in b.reasons[0].text
    # Ориентир — от самой низкой цены квартиры ($91 000): 3–5% ниже, шаг $500.
    assert (b.offer_low, b.offer_high) == (Decimal(86500), Decimal(88500))
    assert b.recommendation.startswith("Торг, скорее всего, реален")
    assert "$86 500–88 500" in b.recommendation and "Onliner" in b.recommendation
    assert "не гарантия" in b.disclaimer


def test_bargaining_low_for_fresh_listing_below_median():
    b = bargaining_index(
        BargainInputs(
            has_history=True,
            days_on_market=5,
            exposure_median_days=60,
            vs_median_pct=-12,
            current_amount=Decimal(80000),
            currency="USD",
        )
    )
    assert b.level == "low" and b.score == -2 and b.offer_low is None
    assert "Поводов для торга мало" in b.recommendation


def test_bargaining_uses_absolute_days_without_district_median():
    b = bargaining_index(BargainInputs(has_history=True, days_on_market=95, drops=1))
    assert [(r.code, r.weight) for r in b.reasons] == [("long_exposure", 2), ("price_drops", 1)]
    assert b.level == "medium" and "медианы срока по району пока нет" in b.reasons[0].text


# --- Отчёт /check: сценарий из docs ------------------------------------------------------


def test_check_report_docs_scenario(docs):
    r = build_check_report(docs, KUFAR_URL, NOW)
    assert (r.status, r.scope, r.data_level) == ("ok", "object", "full")
    assert r.observation_start == OBS_START
    assert r.listing.source == "kufar" and r.listing.price.amount == Decimal(92500)
    assert (r.flat.rooms, r.flat.floor, r.flat.floors_total) == (2, 5, 9)
    assert (r.flat.street, r.flat.district) == ("ул. Притыцкого", "Фрунзенский")

    e = r.exposure
    assert (e.days, e.at_least, e.start_source, e.relist_count, e.site_days) == (
        94,
        False,
        "realt",
        1,
        19,
    )
    assert [(p.at.date().isoformat(), int(p.amount), p.source, p.kind) for p in r.history] == [
        ("2026-07-20", 98000, "realt", "first"),
        ("2026-08-28", 95000, "realt", "change"),
        ("2026-10-03", 92500, "kufar", "new_listing"),
        ("2026-10-05", 91000, "onliner", "parallel"),
    ]
    assert (r.moves.drops, r.moves.ups) == (2, 0)  # 98→95 на Realt и 95→92.5 при перевыкладывании
    assert r.sellers.count == 2 and r.sellers.agencies == 2

    [other] = r.others
    assert (other.source, other.cheaper_by, other.other_seller) == ("onliner", Decimal(1500), True)
    assert r.match.level == "high" and not r.match.single
    assert "совпавших фото" in r.match.basis

    assert r.median.cell.median == 1820 and r.median.cell.district == "Фрунзенский"
    assert round(r.median.diff_pct, 1) == -6.4
    assert r.bargaining.level == "high"
    assert {x.code for x in r.bargaining.reasons} >= {
        "price_drops",
        "relisted",
        "cheaper_elsewhere",
    }
    assert not r.suggest_watch


def test_check_text_docs_scenario(docs):
    text = render_check_text(build_check_report(docs, KUFAR_URL, NOW))
    lines = text.splitlines()
    ordered = (  # порядок блоков — спека, раздел 4
        "🏠 2-комн., 54.3 м², 5/9 эт., ул. Притыцкого, Фрунзенский р-н",
        "Сейчас: $92 500 ($1 703/м²) — Kufar, агентство",
        "✅ Стоит смотреть: цена на 6.4% ниже медианы района, торг реален",
        "📍 Метро «Спортивная» — ~5 мин пешком (400 м). 9-этажный дом",
        "👥 Подойдёт: паре или небольшой семье",
        "👍 Плюсы:",
        "   • метро рядом",
        "📊 Цена: $1 703/м² — на 6.4% ниже медианы района",
        "   (Фрунзенский р-н, 2-комн.: медиана $1 820/м², 13 объектов)",
        "⏳ На рынке: 94 дня (впервые замечена 20 июля на Realt)",
        "   ↻ Перевыкладывалась 1 раз",
        "   На сайте объявлению 19 дней, на самом деле квартира продаётся 94 дня",
        "   20.07  $98 000  Realt, агентство",
        "   28.08  $95 000  Realt (−3.1%)",
        "   03.10  $92 500  Kufar, агентство (новое объявление, тот же объект, −2.6%)",
        "   Onliner — $91 000 (другое агентство)  ← дешевле на $1 500",
        "Цены объявлений, а не сделок. Это рекомендация, а не гарантия.",
        "Наблюдаем рынок с 15.07.2026",
    )
    positions = [lines.index(line) for line in ordered]
    assert positions == sorted(positions)
    [torg] = [i for i, line in enumerate(lines) if line.startswith("💬 Торг: ")]
    assert positions[-3] < torg < positions[-2]
    assert next(line for line in lines if line.startswith("Уверенность склейки: высокая ("))
    assert "Вывод:" not in text and "realt.by/statistics" not in text
    assert "Торг, скорее всего, реален" in text and "сошлитесь на цену на Onliner" in text
    assert "\n\n\n" not in text and not text.endswith("\n")
    plain = render_check_text(build_check_report(docs, KUFAR_URL, NOW), icons=False)
    assert plain.startswith("2-комн.") and "Торг: " in plain
    assert not any(ch in plain for ch in "🏠✅⚠📍👥👍📊🏢💬ℹ")


def test_check_by_listing_id_and_jsonable(docs):
    item = listing(docs, "kufar", "1000201")
    by_id = build_check_report(docs, item.id, NOW)
    by_ref = build_check_report(docs, f"listing:{item.id}", NOW)
    baseline = market_baseline(docs, NOW)
    cached = build_check_report(docs, KUFAR_URL, NOW, baseline=baseline)
    assert by_id.exposure == by_ref.exposure == cached.exposure
    assert by_id.median == cached.median
    payload = json.loads(json.dumps(to_jsonable(by_id)))
    assert payload["exposure"]["days"] == 94
    assert payload["history"][0]["amount"] == "98000.00"
    assert payload["median"]["cell"]["enough"] is True


def test_report_from_inactive_listing_points_to_live_ones(docs):
    r = build_check_report(docs, REALT_URL, NOW)
    assert r.listing.status == "inactive" and r.exposure.days == 94
    text = render_check_text(r)
    assert "Последняя цена: $95 000" in text and "объявление снято" in text
    assert "Kufar — $92 500 (тот же продавец)  ← дешевле на $2 500" in text


def test_object_level_helpers(docs):
    oid = listing(docs, "kufar", "1000201").object_id
    assert object_exposure(docs, oid, NOW).days == 94
    assert [int(p.amount) for p in object_price_history(docs, oid)] == [98000, 95000, 92500, 91000]


# --- Отчёт /check: склейка low, мало данных ------------------------------------------------


def _low_scenario(session):
    start_observation(session)
    t = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    seller = {"agency_name": None, "seller_key": "kufar-x"}
    price = {"price_amount": Decimal(97000), "price_usd": Decimal(97000)}
    cheaper = {"price_amount": Decimal(94000), "price_usd": Decimal(94000)}
    observe(session, "realt", t, realt("101"))
    observe(session, "kufar", t + DAY, kufar("1000202", **seller, **price))
    observe(session, "kufar", t + 10 * DAY, kufar("1000202", **seller, **cheaper))
    observe(session, "realt", NOW - HOUR, realt("101"))
    observe(session, "kufar", NOW - HOUR, kufar("1000202", **seller, **cheaper))
    assert listing(session, "kufar", "1000202").match_confidence == "low"


def test_low_link_report_shows_listing_history_and_possible_object(session):
    _low_scenario(session)
    r = build_check_report(session, "https://re.kufar.by/vi/1000202", NOW)
    assert r.scope == "listing" and r.match.level == "low"
    # Срок и история — только этого объявления, а не объекта Realt.
    assert r.exposure.start_source == "kufar" and r.exposure.days == 50
    assert [int(p.amount) for p in r.history] == [97000, 94000]
    assert r.moves.drops == 1  # событие с notify=False, но это история самого объявления
    assert r.others == () and r.sellers.count == 1
    # Поля объекта (этаж с Realt) не подставляем: это может быть другая квартира.
    assert r.flat.floor is None
    p = r.possible_object
    assert p.sources == ("realt",) and p.exposure.days == 51 and p.basis
    text = render_check_text(r)
    assert "❓ Возможно, это та же квартира, что продаётся на Realt с 01.09" in text
    assert "Склейка не подтверждена" in text and "Уверенность склейки: низкая" in text


def test_low_duplicate_does_not_drive_conclusions(session):
    _low_scenario(session)
    r = build_check_report(session, REALT_URL, NOW)
    assert r.scope == "object" and r.match.single and r.match.low_listings == 1
    [other] = r.others
    assert other.confidence == "low" and other.cheaper_by == Decimal(4000)
    # Дешевле на Kufar — но склейка не подтверждена: в вывод о торге это не идёт.
    assert "cheaper_elsewhere" not in {x.code for x in r.bargaining.reasons}
    assert r.exposure.relist_count == 0 and r.moves.drops == 0
    text = render_check_text(r)
    assert "возможно, та же квартира, склейка не подтверждена" in text
    assert "других объявлений этой квартиры мы не нашли" in text


def test_first_time_listing_suggests_watch(session):
    start_observation(session)
    add_background(session, 3)
    observe(session, "realt", NOW - HOUR, realt("555"))
    r = build_check_report(session, "https://realt.by/sale-flats/object/555/", NOW)
    assert r.data_level == "first_time" and r.suggest_watch
    assert r.bargaining.level == "unknown"
    text = render_check_text(r)
    assert "ℹ️ Эту квартиру мы видим впервые. Истории пока нет." in text
    lines = text.splitlines()
    assert lines.index("ℹ️ Похожих квартир рядом мало, сравнить цену не можем") < lines.index(
        "ℹ️ Эту квартиру мы видим впервые. Истории пока нет."
    )
    assert "Нажмите «Следить»" in text and "📊" not in text and "⏳" not in text
    assert lines[-3] == "Цены объявлений, а не сделок. Это рекомендация, а не гарантия."
    assert "Наблюдаем рынок с 15.07.2026" in text


def test_short_history_right_after_start(session):
    start = NOW - 5 * DAY
    start_observation(session, start)
    observe(session, "realt", start + HOUR, realt("101"))
    observe(session, "realt", NOW - HOUR, realt("101"))
    r = build_check_report(session, REALT_URL, NOW)
    assert r.exposure.at_least and r.data_level == "short" and r.suggest_watch
    text = render_check_text(r)
    assert "На рынке: как минимум 4 дня" in text
    assert "её настоящая история длиннее нашей" in text


def test_listing_waiting_for_matching_is_shown_alone(session):
    # Агентство выложило вторую такую же квартиру, старое объявление ещё висит: Emma ждёт сутки.
    start_observation(session)
    observe(session, "realt", NOW - 2 * HOUR, realt("101"))
    observe(session, "realt", NOW - HOUR, realt("101"), realt("102"))
    waiting = listing(session, "realt", "102")
    assert waiting.object_id is None
    r = build_check_report(session, str(waiting.id), NOW)
    assert r.scope == "listing" and r.object_id is None and r.match.level is None
    assert "Склейка этого объявления ещё не выполнена" in render_check_text(r)


def test_unknown_and_unsupported_links(docs):
    missing = build_check_report(docs, "https://realt.by/sale-flats/object/999999/", NOW)
    assert (missing.status, missing.source) == ("not_found", "realt")
    assert "пока нет в нашей базе" in render_check_text(missing)
    assert "Наблюдаем рынок с 15.07.2026" in render_check_text(missing)
    bad = build_check_report(docs, "https://example.com/flat/1", NOW)
    assert bad.status == "unsupported_url"
    assert "Kufar, Realt, Onliner или Domovita" in render_check_text(bad)


# --- Недельная сводка и гипотеза на данных в памяти --------------------------------------


def _market(objects: int, *, start=OBS_START) -> MarketData:
    """`objects` объектов во Фрунзенском, по одному объявлению, $90 000 за 50 м²."""
    data = MarketData(source_starts={"kufar": start, "realt": start})
    for i in range(1, objects + 1):
        first = start + (i % 7) * DAY + 2 * DAY
        data.objects[i] = ObjectRow(
            i, f"ул тест {i}", "1", 2, 50.0, 1, 9, "фрунзенский", None, first
        )
        data.listings[i] = _row(i, object_id=i, first=first, source="kufar")
        data.snapshots[i] = [_snap(i, first, 90000 + 100 * i)]
        data.events[i] = [_event(1000 + i, "new", first, listing_id=i, object_id=i)]
    data.reindex()
    return data


def _drop(data: MarketData, oid: int, at: datetime, old: int, new: int, *, notify=True):
    pct_ = round((new - old) / old * 100, 2)
    lid = oid
    data.snapshots[lid].append(_snap(lid, at, new))
    payload = {"old": f"{old}.00", "new": f"{new}.00", "currency": "USD", "pct": pct_}
    data.events[oid].append(
        _event(len(data.events[oid]) + 10_000 * oid, "price_drop", at, lid, oid, notify, **payload)
    )


def test_weekly_summary_counts_and_top(session):
    data = _market(14)
    in_week = NOW - 2 * DAY
    _drop(data, 1, in_week, 90100, 87400)  # −3.0%
    _drop(data, 2, in_week, 90200, 85690)  # −5.0%
    _drop(data, 3, in_week, 90300, 54180)  # −40%: скорее опечатка, в топ не идёт
    _drop(data, 4, in_week, 90400, 89000, notify=False)  # склейка low — не считается
    _drop(data, 5, NOW - 10 * DAY, 90500, 88000)  # прошлая неделя
    # Новое объявление недели — перевыкладывание объекта 6; ещё одно — новый объект 15.
    data.listings[6] = replace(data.listings[6], status="inactive", last_seen=NOW - 4 * DAY)
    data.listings[60] = _row(60, object_id=6, first=NOW - 3 * DAY, source="kufar")
    data.snapshots[60] = [_snap(60, NOW - 3 * DAY, 90600)]
    data.events[6].append(_event(6000, "relisted", NOW - 3 * DAY, 60, 6))
    data.objects[15] = ObjectRow(15, "ул тест 15", "1", 2, 50.0, 1, 9, "фрунзенский", None, NOW)
    data.listings[15] = _row(15, object_id=15, first=NOW - DAY, source="kufar")
    data.snapshots[15] = [_snap(15, NOW - DAY, 91000)]
    data.events[15] = [_event(1015, "new", NOW - DAY, 15, 15)]
    data.reindex()

    s = compute_weekly(data, NOW)
    c = s.counts
    assert (s.year, s.week) == (2026, 43)
    assert (c.dropped_objects, c.drop_events, c.drop_step_median_pct) == (3, 3, -5.0)
    assert (c.new_listings, c.relisted_listings, c.new_objects) == (2, 1, 1)
    assert [(d.object_id, d.pct) for d in s.top_drops] == [(2, -5.0), (1, -3.0)]
    assert s.median.enough and s.median_prev.enough and s.median_change_pct is not None
    text = render_weekly_text(s)
    assert text.startswith("📊 Квартиры Минска на продажу, неделя 43")
    assert "• Подешевели: 3 объекта, медианный шаг −5%" in text
    assert "• Перевыложено «как новые»: 50% новых объявлений" in text
    assert "$90 200 → $85 690 (−5%)" in text and "Наблюдаем рынок с 15.07.2026" in text


def test_weekly_small_market_says_not_enough():
    s = compute_weekly(_market(4), NOW)
    assert not s.median.enough
    assert "• Медиана: мало данных (4 объекта)" in render_weekly_text(s)


def _hypothesis_market(n=20, relisted=2, cross=1, drops=3) -> MarketData:
    data = _market(n)
    for oid in range(1, relisted + 1):
        data.events[oid].append(_event(5000 + oid, "relisted", NOW - 20 * DAY, oid, oid))
    for oid in range(relisted + 1, relisted + cross + 1):
        data.events[oid].append(_event(6000 + oid, "cross_listed", NOW - 20 * DAY, oid, oid))
    for oid in range(n - drops + 1, n + 1):
        _drop(data, oid, NOW - 5 * DAY, 90000 + 100 * oid, 88000)
    return data


def test_hypothesis_metrics_and_too_early_by_default():
    h = compute_hypothesis(_hypothesis_market(), NOW)
    assert (h.relisted.count, h.cross_listed.count, h.relisted_or_cross.count) == (2, 1, 3)
    assert h.relisted_or_cross.share == 0.15 and h.with_drops.share == 0.15
    assert h.with_insight.count == 6 and h.mature_objects == 20
    assert h.exposure.n == 20 and h.exposure.p50 is not None
    assert sum(b.count for b in h.exposure.buckets) == 20
    # 99 дней наблюдения, но 20 объектов < 200: решать рано.
    assert h.decision == "too_early"
    assert any("объектов 20" in r for r in h.reasons)
    assert len(h.sample_object_ids) == 5


def test_hypothesis_decisions():
    cfg = replace(DEFAULT_ANALYTICS, min_objects=10, insight_min=0.25)
    good = compute_hypothesis(_hypothesis_market(), NOW, cfg=cfg)
    assert good.decision == "continue" and all(c.passed for c in good.criteria)
    boring = compute_hypothesis(_hypothesis_market(relisted=0, cross=0, drops=1), NOW, cfg=cfg)
    assert boring.decision == "stop"
    mixed = compute_hypothesis(_hypothesis_market(relisted=3, cross=0, drops=0), NOW, cfg=cfg)
    # 99 дней — уже больше 12 недель: пограничная картина по правилу означает остановку.
    assert mixed.decision == "stop"
    assert any("есть снижения цены" in r for r in mixed.reasons)
    assert any("после 12 недель" in r for r in mixed.reasons)
    longer = replace(cfg, extended_weeks=20)
    extend = compute_hypothesis(_hypothesis_market(relisted=3, cross=0, drops=0), NOW, cfg=longer)
    assert extend.decision == "extend"
    assert render_hypothesis_text(extend).count("ПРОДЛЕВАЕМ") == 1
    text = render_hypothesis_text(good)
    assert "Рекомендация: ПРОДОЛЖАЕМ" in text and "| Перевыкладывались ИЛИ cross_listed" in text
    assert "3 из 20 (15.0%)" in text


def test_platform_reported_drops_count_for_the_drops_criterion():
    """История до начала наблюдений обрезана, но Realt и Domovita сами показывают снижения:
    они идут в критерий снижений и отдельной строкой в отчёте."""
    cfg = replace(DEFAULT_ANALYTICS, min_objects=10, insight_min=0.25)
    data = _hypothesis_market(drops=0)
    first_listings = [data.object_listings(oid)[0].id for oid in range(18, 21)]
    data.platform_drops = frozenset(first_listings)
    h = compute_hypothesis(data, NOW, cfg=cfg)
    assert (h.with_drops.count, h.with_drops_platform.count, h.with_drops_any.count) == (0, 3, 3)
    drops = next(c for c in h.criteria if c.key == "with_drops")
    assert drops.passed and drops.value == 0.15
    assert "Снижение показывает площадка" in render_hypothesis_text(h)


def test_platform_drop_detection():
    from minsk_realty.analytics.data import _platform_drop

    assert _platform_drop({"priceChangeDirection": -1})
    assert not _platform_drop({"priceChangeDirection": 1})
    assert _platform_drop({"price_history": [["2026-08-14", 68306], ["2026-07-30", 69089]]})
    # Меньше процента в долларах — может быть курсом, а не решением продавца.
    assert not _platform_drop({"price_history": [["2026-08-14", 68900], ["2026-07-30", 69089]]})


def test_new_objects_cohort():
    data = _hypothesis_market()
    source = data.object_listings(1)[0].source
    cutoff = min(r.first_seen for r in data.listings.values())
    data.baseline_ends = {source: cutoff}  # всё, что позже самого первого, — «новое»
    h = compute_hypothesis(data, NOW)
    assert h.new_objects > 0 and h.relisted_or_cross_new.total == h.new_objects


def test_hypothesis_on_db(docs):
    from minsk_realty.analytics import hypothesis_report

    h = hypothesis_report(docs, NOW)
    assert h.objects == 15 and h.relisted.count == 1 and h.cross_listed.count == 1
    assert h.with_drops.count == 1 and h.with_insight.count == 1


# --- Кабинет: обзор рынка и здоровье источников ----------------------------------------------


def test_market_overview(docs):
    o = market_overview(docs, NOW)
    assert o.active_objects == 15 and o.observation_start == OBS_START
    assert o.city.enough and o.by_district[0].district == "Фрунзенский"
    assert [c.rooms for c in o.by_rooms] == [2]
    assert json.dumps(to_jsonable(o))
    same = compute_overview(
        load_market(docs), NOW, baseline=compute_baseline(load_market(docs), NOW)
    )
    assert same.city == o.city


def test_source_health_summary(docs):
    for i in range(3):
        at = NOW - (3 - i) * HOUR
        docs.add(
            SourceRun(
                source="realt", started_at=at, finished_at=at, status="failed", error="HTTP 500"
            )
        )
    docs.flush()
    health = {h.source: h for h in source_health_summary(docs, NOW)}
    assert health["kufar"].state == "ok" and health["kufar"].active_listings == 15
    assert health["kufar"].last_run_status == "ok"
    realt_ = health["realt"]
    assert realt_.state == "failing" and realt_.failed_24h == 3 and realt_.alerts
    assert health["domovita"].state == "never_run"


# --- Командная строка -------------------------------------------------------------------------


def test_cli(docs, tmp_path, monkeypatch, capsys):
    from minsk_realty.analytics.__main__ import main

    docs.commit()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'analytics.db'}")
    assert main(["check", KUFAR_URL, "--now", NOW.isoformat()]) == 0
    assert "На рынке: 94 дня" in capsys.readouterr().out
    assert main(["hypothesis", "--now", NOW.isoformat()]) == 0
    assert "Рекомендация: РАНО СУДИТЬ" in capsys.readouterr().out
    assert main(["weekly", "--now", NOW.isoformat(), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["week"] == 43
    assert main(["health", "--now", NOW.isoformat()]) == 0
    assert '"source": "kufar"' in capsys.readouterr().out
    assert main(["assessment", "--now", NOW.isoformat()]) == 0
    out = capsys.readouterr().out
    assert "suspicious" in out and "near" in out and "посчитано за" in out
    assert main(["assessment", "--now", NOW.isoformat(), "--json", "--seed", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["stats"]["total"] > 0


def test_report_builds_fast_enough(docs):
    # Порог этапа 6 — ответ меньше 3 секунд; на тестовой БД отчёт строится за доли секунды.
    started = datetime.now(UTC)
    build_check_report(docs, KUFAR_URL, NOW)
    assert datetime.now(UTC) - started < timedelta(seconds=3)
