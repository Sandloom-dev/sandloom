"""The Sandloom look of the CLI: colour, symbols, step lines, the heading.

Standard library only, and every decision takes the environment as an
argument (callers pass :func:`sandloom.core.cli.dev.errors.environment`, the
process-flags seam), so the tests cover each combination without touching
the process. Design: ``design/cli-wizard/design.md``.

- :func:`interactive`: whether a person can answer questions (both ends a
  terminal, no ``--yes``, not CI, no coding agent; :data:`AGENT_MARKERS`).
- :func:`colour_depth` and :func:`motion`, decided per stream: ``NO_COLOR``
  wins, ``FORCE_COLOR`` / ``CLICOLOR_FORCE`` colour a pipe, and only a real
  terminal gets cursor movement and the spinner.
- :func:`symbols_for`: ``✓ ✗ › ● ◆ → ↑ ↓ · …`` and a braille spinner, or ASCII
  where the stream cannot encode them (``!`` marks a failure: ``x`` means
  "selected" in the checklist).
- :class:`Style` paints text; with colour off it returns the text unchanged,
  so the plain output (logs, screen readers, tests) stays byte-identical.
- :class:`StepLine`, :func:`heading`, :func:`error_lines`, :func:`summary`.
"""

from __future__ import annotations

import codecs
import ctypes
import os
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TextIO

if sys.platform == "win32":
    import msvcrt

# --------------------------------------------------------------------------- #
# Who is on the other end
# --------------------------------------------------------------------------- #

#: Environment variables a coding agent sets in the shells it runs commands
#: in: ``(name, required value or None for any non-empty value, source)``.
#: Each was checked against the agent's documentation or source (2026-11-03);
#: the cross-vendor list is Vercel's ``@vercel/detect-agent``
#: (github.com/vercel/vercel, packages/detect-agent/src/index.ts).
AGENT_MARKERS: tuple[tuple[str, str | None, str], ...] = (
    # A cross-vendor convention: Claude Code, GitHub Copilot, v0 and others.
    ("AI_AGENT", None, "@vercel/detect-agent"),
    # code.claude.com/docs/en/env-vars: "Set to 1 in subprocesses Claude Code
    # spawns (Bash and PowerShell tools, ...)".
    ("CLAUDECODE", None, "Claude Code docs, env-vars"),
    # openai/codex codex-rs/core/src/spawn.rs (CODEX_SANDBOX_ENV_VAR,
    # CODEX_SANDBOX_NETWORK_DISABLED_ENV_VAR) and @vercel/detect-agent.
    ("CODEX_SANDBOX", None, "openai/codex spawn.rs"),
    ("CODEX_SANDBOX_NETWORK_DISABLED", None, "openai/codex spawn.rs"),
    ("CODEX_CI", None, "@vercel/detect-agent"),
    ("CODEX_THREAD_ID", None, "@vercel/detect-agent"),
    # Gemini CLI's run_shell_command sets GEMINI_CLI=1 (docs, tools/shell).
    ("GEMINI_CLI", None, "Gemini CLI docs, shell tool"),
    # The Cursor agent CLI sets CURSOR_AGENT=1 for the commands it runs.
    ("CURSOR_AGENT", None, "@vercel/detect-agent"),
    ("CURSOR_EXTENSION_HOST_ROLE", "agent-exec", "@vercel/detect-agent"),
    ("ANTIGRAVITY_AGENT", None, "@vercel/detect-agent"),
    ("AUGMENT_AGENT", None, "@vercel/detect-agent"),
    ("OPENCODE", "1", "@vercel/detect-agent"),
    ("OPENCODE_CLIENT", None, "@vercel/detect-agent"),
)


def truthy(value: str | None) -> bool:
    """``1``, ``true``, ``yes`` or ``on`` (any case)."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def in_ci(environ: Mapping[str, str]) -> bool:
    """``CI`` is truthy (every hosted CI sets ``CI=true``)."""
    return truthy(environ.get("CI"))


def agent_marker(environ: Mapping[str, str]) -> str | None:
    """The first :data:`AGENT_MARKERS` variable set in ``environ``, or ``None``."""
    for name, value, _source in AGENT_MARKERS:
        found = (environ.get(name) or "").strip()
        if found and (value is None or found == value):
            return name
    return None


def is_tty(stream: object) -> bool:
    """``stream.isatty()``, false for a closed or fake stream."""
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        return False


def interactive(
    environ: Mapping[str, str],
    *,
    stdin: object | None = None,
    stdout: object | None = None,
    no_input: bool = False,
) -> bool:
    """Whether to ask questions: stdin and stdout are terminals, ``--yes`` /
    ``--no-input`` was not given, ``CI`` is not truthy and no coding agent
    marker is set. Agents and scripts never block on a question."""
    if no_input or in_ci(environ) or agent_marker(environ) is not None:
        return False
    return is_tty(sys.stdin if stdin is None else stdin) and is_tty(
        sys.stdout if stdout is None else stdout
    )


# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #

#: Colour depths: none, the 16 ANSI colours, the xterm 256, 24-bit.
NONE, BASIC, EXTENDED, TRUECOLOR = 0, 16, 256, 24
_FORCE_DEPTHS = {"": BASIC, "1": BASIC, "true": BASIC, "2": EXTENDED, "3": TRUECOLOR}


def _detected_depth(environ: Mapping[str, str]) -> int:
    if (environ.get("COLORTERM") or "").strip().lower() in {"truecolor", "24bit"}:
        return TRUECOLOR
    if "256" in (environ.get("TERM") or ""):
        return EXTENDED
    return BASIC


def colour_depth(environ: Mapping[str, str], *, tty: bool, vt: bool = True) -> int:
    """The colour depth for one stream.

    ``NO_COLOR`` (non-empty) wins. ``FORCE_COLOR`` decides next: ``0`` (or
    ``false``) is off; ``1``, ``2`` or ``3`` set the depth, also on a pipe.
    ``CLICOLOR_FORCE`` (non-zero) colours a pipe at the detected depth.
    Otherwise only a terminal (``tty``) that is not ``TERM=dumb`` and that
    interprets escapes (``vt``: Windows VT mode) gets colour:
    ``COLORTERM=truecolor``/``24bit``, else ``TERM`` naming 256, else 16."""
    if environ.get("NO_COLOR"):
        return NONE
    force = environ.get("FORCE_COLOR")
    if force is not None:
        value = force.strip().lower()
        if value in {"0", "false", "no", "off"}:
            return NONE
        return _FORCE_DEPTHS.get(value, BASIC)
    if (environ.get("CLICOLOR_FORCE") or "0").strip() not in {"", "0"}:
        return _detected_depth(environ)
    if not tty or not vt or (environ.get("TERM") or "") == "dumb":
        return NONE
    return _detected_depth(environ)


def motion(
    environ: Mapping[str, str], *, tty: bool, vt: bool = True, plain: bool = False
) -> bool:
    """Cursor movement and the spinner: a real terminal only. Forced colour
    never turns it on (a log full of redraws is unreadable)."""
    return tty and vt and not plain and (environ.get("TERM") or "") != "dumb"


#: The one accent: a mid-tone of the brand's caracal brown that reads on a
#: light and on a dark background. 4.5:1 on both #ffffff and #1e1e1e is out
#: of reach for any colour (the best balance is about 4.08:1 each, see
#: ``test_cli_ui.py``); this one is 4.10:1 on white and 4.07:1 on #1e1e1e,
#: and prompts add bold.
ACCENT = "#9a774f"


def _channels(hex_colour: str) -> tuple[int, int, int]:
    value = hex_colour.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def relative_luminance(hex_colour: str) -> float:
    """WCAG 2 relative luminance of ``#rrggbb``."""

    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _channels(hex_colour)
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)


def contrast_ratio(first: str, second: str) -> float:
    """WCAG 2 contrast ratio of two ``#rrggbb`` colours (1 to 21)."""
    light, dark = sorted(
        (relative_luminance(first), relative_luminance(second)), reverse=True
    )
    return (light + 0.05) / (dark + 0.05)


#: The accent on a 256-colour terminal: xterm's #af875f, the warm cube colour
#: next to ACCENT (the nearest by distance, 101, is olive).
ACCENT_256 = 137


# --------------------------------------------------------------------------- #
# Symbols
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Symbols:
    ok: str
    fail: str
    pointer: str
    bullet: str
    diamond: str
    arrow: str
    up: str
    down: str
    dot: str
    ellipsis: str
    spinner: tuple[str, ...]


UNICODE = Symbols(
    ok="✓",
    fail="✗",
    pointer="\u203a",  # › (RUF001: an ASCII look-alike)
    bullet="●",
    diamond="◆",
    arrow="→",
    up="↑",
    down="↓",
    dot="·",
    ellipsis="…",
    spinner=("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"),
)
ASCII = Symbols(
    ok="+",
    fail="!",
    pointer=">",
    bullet="*",
    diamond="*",
    arrow="->",
    up="^",
    down="v",
    dot="-",
    ellipsis="...",
    spinner=("-", "\\", "|", "/"),
)
_UNICODE_SAMPLE = "✓✗\u203a●◆→↑↓·…⠋▀▄█"


def can_encode(encoding: str | None, text: str = _UNICODE_SAMPLE) -> bool:
    """Whether ``encoding`` (a stream's) can write ``text``."""
    if not encoding:
        return False
    try:
        codecs.lookup(encoding)
        text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def symbols_for(encoding: str | None, platform: str = sys.platform) -> Symbols:
    """:data:`UNICODE` where ``encoding`` writes it, else :data:`ASCII`. On
    Windows the spinner is ASCII (console fonts lack braille)."""
    if not can_encode(encoding):
        return ASCII
    if platform == "win32":
        return Symbols(**{**UNICODE.__dict__, "spinner": ASCII.spinner})
    return UNICODE


def utf8(encoding: str | None) -> bool:
    """The stream writes UTF-8 (the mark needs it)."""
    if not encoding:
        return False
    try:
        return codecs.lookup(encoding).name == "utf-8"
    except LookupError:
        return False


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #


def enable_vt(stream: object, platform: str = sys.platform) -> bool:
    """Turn on escape-sequence processing for a Windows console (``True``
    elsewhere). ``False``: an old console; colour and cursor moves stay off."""
    if platform != "win32":
        return True
    return _windows_vt(stream)


def _windows_vt(stream: object) -> bool:
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            handle = msvcrt.get_osfhandle(stream.fileno())  # type: ignore[attr-defined]
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
        except (AttributeError, OSError, ValueError):
            return False
    return False  # asked for Windows elsewhere (tests): no console API


# --------------------------------------------------------------------------- #
# Style
# --------------------------------------------------------------------------- #

_RESET = "\x1b[0m"


@dataclass(frozen=True)
class Style:
    """How one stream is written: colour depth, cursor movement, symbols."""

    depth: int = NONE
    motion: bool = False
    symbols: Symbols = ASCII
    #: The stream is a UTF-8 terminal (the mark may be drawn).
    utf8: bool = False

    def paint(self, text: str, *codes: str) -> str:
        if not self.depth or not codes or not text:
            return text
        return f"\x1b[{';'.join(codes)}m{text}{_RESET}"

    def _accent_code(self) -> str:
        if self.depth == TRUECOLOR:
            r, g, b = _channels(ACCENT)
            return f"38;2;{r};{g};{b}"
        if self.depth == EXTENDED:
            return f"38;5;{ACCENT_256}"
        return "33"

    def accent(self, text: str) -> str:
        return self.paint(text, self._accent_code())

    def prompt(self, text: str) -> str:
        return self.paint(text, "1", self._accent_code())

    def bold(self, text: str) -> str:
        return self.paint(text, "1")

    def muted(self, text: str) -> str:
        return self.paint(text, "2")

    def green(self, text: str) -> str:
        return self.paint(text, "32")

    def red(self, text: str) -> str:
        return self.paint(text, "31")

    def ok(self, text: str = "") -> str:
        """``✓ text`` with a green mark."""
        mark = self.green(self.symbols.ok)
        return f"{mark} {text}" if text else mark

    def fail(self, text: str = "") -> str:
        """``✗ text`` with a red mark."""
        mark = self.red(self.symbols.fail)
        return f"{mark} {text}" if text else mark


PLAIN = Style()


def style_for(
    stream: object,
    environ: Mapping[str, str],
    *,
    plain: bool = False,
    platform: str = sys.platform,
) -> Style:
    """The :class:`Style` of ``stream`` (stdout and stderr each get their
    own). ``plain`` (``--plain``) turns off colour, motion and the mark."""
    encoding = getattr(stream, "encoding", None)
    symbols = symbols_for(encoding, platform)
    if plain:
        return Style(symbols=symbols)
    tty = is_tty(stream)
    vt = enable_vt(stream, platform) if tty else True
    return Style(
        depth=colour_depth(environ, tty=tty, vt=vt),
        motion=motion(environ, tty=tty, vt=vt),
        symbols=symbols,
        utf8=tty and utf8(encoding),
    )


def columns(stream: object, default: int = 80) -> int:
    """The terminal width of ``stream`` (``default`` when it has none)."""
    try:
        return os.get_terminal_size(stream.fileno()).columns  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# The mark, the heading, errors, the summary
# --------------------------------------------------------------------------- #

#: The caracal of ``docs/assets/sandloom-mark.svg`` in braille: 32 columns by
#: 16 rows (64 x 64 dots). Rendered by ``src/sandloom/core/tests/test_cli_mark.py``
#: (``uv run python src/sandloom/core/tests/test_cli_mark.py``), which checks it.
MARK: tuple[str, ...] = (
    "      ⣤⡀                ⢀⣤",
    "      ⣿⣿⡆              ⢰⣿⣿",
    "      ⣿⣿⣧              ⣼⣿⣿",
    "      ⣿⢻⣿⡄            ⢠⣿⡟⣿",
    "     ⢸⡏⠘⣿⣇            ⣸⣿⠃⢹⡇",
    "     ⢸⡇ ⢹⣿⣀⣤⣴⣶⣶⣿⣿⣶⣶⣦⣤⣀⣿⡏ ⢸⡇",
    "    ⢀⣾⡇⣀⣨⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣅⣀⢸⣷⡀",
    "    ⣼⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣧",
    "   ⣼⣿⣿⣿⡿⠛⠛⠛⠿⣿⣿⣿⣿⣿⣿⣿⣿⠿⠛⠛⠛⢿⣿⣿⣿⣧",
    "   ⢹⣿⣿⣿⣷⣄⡀  ⠈⣻⣿⣿⣿⣿⣟⠁  ⢀⣠⣾⣿⣿⣿⡏",
    "    ⢿⣿⣿⣿⣿⣿⣷⣶⣾⣿⣿⣿⣿⣿⣿⣷⣶⣾⣿⣿⣿⣿⣿⡿",
    "    ⠈⣿⣿⣿⣿⣿⣿⣿⣿⣟⠉⠉⠉⠉⣻⣿⣿⣿⣿⣿⣿⣿⣿⠁",
    "     ⠘⢿⣿⣿⣿⣿⣿⣿⣿⣷⡄⢠⣾⣿⣿⣿⣿⣿⣿⣿⡿⠃",
    "       ⠙⠻⣿⣿⣿⣿⣏⣩⣤⣤⣍⣹⣿⣿⣿⣿⠟⠋",
    "         ⠈⠛⠿⣿⣿⣿⣿⣿⣿⣿⣿⠿⠛⠁",
    "             ⠉⠙⠻⠟⠋⠉",
)
#: The same caracal in half blocks (32 x 32 pixels), where braille cannot be
#: drawn: the console fonts of Windows have no braille glyphs.
MARK_BLOCKS: tuple[str, ...] = (
    "      ▄                  ▄",
    "      ██                ██",
    "      ███              ███",
    "      ███              ███",
    "      █ ██            ██ █",
    "      ▀ ██▄▄▄██████▄▄▄██ ▀",
    "     █  ▄██████████████▄  █",
    "    ████████████████████████",
    "   ▄████▀▀▀▀████████▀▀▀▀████▄",
    "   ▀████▄    ██████    ▄████▀",
    "    ████████████████████████",
    "     █████████▀▀▀▀█████████",
    "     ▀█████████  █████████▀",
    "       ▀▀█████▄▄▄▄█████▀▀",
    "          ▀██████████▀",
    "              ▀▀▀▀",
)
MARK_WIDTH = 32
#: The smallest terminal that gets the mark: the caracal, a gap and the name
#: beside it, and room for the first questions below.
MARK_MIN_COLUMNS = 64
MARK_MIN_ROWS = 30
#: The mark's colour runs from light sand (ears) to deep ochre (chin).
MARK_TOP, MARK_BOTTOM = "#d4ac7c", "#83603b"


def _mark_colour(style: Style, row: int, rows: int) -> str:
    """The SGR code of the mark's ``row``: a gradient in truecolor, the accent
    otherwise."""
    if style.depth != TRUECOLOR:
        return style._accent_code()
    top, bottom = _channels(MARK_TOP), _channels(MARK_BOTTOM)
    t = row / max(1, rows - 1)
    r, g, b = (round(a + (z - a) * t) for a, z in zip(top, bottom, strict=True))
    return f"38;2;{r};{g};{b}"


def heading(
    style: Style,
    version: str,
    subtitle: str,
    *,
    width: int = 80,
    height: int = 40,
    mark: bool = False,
    braille: bool = sys.platform != "win32",
) -> list[str]:
    """``◆ sandloom <version> · <subtitle>``; with ``mark`` (the wizard, a
    UTF-8 terminal at least :data:`MARK_MIN_COLUMNS` by :data:`MARK_MIN_ROWS`)
    the caracal stands to the left of the name instead: in braille, or in half
    blocks where braille cannot be drawn."""
    sym = style.symbols
    title = f"{style.bold('sandloom')} {version} {sym.dot} {subtitle}"
    if not (
        mark and style.utf8 and width >= MARK_MIN_COLUMNS and height >= MARK_MIN_ROWS
    ):
        return [f"{style.accent(sym.diamond)} {title}"]
    rows = MARK if braille else MARK_BLOCKS
    middle = len(rows) // 2 - 1
    beside = {
        middle: f"{style.bold('sandloom')} {version}",
        middle + 1: style.muted(subtitle),
    }
    return [
        f"  {style.paint(row, _mark_colour(style, i, len(rows)))}"
        + (f"{' ' * (MARK_WIDTH - len(row))}   {beside[i]}" if i in beside else "")
        for i, row in enumerate(rows)
    ]


def rows(stream: object, default: int = 40) -> int:
    """The terminal height of ``stream`` (``default`` when it has none)."""
    try:
        return os.get_terminal_size(stream.fileno()).lines  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return default


def error_lines(style: Style, what: str, todo: str | None = None) -> str:
    """Two lines: ``✗ what failed`` and ``→ what to do``."""
    first = style.fail(what)
    if not todo:
        return first
    return f"{first}\n{style.accent(style.symbols.arrow)} {todo}"


def summary(
    style: Style,
    title: str,
    rows: Sequence[tuple[str, str]],
    *,
    footer: Sequence[str] = (),
) -> str:
    """The closing block: ``✓ title``, aligned label/value rows, then the
    ``footer`` lines (muted labels end with a colon)."""
    width = max((len(label) for label, _ in rows), default=0)
    lines = ["", f"  {style.ok(style.bold(title))}", ""]
    for label, value in rows:
        if not label and not value:
            lines.append("")
            continue
        lines.append(f"  {style.muted(label.ljust(width))}   {value}")
    if footer:
        lines.append("")
        lines.extend(f"  {line}" for line in footer)
    return "\n".join(lines) + "\n"


def elapsed(seconds: float) -> str:
    """``0.4s``, ``12.3s``, ``2m 05s``."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m {rest:02d}s"


def shorten(text: str, width: int, ellipsis: str = "…") -> str:
    """``text`` cut to ``width`` characters with ``ellipsis`` at the end."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: max(0, width - len(ellipsis))] + ellipsis


@dataclass
class StepLine:
    """One step of a run: a spinner with the elapsed time and the step's last
    output line while it runs (a terminal only), then ``✓ name 12.3s`` or
    ``✗ name``. Without motion only the final line is written."""

    style: Style
    out: TextIO
    name: str
    width: int = 80
    interval: float = 0.1
    _started: float = 0.0
    _last: str = ""
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self) -> StepLine:
        self._started = time.monotonic()
        if self.style.motion:
            self.out.write("\x1b[?25l")
            self._draw(0)
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def note(self, line: str) -> None:
        """The step's latest output line, shown under the spinner."""
        line = line.strip()
        if line:
            with self._lock:
                self._last = line

    def _spin(self) -> None:
        frame = 0
        while not self._stop.wait(self.interval):
            frame += 1
            self._draw(frame)

    def _draw(self, frame: int) -> None:
        sym = self.style.symbols
        spent = elapsed(time.monotonic() - self._started)
        with self._lock:
            last = self._last
        spinner = self.style.accent(sym.spinner[frame % len(sym.spinner)])
        head = f"  {spinner} {self.name} {self.style.muted(spent)}"
        plain_len = 2 + 2 + len(self.name) + 1 + len(spent)
        room = self.width - plain_len - 3 - len(sym.dot)
        tail = (
            f" {self.style.muted(sym.dot + ' ' + shorten(last, room, sym.ellipsis))}"
            if last and room > 10
            else ""
        )
        self.out.write(f"\r\x1b[2K{head}{tail}")
        self.out.flush()

    def finish(self, ok: bool) -> float:
        """Stop the spinner and write the final line; the seconds spent."""
        spent = time.monotonic() - self._started
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self.style.motion:
            self.out.write("\r\x1b[2K\x1b[?25h")
        if ok:
            self.out.write(
                f"  {self.style.ok(self.name)} {self.style.muted(elapsed(spent))}\n"
            )
        else:
            self.out.write(f"  {self.style.fail(self.name)}\n")
        self.out.flush()
        return spent


__all__ = [
    "ACCENT",
    "ACCENT_256",
    "AGENT_MARKERS",
    "ASCII",
    "MARK",
    "MARK_BLOCKS",
    "PLAIN",
    "UNICODE",
    "StepLine",
    "Style",
    "Symbols",
    "agent_marker",
    "colour_depth",
    "rows",
    "columns",
    "contrast_ratio",
    "enable_vt",
    "error_lines",
    "heading",
    "in_ci",
    "interactive",
    "is_tty",
    "motion",
    "style_for",
    "summary",
    "symbols_for",
    "truthy",
]
