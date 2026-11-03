"""The caracal mark of the wizard heading, rasterised from the brand SVG.

The rasteriser below is the offline renderer of :data:`sandloom.core.cli.ui.MARK`:
it parses the path of ``docs/assets/sandloom-mark.svg`` (``M L V H Q C Z``,
even-odd fill), samples each pixel 8 x 8 times and turns pairs of pixel rows
into half blocks. Run it to print the constant and a preview::

    uv run python src/sandloom/core/tests/test_cli_mark.py

The test keeps the stored constant equal to what the SVG renders to.
"""

from __future__ import annotations

import re
from pathlib import Path

from sandloom.core.cli import ui

SVG = Path(__file__).resolve().parents[4] / "docs/assets/sandloom-mark.svg"
#: The shape's box in SVG units (the 80 x 80 view box less its margins).
BOX = (10.0, 3.0, 70.0, 74.0)
COLUMNS, PIXEL_ROWS, THRESHOLD, SAMPLES = 14, 14, 0.4, 8

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


def render(svg: Path = SVG) -> tuple[str, ...]:
    """The mark as half-block rows (trailing blanks stripped)."""
    path = re.search(r' d="([^"]+)"', svg.read_text(encoding="utf-8"))
    assert path is not None
    shapes = polygons(path.group(1))
    x0, y0, x1, y1 = BOX
    pixels = [
        [
            sum(
                _inside(
                    shapes,
                    x0 + (column + (a + 0.5) / SAMPLES) * (x1 - x0) / COLUMNS,
                    y0 + (row + (b + 0.5) / SAMPLES) * (y1 - y0) / PIXEL_ROWS,
                )
                for a in range(SAMPLES)
                for b in range(SAMPLES)
            )
            / SAMPLES**2
            >= THRESHOLD
            for column in range(COLUMNS)
        ]
        for row in range(PIXEL_ROWS)
    ]
    blocks = {(True, True): "█", (True, False): "▀", (False, True): "▄"}
    return tuple(
        "".join(
            blocks.get((top, bottom), " ")
            for top, bottom in zip(pixels[r], pixels[r + 1], strict=True)
        ).rstrip()
        for r in range(0, PIXEL_ROWS, 2)
    )


def test_the_stored_mark_is_the_svg_rendered() -> None:
    assert render() == ui.MARK


def test_the_mark_fits_seven_rows_by_fourteen_columns() -> None:
    assert len(ui.MARK) <= 7
    assert max(len(row) for row in ui.MARK) <= ui.MARK_WIDTH == 14
    assert set("".join(ui.MARK)) <= set(" ▀▄█")


if __name__ == "__main__":  # pragma: no cover - the offline renderer
    rows = render()
    print("MARK: tuple[str, ...] = (")
    for row in rows:
        print(f"    {row!r},")
    print(")\n")
    print("\n".join(rows))
