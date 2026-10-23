"""Защита персональных данных: всё, что пишется в БД или фикстуры, проходит через этот модуль."""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

# Ключи, которые вырезаются из любых ответов площадок (сравнение без учёта регистра).
PII_KEYS = frozenset(
    {
        "contactphones",
        "contactname",
        "contactemail",
        "phone",
        "phones",
        "email",
        "name",
        "contact_person",
        # Реквизиты продавца на Kufar: у ИП это данные конкретного человека.
        "vat_number",
        "company_number",
        "company_address",
        "account_id",
        "useruuid",
        "message_id",
    }
)

PHONE_RE = re.compile(r"(?:\+?375|\b80)[\s\-()]*\d(?:[\s\-()]*\d){8}")
PHONE_PLACEHOLDER = "+375000000000"


def scrub(value: Any) -> Any:
    """Рекурсивно удалить ключи с персональными данными и замаскировать телефоны в тексте."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in PII_KEYS:
                continue
            # Kufar кладёт параметры списком {"p": "name", "v": "..."}.
            if isinstance(item, dict) and str(item.get("p", "")).lower() in PII_KEYS:
                continue
            cleaned[key] = scrub(item)
        return cleaned
    if isinstance(value, list):
        return [
            scrub(item)
            for item in value
            if not (isinstance(item, dict) and str(item.get("p", "")).lower() in PII_KEYS)
        ]
    if isinstance(value, str):
        return PHONE_RE.sub(PHONE_PLACEHOLDER, value)
    return value


def seller_key(source: str, account_id: str | int | None, salt: str | None = None) -> str | None:
    """Псевдоним продавца: по нему видно «тот же продавец», но нельзя восстановить id аккаунта."""
    if account_id in (None, ""):
        return None
    salt = salt if salt is not None else os.environ.get("SELLER_KEY_SALT")
    if not salt:
        # Без соли id аккаунтов Kufar (последовательные числа) восстанавливаются перебором.
        # Соль задаётся один раз в секретах и не меняется: смена соли рвёт связь «тот же продавец».
        raise RuntimeError("SELLER_KEY_SALT не задан")
    digest = hashlib.sha256(f"{salt}:{source}:{account_id}".encode()).hexdigest()
    return digest[:32]
