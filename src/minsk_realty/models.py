"""Модели данных, которые адаптеры отдают дальше по конвейеру."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

Source = Literal["kufar", "realt", "onliner", "domovita"]
SellerType = Literal["owner", "agency", "unknown"]
Currency = Literal["USD", "BYN", "EUR", "RUB"]


class RawListing(BaseModel):
    """Объявление в том виде, как его отдала площадка (уже без персональных данных)."""

    source: Source
    external_id: str
    payload: dict[str, Any]


class ListingSnapshot(BaseModel):
    """Нормализованный замер объявления на момент `seen_at`.

    Телефонов, имён и e-mail здесь нет и быть не должно. Продавец представлен
    только типом, названием агентства (юрлицо) и псевдонимизированным ключом.
    """

    source: Source
    external_id: str
    url: str
    seen_at: datetime

    # Цена как в объявлении: изменения цены считаются именно в ней.
    price_amount: Decimal | None = None
    price_currency: Currency | None = None
    # Пересчёт площадки в USD — только для показа и аналитики.
    price_usd: Decimal | None = None

    seller_type: SellerType = "unknown"
    agency_name: str | None = None
    # sha256(соль + площадка + id аккаунта): нужен, чтобы понять «перевыложил тот же продавец».
    seller_key: str | None = None

    rooms: int | None = None
    area_total: float | None = None
    floor: int | None = None
    floors_total: int | None = None
    street: str | None = None
    house: str | None = None
    address_raw: str | None = None
    district: str | None = None
    town: str | None = None
    lat: float | None = None
    lon: float | None = None

    posted_at: datetime | None = None
    updated_at: datetime | None = None

    # Ссылки на фото нужны только для подсчёта phash; сами фото не храним.
    photo_urls: list[str] = Field(default_factory=list)
    # Параметры объявления без персональных данных — чтобы разбирать новые поля позже.
    extra: dict[str, Any] = Field(default_factory=dict)
