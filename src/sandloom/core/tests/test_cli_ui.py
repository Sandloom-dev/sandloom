"""The Sandloom look of the CLI (``sandloom.core.cli.ui``): who gets questions,
colour and symbols per environment, the accent's contrast, the heading."""

from __future__ import annotations

import io

import pytest

from sandloom.core.cli import ui


class _Stream(io.StringIO):
    def __init__(self, *, tty: bool, encoding: str | None = "utf-8") -> None:
        super().__init__()
        self._tty = tty
        self._encoding = encoding

    def isatty(self) -> bool:
        return self._tty

    @property
    def encoding(self) -> str | None:  # type: ignore[override]
        return self._encoding


TTY = _Stream(tty=True)
PIPE = _Stream(tty=False)

# ------------------------------------------------------------- interactive


def test_a_person_at_a_terminal_is_asked() -> None:
    assert ui.interactive({}, stdin=TTY, stdout=TTY)


@pytest.mark.parametrize("name,value,_source", ui.AGENT_MARKERS)
def test_no_questions_for_any_agent_marker(
    name: str, value: str | None, _source: str
) -> None:
    assert not ui.interactive({name: value or "1"}, stdin=TTY, stdout=TTY)
    assert ui.agent_marker({name: value or "1"}) == name


def test_the_marker_list_names_the_known_agents() -> None:
    names = {name for name, _, _ in ui.AGENT_MARKERS}
    assert {"CLAUDECODE", "CODEX_SANDBOX", "GEMINI_CLI", "CURSOR_AGENT"} <= names
    # A marker with a required value counts only with that value.
    assert ui.agent_marker({"OPENCODE": "0"}) is None
    assert ui.agent_marker({"CURSOR_EXTENSION_HOST_ROLE": "renderer"}) is None
    # An agent's configuration a person sets is no marker.
    assert ui.agent_marker({"CODEX_HOME": "/x", "CLAUDE_CONFIG_DIR": "/y"}) is None


@pytest.mark.parametrize("value", ["true", "1", "TRUE", "yes"])
def test_no_questions_in_ci(value: str) -> None:
    assert not ui.interactive({"CI": value}, stdin=TTY, stdout=TTY)
    assert ui.interactive({"CI": "false"}, stdin=TTY, stdout=TTY)


def test_no_questions_with_yes_or_a_pipe() -> None:
    assert not ui.interactive({}, stdin=TTY, stdout=TTY, no_input=True)
    # stdin decides: piped answers never meet a question.
    assert not ui.interactive({}, stdin=PIPE, stdout=TTY)
    assert not ui.interactive({}, stdin=TTY, stdout=PIPE)


# ------------------------------------------------------------------- colour

COLOUR_CASES = [
    # (environment, tty, vt, depth)
    ({}, True, True, ui.BASIC),
    ({"TERM": "xterm-256color"}, True, True, ui.EXTENDED),
    ({"COLORTERM": "truecolor", "TERM": "xterm-256color"}, True, True, ui.TRUECOLOR),
    ({"COLORTERM": "24bit"}, True, True, ui.TRUECOLOR),
    ({"TERM": "dumb"}, True, True, ui.NONE),
    ({}, False, True, ui.NONE),
    ({}, True, False, ui.NONE),  # Windows without VT mode
    ({"NO_COLOR": "1"}, True, True, ui.NONE),
    ({"NO_COLOR": ""}, True, True, ui.BASIC),  # empty NO_COLOR is unset
    ({"NO_COLOR": "1", "FORCE_COLOR": "3"}, True, True, ui.NONE),  # NO_COLOR wins
    ({"FORCE_COLOR": "0"}, True, True, ui.NONE),
    ({"FORCE_COLOR": "false"}, True, True, ui.NONE),
    ({"FORCE_COLOR": "1"}, False, True, ui.BASIC),
    ({"FORCE_COLOR": ""}, False, True, ui.BASIC),
    ({"FORCE_COLOR": "2"}, False, True, ui.EXTENDED),
    ({"FORCE_COLOR": "3"}, False, True, ui.TRUECOLOR),
    ({"FORCE_COLOR": "1", "TERM": "dumb"}, True, True, ui.BASIC),
    ({"CLICOLOR_FORCE": "1"}, False, True, ui.BASIC),
    ({"CLICOLOR_FORCE": "1", "TERM": "xterm-256color"}, False, True, ui.EXTENDED),
    ({"CLICOLOR_FORCE": "0"}, False, True, ui.NONE),
]


@pytest.mark.parametrize("environ,tty,vt,depth", COLOUR_CASES)
def test_colour_depth(environ: dict[str, str], tty: bool, vt: bool, depth: int) -> None:
    assert ui.colour_depth(environ, tty=tty, vt=vt) == depth


@pytest.mark.parametrize(
    "environ,tty,vt,plain,moves",
    [
        ({}, True, True, False, True),
        ({}, False, True, False, False),
        ({"FORCE_COLOR": "3"}, False, True, False, False),  # forced colour, no motion
        ({"TERM": "dumb"}, True, True, False, False),
        ({}, True, False, False, False),
        ({}, True, True, True, False),  # --plain
    ],
)
def test_motion_needs_a_real_terminal(
    environ: dict[str, str], tty: bool, vt: bool, plain: bool, moves: bool
) -> None:
    assert ui.motion(environ, tty=tty, vt=vt, plain=plain) is moves


def test_style_is_decided_per_stream() -> None:
    out = ui.style_for(TTY, {"TERM": "xterm-256color"})
    err = ui.style_for(PIPE, {"TERM": "xterm-256color"})
    assert (out.depth, out.motion) == (ui.EXTENDED, True)
    assert (err.depth, err.motion) == (ui.NONE, False)
    assert ui.style_for(TTY, {}, plain=True) == ui.Style(symbols=ui.UNICODE)


def test_without_colour_the_text_is_unchanged() -> None:
    plain = ui.Style(symbols=ui.UNICODE)
    for paint in (plain.accent, plain.prompt, plain.bold, plain.muted, plain.red):
        assert paint("text") == "text"
    assert plain.ok("done") == "✓ done"
    coloured = ui.Style(depth=ui.TRUECOLOR, symbols=ui.UNICODE)
    assert coloured.accent("x") == "\x1b[38;2;154;119;79mx\x1b[0m"
    assert (
        ui.Style(depth=ui.EXTENDED).accent("x") == f"\x1b[38;5;{ui.ACCENT_256}mx\x1b[0m"
    )
    assert ui.Style(depth=ui.BASIC).accent("x") == "\x1b[33mx\x1b[0m"


# ------------------------------------------------------------------ contrast


def test_the_accent_reads_on_light_and_dark_backgrounds() -> None:
    """The recorded numbers. 4.5:1 on both is impossible for any colour:
    contrast with white falls as luminance rises and contrast with #1e1e1e
    climbs, and the two meet near 4.08:1; the accent sits at that balance."""
    on_white = ui.contrast_ratio(ui.ACCENT, "#ffffff")
    on_dark = ui.contrast_ratio(ui.ACCENT, "#1e1e1e")
    assert round(on_white, 2) == 4.10
    assert round(on_dark, 2) == 4.07
    dark = ui.relative_luminance("#1e1e1e")
    ceiling = ((1.05) * (dark + 0.05)) ** 0.5 / (dark + 0.05)
    assert 4.07 < ceiling < 4.1
    assert min(on_white, on_dark) > ceiling - 0.02
    # The brand's light and dark accents each fail on the other background.
    assert ui.contrast_ratio("#8b6841", "#1e1e1e") < 3.5
    assert ui.contrast_ratio("#d8b892", "#ffffff") < 2
    assert ui.ACCENT_256 == 137


# ------------------------------------------------------------------ symbols


@pytest.mark.parametrize(
    "encoding,platform,expected",
    [
        ("utf-8", "darwin", ui.UNICODE),
        ("UTF8", "linux", ui.UNICODE),
        ("cp1252", "win32", ui.ASCII),
        ("ascii", "linux", ui.ASCII),
        ("ANSI_X3.4-1968", "linux", ui.ASCII),  # LANG=C without coercion
        (None, "linux", ui.ASCII),
        ("no-such-codec", "linux", ui.ASCII),
    ],
)
def test_symbols_follow_the_encoding(
    encoding: str | None, platform: str, expected: ui.Symbols
) -> None:
    assert ui.symbols_for(encoding, platform) == expected


def test_windows_utf8_keeps_symbols_with_an_ascii_spinner() -> None:
    symbols = ui.symbols_for("utf-8", "win32")
    assert symbols.ok == "✓"
    assert symbols.spinner == ui.ASCII.spinner


def test_the_ascii_failure_mark_is_never_x() -> None:
    assert ui.ASCII.fail == "!"
    for field in ("diamond", "arrow", "up", "down", "dot", "ellipsis"):
        assert getattr(ui.ASCII, field).isascii()
        assert not getattr(ui.UNICODE, field).isascii()


def test_vt_mode_is_only_needed_on_windows() -> None:
    assert ui.enable_vt(TTY, platform="linux")
    # No console behind a StringIO: Windows falls back to no colour.
    assert not ui.enable_vt(TTY, platform="win32")


# ------------------------------------------------------------------ heading


def test_the_heading_is_one_line_without_the_mark() -> None:
    style = ui.Style(symbols=ui.UNICODE, utf8=True)
    assert ui.heading(style, "0.7.0a8", "new application") == [
        "◆ sandloom 0.7.0a8 · new application"
    ]
    assert ui.heading(ui.PLAIN, "0.7.0a8", "new application", mark=True) == [
        "* sandloom 0.7.0a8 - new application"
    ]


def test_the_mark_needs_utf8_and_a_big_enough_terminal() -> None:
    style = ui.Style(symbols=ui.UNICODE, utf8=True)
    lines = ui.heading(style, "0.7.0a8", "new application", width=80, mark=True)
    assert len(lines) == len(ui.MARK) == 16
    assert "sandloom 0.7.0a8" in lines[7]
    assert "new application" in lines[8]
    assert lines[1].strip() == ui.MARK[1].strip()  # braille by default
    blocks = ui.heading(style, "1", "x", width=80, mark=True, braille=False)
    assert blocks[1].strip() == ui.MARK_BLOCKS[1].strip()  # Windows consoles
    assert len(ui.heading(style, "1", "x", width=63, mark=True)) == 1
    assert len(ui.heading(style, "1", "x", width=80, height=29, mark=True)) == 1
    not_utf8 = ui.Style(symbols=ui.UNICODE, utf8=False)
    assert len(ui.heading(not_utf8, "1", "x", width=80, mark=True)) == 1


def test_the_mark_is_a_gradient_in_truecolor_and_the_accent_otherwise() -> None:
    true = ui.Style(depth=ui.TRUECOLOR, symbols=ui.UNICODE, utf8=True)
    lines = ui.heading(true, "1", "x", width=80, mark=True)
    assert "38;2;212;172;124" in lines[0]  # MARK_TOP
    assert "38;2;131;96;59" in lines[-1]  # MARK_BOTTOM
    basic = ui.Style(depth=ui.EXTENDED, symbols=ui.UNICODE, utf8=True)
    assert all(
        f"38;5;{ui.ACCENT_256}" in line
        for line in ui.heading(basic, "1", "x", width=80, mark=True)
    )


def test_errors_and_the_summary_read_without_colour() -> None:
    style = ui.Style(symbols=ui.UNICODE)
    assert ui.error_lines(style, "uv sync failed", "fix it") == (
        "✗ uv sync failed\n→ fix it"
    )
    text = ui.summary(style, "shop is ready", [("App", "http://x"), ("Next", "cd")])
    assert "  ✓ shop is ready" in text
    assert "  App    http://x" in text
    assert "\x1b" not in text


def test_a_step_line_without_motion_is_one_final_line() -> None:
    out = io.StringIO()
    step = ui.StepLine(ui.Style(symbols=ui.UNICODE), out, "uv sync").start()
    step.note("Resolved 120 packages")
    step.finish(ok=True)
    assert out.getvalue().startswith("  ✓ uv sync ")
    assert out.getvalue().endswith("s\n")
    assert "\x1b" not in out.getvalue()
    out = io.StringIO()
    ui.StepLine(ui.PLAIN, out, "uv sync").start().finish(ok=False)
    assert out.getvalue() == "  ! uv sync\n"


def test_a_step_line_with_motion_spins_and_shows_the_last_line() -> None:
    out = io.StringIO()
    style = ui.Style(motion=True, symbols=ui.UNICODE)
    step = ui.StepLine(style, out, "uv sync", interval=0.01).start()
    step.note("Resolved 120 packages")
    step._draw(1)
    step.finish(ok=True)
    text = out.getvalue()
    assert "\x1b[?25l" in text
    assert "⠙ uv sync" in text
    assert "· Resolved 120 packages" in text
    assert text.endswith("\n") and "\x1b[?25h" in text


def test_elapsed_and_shorten() -> None:
    assert ui.elapsed(0.42) == "0.4s"
    assert ui.elapsed(125) == "2m 05s"
    assert ui.shorten("abcdef", 4) == "abc…"
    assert ui.shorten("abc", 4) == "abc"
