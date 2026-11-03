"""A checkbox list for a terminal: arrows move, space toggles, Enter confirms.

Standard library only: raw keys come from ``termios`` on POSIX and ``msvcrt``
on Windows. :func:`select` takes the keys as an iterable, so the state
machine runs without a terminal in tests; :func:`terminal_keys` is the real
source. Callers fall back to a typed answer where :func:`available` is false.
"""

from __future__ import annotations

import os
import select as _select
import sys
from collections.abc import Iterable, Iterator, Sequence
from typing import TextIO

if sys.platform == "win32":
    import msvcrt
else:
    import termios
    import tty

#: Key names :func:`select` understands; anything else is ignored.
UP, DOWN, TOGGLE, ALL, CONFIRM, CANCEL = (
    "up",
    "down",
    "toggle",
    "all",
    "confirm",
    "cancel",
)

_POSIX_KEYS = {
    "\x1b[A": UP,
    "\x1b[B": DOWN,
    "\x1bOA": UP,
    "\x1bOB": DOWN,
    "k": UP,
    "j": DOWN,
    " ": TOGGLE,
    "a": ALL,
    "\r": CONFIRM,
    "\n": CONFIRM,
    "\x1b": CANCEL,
    "q": CANCEL,
    "\x03": CANCEL,
}
_WINDOWS_ARROWS = {"H": UP, "P": DOWN}


class Cancelled(Exception):
    """The person left the list with Esc, q or Ctrl-C."""


def available() -> bool:
    """A terminal on both ends, so raw keys can be read and the list redrawn."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def terminal_keys() -> Iterator[str]:
    """Key names read one at a time from the terminal in raw mode."""
    if sys.platform == "win32":
        while True:
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                key = _WINDOWS_ARROWS.get(msvcrt.getwch())
            else:
                key = _POSIX_KEYS.get(ch)
            if key:
                yield key
    else:
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                ch = os.read(fd, 1).decode(errors="ignore")
                if ch == "\x1b" and _select.select([fd], [], [], 0.05)[0]:
                    ch += os.read(fd, 2).decode(errors="ignore")
                key = _POSIX_KEYS.get(ch)
                if key:
                    yield key
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _move(key: str, state: list[bool], cursor: int) -> int:
    """Apply a key that edits the list in place; the new cursor."""
    if key == UP:
        return (cursor - 1) % len(state)
    if key == DOWN:
        return (cursor + 1) % len(state)
    if key == TOGGLE:
        state[cursor] = not state[cursor]
    elif key == ALL:
        state[:] = [not all(state)] * len(state)
    return cursor


def render(
    labels: Sequence[str], checked: Sequence[bool], cursor: int, note: str = ""
) -> list[str]:
    """The lines of the list: a pointer on the cursor row, a box per item."""
    lines = [
        f"{'>' if i == cursor else ' '} [{'x' if on else ' '}] {label}"
        for i, (label, on) in enumerate(zip(labels, checked, strict=True))
    ]
    lines.append(
        note or "  ↑/↓ move · space check · a all · Enter confirm · Esc cancel"
    )
    return lines


def select(
    labels: Sequence[str],
    checked: Iterable[int] = (),
    *,
    keys: Iterable[str] | None = None,
    out: TextIO | None = None,
    minimum: int = 1,
) -> list[int]:
    """Show ``labels`` as checkboxes and return the indexes checked on Enter.

    ``checked`` are the indexes checked at the start. Enter with fewer than
    ``minimum`` checked keeps the list open with a note. Raises
    :class:`Cancelled` on Esc, q or Ctrl-C.
    """
    out = out or sys.stdout
    state = [i in set(checked) for i in range(len(labels))]
    cursor, note, drawn = 0, "", 0

    def draw() -> None:
        nonlocal drawn
        lines = render(labels, state, cursor, note)
        if drawn:
            out.write(f"\x1b[{drawn}F\x1b[J")
        out.write("\r\n".join(lines) + "\r\n")
        out.flush()
        drawn = len(lines)

    out.write("\x1b[?25l")
    try:
        draw()
        for key in keys if keys is not None else terminal_keys():
            note = ""
            if key == CANCEL:
                raise Cancelled
            if key == CONFIRM:
                chosen = [i for i, on in enumerate(state) if on]
                if len(chosen) >= minimum:
                    return chosen
                note = f"  check at least {minimum} (space), then Enter"
            cursor = _move(key, state, cursor)
            draw()
        raise Cancelled
    finally:
        out.write("\x1b[?25h")
        out.flush()


__all__ = ["Cancelled", "available", "render", "select", "terminal_keys"]
