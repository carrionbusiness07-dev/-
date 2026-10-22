import importlib.util
import sys
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
