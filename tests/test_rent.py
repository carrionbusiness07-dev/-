"""Долгосрочная аренда: адаптеры в режиме deal="rent" на живых фикстурах 31.10.2026."""

import json
from decimal import Decimal

import httpx

from minsk_realty.sources import RENT_ADAPTERS
from minsk_realty.sources.base import PoliteClient
from minsk_realty.sources.domovita_rent import DomovitaRentAdapter
from minsk_realty.sources.kufar import KufarAdapter
from minsk_realty.sources.onliner import OnlinerAdapter
from minsk_realty.sources.realt import RealtAdapter


def client(handler) -> PoliteClient:
    return PoliteClient(transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_kufar_rent_asks_for_let(fixtures_dir):
    body = (fixtures_dir / "kufar" / "search_let_minsk.2026-10-31.json").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["typ"] == "let"
        return httpx.Response(200, text=body)

    adapter = KufarAdapter(client(handler), deal="rent")
    snaps = [adapter.parse(raw) for raw in adapter.search()]
    assert len(snaps) == 3 and adapter.last_search_complete
    assert all(s.price_amount and s.rooms and s.area_total for s in snaps)


def test_realt_rent_urls(fixtures_dir):
    html = (fixtures_dir / "realt" / "rent_flats.2026-10-31.html").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rent/flat-for-long/"
        return httpx.Response(200, text=html)

    adapter = RealtAdapter(client(handler), deal="rent")
    snaps = [adapter.parse(raw) for raw in adapter.search()]
    assert len(snaps) == 3
    assert snaps[0].url == f"https://realt.by/rent-flat-for-long/object/{snaps[0].external_id}/"
    assert adapter.resolve_url(snaps[0].url) == snaps[0].external_id
    assert adapter.resolve_url("https://realt.by/sale-flats/object/1/") is None


def test_onliner_rent_skips_rooms_and_reads_owner(fixtures_dir):
    body = (fixtures_dir / "onliner" / "search_rent_minsk.2026-10-31.json").read_text(
        encoding="utf-8"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "ak.api.onliner.by"
        return httpx.Response(200, text=body)

    adapter = OnlinerAdapter(client(handler), deal="rent")
    raws = list(adapter.search())
    assert len(raws) == 2  # третья — комната, не квартира
    items = {str(i["id"]): i for i in json.loads(body)["apartments"]}
    for raw in raws:
        snap = adapter.parse(raw)
        item = items[raw.external_id]
        assert snap.rooms == int(item["rent_type"].split("_")[0])
        assert snap.seller_type == ("owner" if item["contact"]["owner"] else "agency")
        assert snap.url.startswith("https://r.onliner.by/ak/apartments/")


def test_domovita_rent_cards(fixtures_dir):
    html = (fixtures_dir / "domovita" / "flats_rent_minsk.2026-10-31.html").read_text(
        encoding="utf-8"
    )
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(request.url.params.get("page"))
        return httpx.Response(200, text=html)  # вторая страница повторяет первую: конец

    adapter = DomovitaRentAdapter(client(handler))
    raws = list(adapter.search())
    assert pages == [None, "2"] and adapter.last_search_complete
    snaps = {s.external_id: s for s in (adapter.parse(raw) for raw in raws)}
    owner = snaps["152571"]
    assert owner.seller_type == "owner"
    assert (owner.price_currency, owner.price_amount) == ("USD", Decimal(450))
    assert (owner.rooms, owner.area_total, owner.floor, owner.floors_total) == (2, 46.0, 3, 9)
    assert (owner.district, owner.extra["microdistrict"]) == ("Заводской", "Ангарская")
    assert owner.url.startswith("https://domovita.by/minsk/flats/rent/")
    assert adapter.resolve_url(owner.url)
    assert snaps["152554"].price_currency == "BYN"  # 2250 р. круглая, 764 $ — пересчёт
    payload = json.dumps([r.payload for r in raws], ensure_ascii=False)
    assert "375" not in payload and "Описание" not in payload


def test_rent_registry_covers_every_source():
    from minsk_realty.sources import ADAPTERS

    assert set(RENT_ADAPTERS) == set(ADAPTERS)
