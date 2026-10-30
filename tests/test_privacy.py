import pytest

from minsk_realty.privacy import PHONE_PLACEHOLDER, scrub, seller_key


def test_scrub_drops_pii_keys_and_params():
    data = {
        "contactPhones": ["+375290000001"],
        "contactName": "Иван",
        "account_parameters": [
            {"p": "name", "v": "Иван"},
            {"p": "contact_person", "v": "Иван"},
            {"p": "address", "v": "Минск"},
        ],
        "nested": {"email": "a@b.c", "keep": 1},
    }
    cleaned = scrub(data)
    assert "contactPhones" not in cleaned and "contactName" not in cleaned
    assert cleaned["account_parameters"] == [{"p": "address", "v": "Минск"}]
    assert cleaned["nested"] == {"keep": 1}


def test_scrub_masks_phones_in_text():
    text = "Звоните +375 (29) 000-00-02 или 8029 000 00 03"
    assert scrub(text) == f"Звоните {PHONE_PLACEHOLDER} или {PHONE_PLACEHOLDER}"


def test_seller_key_is_stable_and_not_reversible():
    first = seller_key("kufar", "9000001", salt="s")
    assert first == seller_key("kufar", 9000001, salt="s")
    assert first != seller_key("realt", "9000001", salt="s")
    assert "9000001" not in first
    assert seller_key("kufar", None) is None


def test_seller_key_requires_salt(monkeypatch):
    monkeypatch.delenv("SELLER_KEY_SALT")
    with pytest.raises(RuntimeError):
        seller_key("kufar", "9000001")


def test_free_text_never_reaches_snapshot(fixtures_dir):
    import json

    from minsk_realty.sources.domovita import DomovitaAdapter, extract_next_data, listing_cards
    from minsk_realty.sources.domovita import to_raw as domovita_raw
    from minsk_realty.sources.kufar import KufarAdapter
    from minsk_realty.sources.kufar import to_raw as kufar_raw
    from minsk_realty.sources.realt import RealtAdapter, extract_page_props
    from minsk_realty.sources.realt import to_raw as realt_raw

    text = "Торг, звоните, Анна +375291234567"
    kufar_ad = json.loads(
        (fixtures_dir / "kufar" / "search_sell_minsk.synthetic.json").read_text(encoding="utf-8")
    )["ads"][0]
    kufar_ad["body"], kufar_ad["body_short"] = "", text
    realt_obj = extract_page_props(
        (fixtures_dir / "realt" / "sale_flats.synthetic.html").read_text(encoding="utf-8")
    )["objects"][0]
    realt_obj |= {"description": text, "headline": text, "comments": text, "specialComment": text}
    page = extract_next_data(
        (fixtures_dir / "domovita" / "flats_sale_minsk.2026-10-23.html").read_text(encoding="utf-8")
    )
    domovita_item = dict(listing_cards(page["props"]["pageProps"])["items"][0], ads_text=text)

    for raw, adapter in (
        (kufar_raw(kufar_ad), KufarAdapter(client=object())),
        (realt_raw(realt_obj), RealtAdapter(client=object())),
        (domovita_raw(domovita_item), DomovitaAdapter(client=object())),
    ):
        for dumped in (
            json.dumps(raw.payload, ensure_ascii=False),
            adapter.parse(raw).model_dump_json(),
        ):
            assert "Анна" not in dumped and "звоните" not in dumped and "1234567" not in dumped
        assert raw.payload["_facts"] == ["bargain"]
