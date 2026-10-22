import io
import random

import pytest
from PIL import Image, ImageDraw

from minsk_realty.matching.photos import (
    count_photo_matches,
    hamming,
    is_informative,
    phash_from_bytes,
)


def picture(seed: int, size=(640, 480)) -> Image.Image:
    rnd = random.Random(seed)
    img = Image.new("RGB", size, (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    draw = ImageDraw.Draw(img)
    for _ in range(25):
        x0, y0 = rnd.randrange(size[0]), rnd.randrange(size[1])
        x1, y1 = x0 + rnd.randrange(40, 300), y0 + rnd.randrange(40, 300)
        draw.rectangle((x0, y0, x1, y1), fill=tuple(rnd.randrange(256) for _ in range(3)))
    return img


def to_bytes(img: Image.Image, fmt="JPEG", **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def test_phash_survives_recompression_and_resize():
    original = picture(1)
    a = phash_from_bytes(to_bytes(original, quality=95))
    # Другая площадка: уменьшила, пережала и поставила водяной знак.
    other = original.resize((400, 300))
    ImageDraw.Draw(other).text((10, 10), "kufar.by", fill=(255, 255, 255))
    b = phash_from_bytes(to_bytes(other, quality=60))
    assert len(a) == 16 and is_informative(a)
    assert hamming(a, b) <= 8


def test_different_photos_are_far_apart():
    hashes = [phash_from_bytes(to_bytes(picture(seed))) for seed in range(2, 6)]
    assert all(hamming(x, y) > 8 for i, x in enumerate(hashes) for y in hashes[i + 1 :])


def test_bad_bytes_raise_value_error():
    with pytest.raises(ValueError):
        phash_from_bytes(b"not an image")


def test_uniform_placeholder_is_not_informative():
    placeholder = phash_from_bytes(to_bytes(Image.new("RGB", (300, 200), (200, 200, 200))))
    assert not is_informative(placeholder)
    # Две заглушки «нет фото» не должны считаться совпавшими фото.
    assert count_photo_matches([placeholder], [placeholder], 8) == 0


def test_matches_are_one_to_one():
    h = phash_from_bytes(to_bytes(picture(7)))
    near = f"{int(h, 16) ^ 0b111:016x}"  # 3 бита отличаются
    # Одно фото фасада против трёх почти одинаковых снимков — одно совпадение, не три.
    assert count_photo_matches([h], [near, h, near], 8) == 1
    assert count_photo_matches([h, near], [h, near], 8) == 2
