"""Regenerate approved Sandloom vector assets (maintenance only).

Needs fonttools 4.68.0 and the Manrope TTF recorded in assets/social/SOURCE.json.
Raster/ZIP exports use render_brand_assets.cjs; normal site builds copy assets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from html import escape
from pathlib import Path
from xml.etree import ElementTree

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

REPO = Path(__file__).resolve().parents[2]
ASSETS = REPO / "website/assets"
BRAND = ASSETS / "brand"
COLORS = {"ink": "#262825", "sand": "#d8b892", "white": "#ffffff"}


def mark_paths(path: Path) -> str:
    root = ElementTree.parse(path).getroot()  # noqa: S314 - trusted repository SVG
    output = []
    for node in root:
        if node.tag.rsplit("}", 1)[-1] != "path":
            raise ValueError(f"Canonical mark must contain only paths: {path}")
        attributes = " ".join(
            f'{key}="{escape(value, quote=True)}"' for key, value in node.attrib.items()
        )
        output.append(f"<path {attributes}/>")
    return "".join(output)


def svg(
    body: str,
    *,
    width: float = 80,
    height: int = 80,
    color: str = "currentColor",
    label: str = "Sandloom",
) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:g} {height}" '
        f'fill="{color}" role="img" aria-label="{label}">{body}</svg>\n'
    )


def generate(font_path: Path) -> None:
    provenance = json.loads((ASSETS / "social/SOURCE.json").read_text())
    if hashlib.sha256(font_path.read_bytes()).hexdigest() != provenance["sha256"]:
        raise ValueError("Use the Manrope TTF recorded in assets/social/SOURCE.json")
    BRAND.mkdir(exist_ok=True)
    master = mark_paths(REPO / "docs/assets/sandloom-mark.svg")
    micro = mark_paths(REPO / "docs/assets/sandloom-mark-micro.svg")
    font = instantiateVariableFont(TTFont(font_path), {"wght": 600})
    glyphs = font.getGlyphSet()
    cmap = font.getBestCmap()
    units = font["head"].unitsPerEm

    def outline(
        value: str, *, tracking: float = 0, kern: bool = False
    ) -> tuple[str, float]:
        pairs = {"sa": -6, "an": -8, "nd": -8, "dl": -18, "lo": 3, "oo": -14, "om": -8}
        cursor = 0.0
        output = []
        for index, character in enumerate(value):
            glyph = glyphs[cmap[ord(character)]]
            pen = SVGPathPen(glyphs)
            glyph.draw(pen)
            output.append(
                f'<path transform="translate({cursor:g})" d="{pen.getCommands()}"/>'
            )
            cursor += glyph.width + tracking
            if kern and index + 1 < len(value):
                cursor += pairs.get(value[index : index + 2], 0)
        return "".join(output), cursor

    letters, advance = outline("sandloom", tracking=-16, kern=True)
    word_width = round(advance * 0.033 + 4, 2)
    lockup = f'{master}<g transform="translate(87 64) scale(.033 -.033)">{letters}</g>'
    word = f'<g transform="translate(0 64) scale(.033 -.033)">{letters}</g>'
    (BRAND / "sandloom-mark.svg").write_text(svg(master))
    (BRAND / "sandloom-mark-micro.svg").write_text(svg(micro, width=16, height=16))
    (BRAND / "sandloom-lockup.svg").write_text(svg(lockup, width=87 + word_width))
    (BRAND / "sandloom-wordmark.svg").write_text(svg(word, width=word_width))
    for name, color in COLORS.items():
        (BRAND / f"sandloom-mark-{name}.svg").write_text(svg(master, color=color))
        (BRAND / f"sandloom-lockup-{name}.svg").write_text(
            svg(lockup, width=87 + word_width, color=color)
        )
    (BRAND / "sandloom-avatar.svg").write_text(
        svg(
            '<rect width="96" height="96" rx="20" fill="#111213"/>'
            f'<g transform="translate(8 8)" fill="#d8b892">{master}</g>',
            width=96,
            height=96,
        )
    )
    (ASSETS / "favicon.svg").write_text(
        svg(
            "<style>@media(prefers-color-scheme:dark){:root{color:#d8b892}}</style>"
            + micro,
            width=16,
            height=16,
        )
    )
    (REPO / "website/theme/partials/brand-mark.html").write_text(master + "\n")
    # Inline in the shared header so its semantic colour follows the site theme.
    (REPO / "website/theme/partials/site-logo.html").write_text(
        f'<svg class="sl-brand-logo" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {87 + word_width:g} 80" width="144" height="30" '
        f'fill="currentColor" aria-hidden="true">{lockup}</svg>\n'
    )
    for name in ("LICENSE", "NOTICE"):
        shutil.copyfile(REPO / name, BRAND / name)
    shutil.copyfile(ASSETS / "social/Manrope-OFL.txt", BRAND / "Manrope-OFL.txt")

    def text(
        value: str, x: int, y: int, size: int, color: str, spacing: float = 0
    ) -> str:
        paths, _ = outline(value, tracking=spacing * units / size)
        return (
            f'<g aria-label="{escape(value, quote=True)}" fill="{color}" '
            f'transform="translate({x} {y}) scale({size / units:g} {-size / units:g})">'
            + paths
            + "</g>"
        )

    cards = {
        "home": (
            "PYTHON BACKEND FRAMEWORK",
            "Build with AI.",
            "Keep control.",
            "Enforced architecture. Pluggable infrastructure.",
            "One codebase. Monolith or services.",
        ),
        "docs": (
            "DOCUMENTATION",
            "From your first module",
            "to services.",
            "Guides, concepts and the public SDK.",
            "Build with people and AI agents.",
        ),
        "examples": (
            "EXAMPLE APPLICATIONS",
            "Real apps.",
            "Clear starting points.",
            "Linkly. Helpdesk. Academy. Projecthub.",
            "Explore what isolated modules can build.",
        ),
    }
    for name, (label, first, second, detail, last) in cards.items():
        artwork = f'''<svg xmlns="http://www.w3.org/2000/svg"
 width="1200" height="630" viewBox="0 0 1200 630" role="img"
 aria-label="{escape(first + " " + second, quote=True)}">
<rect width="1200" height="630" fill="#111213"/>
<path d="M780 0v630M0 567h1200" stroke="#303234"/>
<g transform="translate(844 170) scale(3.2)" fill="#d8b892">{master}</g>
<g transform="translate(72 62) scale(.56)" fill="#edeee9">{lockup}</g>
{text(label, 80, 195, 12, "#b8a082", 1.5)}
{text(first, 76, 282, 53 if name == "docs" else 60, "#edeee9")}
{text(second, 76, 358, 54 if name == "examples" else 58, "#d8b892")}
{text(detail, 80, 426, 19, "#a5aaa8")}
{text(last, 80, 458, 19, "#a5aaa8")}
{text("PYTHON / AI-DRIVEN DEVELOPMENT", 80, 535, 11, "#7e8582", 1)}
{text("ONE CODEBASE. ROOM TO GROW.", 80, 599, 11, "#a5aaa8", 1)}
</svg>\n'''
        (ASSETS / f"social/sandloom-{name}.svg").write_text(artwork)
    print("Prepared approved caracal vectors, shared header and sharing cards.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, required=True)
    generate(parser.parse_args().font)
