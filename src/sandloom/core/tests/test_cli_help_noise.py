""":program:`sandloom`: one-shot commands log quietly, ``-v`` brings the INFO lines
back, and ``--help`` groups the commands."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import sandloom
from sandloom.core.cli.dev import errors
from sandloom.core.config.settings import Settings, settings
from sandloom.core.observability import log_level
from sandloom.core.observability import logging as sandloom_logging


@pytest.fixture(autouse=True)
def _no_requested_level() -> Iterator[None]:
    log_level.request_default(None)
    yield
    log_level.request_default(None)


def _quiet(argv: list[str], *, verbose: bool = False) -> str | None:
    errors.quiet_logs(argv, verbose=verbose)
    return log_level.requested_default()


@pytest.mark.parametrize(
    "command",
    ["migrate", "add", "generate", "test", "check", "explain", "doctor", "release"],
)
def test_a_one_shot_command_logs_warnings_only(command: str) -> None:
    assert _quiet(["sandloom", command, "new"]) == "WARNING"


@pytest.mark.parametrize("command", ["dev", "worker", "beat"])
def test_a_long_running_command_keeps_the_configured_level(command: str) -> None:
    assert _quiet(["sandloom", command]) is None


def test_verbose_asks_for_no_default() -> None:
    assert _quiet(["sandloom", "migrate"], verbose=True) is None


def test_no_command_changes_nothing() -> None:
    assert _quiet(["sandloom"]) is None
    assert _quiet(["sandloom", "--debug"]) is None


def _settings(
    monkeypatch: pytest.MonkeyPatch, env_file: Path | None = None
) -> Settings:
    """A settings object as a process would build it: no ``LOG_LEVEL`` exported
    (a test sets one on purpose), ``env_file`` standing for the project's ``.env``."""
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    return Settings(
        _env_file=env_file,
        DATABASE_URL=settings.database_url,
        INFRA=settings.infra,
        JWT_SECRET=settings.jwt_secret,
    )


def test_the_one_shot_default_applies_only_while_no_level_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandloom_logging, "settings", _settings(monkeypatch))
    assert sandloom_logging.configured_level_name() == "INFO"
    log_level.request_default("WARNING")
    assert sandloom_logging.configured_level_name() == "WARNING"


def test_an_exported_level_beats_the_one_shot_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    cfg = Settings(
        _env_file=None,
        DATABASE_URL=settings.database_url,
        INFRA=settings.infra,
        JWT_SECRET=settings.jwt_secret,
    )
    monkeypatch.setattr(sandloom_logging, "settings", cfg)
    log_level.request_default("WARNING")
    assert sandloom_logging.configured_level_name() == "DEBUG"


def test_a_level_in_the_dotenv_file_beats_the_one_shot_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("LOG_LEVEL=info\n", encoding="utf-8")
    monkeypatch.setattr(sandloom_logging, "settings", _settings(monkeypatch, dotenv))
    log_level.request_default("WARNING")
    assert sandloom_logging.configured_level_name() == "INFO"


def test_asking_for_a_level_writes_no_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    before = dict(os.environ)
    errors.quiet_logs(["sandloom", "migrate", "new"], verbose=False)
    assert log_level.requested_default() == "WARNING"
    assert dict(os.environ) == before


def test_init_logging_follows_the_one_shot_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = logging.getLogger()
    before = root.level
    monkeypatch.setattr(sandloom_logging, "settings", _settings(monkeypatch))
    log_level.request_default("WARNING")
    try:
        sandloom_logging.init_logging()
        assert root.level == logging.WARNING
        log_level.request_default(None)
        sandloom_logging.init_logging()
        assert root.level == logging.INFO
    finally:
        root.setLevel(before)


def test_the_generate_child_asks_for_the_quiet_level() -> None:
    """``sandloom add`` runs ``generate`` in a child process: it is a one-shot
    like the command that started it."""
    from sandloom.core.cli import add

    opening = add._GENERATE[: add._GENERATE.index("root = ")]
    done = subprocess.run(
        [sys.executable, "-c", opening + "print(log_level.requested_default())"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.strip() == "WARNING"


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
    for module in (
        "sandloom.sdk",
        "sandloom.sdk.testing",
        "sandloom.sdk.testing.plugin",  # where the `client` fixture lives
    ):
        assert f"`sandloom docs {module}`" in text


def test_r014_offers_your_own_servers_as_well_as_dev_up() -> None:
    from sandloom.core import error_codes

    entry = error_codes.get("SANDLOOM-R014")
    assert "dev up" in entry.fix
    assert "set DATABASE_URL" in entry.fix


def test_admin_help_names_the_commands_the_docs_use() -> None:
    summary = dict(pair for _, entries in sandloom.COMMAND_GROUPS for pair in entries)[
        "admin"
    ]
    for word in ("create", "invite", "list", "export"):
        assert word in summary
