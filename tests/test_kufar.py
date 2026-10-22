import json
from decimal import Decimal

import httpx

from minsk_realty.sources.base import PoliteClient
from minsk_realty.sources.kufar import KufarAdapter, next_cursor, to_raw


def load(fixtures_dir):
    return json.loads((fixtures_dir / "kufar" / "search_sell_minsk.synthetic.json").read_text())


def test_parse_agency_listing_in_usd(fixtures_dir):
    snap = KufarAdapter(client=object()).parse(to_raw(load(fixtures_dir)["ads"][0]))
    assert snap.external_id == "1000000001"
    assert snap.price_currency == "USD" and snap.price_amount == Decimal(92500)
    assert snap.price_usd == Decimal(92500)
    assert snap.seller_type == "agency" and snap.seller_key
    assert snap.rooms == 2 and snap.area_total == 54.2
    assert snap.town == "Минск" and snap.district == "Фрунзенский"
    assert snap.address_raw == "Минск, ул. Притыцкого, 10"
    assert (snap.lat, snap.lon) == (53.9048, 27.4782)
    assert snap.photo_urls[0] == "https://rms.kufar.by/v1/gallery/adim1/fake-0001.jpg"


def test_parse_byn_listing_keeps_original_currency(fixtures_dir):
    snap = KufarAdapter(client=object()).parse(to_raw(load(fixtures_dir)["ads"][1]))
    assert snap.price_currency == "BYN" and snap.price_amount == Decimal(234300)
    assert snap.price_usd == Decimal(71000)
    assert snap.seller_type == "owner"


def test_raw_payload_has_no_personal_data(fixtures_dir):
    raw = to_raw(load(fixtures_dir)["ads"][0])
    dumped = json.dumps(raw.payload, ensure_ascii=False)
    assert "account_id" not in raw.payload
    assert '"p": "name"' not in dumped


def test_resolve_url():
    adapter = KufarAdapter(client=object())
    assert adapter.resolve_url("https://re.kufar.by/vi/1000000001") == "1000000001"
    assert adapter.resolve_url("https://www.kufar.by/item/1000000001?utm=x") == "1000000001"
    assert (
        adapter.resolve_url("https://re.kufar.by/vi/minsk/kupit/kvartiru/1000000001")
        == "1000000001"
    )
    assert adapter.resolve_url("https://realt.by/sale-flats/object/4100001/") is None


def test_search_follows_cursor_and_pauses(fixtures_dir):
    page1 = load(fixtures_dir)
    page2 = {"ads": [], "pagination": {"pages": [{"label": "self", "token": None}]}}
    seen_params = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(dict(request.url.params))
        return httpx.Response(200, json=page2 if "cursor" in request.url.params else page1)

    sleeps = []
    client = PoliteClient(transport=httpx.MockTransport(handler), sleep=sleeps.append)
    ids = [r.external_id for r in KufarAdapter(client).search()]

    assert ids == ["1000000001", "1000000002"]
    assert seen_params[0]["typ"] == "sell" and seen_params[0]["cat"] == "1010"
    assert seen_params[1]["cursor"] == next_cursor(page1)
    assert len(sleeps) == 1 and 2.0 <= sleeps[0] <= 5.0
