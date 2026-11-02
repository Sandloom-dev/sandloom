#!/usr/bin/env python3
"""Render versions of a docs page side by side, as one local HTML page.

Two layouts:

- **pages** (default): one column per version, the whole page in each.
- **blocks** (``--blocks``): one row per block, the versions of that block side
  by side. A block is the text before the first ``##`` heading, then each ``##``
  section; versions are matched by position, so write each variant with the same
  block order.

``--blind`` labels the versions A, B, C … in a shuffled order and writes the key
beside the page (``<out>.key.txt``), so the owner or a judge compares them
without knowing which is which.

Usage::

    uv run python tools/docs/docs_compare.py OUT.html LABEL=PATH LABEL=PATH ...
    uv run python tools/docs/docs_compare.py OUT.html --blocks --blind a=v1.md b=v2.md
    uv run python tools/docs/docs_compare.py --self-test

Open OUT.html in a browser, or serve its folder with ``python3 -m http.server``.
Needs ``markdown`` and ``pymdown-extensions`` (in the dev environment).
"""

# ruff: noqa: E501 - the inline stylesheet in PAGE keeps one rule per line

from __future__ import annotations

import argparse
import html
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import markdown  # type: ignore[import-untyped]

EXTENSIONS = [
    "admonition",
    "attr_list",
    "def_list",
    "md_in_html",
    "tables",
    "pymdownx.details",
    "pymdownx.superfences",
    "pymdownx.tabbed",
    "pymdownx.inlinehilite",
]
EXTENSION_CONFIGS = {"pymdownx.tabbed": {"alternate_style": True}}

_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.S)
_NOISE = re.compile(r"\s*<!-- prose-ok:.*?-->|:material-[\w-]+:")
_FENCED = re.compile(r"```.*?```", re.S)


@dataclass
class Version:
    label: str
    path: Path
    text: str

    @classmethod
    def load(cls, spec: str) -> Version:
        label, sep, path = spec.partition("=")
        if not sep:
            label, path = Path(spec).stem, spec
        raw = Path(path).read_text(encoding="utf-8")
        return cls(label, Path(path), _NOISE.sub("", _FRONT_MATTER.sub("", raw)))


def prose_words(text: str) -> int:
    return len(_FENCED.sub("", text).split())


def blocks(text: str) -> list[str]:
    """The text before the first ``##`` heading, then each ``##`` section."""
    parts = re.split(r"(?m)^(?=## )", text)
    return [p.strip() for p in parts if p.strip()]


def render(text: str) -> str:
    html_text: str = markdown.markdown(
        text, extensions=EXTENSIONS, extension_configs=EXTENSION_CONFIGS
    )
    return html_text


def _cell(title: str, note: str, text: str) -> str:
    return (
        f"<section><header><h2>{html.escape(title)}</h2>"
        f"<p>{html.escape(note)}</p></header>"
        f'<div class="page">{render(text)}</div></section>'
    )


def page_layout(versions: list[Version]) -> str:
    cells = [_cell(v.label, f"~{prose_words(v.text)} words", v.text) for v in versions]
    return f'<main style="--cols:{len(versions)}">{"".join(cells)}</main>'


def block_layout(versions: list[Version]) -> str:
    split = [blocks(v.text) for v in versions]
    rows = []
    for i in range(max(len(b) for b in split)):
        cells = []
        for v, parts in zip(versions, split, strict=True):
            text = parts[i] if i < len(parts) else "*(no block)*"
            note = f"block {i + 1}, ~{prose_words(text)} words"
            cells.append(_cell(v.label, note, text))
        rows.append(f'<main style="--cols:{len(versions)}">{"".join(cells)}</main>')
    return "<hr>".join(rows)


def blind(versions: list[Version], seed: int | None) -> tuple[list[Version], str]:
    order = versions[:]
    random.Random(seed).shuffle(order)  # noqa: S311 - shuffles labels, not secrets
    key = []
    for letter, v in zip("ABCDEFGHIJ", order, strict=False):
        key.append(f"{letter} = {v.label} ({v.path})")
        v.label = letter
    return order, "\n".join(key) + "\n"


def build(
    out: Path, specs: list[str], *, by_block: bool, hide: bool, seed: int | None
) -> None:
    versions = [Version.load(s) for s in specs]
    if hide:
        versions, key = blind(versions, seed)
        out.with_suffix(".key.txt").write_text(key, encoding="utf-8")
    body = block_layout(versions) if by_block else page_layout(versions)
    out.write_text(PAGE.replace("{{BODY}}", body), encoding="utf-8")


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Docs versions compared</title>
<style>
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a64;--line:#e4e1d8;--card:#fff;--tip:#e9f6ee;--tipline:#2f9e5b;--code:#f3f1ea}
@media (prefers-color-scheme: dark){:root{--bg:#161614;--fg:#ecebe6;--muted:#a3a29b;--line:#34332f;--card:#1f1e1b;--tip:#16301f;--tipline:#3fbf73;--code:#26251f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{display:grid;grid-template-columns:repeat(var(--cols),minmax(0,1fr));gap:16px;padding:16px}
@media (max-width:1100px){main{grid-template-columns:1fr}}
hr{border:0;border-top:2px dashed var(--line);margin:8px 16px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:0 20px 20px;min-width:0}
header{position:sticky;top:0;background:var(--card);border-bottom:1px solid var(--line);padding:12px 0 8px;margin-bottom:8px}
header h2{margin:0;font-size:16px}header p{margin:2px 0 0;color:var(--muted);font-size:13px}
.page h1{font-size:24px;margin:14px 0 8px}.page h2{font-size:18px;margin:22px 0 6px}
pre{background:var(--code);padding:10px;border-radius:6px;overflow-x:auto;white-space:pre-wrap;font-size:12.5px}
code{background:var(--code);padding:1px 4px;border-radius:4px;font-size:.9em}pre code{background:none;padding:0}
.admonition{background:var(--tip);border-left:4px solid var(--tipline);border-radius:6px;padding:4px 14px 10px;margin:14px 0}
.admonition-title{font-weight:600;margin:8px 0}
.tabbed-set{display:flex;flex-wrap:wrap;margin:8px 0}.tabbed-set>input{display:none}
.tabbed-labels{display:flex;gap:4px;width:100%;border-bottom:1px solid var(--line)}
.tabbed-labels label{padding:6px 10px;cursor:pointer;color:var(--muted);font-size:13px;border-bottom:2px solid transparent}
.tabbed-content{width:100%}.tabbed-block{display:none}
.tabbed-set>input:nth-child(1):checked~.tabbed-labels>:nth-child(1),.tabbed-set>input:nth-child(2):checked~.tabbed-labels>:nth-child(2){color:var(--fg);border-bottom-color:var(--tipline)}
.tabbed-set>input:nth-child(1):checked~.tabbed-content>:nth-child(1),.tabbed-set>input:nth-child(2):checked~.tabbed-content>:nth-child(2){display:block}
.grid.cards ul{list-style:none;padding:0;display:grid;grid-template-columns:1fr 1fr;gap:8px}
.grid.cards li{border:1px solid var(--line);border-radius:8px;padding:8px 10px}.grid.cards li p{margin:4px 0}
a{color:inherit;text-decoration-color:var(--tipline)}
</style></head><body>
{{BODY}}
</body></html>"""


def self_test() -> int:
    text = "---\ntitle: x\n---\n# T\n\nIntro.\n\n## One\n\nA b.\n\n## Two\n\nC.\n"
    v = Version("v", Path("v.md"), _NOISE.sub("", _FRONT_MATTER.sub("", text)))
    order, key = blind([Version("a", Path("a"), ""), Version("b", Path("b"), "")], 1)
    checks = {
        "front matter dropped": "title:" not in v.text,
        "three blocks": len(blocks(v.text)) == 3,
        "fences not counted": prose_words("A b.\n```py\nx y z\n```\n") == 2,
        "renders": "<h2>One</h2>" in render(blocks(v.text)[1]),
        "blind letters": sorted(x.label for x in order) == ["A", "B"],
        "blind key": "a (a)" in key and "b (b)" in key,
    }
    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"self-test FAIL: {name}")
    print("self-test: FAIL" if failed else "self-test: ok")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if argv == ["--self-test"]:
        return self_test()
    parser = argparse.ArgumentParser(description="Compare docs page versions.")
    parser.add_argument("out", type=Path)
    parser.add_argument("versions", nargs="+", help="LABEL=PATH, or PATH")
    parser.add_argument("--blocks", action="store_true", help="one row per block")
    parser.add_argument("--blind", action="store_true", help="hide labels, write a key")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)
    build(
        args.out, args.versions, by_block=args.blocks, hide=args.blind, seed=args.seed
    )
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
