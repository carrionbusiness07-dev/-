"""Перцептивные хеши фото (уровень 2 склейки).

Фото не храним: `phash_from_bytes` получает байты, считает хеш и ничего не пишет на диск.
В БД (`photo_hashes`) попадает только 16-значный hex. Скачивание — забота сборщика (Стас).
"""

from __future__ import annotations

import io
import warnings
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from minsk_realty.db.models import PhotoHash

# Фото объявлений — до нескольких мегапикселей. Больше — подозрительно (decompression bomb).
MAX_PIXELS = 40_000_000

# У осмысленного фото phash (порог по медиане DCT) содержит около половины единиц.
# Однотонная заглушка «нет фото» даёт почти пустой хеш, и такие заглушки «совпадали» бы
# у любых объявлений. Хеши с числом единиц вне этого коридора не учитываем.
_MIN_BITS, _MAX_BITS = 8, 56


def phash_from_bytes(data: bytes) -> str:
    """Посчитать phash (imagehash, 64 бита) по байтам изображения. Возвращает 16 hex-символов.

    Бросает ValueError, если байты не изображение или изображение слишком большое.
    """
    import imagehash
    from PIL import Image, UnidentifiedImageError

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as img:
                if img.width * img.height > MAX_PIXELS:
                    raise ValueError("изображение слишком большое")
                img.draft("L", (256, 256))  # JPEG: декодировать сразу в уменьшенном виде
                return str(imagehash.phash(img.convert("L")))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombWarning) as exc:
        raise ValueError(f"не удалось прочитать изображение: {type(exc).__name__}") from exc


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def is_informative(phash: str) -> bool:
    try:
        bits = int(phash, 16).bit_count()
    except ValueError:
        return False
    return len(phash) == 16 and _MIN_BITS <= bits <= _MAX_BITS


def count_photo_matches(a: Iterable[str], b: Iterable[str], max_distance: int) -> int:
    """Сколько фото совпало, если каждое фото участвует не больше чем в одной паре.

    Без этого одно фото фасада, похожее на три снимка другого объявления, дало бы
    «три совпадения». Пары берутся жадно от самых близких.
    """
    left = sorted({h for h in a if is_informative(h)})
    right = sorted({h for h in b if is_informative(h)})
    pairs = sorted(
        (hamming(x, y), i, j)
        for i, x in enumerate(left)
        for j, y in enumerate(right)
        if hamming(x, y) <= max_distance
    )
    used_left: set[int] = set()
    used_right: set[int] = set()
    for _, i, j in pairs:
        if i not in used_left and j not in used_right:
            used_left.add(i)
            used_right.add(j)
    return len(used_left)


def save_photo_hashes(session: Session, listing_id: int, hashes: Iterable[str]) -> int:
    """Записать хеши фото объявления (без дублей). Возвращает число новых строк.

    Для сборщика: скачал фото → `phash_from_bytes` → байты выбросил → сюда.
    """
    existing = set(
        session.scalars(select(PhotoHash.phash).where(PhotoHash.listing_id == listing_id))
    )
    added = 0
    for phash in hashes:
        phash = phash.lower()
        if phash in existing or not is_informative(phash):
            continue
        session.add(PhotoHash(listing_id=listing_id, phash=phash))
        existing.add(phash)
        added += 1
    session.flush()
    return added
