"""Район по координатам: границы из OpenStreetMap, запасной вариант в медианах, отчёте и фильтре.

Сети нет: файл границ читается из пакета.
"""

import pytest
from analytics_data import HOUR, NOW, add_background, observe, realt, start_observation
from test_analytics import session  # noqa: F401  (фикстура БД)

from minsk_realty.analytics import build_check_report, load_market, market_baseline
from minsk_realty.analytics.districts import district_at, load_districts
from minsk_realty.analytics.fmt import MINSK_DISTRICTS
from minsk_realty.web import views

# Внутренние точки (на 300 м от границы во все стороны), найдены по самим полигонам.
INSIDE = {
    "Заводской": [(53.85, 27.78), (53.84, 27.61)],
    "Ленинский": [(53.86, 27.61), (53.895, 27.56)],
    "Московский": [(53.87, 27.43), (53.90, 27.545)],
    "Октябрьский": [(53.875, 27.54), (53.88, 28.07)],  # вторая — аэропорт, анклав района
    "Партизанский": [(53.895, 27.845), (53.905, 27.575)],
    "Первомайский": [(53.94, 27.65), (53.955, 27.74)],
    "Советский": [(53.945, 27.605), (53.96, 27.56)],
    "Фрунзенский": [(53.905, 27.49), (53.87, 27.385)],
    "Центральный": [(53.945, 27.455), (53.91, 27.57)],
}


def test_boundaries_file_has_nine_districts_with_known_names():
    districts = load_districts()
    assert sorted(districts) == sorted(MINSK_DISTRICTS)
    assert all(rings for rings in districts.values())
    from importlib.resources import files

    size = (files("minsk_realty.analytics") / "minsk_districts.json").stat().st_size
    assert size < 300_000


@pytest.mark.parametrize(
    ("name", "lat", "lon"),
    [(n, lat, lon) for n, pts in INSIDE.items() for lat, lon in pts],
)
def test_district_at_inside_known_districts(name, lat, lon):
    assert district_at(lat, lon) == name


def test_district_at_outside_minsk_or_without_point_is_none():
    assert district_at(54.2, 27.5) is None  # севернее города
    assert district_at(53.90, 29.0) is None
    assert district_at(0.0, 0.0) is None
    assert district_at(None, 27.5) is None


def test_district_at_respects_holes_of_multipolygon():
    from minsk_realty.analytics import districts

    outer = ((0, 0), (10, 0), (10, 10), (0, 10))
    hole = ((4, 4), (6, 4), (6, 6), (4, 6))
    assert districts._inside(2, 2, (outer, hole))
    assert not districts._inside(5, 5, (outer, hole))
    assert not districts._inside(12, 5, (outer, hole))


def test_object_without_district_gets_it_from_point_in_data_and_medians(session):  # noqa: F811
    start_observation(session)
    # Центр города; района ни в объекте, ни в снимках нет (как у Realt/Onliner).
    ids = add_background(session, 3, district=None, near=(53.9045, 27.5547))
    data = load_market(session)
    assert {data.object_district(i) for i in ids} == {"Центральный"}
    assert market_baseline(session, NOW).price_cell("Центральный", None).n_raw == 3


def test_report_district_by_listing_point(session):  # noqa: F811
    start_observation(session)
    add_background(session, 3)
    observe(session, "realt", NOW - HOUR, realt("555", lat=53.9045, lon=27.5547))
    r = build_check_report(session, "https://realt.by/sale-flats/object/555/", NOW)
    assert r.flat.district == "Центральный"


def test_objects_filter_lists_normalized_districts_without_duplicates(session):  # noqa: F811
    start_observation(session)
    add_background(session, 2, district="Октябрьский", prefix="a")
    add_background(session, 2, district="Октябрьский район", prefix="b")
    add_background(session, 2, district=None, near=(53.875, 27.54), prefix="c")  # по точке
    snap = views.Snapshot(data=load_market(session), now=NOW, demo=False, loaded_at=0.0)
    assert views.object_districts(snap) == ["Октябрьский"]
    assert len(views.object_rows(snap, district="Октябрьский")) == 6
    assert len(views.object_rows(snap, district="Октябрьский район")) == 6
