import importlib.util
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    spec = importlib.util.spec_from_file_location("demo_db", ROOT / "scripts" / "demo_db.py")
    demo = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = demo
    spec.loader.exec_module(demo)
    db = tmp_path_factory.mktemp("web") / "demo.db"
    demo.build(db, days=12, flats_at_start=40)

    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", f"sqlite:///{db}")
    from minsk_realty.web import app as web_app
    from minsk_realty.web import views

    web_app._factory.cache_clear()
    views._cache.clear()
    with TestClient(web_app.app) as client:
        yield client
    web_app._factory.cache_clear()
    views._cache.clear()
    mp.undo()


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/?limit=3",
        "/objects?page=2",
        "/objects?page=999&sort=price",
        "/?kind=price_drop&days=30",
        "/?kind=bogus",
        "/objects",
        "/objects?sort=ppm",
        "/market",
        "/sources",
    ],
)
def test_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "Хроника" in response.text
    assert "Демо-данные" in response.text


def test_object_page_and_check_redirect(client):
    page = client.get("/objects")
    href = page.text.split('href="/objects/', 1)[1].split('"', 1)[0]
    assert client.get(f"/objects/{href}").status_code == 200

    missing = client.get("/check", params={"url": "https://example.com/x"}, follow_redirects=False)
    assert missing.status_code == 303 and "missing=unsupported" in missing.headers["location"]


def test_unknown_object_redirects(client):
    response = client.get("/objects/999999", follow_redirects=False)
    assert response.status_code == 303


def test_feed_is_paged(client):
    page = client.get("/?kind=all&days=30&limit=3")
    assert page.text.count('class="entry entry-') == 3
    assert "Показать ещё" in page.text and "limit=53" in page.text


def test_summary_shows_observation_progress(client):
    page = client.get("/")
    assert "Ход наблюдения" in page.text and "Полных проходов за сутки" in page.text


def test_objects_are_paged(client):
    from minsk_realty.web import views

    first = client.get("/objects?sort=price").text
    rows = first.count('<th scope="row">')
    assert 0 < rows <= views.OBJECTS_PAGE


def test_stale_running_pass_shows_as_interrupted(client):
    from minsk_realty.db.models import SourceRun
    from minsk_realty.web import app as web_app
    from minsk_realty.web import views

    with web_app._factory()() as session:
        now = views.market_snapshot(session, "probe").now
        session.add(
            SourceRun(source="realt", started_at=now - timedelta(hours=3), status="running")
        )
        session.commit()
    page = client.get("/sources").text
    assert "прерван" in page and "сборщик остановили посреди прохода" in page


def test_share_format():
    from minsk_realty.web.views import share

    assert (share(None), share(0.004), share(0.1), share(0.123)) == ("—", "0,4%", "10%", "12%")


def _loc(approx=False, station=None):
    from minsk_realty.analytics.geo import Location

    return Location(53.9, 27.55, approx, station, None, None)


def test_map_data_none_without_precise_location():
    from types import SimpleNamespace

    from minsk_realty.web import views

    data = SimpleNamespace(objects={})
    no_cmp = SimpleNamespace(location=None, comparison=None, listing=None)
    assert views.map_data(no_cmp, data) is None
    assert views.map_data(SimpleNamespace(location=_loc(approx=True), comparison=None), data) is None


def test_map_data_groups_points_by_coordinates():
    from types import SimpleNamespace

    from minsk_realty.analytics.comparables import ComparePoint
    from minsk_realty.analytics.geo import Station
    from minsk_realty.web import views

    def obj(rooms, area, floor, total):
        return SimpleNamespace(rooms=rooms, area=area, floor=floor, floors_total=total)

    # 13 квартир в одной точке (разница в 5-м знаке схлопывается) и одна отдельная.
    points = [ComparePoint(100 + i, 53.91 + i * 1e-6, 27.56, 2, None, 2000.0 + i, None) for i in range(13)]
    points.append(ComparePoint(7, 53.95, 27.6, 3, "1970_1990", 1800.4, None, "done"))
    objects = {100 + i: obj(2, 54.0, 5, 9) for i in range(13)} | {7: obj(3, 79.5, 2, 9)}
    report = SimpleNamespace(
        location=_loc(station=Station("Парк Челюскинцев", 53.92, 27.58)),
        comparison=SimpleNamespace(points=tuple(points), radius_km=1.5),
        listing=SimpleNamespace(price=SimpleNamespace(usd_per_m2=1950.4)),
    )
    got = views.map_data(report, SimpleNamespace(objects=objects))
    assert got["flat"] == {"lat": 53.9, "lon": 27.55, "ppm": 1950}
    assert got["radius_km"] == 1.5
    assert got["station"] == {"name": "Парк Челюскинцев", "lat": 53.92, "lon": 27.58}
    big, single = sorted(got["similar"], key=lambda g: -len(g["items"]))
    assert (big["lat"], big["lon"], len(big["items"]), big["more"]) == (53.91, 27.56, 10, 3)
    assert [i["ppm"] for i in big["items"]] == list(range(2000, 2010))  # от дешёвых
    assert big["items"][0] == {
        "id": 100, "url": "/objects/100", "ppm": 2000, "rooms": 2, "area": 54.0,
        "floor": 5, "floors_total": 9, "finish": None,
    }
    assert single["items"] == [
        {"id": 7, "url": "/objects/7", "ppm": 1800, "rooms": 3, "area": 79.5,
         "floor": 2, "floors_total": 9, "finish": "done"}
    ]
    assert single["more"] == 0
    assert "color" not in str(got)
    # Нет цены у квартиры, сравнения и метро: карта всё равно строится.
    report.listing, report.comparison = None, None
    report.location = _loc()
    assert views.map_data(report, SimpleNamespace(objects={})) == {
        "flat": {"lat": 53.9, "lon": 27.55, "ppm": None},
        "radius_km": None, "station": None, "similar": [],
    }


def test_object_page_has_map(client):
    hrefs = client.get("/objects").text.split('href="/objects/')[1:]
    pages = [client.get("/objects/" + h.split('"', 1)[0]).text for h in hrefs]
    page = next(p for p in pages if 'id="map-data"' in p)
    assert "/static/leaflet/leaflet.js" in page and 'id="map"' in page
    assert 'aria-labelledby="assessment-title"' in page
    assert page.index('id="assessment-title"') < page.index('id="verdict-title"')
    assert "yandex.by/maps/?pt=" in page and "google.com/maps?q=" in page
    assert "tile.openstreetmap.org/{z}/{x}/{y}.png" in page and "cartocdn" not in page
    assert 'class="map-legend"' in page and "дешевле" in page and "дороже" in page
    assert all("leaflet" not in p for p in pages if 'id="map-data"' not in p)
    assert page.index('id="map"') < page.index('class="place-address"') < page.index("place-links")


def test_object_page_without_map_keeps_address_and_metro(client, monkeypatch):
    from dataclasses import replace

    from minsk_realty.web import app as web_app

    real = web_app.build_check_report

    def approx_report(*args, **kwargs):
        report = real(*args, **kwargs)
        return replace(report, location=replace(report.location, approx=True, walk_min=None))

    monkeypatch.setattr(web_app, "build_check_report", approx_report)
    page = client.get("/objects/1").text
    assert "map-data" not in page and "leaflet" not in page
    assert 'id="place-title"' in page and "Где находится" in page
    assert 'class="place-address"' in page and "ближайшая станция" in page
    assert "place-links" not in page


def test_leaflet_static_served(client):
    for path in ("leaflet.js", "leaflet.css", "images/marker-icon.png"):
        assert client.get(f"/static/leaflet/{path}").status_code == 200


def test_object_page_shows_assessment_cards(client):
    hrefs = client.get("/objects").text.split('href="/objects/')[1:]
    pages = [client.get("/objects/" + h.split('"', 1)[0]).text for h in hrefs]
    shown = [p for p in pages if 'class="card"' in p]
    assert shown
    for page in shown:
        assert 'class="cards"' in page or "note-house" in page
        for key, title in (
            ("minuses", "Минусы"),
            ("pluses", "Плюсы"),
            ("audience", "Кому подойдёт"),
        ):
            if f'id="{key}-title"' in page:
                col = page.split(f'id="{key}-title"', 1)[1].split("</section>", 1)[0]
                assert title in col and 'class="card-title"' in col
    assert any('class="card-detail"' in p for p in shown)
    assert any("Кому подойдёт" in p and "note-audience" in p for p in shown)
