"""The caracal mark of the wizard heading, rasterised from the brand SVG.

The rasteriser below is the offline renderer of :data:`sandloom.core.cli.ui.MARK`
(braille) and :data:`sandloom.core.cli.ui.MARK_BLOCKS` (half blocks, where
braille cannot be drawn). It parses the path of ``docs/assets/sandloom-mark.svg``
(``M L V H Q C Z``, even-odd fill), fits the shape's box (plus one unit) into a
square grid with square cells, samples each cell 6 x 6 times, and keeps a cell
when the shape covers enough of it: a high threshold keeps the logo's thin
cut-outs (inner ears, eyes, nose, mouth) open. Run it to print the constants::

    uv run python src/sandloom/core/tests/test_cli_mark.py

The tests keep the stored constants equal to what the SVG renders to.
"""

from __future__ import annotations

import re
from pathlib import Path

from sandloom.core.cli import ui

SVG = Path(__file__).resolve().parents[4] / "docs/assets/sandloom-mark.svg"
#: Braille: 64 x 64 dots (32 x 16 cells); half blocks: 32 x 32 pixels (32 x 16).
BRAILLE_DOTS, BRAILLE_THRESHOLD = 64, 0.7
BLOCK_PIXELS, BLOCK_THRESHOLD = 32, 0.6
SAMPLES = 6

Point = tuple[float, float]


def polygons(path: str) -> list[list[Point]]:
    """The path's subpaths as polygons (curves flattened to 16 segments)."""
    tokens = re.findall(r"[MLQCVHZmlqcvhz]|-?\d*\.?\d+", path)
    shapes: list[list[Point]] = []
    current: list[Point] = []
    pos: Point = (0.0, 0.0)
    index, command = 0, ""

    def number() -> float:
        nonlocal index
        index += 1
        return float(tokens[index - 1])

    def curve(points: list[Point]) -> None:
        n = len(points) - 1
        coefficients = [1, 2, 1] if n == 2 else [1, 3, 3, 1]
        for k in range(1, 17):
            s = k / 16
            weights = [
                c * (1 - s) ** (n - i) * s**i for i, c in enumerate(coefficients)
            ]
            current.append(
                (
                    sum(w * p[0] for w, p in zip(weights, points, strict=True)),
                    sum(w * p[1] for w, p in zip(weights, points, strict=True)),
                )
            )

    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
            if command in "Zz":
                if current:
                    shapes.append(current)
                current = []
                continue
        if command == "M":
            if current:
                shapes.append(current)
            pos = (number(), number())
            current = [pos]
            command = "L"
        elif command == "L":
            pos = (number(), number())
            current.append(pos)
        elif command == "V":
            pos = (pos[0], number())
            current.append(pos)
        elif command == "H":
            pos = (number(), pos[1])
            current.append(pos)
        elif command == "Q":
            control, end = (number(), number()), (number(), number())
            curve([pos, control, end])
            pos = end
        elif command == "C":
            first, second = (number(), number()), (number(), number())
            end = (number(), number())
            curve([pos, first, second, end])
            pos = end
        else:  # pragma: no cover - the mark uses absolute commands only
            raise ValueError(f"unsupported path command {command}")
    if current:
        shapes.append(current)
    return shapes


def _inside(shapes: list[list[Point]], x: float, y: float) -> bool:
    crossings = 0
    for shape in shapes:
        for (x1, y1), (x2, y2) in zip(shape, shape[1:] + shape[:1], strict=True):
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                crossings += 1
    return crossings % 2 == 1


def _shapes(svg: Path) -> list[list[Point]]:
    path = re.search(r' d="([^"]+)"', svg.read_text(encoding="utf-8"))
    assert path is not None
    return polygons(path.group(1))


def grid(size: int, threshold: float, svg: Path = SVG) -> list[list[bool]]:
    """``size`` x ``size`` square cells over the shape, centred."""
    shapes = _shapes(svg)
    xs = [x for shape in shapes for x, _ in shape]
    ys = [y for shape in shapes for _, y in shape]
    x0, x1, y0, y1 = min(xs) - 1, max(xs) + 1, min(ys) - 1, max(ys) + 1
    step = max(x1 - x0, y1 - y0) / size
    left = x0 - (size * step - (x1 - x0)) / 2
    top = y0 - (size * step - (y1 - y0)) / 2
    return [
        [
            sum(
                _inside(
                    shapes,
                    left + (column + (a + 0.5) / SAMPLES) * step,
                    top + (row + (b + 0.5) / SAMPLES) * step,
                )
                for a in range(SAMPLES)
                for b in range(SAMPLES)
            )
            / SAMPLES**2
            >= threshold
            for column in range(size)
        ]
        for row in range(size)
    ]


#: Braille dot bits by (column, row) inside a 2 x 4 cell.
_DOTS = {
    (0, 0): 1,
    (0, 1): 2,
    (0, 2): 4,
    (1, 0): 8,
    (1, 1): 16,
    (1, 2): 32,
    (0, 3): 64,
    (1, 3): 128,
}


def render(svg: Path = SVG) -> tuple[str, ...]:
    """The mark in braille: 2 x 4 dots per character."""
    px = grid(BRAILLE_DOTS, BRAILLE_THRESHOLD, svg)
    return tuple(
        "".join(
            chr(0x2800 + bits) if bits else " "
            for bits in (
                sum(bit for (dx, dy), bit in _DOTS.items() if px[row + dy][column + dx])
                for column in range(0, BRAILLE_DOTS, 2)
            )
        ).rstrip()
        for row in range(0, BRAILLE_DOTS, 4)
    )


def render_blocks(svg: Path = SVG) -> tuple[str, ...]:
    """The mark in half blocks: 1 x 2 pixels per character."""
    px = grid(BLOCK_PIXELS, BLOCK_THRESHOLD, svg)
    blocks = {(True, True): "█", (True, False): "▀", (False, True): "▄"}
    return tuple(
        "".join(
            blocks.get((top, bottom), " ")
            for top, bottom in zip(px[r], px[r + 1], strict=True)
        ).rstrip()
        for r in range(0, BLOCK_PIXELS, 2)
    )


def test_the_stored_mark_is_the_svg_rendered() -> None:
    assert render() == ui.MARK
    assert render_blocks() == ui.MARK_BLOCKS


def test_the_mark_is_thirty_two_columns_by_sixteen_rows() -> None:
    for mark, alphabet in ((ui.MARK, None), (ui.MARK_BLOCKS, set(" ▀▄█"))):
        assert len(mark) == 16
        assert max(len(row) for row in mark) <= ui.MARK_WIDTH == 32
        if alphabet:
            assert set("".join(mark)) <= alphabet
    assert all(ch == " " or "\u2801" <= ch <= "\u28ff" for ch in "".join(ui.MARK))


def test_the_logos_cut_outs_survive() -> None:
    """Eyes, nose and mouth are holes in the logo: blank dots inside the face."""
    px = grid(BRAILLE_DOTS, BRAILLE_THRESHOLD)
    face = px[34:58]  # the rows from the eyes down to the chin
    holes = sum(
        1
        for row in face
        for i in range(1, BRAILLE_DOTS - 1)
        if not row[i] and any(row[:i]) and any(row[i + 1 :])
    )
    assert holes > 60


if __name__ == "__main__":  # pragma: no cover - the offline renderer
    for name, rows in (("MARK", render()), ("MARK_BLOCKS", render_blocks())):
        print(f"{name}: tuple[str, ...] = (")
        for row in rows:
            print(f"    {row!r},")
        print(")\n")
        print("\n".join(rows) + "\n")
