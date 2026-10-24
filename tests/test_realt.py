import json
from decimal import Decimal

import httpx

from minsk_realty.sources.base import PoliteClient
from minsk_realty.sources.realt import RealtAdapter, extract_page_props, to_raw


def html(fixtures_dir):
    return (fixtures_dir / "realt" / "sale_flats.synthetic.html").read_text(encoding="utf-8")


def test_parse_object(fixtures_dir):
    obj = extract_page_props(html(fixtures_dir))["objects"][0]
    snap = RealtAdapter(client=object()).parse(to_raw(obj))
    assert snap.external_id == "4100001"
    assert snap.url == "https://realt.by/sale-flats/object/4100001/"
    assert snap.price_currency == "USD" and snap.price_amount == Decimal(92500)
    assert snap.price_usd == Decimal(92500)
    assert snap.seller_type == "agency" and snap.agency_name == "Агентство Пример"
    assert (snap.rooms, snap.area_total, snap.floor, snap.floors_total) == (2, 54.2, 5, 9)
    assert (snap.street, snap.house) == ("Притыцкого", "10/1")
    assert snap.extra["priceChangeDirection"] == -1
    assert snap.posted_at.isoformat() == "2026-07-20T12:00:00+03:00"


def test_raw_payload_has_no_personal_data(fixtures_dir):
    obj = extract_page_props(html(fixtures_dir))["objects"][0]
    payload = to_raw(obj).payload
    for key in ("contactName", "contactPhones", "contactEmail", "userUuid"):
        assert key not in payload
    assert payload["_seller_key"]
    assert "user@example.com" not in json.dumps(payload, ensure_ascii=False)


def test_search_keeps_only_minsk(fixtures_dir):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sale/flats/"
        return httpx.Response(200, text=html(fixtures_dir))

    client = PoliteClient(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert [r.external_id for r in RealtAdapter(client).search()] == ["4100001"]


def test_resolve_url():
    adapter = RealtAdapter(client=object())
    assert adapter.resolve_url("https://realt.by/sale-flats/object/4242994/") == "4242994"
    assert adapter.resolve_url("https://realt.by/rent-flat-for-long/object/1/") is None


def test_repair_state_goes_to_extra(fixtures_dir):
    data = json.loads(
        (fixtures_dir / "realt" / "sale_flats.2026-10-23.json").read_text(encoding="utf-8")
    )
    obj = next(o for o in data["objects"] if o.get("repairState") is not None)
    snap = RealtAdapter(client=object()).parse(to_raw(obj))
    assert snap.extra["repairState"] == obj["repairState"]
    bare = next(o for o in data["objects"] if o.get("repairState") is None)
    assert "repairState" not in RealtAdapter(client=object()).parse(to_raw(bare)).extra


def test_repair_labels_match_realt_own_dictionary(fixtures_dir):
    from minsk_realty.sources.realt import REALT_REPAIR_LABELS

    path = fixtures_dir / "realt" / "repair_state_options.2026-10-24.json"
    options = json.loads(path.read_text(encoding="utf-8"))["repairState"]
    assert REALT_REPAIR_LABELS == {o["value"]: o["label"] for o in options}
