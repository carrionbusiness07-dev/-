import pytest

from minsk_realty.analytics.facts import (
    FACT_LABELS,
    WARNING_FACTS,
    extract_facts,
    facts_line,
)


@pytest.mark.parametrize("text", ["Торг уместен", "возможен торг при осмотре"])
def test_bargain(text):
    assert extract_facts(text) == ["bargain"]


@pytest.mark.parametrize(
    "text", ["без торга", "торг не уместен", "Торга нет", "Торговый центр рядом"]
)
def test_bargain_negations(text):
    assert extract_facts(text) == []


def test_negation_does_not_cross_clauses():
    assert extract_facts("Без мебели, торг уместен") == ["bargain"]


def test_share_is_warning_and_first():
    line = facts_line(extract_facts("Продаётся 1/2 доли, мебель остаётся"))
    assert line.startswith("⚠️ продаётся доля")
    assert "с мебелью" in line


@pytest.mark.parametrize("text", ["ТЁПЛЫЙ ПОЛ", "теплый пол"])
def test_yo_and_case(text):
    assert extract_facts(text) == ["warm_floor"]


def test_mortgage_vs_no_mortgage():
    assert extract_facts("возможна ипотека") == ["mortgage"]
    assert extract_facts("под ипотеку не подходит") == ["no_mortgage"]
    assert extract_facts("без ипотеки") == ["no_mortgage"]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("чистая продажа", "clean_sale"),
        ("альтернатива", "alternative"),
        ("встречная покупка", "alternative"),
        ("продажа без встречной покупки", "clean_sale"),
    ],
)
def test_clean_sale_and_alternative(text, code):
    assert extract_facts(text) == [code]


def test_limit_and_empty():
    assert facts_line([], 5) is None
    assert facts_line([], None) is None
    codes = ["bargain", "urgent", "exchange", "furniture", "appliances", "storage", "elevator"]
    line = facts_line(codes, 5)
    assert sum(FACT_LABELS[c] in line for c in codes) == 5
    assert facts_line(codes, None).count(",") == 6


def test_warnings_survive_limit():
    line = facts_line(["bargain", "urgent", "share", "registered"], 3)
    assert line == "⚠️ продаётся доля · ⚠️ прописаны люди\n📝 Из описания: торг"


def test_none_and_blank():
    assert extract_facts(None) == []
    assert extract_facts("  \n ") == []


def test_negated_warnings_are_not_flagged():
    assert extract_facts("Никто не прописан, обременений нет") == []
    assert extract_facts("Прописан 1 человек") == ["registered"]


def test_repair_variants():
    assert extract_facts("Свежий ремонт") == ["fresh_repair"]
    assert extract_facts("Требуется ремонт") == ["needs_repair"]
    assert extract_facts("Квартира без ремонта") == ["needs_repair"]
    assert extract_facts("Не требует ремонта") == []
    assert extract_facts("Требуется капитальный ремонт") == ["needs_repair"]


def test_courtyard_windows():
    assert extract_facts("Окна выходят во двор") == ["courtyard_windows"]
    assert extract_facts("Окна на улицу и на двор") == []


def test_cut_off_last_word_is_ignored():
    pad = "Светлая квартира в тихом районе " * 10
    cut = pad[:145] + " лифт"  # 150 символов, как у Kufar: «лифтовой» обрезан на «лифт»
    assert len(cut) == 150
    assert extract_facts(cut) == []
    assert extract_facts("Светлая квартира, есть лифт") == ["elevator"]


def test_every_code_has_label():
    assert WARNING_FACTS <= set(FACT_LABELS)
    # каждый код, который умеет находить extract_facts, подписан
    samples = (
        "доля общежитие прописан залог ипотека не подходит только наличные торг срочно обмен "
        "чистая продажа альтернатива ипотека рассрочка документы готовы мебель техника "
        "свежий ремонт требуется ремонт перепланировка кладовка окна во двор теплый пол "
        "лифт закрытая территория парковка капремонт"
    )
    found = extract_facts(samples)
    assert found and set(found) <= set(FACT_LABELS)
    assert found == [c for c in FACT_LABELS if c in found]


def test_full_text_ending_in_word_keeps_last_word():
    long_text = "Просторная светлая квартира в тихом районе рядом с парком и школой. " * 5 + "Торг"
    assert len(long_text) > 150
    assert extract_facts(long_text) == ["bargain"]


@pytest.mark.parametrize(
    "text", ["Не против ипотеки", "без первоначального взноса, ипотека", "ипотека не исключена"]
)
def test_no_false_no_mortgage(text):
    assert "no_mortgage" not in extract_facts(text)


@pytest.mark.parametrize(
    "text", ["Право собственности зарегистрировано", "Свободна от прописанных", "Нет прописанных"]
)
def test_registered_needs_people(text):
    assert extract_facts(text) == []


def test_registered_with_people():
    assert extract_facts("В квартире прописаны 2 человека") == ["registered"]
    assert extract_facts("Прописаны люди") == ["registered"]
