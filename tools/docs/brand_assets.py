"""Regenerate outlined brand SVGs; not part of a normal website build.

Requires fonttools and an upstream Manrope variable TTF (see assets/social/README.md).
PNG/ICO rendering is a separate maintenance step so builds need no graphics stack.
"""
from __future__ import annotations

import argparse
from html import escape
from pathlib import Path
from xml.etree import ElementTree

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

REPO = Path(__file__).resolve().parents[2]
ASSETS = REPO / "website/assets"


def generate(font_path: Path) -> None:
    font = instantiateVariableFont(TTFont(font_path), {"wght": 500})
    glyphs = font.getGlyphSet()
    cmap = font.getBestCmap()
    units = font["head"].unitsPerEm
    mark = ElementTree.parse(REPO / "docs/assets/sandloom-mark.svg").getroot()
    paths = "".join(f'<path d="{node.attrib["d"]}"/>' for node in mark)

    def text(value: str, x: int, y: int, size: int, color: str, spacing: float = 0) -> str:
        # Outline the glyphs so sharing crawlers never need a web font.
        scale = size / units
        pen_x = 0.0
        output = []
        for character in value:
            glyph = glyphs[cmap[ord(character)]]
            pen = SVGPathPen(glyphs)
            glyph.draw(pen)
            output.append(f'<path transform="translate({pen_x:g})" d="{pen.getCommands()}"/>')
            pen_x += glyph.width + spacing / scale
        return (
            f'<g aria-label="{escape(value, quote=True)}" fill="{color}" '
            f'transform="translate({x} {y}) scale({scale:g} {-scale:g})">'
            + "".join(output) + "</g>"
        )

    icon = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80">'
        '<rect width="80" height="80" rx="18" fill="#111213"/>'
        f'<g transform="translate(7 7) scale(.825)" fill="#d8b892">{paths}</g></svg>\n'
    )
    (ASSETS / "favicon.svg").write_text(icon)
    social = ASSETS / "social"
    social.mkdir(exist_ok=True)
    cards = {
        "home": ("AI-FIRST PYTHON FRAMEWORK", "Build SaaS with AI.", "Stay fast as you grow.",
                 "Isolated modules. Built-in SaaS.", "Monolith or services. One codebase."),
        "docs": ("DOCUMENTATION", "From your first module", "to services.",
                 "Guides, concepts and the public SDK.", "Build with people and AI agents."),
        "examples": ("EXAMPLE APPLICATIONS", "Real apps.", "Clear starting points.",
                     "Linkly. Helpdesk. Academy. Projecthub.", "Explore what isolated modules can build."),
    }
    for name, (label, first, second, detail, last) in cards.items():
        artwork = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630" role="img" aria-label="{escape(first + ' ' + second, quote=True)}">
<defs>
  <radialGradient id="glow"><stop stop-color="#d8b892" stop-opacity=".13"/><stop offset="1" stop-color="#111213" stop-opacity="0"/></radialGradient>
  <linearGradient id="top" x1="110" y1="60" x2="440" y2="205" gradientUnits="userSpaceOnUse"><stop stop-color="#35393d"/><stop offset="1" stop-color="#202326"/></linearGradient>
  <linearGradient id="side"><stop stop-color="#292d30"/><stop offset="1" stop-color="#121416"/></linearGradient>
  <linearGradient id="sand" x1="10" y1="10" x2="75" y2="70" gradientUnits="userSpaceOnUse"><stop stop-color="#edcfaa"/><stop offset="1" stop-color="#ac8054"/></linearGradient>
</defs>
<rect width="1200" height="630" fill="#111213"/>
<ellipse cx="1000" cy="330" rx="420" ry="380" fill="url(#glow)"/>
<path d="M752 0v630M0 567h1200M1199 0v630" stroke="#2c2f31" stroke-opacity=".7"/>
<g transform="translate(774 216) scale(.79)">
  <path d="m-10 180 292-104 300 104M36 224l246-87 246 87M66 69l216 77L499 69" fill="none" stroke="#34383b"/>
  <ellipse cx="282" cy="209" rx="176" ry="36" fill="#08090a" opacity=".8"/>
  <path d="m74 129 208-74 208 74v26l-208 75-208-75Z" fill="url(#side)" stroke="#484d51"/>
  <path d="m74 129 208-74 208 74-208 75Z" fill="url(#top)" stroke="#484d51"/>
  <path d="m95 125 187-66 187 66-187 67Z" fill="#181b1d" stroke="#41464a"/>
  <g transform="matrix(1.77 -.63 1.77 .63 141 125)" fill="url(#sand)">{paths}</g>
  <path d="M282 204v26m-208-88 208 75 208-75" fill="none" stroke="#484d51"/>
</g>
<g transform="translate(74 66) scale(.55)" fill="#d8b892">{paths}</g>
{text('sandloom', 132, 100, 32, '#edeee9')}
{text(label, 80, 195, 13, '#b8a082', 2)}
{text(first, 76, 282, 56 if name == 'docs' else 60, '#edeee9')}
{text(second, 76, 358, 58, '#d8b892')}
{text(detail, 80, 426, 20, '#a5aaa8')}
{text(last, 80, 458, 20, '#a5aaa8')}
{text('PYTHON / MODULAR SAAS', 80, 535, 12, '#7e8582', 1.5)}
<path d="M80 590h30m8 0h8" stroke="#d8b892" stroke-width="2"/>
{text('ONE CODEBASE. ROOM TO GROW.', 800, 594, 11, '#a5aaa8', 1)}
</svg>\n'''
        (social / f"sandloom-{name}.svg").write_text(artwork)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, required=True)
    generate(parser.parse_args().font)
