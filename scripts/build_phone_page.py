"""Собирает мобильную страницу конспекта из Markdown-файлов репозитория.

Источник правды — Markdown: konspekt/00-shpargalka.md и план в docs/plans/.
После их правки пересоберите страницу и опубликуйте phone/konspekt.html заново:

    pip install markdown
    python3 scripts/build_phone_page.py
"""
import html
import re
from pathlib import Path

import markdown
from markdown.extensions.toc import slugify_unicode

ROOT = Path(__file__).resolve().parent.parent
TRAINER_URL = "https://claude.ai/artifact/BoVfoPJeUNviULzAZ3n6vd"
SECTIONS = [
    ("shpargalka", "Шпаргалка", ROOT / "konspekt" / "00-shpargalka.md"),
    ("plan", "План", ROOT / "docs" / "plans" / "2026-10-22-coinglass-study-plan.md"),
]
OUT = ROOT / "phone" / "konspekt.html"

# links between the Markdown files become tab switches on the page
LINK_TARGETS = {
    "00-shpargalka.md": "#shpargalka",
    "2026-10-22-coinglass-study-plan.md": "#plan",
}


def render(md_text, prefix):
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
        extension_configs={"toc": {"slugify": lambda v, s: prefix + "-" + slugify_unicode(v, s)}},
    )
    body = md.convert(md_text)
    for name, target in LINK_TARGETS.items():
        body = re.sub(r'href="[^"]*' + re.escape(name) + '"', f'href="{target}"', body)
    body = body.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")
    toc = [t for t in md.toc_tokens[0]["children"]] if md.toc_tokens else []
    return body, toc


def toc_html(tokens):
    items = "".join(
        f'<li><a href="#{t["id"]}" data-jump="{t["id"]}">{html.escape(t["name"])}</a></li>' for t in tokens
    )
    return f'<details class="toc"><summary>Содержание</summary><ol>{items}</ol></details>' if items else ""


def main():
    template = (ROOT / "scripts" / "phone_template.html").read_text(encoding="utf-8")
    tabs, panes = [], []
    for sid, title, path in SECTIONS:
        body, toc = render(path.read_text(encoding="utf-8"), sid)
        tabs.append(f'<button type="button" class="tab" data-tab="{sid}">{title}</button>')
        panes.append(f'<section class="pane" id="{sid}" hidden>{toc_html(toc)}<article class="md">{body}</article></section>')
    tabs.append(f'<a class="tab ext" href="{TRAINER_URL}" target="_blank" rel="noopener">Тренажёр ↗</a>')
    page = template.replace("{{TABS}}", "\n".join(tabs)).replace("{{PANES}}", "\n".join(panes))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    print(f"written {OUT.relative_to(ROOT)} ({len(page) // 1024} KB)")


if __name__ == "__main__":
    main()
