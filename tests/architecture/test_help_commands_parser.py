"""The ``packages`` CI job reads the commands of ``sandloom --help`` to run
``--help`` on each one in a clean install: its parser follows the grouped help
(``start`` / ``build`` / ``operate``) and refuses a help it cannot read."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

import sandloom

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "packaging" / "check_package_distributions.py"


def _tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_package_distributions", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _real_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    monkeypatch.setattr(sys, "argv", ["sandloom", "--help"])
    with pytest.raises(SystemExit):
        sandloom._main()
    return capsys.readouterr().out


def test_the_parser_reads_every_command_of_the_real_grouped_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found = _tool().help_commands(_real_help(monkeypatch, capsys))
    expected = [name for _, entries in sandloom.COMMAND_GROUPS for name, _ in entries]
    assert found == expected
    assert len(found) >= 20  # the floor the lean-install check also asks for


GROUPED = """\
usage: sandloom [-v] <command>

options:
  -h, --help  show this help message and exit

start:
  new   scaffold an application
  dev   local services

build:
  add   generate code
"""


def test_the_parser_ignores_the_options_block_and_keeps_group_order() -> None:
    assert _tool().help_commands(GROUPED) == ["new", "dev", "add"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("usage: sandloom <command>\n\n{new,dev,add}\n", "no command groups"),
        (GROUPED + "\noperate:\n", "list no command"),
        (GROUPED + "\noperate:\n  dev   again\n", "twice"),
    ],
)
def test_the_parser_refuses_a_help_it_cannot_read(text: str, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        _tool().help_commands(text)
