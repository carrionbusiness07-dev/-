"""Оценка квартиры: год постройки и координаты в срезе рынка. Сети нет."""

from datetime import UTC, datetime, timedelta

import pytest
from analytics_data import NOW
from test_analytics import _market as _flat_market

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
    load_market,
    parse_year,
)
from minsk_realty.analytics.geo import Location, Station
from minsk_realty.analytics.medians import MedianCell, compute_baseline
from minsk_realty.db.models import Base, Listing, RealtyObject, Snapshot
from minsk_realty.db.session import make_engine, make_session_factory

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
        source=source,
        external_id=ext,
        url=f"https://example.test/{ext}",
        object_id=oid,
        match_confidence="high",
        first_seen=T0,
        last_seen=T0,
        **kw,
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


def _pts(n, *, dist_m=1000, ep="1970_1990", rooms=2, first_id=1, base=1500):
    """n точек на заданном расстоянии к северу от (LAT, LON); цена растёт на $1 за точку."""
    return tuple(
        ComparePoint(first_id + i, LAT + dist_m / M_PER_DEG_LAT, LON, rooms, ep, base + i, None)
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


def test_approx_point_goes_to_district():
    cell = MedianCell(None, None, 100, 110, 1700.0)
    c = _compare(_pts(12), point=(LAT, LON, True), district_cell=cell)
    assert (c.step, c.points) == ("district", ())
    assert _compare(_pts(12), point=None, district_cell=cell).step == "district"
    assert _compare(_pts(12), rooms=None, district_cell=cell).step == "district"


def test_no_price_no_comparison():
    assert _compare(_pts(12), usd_per_m2=None) is None


def test_price_group_bounds():
    assert [price_group(x, CFG) for x in (-15, -14.9, -5, -4.9, 4.9, 5)] == [
        "suspicious", "below", "below", "market", "market", "above",
    ]  # fmt: skip


def test_baseline_has_points():
    from dataclasses import replace

    data = _flat_market(12)
    for i, snaps in data.snapshots.items():
        data.snapshots[i] = [replace(s, lat=53.9 + i / 10_000, lon=27.5) for s in snaps]
    data.building_years[3] = 1975
    data.objects[4] = replace(data.objects[4], street_norm=None)
    base = compute_baseline(data, NOW)
    assert len(base.points) == 12
    by_id = {p.object_id: p for p in base.points}
    assert by_id[3].epoch == "1970_1990" and by_id[1].epoch is None
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
