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


def test_map_data_caps_groups_keeping_nearest():
    from types import SimpleNamespace

    from minsk_realty.analytics.comparables import ComparePoint
    from minsk_realty.web import views

    # 305 разных точек по линии от квартиры (53.9, 27.55); самые дорогие стоят дальше всех.
    points = [ComparePoint(i, 53.9 + (i + 1) * 0.001, 27.55, 2, None, 1500.0 + i, None) for i in range(305)]
    report = SimpleNamespace(
        location=_loc(),
        comparison=SimpleNamespace(points=tuple(points), radius_km=3),
        listing=None,
    )
    got = views.map_data(report, SimpleNamespace(objects={}))["similar"]
    assert len(got) == views.MAP_GROUPS_MAX == 300
    assert {g["items"][0]["id"] for g in got} == set(range(300))


def test_object_page_has_map(client):
    hrefs = client.get("/objects").text.split('href="/objects/')[1:]
    pages = [client.get("/objects/" + h.split('"', 1)[0]).text for h in hrefs]
    page = next(p for p in pages if 'id="map-data"' in p)
    assert "/static/leaflet/leaflet.js" in page and 'id="map"' in page
    assert 'aria-labelledby="assessment-title"' in page
    assert page.index('id="assessment-title"') < page.index('id="verdict-title"')
    assert "yandex.by/maps/?pt=" in page and "google.com/maps?q=" in page
    assert "tile.openstreetmap.org/{z}/{x}/{y}.png" in page and "cartocdn" not in page
    assert 'class="map-legend"' in page and 'id="map-legend"' in page
    # Строки «дешевле/дороже» только когда на карте есть похожие.
    assert all("▼ дешевле" not in p for p in pages if '"similar": []' in p)
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


# --- Фото и таблица площадок --------------------------------------------------------------


def _photo_world(tmp_path, extras):
    """Объект из нескольких объявлений; extras — {source: extra последнего снимка}."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from minsk_realty.db.models import Base, Listing, Snapshot
    from minsk_realty.db.session import make_engine, make_session_factory

    engine = make_engine(f"sqlite:///{tmp_path / 'p.db'}")
    Base.metadata.create_all(engine)
    session = make_session_factory(engine)()
    t0 = datetime(2026, 10, 20, tzinfo=UTC)
    ids = {}
    for i, (source, extra) in enumerate(extras.items(), 1):
        listing = Listing(
            source=source, external_id=str(i), url=f"https://{source}.test/{i}", first_seen=t0,
            last_seen=t0, seller_type="owner",
        )
        session.add(listing)
        session.flush()
        ids[source] = listing.id
        for day, ex in enumerate(extra):  # от старого к новому
            session.add(
                Snapshot(listing_id=listing.id, seen_at=t0 + timedelta(days=day), extra=ex,
                         content_hash=f"h{listing.id}{day}")
            )
    session.commit()

    def report(current, *others):
        return SimpleNamespace(
            listing=SimpleNamespace(listing_id=ids[current], source=current),
            others=tuple(
                SimpleNamespace(listing_id=ids[s], source=s, confidence=c) for s, c in others
            ),
        )

    return session, report


def test_photos_order_limit_and_https_only(tmp_path):
    from minsk_realty.web import views

    urls = [f"https://img.test/{n}.jpg" for n in range(8)]
    session, report = _photo_world(
        tmp_path,
        {"kufar": [{"photo_urls": ["https://old.test/x.jpg"]}, {"photo_urls": ["http://bad/1", *urls]}]},
    )
    got = views.photos(session, report("kufar"))
    assert [p["url"] for p in got] == urls[:6]  # последний снимок, http:// отброшен, лимит 6
    assert {p["source"] for p in got} == {"kufar"}


def test_photos_fall_back_to_trusted_other_listing(tmp_path):
    from minsk_realty.web import views

    session, report = _photo_world(
        tmp_path,
        {
            "kufar": [{}],
            "realt": [{"photo_urls": ["https://r.test/1.jpg"]}],
            "onliner": [{"photo_urls": ["https://o.test/1.jpg"]}],
        },
    )
    got = views.photos(session, report("kufar", ("realt", "low"), ("onliner", "high")))
    assert got == [{"url": "https://o.test/1.jpg", "source": "onliner"}]  # low не берём


def test_photos_empty(tmp_path):
    from types import SimpleNamespace

    from minsk_realty.web import views

    session, report = _photo_world(tmp_path, {"kufar": [{}], "realt": [{"photo_urls": []}]})
    assert views.photos(session, report("kufar", ("realt", "high"))) == []
    assert views.photos(session, SimpleNamespace(listing=None, others=())) == []


def test_object_page_photo_strip(client, monkeypatch):
    from minsk_realty.web import app as web_app

    shown = [{"url": "https://i.kufar.test/1.jpg?a=1&b=2", "source": "kufar"},
             {"url": "https://i.kufar.test/2.jpg", "source": "kufar"}]
    monkeypatch.setattr(web_app.views, "photos", lambda session, report: shown)
    page = client.get("/objects/1").text
    assert 'class="photo-strip"' in page and "Фото с Kufar" in page
    assert 'alt="Фото 2, Kufar"' in page
    assert page.count('referrerpolicy="no-referrer"') == 2 and "referrerPolicy" in page and 'loading="lazy"' in page
    assert "<dialog" in page and "a=1&amp;b=2" in page and "onerror=" in page
    assert "Фото не загрузилось" in page and "lb-prev" in page
    assert "<img" not in page.split("<dialog", 1)[1].split("</dialog>", 1)[0]  # без <img> без src

    monkeypatch.setattr(web_app.views, "photos", lambda session, report: [])
    assert 'class="photos"' not in client.get("/objects/1").text


def test_hidden_thumbnails_and_viewer_css():
    css = (ROOT / "src/minsk_realty/web/static/khronika.css").read_text(encoding="utf-8")
    # display:block у .thumb перебивает [hidden], поэтому нужно явное правило
    assert ".thumb[hidden]" in css and ".lightbox > img[hidden]" in css
    assert "body:has(dialog[open])" in css and "color-mix(in srgb, var(--ink) 86%" in css


def test_other_rows_and_table(client, monkeypatch):
    from decimal import Decimal
    from types import SimpleNamespace

    from minsk_realty.analytics.report import OtherListing
    from minsk_realty.web import app as web_app
    from minsk_realty.web import views

    def other(lid, source):
        return OtherListing(
            listing_id=lid, source=source, url=f"https://{source}.test/{lid}", seller_type="owner",
            agency_name=None, amount=Decimal(100000), currency="USD", usd=Decimal(100000),
            confidence="high", other_seller=True, cheaper_by=None, cheaper_currency=None,
        )

    snaps = {
        1: SimpleNamespace(address_raw="Минск, ул. Тестовая, 5", floor=3, floors_total=9, area=54.0),
        2: SimpleNamespace(address_raw=None, floor=None, floors_total=None, area=None),
    }
    data = SimpleNamespace(
        last_snapshot=lambda lid: snaps[lid],
        listing_point=lambda lid, approx: (53.9, 27.5, lid == 2),
    )
    report = SimpleNamespace(others=(other(1, "kufar"), other(2, "realt")))
    monkeypatch.setattr(views, "_photo_urls", lambda s, ids: {1: ["https://t.test/1.jpg"], 2: []})
    rows = views.other_rows(None, data, report)
    assert rows[0]["thumb"] == "https://t.test/1.jpg" and rows[0]["address"] == "Минск, ул. Тестовая, 5"
    assert (rows[0]["floor"], rows[0]["floors_total"], rows[0]["area"]) == (3, 9, 54.0)
    assert rows[0]["map_url"] == "https://yandex.by/maps/?pt=27.5,53.9&z=17"
    assert rows[1]["thumb"] is None and rows[1]["map_url"] is None  # неточная площадка

    monkeypatch.setattr(web_app.views, "other_rows", lambda *a: rows)
    page = client.get("/objects/1").text
    assert "Минск, ул. Тестовая, 5" in page and "3/9" in page and "54 м²" in page
    assert page.count("на карте") == 1 and 'class="thumb"' in page


# --- Хроника цены: события со слов площадки --------------------------------------------------


def _chart_world(tmp_path, extras, others=()):
    session, report = _photo_world(tmp_path, extras)
    first = next(iter(extras))
    return session, report(first, *others)


def test_platform_events_domovita_and_realt(tmp_path):
    from datetime import UTC, datetime

    from minsk_realty.web import views

    session, report = _chart_world(
        tmp_path,
        {
            "domovita": [{"price_history": [["2026-10-10", 90000], ["2026-09-20", 100000], ["2026-08-01", 100000]]}],
            "realt": [{"priceChangeDate": "2026-10-15T10:00:00+03:00", "priceChangeDirection": -1}],
            "kufar": [{"priceChangeDate": "2026-10-01T10:00:00+03:00", "priceChangeDirection": -1}],
        },
        others=[("realt", "high"), ("kufar", "low")],  # low не берём
    )
    got = views.platform_events(session, report, datetime(2026, 10, 24, tzinfo=UTC))
    assert [(e.source, e.at.date().isoformat(), e.usd) for e in got] == [
        ("domovita", "2026-08-01", 100000.0),
        ("domovita", "2026-09-20", 100000.0),
        ("domovita", "2026-10-10", 90000.0),
        ("realt", "2026-10-15", None),
    ]
    assert got[2].change_pct == -10.0 and got[2].direction == -1 and got[3].direction == -1
    assert got[0].at.tzinfo is not None and datetime.now(UTC) > got[0].at
    feed = views.price_feed([], got)
    assert feed[0].text == "цену снижали" and feed[0].platform
    assert feed[1].text == "$90 000" and "со слов площадки" in feed[1].note


def test_platform_events_nothing(tmp_path):
    from types import SimpleNamespace

    from minsk_realty.web import views

    session, report = _chart_world(
        tmp_path,
        {"domovita": [{"price_history": ["junk", [1], ["bad-date", 5]]}], "realt": [{"priceChangeDirection": 0}]},
        others=[("realt", "high")],
    )
    assert views.platform_events(session, report) == []
    assert views.platform_events(session, SimpleNamespace(listing=None, others=())) == []
    assert views.price_feed([], []) == []


def _tl_report(days_apart, n_points, usd0=100000):
    from datetime import UTC, datetime
    from decimal import Decimal
    from types import SimpleNamespace

    from minsk_realty.analytics.history import PricePoint

    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    pts = [
        PricePoint(
            at=t0 + timedelta(days=days_apart * i / max(n_points - 1, 1)), amount=Decimal(usd0 - 1000 * i),
            currency="USD", usd=Decimal(usd0 - 1000 * i), source="kufar", listing_id=1, seller_type="owner",
            agency_name=None, kind="first" if i == 0 else "change", change_pct=None if i == 0 else -1.0,
            is_current_listing=True,
        )
        for i in range(n_points)
    ]
    exposure = SimpleNamespace(start=t0 - timedelta(days=40), state="active", end=t0)
    snap = SimpleNamespace(now=t0 + timedelta(days=days_apart), data=SimpleNamespace(object_events=lambda oid: []))
    return snap, SimpleNamespace(history=pts, exposure=exposure, object_id=None)


def test_timeline_short_observation_gives_no_chart():
    from minsk_realty.web import views

    assert views.timeline(*_tl_report(30, 2)) is None  # мало точек
    assert views.timeline(*_tl_report(3, 4)) is None  # наблюдаем меньше 7 дней
    assert views.timeline(*_tl_report(0, 0)) is None  # нет данных
    assert views.timeline(*_tl_report(20, 4)) is not None


def test_timeline_axis_starts_at_earliest_point_with_platform_dots():
    from datetime import UTC, datetime

    from minsk_realty.web import views

    snap, report = _tl_report(20, 4)
    plain = views.timeline(snap, report)
    assert plain.dots[0][0] == 0 and all(d[2] != "platform" for d in plain.dots)  # публикация за 40 дней не сдвигает ось
    events = [
        views.PlatformEvent(datetime(2026, 9, 21, tzinfo=UTC), "domovita", 120000.0, 0, None),
        views.PlatformEvent(datetime(2026, 10, 10, tzinfo=UTC), "realt", None, -1, None),
    ]
    chart = views.timeline(snap, report, events)
    hollow = [d for d in chart.dots if d[2] == "platform"]
    assert len(hollow) == 1 and hollow[0][0] == 0 and "со слов площадки" in hollow[0][3]
    assert chart.dots[1][0] > 0  # наша первая точка уехала вправо от начала оси
    assert any(m.kind == "platform" and "Realt" in m.label for m in chart.marks)
    assert hollow[0][1] < min(d[1] for d in plain.dots)  # шкала Y подстроена под цену площадки


def test_platform_events_guard_values_and_age(tmp_path):
    from datetime import UTC, datetime

    from minsk_realty.web import views

    session, report = _chart_world(
        tmp_path,
        {
            "domovita": [
                {
                    "price_history": [
                        ["2026-10-10", True], ["2026-10-09", 0], ["2026-10-08", -5],
                        ["2026-10-07", float("nan")], ["2026-10-06", float("inf")],
                        ["2026-10-05", 80000], ["2024-01-01", 70000],
                    ]
                }
            ]
        },
    )
    got = views.platform_events(session, report, datetime(2026, 10, 24, tzinfo=UTC))
    assert [(e.at.date().isoformat(), e.usd) for e in got] == [("2026-10-05", 80000.0)]  # >365 дней и мусор отброшены


def test_object_page_chart_or_feed(client, monkeypatch):
    from datetime import UTC, datetime

    from minsk_realty.web import app as web_app
    from minsk_realty.web import views

    events = [
        views.PlatformEvent(datetime(2026, 10, 10, tzinfo=UTC), "domovita", 90000.0, -1, -10.0),
        views.PlatformEvent(datetime(2026, 10, 15, tzinfo=UTC), "realt", None, -1, None),
    ]
    monkeypatch.setattr(web_app.views, "platform_events", lambda session, report, now=None: events)
    real_timeline = views.timeline
    monkeypatch.setattr(web_app.views, "timeline", lambda *a, **k: None)
    page = client.get("/objects/1").text
    assert 'class="price-feed"' in page and 'class="chart"' not in page and "со слов площадки" in page
    assert "цену снижали" in page and "feed-platform" in page and "в долларах" in page
    assert 'class="history"' not in page.split("price-feed", 1)[1].split("</section>", 1)[0]

    # Режим графика: подставляем готовую Timeline, чтобы не зависеть от длины демо-истории.
    chart = views.Timeline("M0 100 H1000", [(100.0, "$90k")], [(0.0, "окт")], [(500.0, 100.0, "first", "x")], [])
    monkeypatch.setattr(web_app.views, "timeline", lambda *a, **k: chart)
    page = client.get("/objects/1").text
    assert 'class="chart"' in page and "price-feed" not in page
    assert "pt-platform" not in page  # точки приходят из timeline; здесь они в таблице ниже
    table = page.split('<table class="history">', 1)[1].split("</table>", 1)[0]
    assert table.count('class="row-platform"') == 2
    assert "$90 000" in table.replace(" ", " ") and "в долларах" in table and "Domovita" in table
    assert "цену снижали" in table and table.count("со слов площадки") >= 2
    assert "в долларах, которые сообщает сама площадка" in page
    monkeypatch.setattr(web_app.views, "timeline", real_timeline)


def test_history_table_merges_platform_rows_by_date():
    from datetime import UTC, datetime
    from decimal import Decimal

    from minsk_realty.web import views

    own = views.HistoryRow(datetime(2026, 10, 12, tzinfo=UTC), Decimal(1), "USD", None, "first", "Kufar", "—")
    ev = views.PlatformEvent(datetime(2026, 10, 5, tzinfo=UTC), "domovita", 5.0, 0, None)
    rows = views.history_table([own], [ev])
    assert rows == [ev, own] and rows[0].platform and not rows[1].platform


def test_chart_css_uses_tokens_only():
    css = (ROOT / "src/minsk_realty/web/static/khronika.css").read_text(encoding="utf-8")
    block = css[css.index(".pt-platform"):css.index(".feed-platform .delta")]
    assert "#" not in block and "rgb" not in block and "var(--ink-3)" in block
