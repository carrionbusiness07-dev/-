"""Собирает мастер-файлы логотипа «Хроника» в docs/brand/logo/final/.

Символ — дом, у которого правый скат крыши спускается ступенями, как цена в истории квартиры.
Название переведено в кривые из PT Sans Narrow 700 (OFL), поэтому шрифт в файлах не нужен.

Запуск (нужен fontTools + brotli для woff2):  python docs/brand/logo/build_logo.py
"""
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
FONT = ROOT / "src/minsk_realty/web/static/fonts/pt-sans-narrow-700.woff2"
OUT = HERE / "final"

SPOT = "#1c3dbf"  # «печатный синий» из DESIGN.md
INK = "#15171b"
PAPER = "#eceef0"

# 45° левый скат, справа 3 ступени (24 вбок, 30 вниз), дверь — вырез снизу. Сдвинуто на 3 вверх: оптический центр.
SYMBOL = "M47 125 L137 35 V65 H161 V95 H185 V125 H209 V215 H153 V159 H121 V215 H65 V125 Z"
# Для 16–32 px: две ступени крупнее и дверь шире, чтобы не слипались.
SYMBOL_SMALL = "M47 125 L137 35 V80 H173 V125 H209 V215 H156 V151 H118 V215 H65 V125 Z"
SYMBOL_BOX = (47, 35, 209, 215)  # x0, y0, x1, y1

WORD = "Хроника"
TRACKING = -8  # единиц шрифта на букву: узкому гротеску в крупном кегле нужно чуть плотнее


def word_path(size: float, x: float, baseline: float) -> tuple[str, float]:
    """Контур слова в кривых и его ширина в px."""
    font = TTFont(FONT)
    glyphs = font.getGlyphSet()
    cmap = font.getBestCmap()
    scale = size / font["head"].unitsPerEm
    pen = SVGPathPen(glyphs, ntos=lambda v: f"{v:.2f}".rstrip("0").rstrip("."))
    advance = 0.0
    for i, ch in enumerate(WORD):
        name = cmap[ord(ch)]
        glyphs[name].draw(TransformPen(pen, (scale, 0, 0, -scale, x + advance * scale, baseline)))
        advance += glyphs[name].width + (TRACKING if i < len(WORD) - 1 else 0)
    return pen.getCommands(), advance * scale


def svg(view_w: float, view_h: float, body: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {view_w:g} {view_h:g}" '
        f'width="{view_w:g}" height="{view_h:g}">\n  <title>{title}</title>\n{body}</svg>\n'
    )


def write(name: str, content: str) -> None:
    (OUT / name).write_text(content, encoding="utf-8")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    x0, y0, x1, y1 = SYMBOL_BOX
    sym_w = x1 - x0

    write("symbol.svg", svg(256, 256, f'  <path fill="{SPOT}" d="{SYMBOL}"/>\n', "Хроника — символ"))
    write("symbol-black.svg", svg(256, 256, f'  <path fill="#000" d="{SYMBOL}"/>\n', "Хроника — символ"))
    write("symbol-small.svg", svg(256, 256, f'  <path fill="#000" d="{SYMBOL_SMALL}"/>\n', "Хроника — символ, мелкий размер"))
    write(
        "avatar.svg",
        svg(
            512,
            512,
            f'  <circle cx="256" cy="256" r="256" fill="{SPOT}"/>\n'
            # символ ≈ 60% диаметра: Telegram обрезает аватарку по кругу, углы не должны уйти за край
            f'  <path fill="#fff" transform="translate(256 256) scale(1.75) translate(-128 -125)" d="{SYMBOL}"/>\n',
            "Хроника — аватар Telegram",
        ),
    )

    # Горизонтальный локап: высота строчных ≈ половина символа, середина строчных на середине символа.
    size = 132
    gap = 0.38 * sym_w
    baseline = (y0 + y1) / 2 + size * 0.5 / 2
    word_x = x1 + gap
    word_d, word_w = word_path(size, word_x, baseline)
    width = word_x + word_w + x0
    for name, color in (("horizontal.svg", SPOT), ("horizontal-black.svg", "#000"), ("horizontal-ink.svg", INK)):
        body = f'  <path fill="{color}" d="{SYMBOL}"/>\n  <path fill="{color}" d="{word_d}"/>\n'
        write(name, svg(round(width), 256, body, "Хроника — горизонтальный логотип"))
    body = f'  <path fill="#fff" d="{SYMBOL}"/>\n  <path fill="#fff" d="{word_d}"/>\n'
    write("horizontal-white.svg", svg(round(width), 256, body, "Хроника — горизонтальный логотип, инверсный"))

    # Вертикальный локап: слово под символом, по центру.
    size_v = 104
    word_d_v, word_w_v = word_path(size_v, 0, 0)
    view_w = max(word_w_v, 256) + 2 * 36
    sym_dx = (view_w - 256) / 2
    word_dx = (view_w - word_w_v) / 2
    word_base = y1 + 0.32 * sym_w + size_v * 0.7
    view_h = word_base + y0 + 8
    for name, color in (("stacked.svg", SPOT), ("stacked-black.svg", "#000")):
        body = (
            f'  <path fill="{color}" transform="translate({sym_dx:g} 0)" d="{SYMBOL}"/>\n'
            f'  <path fill="{color}" transform="translate({word_dx:.2f} {word_base:.2f})" d="{word_d_v}"/>\n'
        )
        write(name, svg(round(view_w), round(view_h), body, "Хроника — вертикальный логотип"))

    word_d_only, word_w_only = word_path(132, 24, 132)
    write(
        "wordmark.svg",
        svg(round(word_w_only + 48), 172, f'  <path fill="{SPOT}" d="{word_d_only}"/>\n', "Хроника — название"),
    )


if __name__ == "__main__":
    main()
