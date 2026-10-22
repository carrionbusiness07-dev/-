import pytest

from minsk_realty.matching.address import (
    Street,
    normalize_house,
    normalize_street,
    parse_address,
    streets_match,
)


@pytest.mark.parametrize(
    "raw",
    ["ул. Притыцкого", "Притыцкого ул.", "улица Притыцкого", "УЛ.ПРИТЫЦКОГО", "  ул  Притыцкого "],
)
def test_street_forms_normalize_to_one(raw):
    assert normalize_street(raw) == Street("ул", ("притыцкого",))


def test_street_kinds_and_yo():
    assert normalize_street("пр-т Независимости") == normalize_street("просп. Независимости")
    assert normalize_street("пр. Независимости").kind == "пр-т"
    assert normalize_street("ул. Алёшина") == normalize_street("ул. Алешина")
    assert normalize_street("2-й пер. Ленина") == Street("пер", ("2", "ленина"))


def test_streets_match_rules():
    s = normalize_street
    assert streets_match(s("ул. Притыцкого"), s("Притыцкого"))  # тип известен у одной стороны
    assert streets_match(s("ул. Якуба Коласа"), s("ул. Я. Коласа"))
    assert streets_match(s("Якуба Коласа"), s("ул. Коласа"))
    assert streets_match(s("ул. Ленина"), s("пер. Ленина")) is False
    assert streets_match(s("Ленина"), s("2-й пер. Ленина")) is False  # номер переулка важен
    assert streets_match(s("ул. Притыцкого"), s("ул. Пушкина")) is False
    assert streets_match(s("ул. Притыцкого"), None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10", "10"),
        ("д. 10", "10"),
        ("10А", "10а"),
        ("10 a", "10а"),  # латинская «a»
        ("10 к.2", "10/2"),
        ("10к2", "10/2"),
        ("10 корп. 2", "10/2"),
        ("10/2", "10/2"),
        ("10а/1", "10а/1"),
        ("10-2", None),  # неоднозначно: корпус или квартира
        ("", None),
        (None, None),
    ],
)
def test_house(raw, expected):
    assert normalize_house(raw) == expected


@pytest.mark.parametrize(
    ("raw", "street", "house"),
    [
        ("Минск, ул. Притыцкого, 10", "ул притыцкого", "10"),
        ("г. Минск, пр-т Независимости, д. 95к2", "пр-т независимости", "95/2"),
        ("Притыцкого ул., 10А", "ул притыцкого", "10а"),
        ("ул.Я.Коласа 21/1", "ул коласа", "21/1"),
        ("Минск, Фрунзенский р-н, Притыцкого 10 к.2", "притыцкого", "10/2"),
        ("Минск, ул. Облачная, 3, кв. 5", "ул облачная", "3"),
        ("Минск, 2-й пер. Ленина 4", "пер 2 ленина", "4"),
        ("Минск, ул. Притыцкого", "ул притыцкого", None),
        ("Минск", None, None),
        # Микрорайон с номером — не улица; улица с типом важнее части без типа.
        ("Минск, Сухарево-5, ул. Притыцкого, 10", "ул притыцкого", "10"),
        ("Минск, Сухарево-5, Притыцкого, 10", "притыцкого", "10"),
        # Второе число: большое — квартира (отбрасываем), маленькое — корпус или квартира.
        ("Минск, ул. Притыцкого, 10, 25", "ул притыцкого", "10"),
        ("Минск, ул. Притыцкого 10, 154", "ул притыцкого", "10"),
        ("Минск, ул. Притыцкого, 10, 2", "ул притыцкого", None),
        # Две разные улицы с типом — не угадываем.
        ("Минск, ул. Притыцкого, пр-т Пушкина, 10", None, None),
    ],
)
def test_parse_kufar_address(raw, street, house):
    parsed = parse_address(raw)
    assert (str(parsed.street) if parsed.street else None, parsed.house) == (street, house)


def test_street_tail_is_shared_by_matching_streets():
    s = normalize_street
    assert s("ул. Якуба Коласа").tail == s("Коласа").tail == "коласа"
    assert normalize_street("ул. Притыцкого") is normalize_street("ул. Притыцкого")  # кеш
