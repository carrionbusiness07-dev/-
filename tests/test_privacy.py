from minsk_realty.privacy import PHONE_PLACEHOLDER, scrub, seller_key


def test_scrub_drops_pii_keys_and_params():
    data = {
        "contactPhones": ["+375290000001"],
        "contactName": "Иван",
        "account_parameters": [{"p": "name", "v": "Иван"}, {"p": "address", "v": "Минск"}],
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
