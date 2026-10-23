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
