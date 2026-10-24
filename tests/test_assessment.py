"""Оценка квартиры: год постройки и координаты в срезе рынка. Сети нет."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from analytics_data import KUFAR_URL, NOW, add_background, build_docs_scenario
from sqlalchemy import select, update
from test_analytics import FRUNZ_PPM, OUTLIERS
from test_analytics import _market as _flat_market

from minsk_realty.analytics import render_check_text, to_jsonable
from minsk_realty.analytics.assessment import assess, same_house
from minsk_realty.analytics.comparables import (
    ComparePoint,
    Comparison,
    epoch,
    find_comparison,
    price_group,
)
from minsk_realty.analytics.config import DEFAULT_ANALYTICS as CFG
from minsk_realty.analytics.data import (
    ListingRow,
    MarketData,
    SnapRow,
    building_years,
    finishing_kind,
    finishings,
    load_market,
    parse_year,
)
from minsk_realty.analytics.geo import Location, Station
from minsk_realty.analytics.medians import MedianCell, compute_baseline
from minsk_realty.analytics.report import build_check_report
from minsk_realty.db.models import Base, Listing, RealtyObject, Snapshot
from minsk_realty.db.session import make_engine, make_session_factory
from minsk_realty.matching import process_after_run

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'assessment.db'}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        yield session


def test_parse_year():
    assert parse_year(1978, 2026) == 1978
    assert parse_year(" 1978 ", 2026) == 1978
    for bad in (0, 1700, 2100, None, "abc", "", True, 1978.5, "19.78"):
        assert parse_year(bad, 2026) is None
    assert parse_year(2030, 2026) == 2030  # строящийся дом
    assert parse_year(2037, 2026) is None


def _object(session) -> int:
    obj = RealtyObject(first_seen_at=T0)
    session.add(obj)
    session.flush()
    return obj.id


def _listing(session, oid, source, ext, **kw) -> int:
    row = Listing(
        **{
            "source": source,
            "external_id": ext,
            "url": f"https://example.test/{ext}",
            "object_id": oid,
            "match_confidence": "high",
            "first_seen": T0,
            "last_seen": T0,
        }
        | kw
    )
    session.add(row)
    session.flush()
    return row.id


def _snap(session, lid, days, **kw) -> None:
    session.add(
        Snapshot(listing_id=lid, seen_at=T0 + timedelta(days=days), content_hash=f"h{days}", **kw)
    )
    session.flush()


def test_building_years_latest_wins(session):
    oid, other = _object(session), _object(session)
    kufar = _listing(session, oid, "kufar", "k1")
    realt = _listing(session, oid, "realt", "r1")
    lone = _listing(session, other, "kufar", "k2")
    _snap(session, kufar, 0, extra={"year_built": 1975})
    _snap(session, kufar, 2, extra={"year_built": 1976})
    _snap(session, kufar, 3, extra={})  # свежий снимок без года не затирает год
    _snap(session, realt, 1, extra={"buildingYear": "1900"})
    _snap(session, lone, 0, extra={"year_built": 9999})  # невалидный год пропускается

    assert building_years(session) == {oid: 1976}
    assert building_years(session, [other]) == {}
    assert building_years(session, [oid]) == {oid: 1976}
    # load_market заполняет годы и при фильтре только по объявлениям
    assert load_market(session).building_years == {oid: 1976}
    assert load_market(session, listing_ids=[realt]).building_years == {oid: 1976}
    assert load_market(session, object_ids=[other]).building_years == {}


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("Без отделки", "none"),
        ("без отделки", "none"),
        ("Строительная отделка", "none"),
        ("Черновая", "none"),
        ("Косметический", "done"),
        ("Дизайнерский", "done"),
        ("евроремонт", "done"),
        (None, None),
        ("", None),
        ("  ", None),
    ],
)
def test_finishing_kind(label, kind):
    assert finishing_kind(label) == kind


def test_finishings_loader(session):
    oid, other, weak = _object(session), _object(session), _object(session)
    kufar = _listing(session, oid, "kufar", "k1")
    realt = _listing(session, oid, "realt", "r1")
    lone = _listing(session, other, "realt", "r2")
    low = _listing(session, weak, "kufar", "k3", match_confidence="low")
    _snap(session, kufar, 0, extra={"repair": "Косметический"})
    _snap(session, realt, 1, extra={"repairState": 7})  # свежее: код Realt 7 = «без отделки»
    _snap(session, kufar, 2, extra={})  # свежий снимок без отделки не затирает
    _snap(session, lone, 0, extra={"repairState": 2})  # «хороший» = ремонт
    _snap(session, low, 0, extra={"repair": "Без отделки"})  # ненадёжная склейка не в счёт

    assert finishings(session) == {oid: "none", other: "done"}
    assert finishings(session, [other]) == {other: "done"}
    assert finishings(session, [weak]) == {}
    assert load_market(session).finishings == {oid: "none", other: "done"}
    assert load_market(session, listing_ids=[lone]).finishings == {other: "done"}
    assert load_market(session, object_ids=[oid]).finishings == {oid: "none"}


def _row(lid, source, oid=1, confidence="high") -> ListingRow:
    return ListingRow(
        id=lid,
        object_id=oid,
        source=source,
        external_id=str(lid),
        url="u",
        confidence=confidence,
        status="active",
        first_seen=T0,
        last_seen=T0,
        posted_at=None,
        seller_type="unknown",
        agency_name=None,
        seller_key=None,
    )


def _sr(lid, days, lat, lon) -> SnapRow:
    return SnapRow(
        lid, T0 + timedelta(days=days), None, None, None, None, None, None, None, None, None,
        lat=lat, lon=lon,
    )  # fmt: skip


def _market(rows, snaps) -> MarketData:
    return MarketData(
        listings={r.id: r for r in rows},
        snapshots={lid: s for lid, s in snaps.items()},
    )


def test_object_point_prefers_precise_source():
    data = _market(
        [_row(1, "kufar"), _row(2, "realt")],
        {1: [_sr(1, 5, 53.9, 27.5)], 2: [_sr(2, 1, 53.91, 27.51)]},
    )
    approx = frozenset({"kufar"})
    assert data.object_point(1, approx) == (53.91, 27.51, False)
    assert data.listing_point(1, approx) == (53.9, 27.5, True)
    assert data.listing_point(2, approx) == (53.91, 27.51, False)

    only_kufar = _market([_row(1, "kufar")], {1: [_sr(1, 5, 53.9, 27.5)]})
    assert only_kufar.object_point(1, approx) == (53.9, 27.5, True)
    assert only_kufar.object_point(2, approx) is None


def test_point_skips_invalid_and_untrusted():
    approx = frozenset({"kufar"})
    data = _market(
        [_row(1, "realt"), _row(2, "realt"), _row(3, "realt", confidence="low")],
        {
            1: [_sr(1, 1, 53.9, 27.5), _sr(1, 2, None, None)],  # снимок без координат пропущен
            2: [_sr(2, 9, 0.0, 0.0)],  # вне Минска — пропущен, берём следующее объявление
            3: [_sr(3, 9, 53.95, 27.6)],  # ненадёжная склейка не в счёт
        },
    )
    assert data.object_point(1, approx) == (53.9, 27.5, False)
    assert data.listing_point(2, approx) is None


def test_epoch_bounds():
    assert [epoch(y, 2026) for y in (1969, 1970, 1990, 1991, 2010, 2011, 2026, 2027, None)] == [
        "pre1970", "1970_1990", "1970_1990", "1991_2010", "1991_2010", "post2010", "post2010",
        "building", None,
    ]  # fmt: skip


LAT, LON = 53.9, 27.5
M_PER_DEG_LAT = 111_195


def _pts(n, *, dist_m=1000, ep="1970_1990", rooms=2, first_id=1, base=1500, finish=None):
    """n точек на заданном расстоянии к северу от (LAT, LON); цена растёт на $1 за точку."""
    return tuple(
        ComparePoint(
            first_id + i, LAT + dist_m / M_PER_DEG_LAT, LON, rooms, ep, base + i, None, finish
        )
        for i in range(n)
    )


def _compare(points, **kw):
    args = {
        "object_id": 999,
        "point": (LAT, LON, False),
        "rooms": 2,
        "year": 1980,
        "usd_per_m2": 1500.0,
        "district_cell": None,
        "now_year": 2026,
        "cfg": CFG,
    }
    return find_comparison(points, **(args | kw))


def test_comparison_near():
    c = _compare(_pts(12))
    assert (c.step, c.radius_km, c.n, c.epoch, c.rooms) == ("near", 1.5, 12, "1970_1990", 2)
    assert c.year_known and len(c.points) == 12
    assert c.diff_pct == pytest.approx((1500 / c.median - 1) * 100)


def test_comparison_widens_to_3km():
    c = _compare(_pts(5) + _pts(7, dist_m=2500, first_id=100))
    assert (c.step, c.radius_km, c.n, len(c.points)) == ("wide", 3.0, 12, 12)


def test_comparison_falls_back_to_district():
    cell = MedianCell("фрунзенский", 2, 40, 45, 1800.0)
    c = _compare(_pts(3), district_cell=cell)
    assert (c.step, c.n, c.median, c.district, c.points) == (
        "district",
        40,
        1800.0,
        "фрунзенский",
        (),
    )
    assert c.radius_km is None and c.year_known
    assert _compare(_pts(3)) is None
    assert _compare(_pts(3), district_cell=MedianCell(None, 2, 4, 4, None)) is None


def test_comparison_skips_other_epochs_and_self():
    assert _compare(_pts(12, ep="pre1970"), year=2015) is None
    # свой объект в выборку не входит
    c = _compare(_pts(11), object_id=1)
    assert c.n == 10 and 1 not in {p.object_id for p in c.points}
    assert _compare(_pts(10), object_id=1) is None
    # другая комнатность не считается; 4+ комнат — одна корзина
    assert _compare(_pts(12, rooms=3)) is None
    assert _compare(_pts(12, rooms=4), rooms=6).n == 12


def test_comparison_without_year():
    mixed = _pts(6, ep="pre1970") + _pts(6, ep="post2010", first_id=50)
    c = _compare(mixed, year=None)
    assert (c.step, c.n, c.epoch, c.year_known) == ("near", 12, None, False)


def test_comparison_same_finishing_only():
    mixed = (
        _pts(12, finish="none")
        + _pts(7, finish="done", first_id=50)
        + _pts(8, finish=None, first_id=100)
    )
    c = _compare(mixed, finish="none")
    assert (c.step, c.n, c.finish) == ("near", 12, "none")
    assert {p.finish for p in c.points} == {"none"}
    # отделка цели неизвестна: фильтра нет, в выборке все 27 точек
    c = _compare(mixed)
    assert (c.step, c.n, c.finish) == ("near", 27, None)
    c = _compare(_pts(12, finish="done") + _pts(9, finish="none", first_id=50), finish="done")
    assert (c.step, c.n, c.finish) == ("near", 12, "done")
    # квартир с той же отделкой мало, а «done» и неизвестные в выборку не берём
    few = _pts(9, finish="none") + _pts(20, finish="done", first_id=50) + _pts(20, first_id=100)
    assert _compare(few, finish="none") is None


def test_comparison_finishing_district_step_ignores_finish():
    cell = MedianCell("фрунзенский", 2, 40, 45, 1800.0)
    c = _compare(_pts(12, finish="done"), finish="none", district_cell=cell)
    assert (c.step, c.finish) == ("district", None)


def test_approx_point_goes_to_district():
    cell = MedianCell(None, None, 100, 110, 1700.0)
    c = _compare(_pts(12), point=(LAT, LON, True), district_cell=cell)
    assert (c.step, c.points) == ("district", ())
    assert _compare(_pts(12), point=None, district_cell=cell).step == "district"
    assert _compare(_pts(12), rooms=None, district_cell=cell).step == "district"


def test_no_price_no_comparison():
    assert _compare(_pts(12), usd_per_m2=None) is None


def test_price_group_bounds():
    assert [price_group(x, CFG) for x in (-25, -24.9, -10, -9.9, 9.9, 10)] == [
        "suspicious", "below", "below", "market", "market", "above",
    ]  # fmt: skip


def test_baseline_has_points():
    from dataclasses import replace

    data = _flat_market(12)
    for i, snaps in data.snapshots.items():
        data.snapshots[i] = [replace(s, lat=53.9 + i / 10_000, lon=27.5) for s in snaps]
    data.building_years[3] = 1975
    data.finishings[3] = "none"
    data.objects[4] = replace(data.objects[4], street_norm=None)
    base = compute_baseline(data, NOW)
    assert len(base.points) == 12
    by_id = {p.object_id: p for p in base.points}
    assert by_id[3].epoch == "1970_1990" and by_id[1].epoch is None
    assert by_id[3].finish == "none" and by_id[1].finish is None
    assert by_id[3].house_key == "ул тест 3|1" and by_id[4].house_key is None
    assert by_id[3].rooms == 2 and by_id[3].usd_per_m2 == pytest.approx(90300 / 50)
    # без координат точек нет
    assert compute_baseline(_flat_market(12), NOW).points == ()


# --- Вывод, минусы, плюсы, аудитория, дом (спека 4.1, 4.2, 4.4, 4.5) ---


def _cmp(group="market", diff=0.0) -> Comparison:
    return Comparison(
        step="near", radius_km=1.5, epoch="1970_1990", rooms=2, n=20, median=1800.0,
        usd_per_m2=1800.0, diff_pct=diff, group=group, year_known=True, district=None, points=(),
    )  # fmt: skip


def _loc(walk_min=10, *, far=False, approx=False) -> Location:
    if far:
        return Location(53.9, 27.5, approx, None, None, None)
    return Location(53.9, 27.5, approx, Station("Спортивная", 53.9, 27.5), 600.0, walk_min)


def _assess(**kw):
    args = {
        "comparison": _cmp(),
        "bargaining_level": "unknown",
        "rooms": 2,
        "area": 54.0,
        "floor": 5,
        "floors_total": 9,
        "year": 2005,
        "now_year": 2026,
        "location": None,
    }
    args.update(kw)
    return assess(**args)


@pytest.mark.parametrize(
    ("group", "diff", "level", "kind", "verdict"),
    [
        ("suspicious", -35.0, "unknown", "suspicious",
         "Подозрительно дёшево: на 35% ниже похожих. Узнайте причину до просмотра (доля, состояние, документы)"),
        ("suspicious", -35.0, "high", "suspicious",
         "Подозрительно дёшево: на 35% ниже похожих. Узнайте причину до просмотра (доля, состояние, документы)"),
        ("below", -7.0, "unknown", "below", "Стоит смотреть: цена на 7% ниже похожих"),
        ("below", -7.4, "medium", "below", "Стоит смотреть: цена на 7.4% ниже похожих"),
        ("below", -7.0, "high", "below", "Стоит смотреть: цена на 7% ниже похожих, торг реален"),
        ("market", 1.0, "unknown", "market", "Цена в рынке"),
        ("market", 1.0, "high", "market", "Цена в рынке, торг реален"),
        ("market", 1.0, "low", "market", "Цена в рынке, большой скидки не ждите"),
        ("above", 12.0, "unknown", "above", "Дороже похожих на 12%: смотреть, если готовы торговаться"),
        ("above", 12.0, "low", "above",
         "Дороже похожих на 12%: смотреть, если готовы торговаться, большой скидки не ждите"),
    ],
)  # fmt: skip
def test_verdict_texts(group, diff, level, kind, verdict):
    a = _assess(comparison=_cmp(group, diff), bargaining_level=level)
    assert (a.kind, a.verdict) == (kind, verdict)


def test_verdict_unknown_without_comparison():
    a = _assess(comparison=None, bargaining_level="high")
    assert a.kind == "unknown"
    assert a.verdict == "Похожих квартир рядом мало, сравнить цену не можем"


def _minuses(**kw):
    return [(m.text, m.hint) for m in _assess(**kw).minuses]


FIRST = ("Первый этаж", "спросите про подвал, сырость и решётки")
LAST = ("Последний этаж", "спросите про крышу и протечки")


def _district(group, diff, name="Фрунзенский") -> Comparison:
    return replace(_cmp(group, diff), step="district", radius_km=None, epoch=None, district=name)


@pytest.mark.parametrize(
    ("name", "ref"), [("Фрунзенский", "медианы района"), (None, "медианы по Минску")]
)
def test_district_step_wording(name, ref):
    below = _assess(comparison=_district("below", -7.0, name), bargaining_level="unknown")
    assert below.verdict == f"Стоит смотреть: цена на 7% ниже {ref}"
    assert below.pluses == (f"Цена ниже {ref}",)
    above = _assess(comparison=_district("above", 12.0, name), bargaining_level="unknown")
    assert above.verdict == f"Дороже {ref} на 12%: смотреть, если готовы торговаться"
    assert [m.text for m in above.minuses] == [f"Дороже {ref}"]
    cheap = _assess(comparison=_district("suspicious", -30.0, name), bargaining_level="high")
    assert cheap.verdict.startswith(f"Подозрительно дёшево: на 30% ниже {ref}. Узнайте причину")
    assert "похож" not in cheap.verdict + below.verdict + above.verdict
    assert _assess(comparison=_district("market", 1.0, name)).verdict == "Цена в рынке"


def test_wide_step_keeps_similar_wording():
    wide = replace(_cmp("below", -7.0), step="wide", radius_km=3.0)
    assert _assess(comparison=wide, bargaining_level="unknown").verdict.endswith("ниже похожих")


def test_minuses_rules():
    assert _minuses(floor=1) == [FIRST]
    assert _minuses(floor=9, floors_total=9) == [LAST]
    assert _minuses(floor=3, floors_total=5, year=1985) == [
        ("Скорее всего без лифта", None),
        ("Дом 1985 г.", "узнайте, был ли капремонт и меняли ли стояки"),
    ]
    assert _minuses(year=1990) == [("Дом 1990 г.", "узнайте, был ли капремонт и меняли ли стояки")]
    assert _minuses(year=1991) == []
    assert _minuses(floor=3, floors_total=5, year=2000) == []
    assert _minuses(floor=3, floors_total=5, year=None) == []
    assert _minuses(year=2028) == [("Дом ещё строится", "сдача в 2028, проверьте застройщика")]
    assert _minuses(location=_loc(far=True)) == [("Далеко от метро", None)]
    assert _minuses(location=_loc(25)) == [("Далеко от метро", None)]
    assert _minuses(location=_loc(20)) == []
    assert _minuses(location=_loc(None, approx=True)) == []
    assert _minuses(comparison=_cmp("above", 10.0)) == [("Дороже похожих", None)]
    assert _minuses(comparison=None, floor=None, floors_total=None, year=None) == []


def test_one_storey_house_has_no_last_floor():
    assert _minuses(floor=1, floors_total=1) == [FIRST]


def test_minuses_capped_at_four():
    got = _minuses(
        floor=1, floors_total=5, year=1980, location=_loc(far=True), comparison=_cmp("above", 9.0)
    )
    assert [t for t, _ in got] == [
        "Первый этаж",
        "Скорее всего без лифта",
        "Дом 1980 г.",
        "Далеко от метро",
    ]


def test_pluses_rules():
    assert _assess(location=_loc(10)).pluses == ("Метро рядом",)
    assert _assess(location=_loc(15)).pluses == ()
    assert _assess(location=_loc(None, approx=True)).pluses == ()
    assert _assess(year=2012).pluses == ("Новый дом",)
    assert _assess(comparison=_cmp("below", -8.0)).pluses == ("Цена ниже похожих",)
    assert _assess(year=2015, location=_loc(5), comparison=_cmp("below", -8.0)).pluses == (
        "Метро рядом",
        "Новый дом",
        "Цена ниже похожих",
    )


@pytest.mark.parametrize(
    ("rooms", "area", "location", "expected"),
    [
        (1, 40.0, _loc(10), ("одному или под сдачу", "паре или небольшой семье")),
        (1, 40.0, _loc(20), ("паре или небольшой семье",)),
        (1, 40.0, None, ("паре или небольшой семье",)),
        (1, 50.0, _loc(10), ("паре или небольшой семье",)),
        (2, 54.0, _loc(10), ("паре или небольшой семье",)),
        (3, 70.0, _loc(far=True), ("семье с машиной", "семье с детьми")),
        (3, 70.0, _loc(25), ("семье с машиной", "семье с детьми")),
        (3, 70.0, _loc(8), ("семье с детьми",)),
        (2, 72.0, _loc(8), ("паре или небольшой семье", "семье с детьми")),
        (3, None, None, ("семье с детьми",)),
        (None, None, None, ()),
    ],
)
def test_audience_rules(rooms, area, location, expected):
    assert _assess(rooms=rooms, area=area, location=location).audience == expected


def _p(oid, price, key="ул х|1") -> ComparePoint:
    return ComparePoint(oid, 53.9, 27.5, 2, "1970_1990", price, key)


def test_same_house():
    pts = [
        _p(1, 1700.0),
        _p(2, 1880.0),
        _p(3, 1690.0),
        _p(4, 1500.0, "ул у|2"),
        _p(5, 1600.0, None),
    ]
    assert same_house(pts, object_id=1, house_key="ул х|1") == (1690.0, 1880.0)
    assert same_house(pts, object_id=None, house_key="ул х|1") == (1690.0, 1700.0, 1880.0)
    assert same_house(pts, object_id=1, house_key=None) == ()
    assert same_house([], object_id=1, house_key="ул х|1") == ()


# --- Отчёт /check: сравнение с похожими, вывод, метро ---

SCENARIO_POINT = (53.9050, 27.4785)  # Притыцкого, 10
STATIONS = (Station("Тестовая", 53.9050, 27.4785),)


def _docs(session, *, year=1978, coords=True, scale=1.0) -> None:
    """Сценарий docs + фон Фрунзенского района (в радиусе 1 км, тот же год постройки)."""
    build_docs_scenario(session, year=year, coords=coords)
    near = SCENARIO_POINT if coords else None
    ppm = [v * scale for v in FRUNZ_PPM]
    add_background(session, len(ppm), ppm_values=ppm, year=year, near=near)
    add_background(session, 2, ppm_values=OUTLIERS, prefix="out", year=year, near=near)
    process_after_run(session, None, now=NOW, full=True)


@pytest.fixture
def stations(monkeypatch):
    monkeypatch.setattr("minsk_realty.analytics.geo.load_stations", lambda: STATIONS)


def test_report_has_comparison_and_assessment(session, stations):
    _docs(session)
    r = build_check_report(session, KUFAR_URL, NOW)
    assert r.flat.year == 1978 and r.flat.house_key == "ул притыцкого|10"
    c = r.comparison
    assert (c.step, c.epoch, c.rooms) == ("near", "1970_1990", 2)
    assert c.group == "market" and c.diff_pct == pytest.approx(-7.2, abs=0.2)
    assert r.object_id not in {p.object_id for p in c.points}
    assert r.assessment.kind == "market"
    assert r.location.station.name == "Тестовая" and r.location.walk_min == 5
    assert r.same_house == ()
    assert r.bargaining.level == "high"
    payload = to_jsonable(r)
    assert payload["comparison"]["step"] == "near"
    assert payload["assessment"]["kind"] == "market"


def test_bargaining_uses_similar_flats(session, stations):
    _docs(session, scale=0.8)  # похожие дешевле: квартира заметно дороже медианы
    r = build_check_report(session, KUFAR_URL, NOW)
    assert (r.comparison.step, r.comparison.group) == ("near", "above")
    [reason] = [x for x in r.bargaining.reasons if x.code == "above_median"]
    assert "медианы похожих квартир" in reason.text


def _set_kufar_repair(session, target: str | None, background: str | None) -> None:
    rows = session.execute(
        select(Snapshot, Listing.external_id)
        .join(Listing, Listing.id == Snapshot.listing_id)
        .where(Listing.source == "kufar")
    ).all()
    for snap, ext in rows:
        label = target if ext == "1000201" else background
        if label:
            snap.extra = {**(snap.extra or {}), "repair": label}
    session.flush()


def test_report_compares_with_same_finishing(session, stations):
    _docs(session)
    _set_kufar_repair(session, "Без отделки", "Без отделки")
    r = build_check_report(session, KUFAR_URL, NOW)
    assert r.flat.finish == "none"
    assert (r.comparison.step, r.comparison.finish, r.comparison.n) == ("near", "none", 12)
    assert "12 двушек без отделки в домах 1970–90-х в радиусе 1,5 км" in render_check_text(r)


def test_report_other_finishing_goes_to_district(session, stations):
    _docs(session)
    _set_kufar_repair(session, "Без отделки", "Косметический")  # у соседей ремонт
    r = build_check_report(session, KUFAR_URL, NOW)
    assert r.flat.finish == "none"
    assert (r.comparison.step, r.comparison.finish) == ("district", None)
    # отделка цели неизвестна: фильтра нет
    _set_kufar_repair(session, None, "Косметический")
    session.execute(
        update(Snapshot)
        .where(Snapshot.extra["repair"].as_string() == "Без отделки")
        .values(extra={"year_built": 1978})
    )
    r = build_check_report(session, KUFAR_URL, NOW)
    assert r.flat.finish is None and r.comparison.step == "near" and r.comparison.finish is None


def test_report_for_removed_listing_still_compares(session, stations):
    _docs(session)
    item = session.scalar(select(Listing).where(Listing.external_id == "1000201"))
    # Снятая квартира: все объявления объекта неактивны, но отчёт строится.
    session.execute(
        update(Listing).where(Listing.object_id == item.object_id).values(status="inactive")
    )
    r = build_check_report(session, item.id, NOW)
    assert r.status == "ok" and r.comparison is not None
    assert item.object_id not in {p.object_id for p in r.comparison.points}


def test_report_without_coordinates(session, stations):
    _docs(session, coords=False)
    r = build_check_report(session, KUFAR_URL, NOW)
    assert r.location is None
    assert r.comparison.step == "district" and r.comparison.points == ()
    assert r.assessment is not None


def test_report_same_house_prices(session, stations):
    _docs(session)
    oid = session.scalar(select(Listing.object_id).where(Listing.external_id == "1000201"))
    other = session.scalar(
        select(RealtyObject).where(RealtyObject.id != oid).order_by(RealtyObject.id)
    )
    other.street_norm, other.house = "ул притыцкого", "10"
    session.flush()
    r = build_check_report(session, KUFAR_URL, NOW)
    assert len(r.same_house) == 1 and r.same_house[0] == pytest.approx(FRUNZ_PPM[0], abs=1)


# --- Текст /check: блоки оценки (спека, раздел 4) ---


def _text(session, **kw) -> str:
    return render_check_text(build_check_report(session, KUFAR_URL, NOW), **kw)


def _station_line(text: str) -> str:
    return next(line for line in text.splitlines() if line.startswith("📍"))


def test_text_follows_spec_layout(session, stations):
    _docs(session)
    oid = session.scalar(select(Listing.object_id).where(Listing.external_id == "1000201"))
    other = session.scalar(
        select(RealtyObject).where(RealtyObject.id != oid).order_by(RealtyObject.id)
    )
    other.street_norm, other.house = "ул притыцкого", "10"
    session.flush()
    text = _text(session)
    head = text.split("\n\n")[:4]
    assert head[0].splitlines()[0].startswith("🏠 2-комн.")
    assert head[1] == "✅ Цена в рынке, торг реален"
    assert head[2] == (
        "📍 Метро «Тестовая» — ~5 мин пешком (50 м). Дом 1978 г., 9 этажей\n"
        "👥 Подойдёт: паре или небольшой семье"
    )
    assert head[3] == (
        "⚠️ Минусы:\n"
        "   • дом 1978 г. — узнайте, был ли капремонт и меняли ли стояки\n"
        "👍 Плюсы:\n"
        "   • метро рядом"
    )
    price_block = text.split("\n\n")[4]
    assert price_block.splitlines()[:2] == [
        "📊 Цена: $1 703/м² — на уровне похожих",
        "   (12 двушек в домах 1970–90-х в радиусе 1,5 км, медиана $1 835/м²)",
    ]
    assert price_block.splitlines()[2].startswith("🏢 Ещё в этом доме продаётся 1 квартира: $1 6")
    assert "\n\n\n" not in text


def test_text_without_location(session, stations):
    _docs(session, coords=False)
    text = _text(session)
    assert "📍" not in text and "👍 Плюсы:\n   • метро рядом" not in text
    assert "Дом 1978" not in text and "📊 Цена:" in text


def test_text_approx_location(session, stations):
    _docs(session)
    r = build_check_report(session, KUFAR_URL, NOW)
    approx = replace(r.location, approx=True, walk_min=None)
    line = _station_line(render_check_text(replace(r, location=approx)))
    assert line.startswith("📍 Метро «Тестовая» — ближайшая станция") and "мин" not in line


def test_text_metro_far(session, stations):
    _docs(session)
    r = build_check_report(session, KUFAR_URL, NOW)
    far = replace(r.location, station=None, station_m=None, walk_min=None)
    line = _station_line(render_check_text(replace(r, location=far)))
    assert line == "📍 Метро дальше 3 км. Дом 1978 г., 9 этажей"


def test_text_metro_distance_rounded_to_50_m(session, stations):
    _docs(session)
    r = build_check_report(session, KUFAR_URL, NOW)
    near = replace(r.location, station_m=620.0, walk_min=10)
    line = _station_line(render_check_text(replace(r, location=near)))
    assert line.startswith("📍 Метро «Тестовая» — ~10 мин пешком (600 м). Дом 1978 г.")


@pytest.mark.parametrize(
    ("station_m", "shown"), [(625.0, 650), (10.0, 50), (24.0, 50), (575.0, 600)]
)
def test_text_metro_distance_half_up_and_never_zero(session, stations, station_m, shown):
    _docs(session)
    r = build_check_report(session, KUFAR_URL, NOW)
    near = replace(r.location, station_m=station_m, walk_min=5)
    line = _station_line(render_check_text(replace(r, location=near)))
    assert f"~5 мин пешком ({shown} м)." in line


def test_text_without_year(session, stations):
    _docs(session, year=None)
    r = build_check_report(session, KUFAR_URL, NOW)
    assert r.comparison.year_known is False
    text = render_check_text(r)
    assert "без учёта возраста дома в радиусе" in text
    assert _station_line(text).endswith(". 9-этажный дом") and " г." not in _station_line(text)


def test_text_house_line(session, stations):
    _docs(session)
    assert _station_line(_text(session)).endswith("Дом 1978 г., 9 этажей")


@pytest.mark.parametrize(
    ("changes", "first", "second"),
    [
        (
            {"rooms": 1, "n": 21, "epoch": "pre1970", "radius_km": 3.0},
            "📊 Цена: $1 703/м² — на 7.2% ниже похожих",
            "   (21 однушка в домах до 1970 г. в радиусе 3 км, медиана $1 835/м²)",
        ),
        (
            {"rooms": 4, "n": 12, "epoch": "building"},
            "📊 Цена: $1 703/м² — на 7.2% ниже похожих",
            "   (12 квартир 4+ комн. в строящихся домах в радиусе 1,5 км, медиана $1 835/м²)",
        ),
        (
            {"rooms": 3, "n": 3, "epoch": "post2010", "group": "above", "diff_pct": 12.0},
            "📊 Цена: $1 703/м² — на 12% выше похожих",
            "   (3 трёшки в домах после 2010 г. в радиусе 1,5 км, медиана $1 835/м²)",
        ),
        (
            {"epoch": "1991_2010", "group": "market", "diff_pct": 1.0},
            "📊 Цена: $1 703/м² — на уровне похожих",
            "   (12 двушек в домах 1991–2010 гг. в радиусе 1,5 км, медиана $1 835/м²)",
        ),
        (
            {"epoch": None, "group": "suspicious", "diff_pct": -30.0},
            "📊 Цена: $1 703/м² — на 30% ниже похожих",
            "   (12 двушек без учёта возраста дома в радиусе 1,5 км, медиана $1 835/м²)",
        ),
        (
            {"finish": "none"},
            "📊 Цена: $1 703/м² — на 7.2% ниже похожих",
            "   (12 двушек без отделки в домах 1970–90-х в радиусе 1,5 км, медиана $1 835/м²)",
        ),
        (
            {"rooms": 1, "n": 1, "epoch": "building", "finish": "done"},
            "📊 Цена: $1 703/м² — на 7.2% ниже похожих",
            "   (1 однушка с ремонтом в строящихся домах в радиусе 1,5 км, медиана $1 835/м²)",
        ),
        (
            {"step": "district", "radius_km": None, "epoch": None, "district": "Фрунзенский"},
            "📊 Цена: $1 703/м² — на 7.2% ниже медианы района",
            "   (Фрунзенский р-н, 2-комн.: медиана $1 835/м², 12 объектов)",
        ),
        (
            {"step": "district", "radius_km": None, "epoch": None, "district": None},
            "📊 Цена: $1 703/м² — на 7.2% ниже медианы по Минску",
            "   (по Минску, 2-комн.: медиана $1 835/м², 12 объектов)",
        ),
    ],
)
def test_comparison_line_wording(session, stations, changes, first, second):
    _docs(session)
    r = build_check_report(session, KUFAR_URL, NOW)
    base = {"usd_per_m2": 1703.0, "median": 1835.0, "diff_pct": -7.2, "n": 12, "group": "below"}
    c = replace(r.comparison, **{**base, **changes})
    lines = render_check_text(replace(r, comparison=c)).splitlines()
    assert lines[lines.index(first) + 1] == second


def test_text_without_comparison(session, stations):
    _docs(session)
    r = build_check_report(session, KUFAR_URL, NOW)
    text = render_check_text(replace(r, comparison=None))
    assert "📊" not in text and "Цена: $" not in text


@pytest.mark.parametrize(
    ("prices", "expected"),
    [
        ((), None),
        ((1690.0,), "🏢 Ещё в этом доме продаётся 1 квартира: $1 690/м²"),
        ((1690.0, 1880.0), "🏢 Ещё в этом доме продаются 2 квартиры: $1 690 и $1 880/м²"),
        (
            (1600.0, 1690.0, 1880.0),
            "🏢 Ещё в этом доме продаются 3 квартиры: $1 600, $1 690 и $1 880/м²",
        ),
        (
            (1600.0, 1690.0, 1880.0, 2100.0),
            "🏢 Ещё в этом доме продаются 4 квартиры: от $1 600 до $2 100/м²",
        ),
    ],
)
def test_text_same_house_line(session, stations, prices, expected):
    _docs(session)
    r = build_check_report(session, KUFAR_URL, NOW)
    text = render_check_text(replace(r, same_house=prices))
    if expected is None:
        assert "🏢" not in text
    else:
        assert expected in text.splitlines()


def test_text_first_time_has_assessment_before_the_note(session, stations):
    _docs(session)
    r = build_check_report(session, KUFAR_URL, NOW)
    lines = render_check_text(replace(r, data_level="first_time")).splitlines()
    note = lines.index("ℹ️ Эту квартиру мы видим впервые. Истории пока нет.")
    assert lines.index("📊 Цена: $1 703/м² — на уровне похожих") < note
    assert lines[1] == "Сейчас: $92 500 ($1 703/м²) — Kufar, агентство"
    assert not any(line.startswith(("⏳", "📉", "💬")) for line in lines)
    assert lines[-3] == "Цены объявлений, а не сделок. Это рекомендация, а не гарантия."


@pytest.mark.parametrize(
    ("kind", "icon"),
    [("suspicious", "⚠️"), ("below", "✅"), ("market", "✅"), ("above", "🟡"), ("unknown", "ℹ️")],
)
def test_text_verdict_icon_by_kind(session, stations, kind, icon):
    _docs(session)
    r = build_check_report(session, KUFAR_URL, NOW)
    text = render_check_text(replace(r, assessment=replace(r.assessment, kind=kind)))
    assert f"{icon} {r.assessment.verdict}" in text.splitlines()


# --- Статистика оценки по всему рынку ---


def test_assessment_stats_counts_add_up():
    from minsk_realty.analytics.assessment_stats import assessment_stats

    data = _flat_market(12)
    for i, snaps in data.snapshots.items():
        data.snapshots[i] = [replace(s, lat=53.9 + i / 10_000, lon=27.5) for s in snaps]
    data.snapshots[12] = [replace(s, lat=None, lon=None) for s in data.snapshots[12]]  # без точки
    data.snapshots[11] = [replace(s, usd=None, amount=None) for s in data.snapshots[11]]  # без цены
    data.listings[10] = replace(data.listings[10], source="realt")  # неточная точка
    data.reindex()
    st = assessment_stats(data, compute_baseline(data, NOW), NOW, seed=1)

    assert st.total == 12
    assert sum(b.n for b in st.groups.values()) == 12
    assert set(st.groups) == {"suspicious", "below", "market", "above", "unknown"}
    assert st.groups["unknown"].n == 1  # цены нет
    assert st.no_coords.n == 1 and st.approx.n == 1
    # 12-я без координат: сравнение только по району, а не по радиусу
    assert st.steps["district"].n >= 1
    assert st.steps["near"].n + st.steps["wide"].n + st.steps["district"].n == 11
    assert st.groups["market"].share == pytest.approx(st.groups["market"].n / 12)
    assert len(st.sample) == 10 and set(st.sample) <= set(data.objects)
    assert st == assessment_stats(data, compute_baseline(data, NOW), NOW, seed=1)


def test_assessment_stats_by_finishing():
    from minsk_realty.analytics.assessment_stats import assessment_stats, render_stats_text

    data = _flat_market(12)
    for i, snaps in data.snapshots.items():
        data.snapshots[i] = [replace(s, lat=53.9 + i / 10_000, lon=27.5) for s in snaps]
    data.finishings.update({1: "none", 2: "none", 3: "none", 4: "done"})
    st = assessment_stats(data, compute_baseline(data, NOW), NOW)

    assert st.finishing_known.n == 4 and st.finishing_known.share == pytest.approx(4 / 12)
    assert set(st.by_finish) == {"none", "done", "unknown"}
    assert [sum(c.n for c in st.by_finish[k].values()) for k in ("none", "done", "unknown")] == [
        3, 1, 8,
    ]  # fmt: skip
    assert all(set(g) == set(st.groups) for g in st.by_finish.values())
    # общая раскладка не изменилась от разбивки
    assert sum(c.n for c in st.groups.values()) == 12
    text = render_stats_text(st, 0.1)
    assert "отделка известна: 4 (33.3%)" in text
    assert "без отделки (3):" in text and "с ремонтом (1):" in text and "неизвестна (8):" in text
