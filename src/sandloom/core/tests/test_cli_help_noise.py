""":program:`sandloom`: one-shot commands log quietly, ``-v`` brings the INFO lines
back, and ``--help`` groups the commands."""

from __future__ import annotations

import re
import sys

import pytest

import sandloom
from sandloom.core.cli.dev import errors


def _quiet(argv: list[str], environ: dict[str, str], *, verbose: bool = False) -> str:
    errors.quiet_logs(argv, environ, verbose=verbose)
    return environ.get("LOG_LEVEL", "")


@pytest.mark.parametrize(
    "command",
    ["migrate", "add", "generate", "test", "check", "explain", "doctor", "release"],
)
def test_a_one_shot_command_logs_warnings_only(command: str) -> None:
    assert _quiet(["sandloom", command, "new"], {}) == "WARNING"


@pytest.mark.parametrize("command", ["dev", "worker", "beat"])
def test_a_long_running_command_keeps_the_configured_level(command: str) -> None:
    assert _quiet(["sandloom", command], {}) == ""


def test_verbose_and_an_exported_level_win() -> None:
    assert _quiet(["sandloom", "migrate"], {}, verbose=True) == ""
    assert _quiet(["sandloom", "migrate"], {"LOG_LEVEL": "DEBUG"}) == "DEBUG"


def test_no_command_changes_nothing() -> None:
    assert _quiet(["sandloom"], {}) == ""
    assert _quiet(["sandloom", "--debug"], {}) == ""


def test_verbose_is_read_before_the_command_only() -> None:
    before = ["sandloom", "-v", "migrate", "new"]
    assert errors.verbose_requested(before) is True
    assert before == ["sandloom", "migrate", "new"]
    after = ["sandloom", "test", "-v"]
    assert errors.verbose_requested(after) is False
    assert after == ["sandloom", "test", "-v"]  # pytest's own -v


def _help(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> str:
    monkeypatch.setattr(sys, "argv", ["sandloom", "--help"])
    with pytest.raises(SystemExit) as stop:
        sandloom._main()
    assert stop.value.code == 0
    return capsys.readouterr().out


def test_help_lists_every_command_once_under_its_group(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _help(monkeypatch, capsys)
    headings = re.findall(r"^(\w+):$", text, flags=re.MULTILINE)
    assert headings == ["options", "start", "build", "operate"]
    listed = re.findall(r"^  (\w+)  ", text, flags=re.MULTILINE)
    expected = [name for _, entries in sandloom.COMMAND_GROUPS for name, _ in entries]
    assert listed == expected
    assert len(set(expected)) == len(expected) == 28
    assert "-v" in text


def test_every_dispatched_command_is_listed_and_every_listed_one_dispatched() -> None:
    """A command added to the dispatch without a group fails here."""
    import inspect

    source = inspect.getsource(sandloom._main)
    sets = " ".join(re.findall(r"args\.command in \{([^}]*)\}", source))
    dispatched = set(re.findall(r'args\.command == "(\w+)"', source))
    dispatched |= set(re.findall(r'"(\w+)"', sets))
    listed = {name for _, entries in sandloom.COMMAND_GROUPS for name, _ in entries}
    assert dispatched == listed


def test_no_explain_text_cites_a_spec_or_a_path_no_app_has() -> None:
    """An application has no ``spec.md`` and no ``tests/fixtures``: nothing
    `sandloom explain` prints may send its reader there."""
    from sandloom.core import error_codes

    for entry in error_codes.CATALOGUE:
        if entry.planned:  # a reserved code names its hand-off: not shown yet
            continue
        text = error_codes.explain_text(entry)
        assert "§" not in text, entry.code
        assert "tests/fixtures" not in text, entry.code


@pytest.mark.parametrize("code", ["SANDLOOM-C031", "SANDLOOM-C033", "SANDLOOM-B037"])
def test_the_long_texts_are_short_and_the_cause_comes_first(code: str) -> None:
    from sandloom.core import error_codes

    entry = error_codes.get(code)
    assert len(entry.explain.split()) <= 150, code
    # The first sentence of the explanation is not the context of a spec.
    assert "(spec" not in entry.explain


def test_t009_points_at_the_beginner_tier() -> None:
    from sandloom.core import error_codes

    text = error_codes.get("SANDLOOM-T009").explain
    assert "sandloom docs sandloom.sdk" in text


def test_admin_help_names_the_commands_the_docs_use() -> None:
    summary = dict(pair for _, entries in sandloom.COMMAND_GROUPS for pair in entries)[
        "admin"
    ]
    for word in ("create", "invite", "list", "export"):
        assert word in summary
