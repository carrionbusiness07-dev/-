"""Факты из описания продавца: правила на регулярных выражениях, текст нигде не хранится.

extract_facts(text) → коды фактов; facts_line(codes, limit) → строка для отчёта.
Kufar отдаёт лишь первые 150 символов описания (последнее слово обрезано), Realt и Domovita —
весь текст, поэтому слова в правилах ищутся целиком (\\b в конце), а возможное обрезанное
последнее слово отбрасывается.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

FACT_LABELS: dict[str, str] = {
    # тревожные
    "share": "продаётся доля",
    "dormitory": "общежитие",
    "registered": "прописаны люди",
    "encumbrance": "залог или арест",
    "no_mortgage": "ипотека не подходит",
    "cash_only": "только наличные",
    # сделка
    "bargain": "торг",
    "urgent": "срочно",
    "exchange": "обмен",
    "clean_sale": "чистая продажа",
    "alternative": "встречная покупка",
    "mortgage": "ипотека или кредит",
    "installment": "рассрочка",
    "docs_ready": "документы готовы",
    # квартира
    "furniture": "с мебелью",
    "appliances": "с техникой",
    "fresh_repair": "свежий ремонт",
    "needs_repair": "требует ремонта",
    "replanning": "перепланировка",
    "storage": "кладовка",
    "courtyard_windows": "окна во двор",
    "warm_floor": "тёплый пол",
    # дом
    "elevator": "лифт",
    "gated": "закрытая территория",
    "parking": "парковка",
    "major_repair": "капремонт",
}
WARNING_FACTS: frozenset[str] = frozenset(
    {"share", "dormitory", "registered", "encumbrance", "no_mortgage", "cash_only"}
)

# Паттерны пишутся под нормализованный текст: нижний регистр, ё→е.
# (паттерны, проверять ли отрицание рядом)
_RULES: dict[str, tuple[tuple[str, ...], bool]] = {
    "share": (
        (
            r"\bдол(?:я|ю)\b",
            r"\b\d+\s*/\s*\d+\s+(?:доли|части)\b",
            r"\bдоли\s+в\s+(?:квартир\w+|праве)\b",
        ),
        True,
    ),
    "dormitory": ((r"\bобщежити(?:е|я|и|ем)\b", r"\bобщаг(?:а|и|е|у|ой)\b"), True),
    # только с людьми рядом: «зарегистрировано право собственности» и «свободна от прописанных» не в счёт
    "registered": (
        (
            (
                r"\b(?:прописан|зарегистрирован)(?:а|ы|о)?\s+(?:\w+\s+){0,2}?"
                r"(?:люди|человек\w*|чел|жильц\w+|жител\w+|ребен\w+|дет(?:и|ей)"
                r"|взросл\w+|пенсионер\w*)\b"
            ),
            (
                r"\b(?:люди|человек\w*|чел|жильц\w+|ребен\w+|дет(?:и|ей))\s+(?:\w+\s+)?"
                r"(?:прописан|зарегистрирован)(?:а|ы|о)?\b"
            ),
        ),
        True,
    ),
    "encumbrance": (
        (
            r"\bзалог(?:а|е|у|ом)?\b",
            r"\bарест(?:а|е|у|ом|ован(?:а|о|ная|ной))?\b",
            r"\bобременени(?:е|я|й|ем|ями)\b",
        ),
        True,
    ),
    # «ипотека не подходит», «под ипотеку не подходит»: отрицание уже в паттерне («без ипотеки» не в счёт);
    # только с глаголами отказа, иначе «не против ипотеки» и «без взноса, ипотека» дают ложную тревогу
    "no_mortgage": (
        (
            (
                r"\bне\s+(?:подходит|рассматриваю|рассматриваем|рассматривается|принимается"
                r"|принимаем|возможна|для|под)(?:\s+(?:под|для))?\s+ипотек(?:а|у|и|е)\b"
            ),
            (
                r"\bипотек(?:а|у|и|е)\s+(?:не\s+(?:подходит|рассматриваю|рассматриваем"
                r"|рассматривается|принимается|принимаем|возможна)|невозможна|исключена|нет)\b"
            ),
        ),
        False,
    ),
    "cash_only": (
        (r"\bтолько\s+(?:за\s+)?(?:наличными|наличные|наличку|нал)\b",),
        True,
    ),
    "bargain": ((r"\bторг(?:а|у|е|ом)?\b",), True),
    "urgent": ((r"\bсрочн(?:о|ая|ую|ый|ые)\b",), True),
    "exchange": ((r"\bобмен(?:а|у|е|ом)?\b", r"\bменяю\b"), True),
    "clean_sale": (
        (
            r"\b(?:чистая|прямая)\s+продаж(?:а|у|и|е)\b",
            r"\bбез\s+(?:встречн(?:ой|ую)\s+покупк(?:и|у)|альтернатив(?:ы|у))\b",
        ),
        False,
    ),
    "alternative": (
        (
            r"\bальтернатив(?:а|у|ой|ы)\b",
            r"\bвстречн(?:ая|ой|ую)\s+(?:покупк(?:а|и|у|ой)|альтернатив(?:а|у|ой))\b",
        ),
        True,
    ),
    "mortgage": ((r"\bипотек(?:а|у|и|е|ой)\b", r"\bкредит(?:а|у|ом|ы)?\b"), True),
    "installment": ((r"\bрассрочк(?:а|у|и|е|ой)\b",), True),
    "docs_ready": (
        (r"\bдокумент(?:ы|ов)\s+(?:все\s+)?(?:готовы|собраны|в\s+порядке)\b",),
        True,
    ),
    "furniture": ((r"\bмебел(?:ь|ью|и)\b", r"\bмеблирован(?:а|о|ная|ную|ный)\b"), True),
    "appliances": (
        (
            r"\bтехник(?:а|ой|у|и)\b",
            r"\bхолодильник(?:а|ом)?\b",
            r"\bстиральн(?:ая|ой|ую)\s+машин(?:а|ой|у|ы)\b",
        ),
        True,
    ),
    "fresh_repair": (
        (
            r"\b(?:свежий|свежим|новый|недавний)\s+ремонт(?:ом|е)?\b",
            r"\bевроремонт(?:ом|е)?\b",
            r"\bпосле\s+ремонта\b",
        ),
        True,
    ),
    "needs_repair": (
        (
            r"\bтребу(?:ет|ется)\s+(?:(?:косметическ|капитальн)(?:ий|ый|ого)\s+)?(?:кап)?ремонт(?:а)?\b",
            r"\bнужен\s+(?:кап)?ремонт\b",
            r"\bремонт\s+(?:нужен|требуется)\b",
            r"\bпод\s+ремонт\b",
            r"\bбез\s+ремонта\b",
        ),
        True,
    ),
    "replanning": (
        (r"\bперепланировк(?:а|у|и|е|ой|ок)\b", r"\bперепланирован(?:а|о|ная|ную|ный)\b"),
        True,
    ),
    "storage": ((r"\bкладов(?:ка|ку|ки|ке|ая|ой)\b",), True),
    "courtyard_windows": (
        (
            r"\b(?:окн(?:а|о|ами)|вид|выход\w*)\s+(?:(?!не\b)\w+\s+){0,2}?(?:выходят?\s+)?(?:во|на)\s+двор(?:ик)?\b",
        ),
        True,
    ),
    "warm_floor": (
        (r"\bтепл(?:ый|ые|ым|ого)\s+пол(?:ы|а|ом|ами)?\b", r"\bподогрев\s+пола\b"),
        True,
    ),
    "elevator": ((r"\bлифт(?:ом|а|ы|у)?\b",), True),
    "gated": (
        (
            r"\b(?:закрыт|охраняем)(?:ая|ой|ую|ый)\s+(?:\w+\s+)?(?:территори(?:я|и|ю|ей)|двор)\b",
            r"\bшлагбаум(?:ом|а)?\b",
        ),
        True,
    ),
    "parking": (
        (r"\bпарковк(?:а|и|у|е|ой)\b", r"\bпаркинг(?:а|и|е|ом)?\b", r"\bмашиноместо\b"),
        True,
    ),
    "major_repair": (
        (
            r"(?<!требуется )(?<!требует )(?<!нужен )\bкапремонт(?:а|у|ом|е)?\b",
            r"(?<!требуется )(?<!требует )(?<!нужен )\bкапитальн(?:ый|ого|ым|ом)\s+ремонт(?:а|ом|е)?\b",
        ),
        True,
    ),
}
assert list(_RULES) == list(FACT_LABELS)  # порядок кодов = порядок подписей

_COMPILED = {code: ([re.compile(p) for p in pats], neg) for code, (pats, neg) in _RULES.items()}
_CLAUSE_SPLIT = re.compile(r"[.,;:!?()\n]+")
_WORD = re.compile(r"\w+")
# «без» управляет словом справа; остальные отрицания стоят по обе стороны («торг не уместен»).
# Перечисление «без залога, ареста и долгов» продолжается только существительными, которые «без»
# может отрицать; первое иное слово («торг», «срочно») обрывает его. Перед каждым звеном
# вставляется своё «без», поэтому отрицание не переходит в другое предложение.
_NEGATABLE = (
    r"(?:залог|арест|обременени|долг|мебел|техник|ипотек|холодильник|прописан|лифт|парковк"
    r"|кладов|перепланировк)\w*"
)
_SEP = r"(?:\s*,\s*|\s+и\s+|\s+или\s+)"
_BEZ_LIST = re.compile(rf"\bбез\s+{_NEGATABLE}(?:{_SEP}{_NEGATABLE})+")
_LIST_ITEM = re.compile(rf"({_SEP})({_NEGATABLE})")
_NEG_EITHER = {"не", "нет", "нету", "нельзя", "отсутствует", "отсутствуют"}
_KUFAR_CUT = 148  # Kufar режет описание ровно на 150 символах; нижняя граница — запас на пробелы


def _negated(clause: str, start: int, end: int) -> bool:
    before = _WORD.findall(clause[:start])
    after = _WORD.findall(clause[end:])
    return (
        "без" in before[-2:]
        or bool(_NEG_EITHER & set(before[-3:]))
        or bool(_NEG_EITHER & set(after[:3]))
    )


def extract_facts(text: str | None) -> list[str]:
    """Коды фактов из описания в порядке FACT_LABELS, без повторов."""
    if not isinstance(text, str) or not text.strip():
        return []
    # shortcut: длинный текст, оборванный на букве, считаем обрезанным (Kufar) и режем последнее
    # слово; полный текст из 148-150 символов потеряет одно слово, пока не передаём признак источника
    if _KUFAR_CUT <= len(text) <= 150 and text[-1].isalnum():
        text = text.rsplit(None, 1)[0] if len(text.split()) > 1 else ""
    text = text.lower().replace("ё", "е")
    text = _BEZ_LIST.sub(lambda m: _LIST_ITEM.sub(r"\1без \2", m.group()), text)
    clauses = [" ".join(c.split()) for c in _CLAUSE_SPLIT.split(text)]
    found: list[str] = []
    for code, (patterns, check_negation) in _COMPILED.items():
        if code == "mortgage" and "no_mortgage" in found:
            continue
        for clause in clauses:
            if any(
                not (check_negation and _negated(clause, m.start(), m.end()))
                for p in patterns
                for m in p.finditer(clause)
            ):
                found.append(code)
                break
    return found


def facts_line(codes: Iterable[str], limit: int | None = 5) -> str | None:
    """«⚠️ продаётся доля · ⚠️ прописаны люди\\n📝 Из описания: торг, с мебелью»; тревожные первыми."""
    wanted = set(codes)
    ordered = [c for c in FACT_LABELS if c in wanted]
    ordered = [c for c in ordered if c in WARNING_FACTS] + [
        c for c in ordered if c not in WARNING_FACTS
    ]
    if limit is not None:
        ordered = ordered[:limit]
    warnings = [f"⚠️ {FACT_LABELS[c]}" for c in ordered if c in WARNING_FACTS]
    others = [FACT_LABELS[c] for c in ordered if c not in WARNING_FACTS]
    lines = []
    if warnings:
        lines.append(" · ".join(warnings))
    if others:
        lines.append("📝 Из описания: " + ", ".join(others))
    return "\n".join(lines) or None
