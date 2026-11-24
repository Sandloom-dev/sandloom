""":program:`sandloom migrate` prints its own results.

A one-shot command logs WARNING and above, and the deploy one-shots
(``migrate upgrade-all --apply``) are read from their output: what a command did,
or would do, is a printed line, never an INFO log line a quiet log hides.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.cli import migrate
from sandloom.core.descriptors import ModuleDescriptor
from sandloom.core.migrations import migrations

URL = "postgresql://app:s3cret-pw@db.internal:5432/app"


@contextmanager
def _no_lock(urls: Any, *, timeout: float | None = None) -> Iterator[None]:
    yield


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrate, "migration_lock", _no_lock)
    monkeypatch.setattr(migrate, "resolve_database_sync_url", lambda name: URL)
    monkeypatch.setattr(migrate, "_uncovered_refusal", lambda *a, **k: None)
    monkeypatch.setattr(migrate, "run_core_upgrade", lambda **_: None)
    monkeypatch.setattr(migrate, "ensure_core_infra_tables", lambda *a, **k: None)


def _run(argv: list[str], core: Any = None, descriptors: Any = ()) -> None:
    args: argparse.Namespace = migrate._build_parser().parse_args(argv)
    assert migrate._COMMANDS[args.cmd](core, list(descriptors), args) == 0


def _module(name: str = "myapp.modules.billing") -> ModuleDescriptor:
    return ModuleDescriptor(name=name, version="1.0", migrations_path="unused")


def test_upgrade_core_dry_run_says_what_it_would_do(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(["upgrade-core"])
    out = capsys.readouterr().out
    assert "would upgrade the core chain to head on" in out
    assert "dry run: nothing was changed; add --apply to run it" in out
    assert "s3cret-pw" not in out  # the URL is masked, as in every migrate line


def test_upgrade_core_apply_says_it_upgraded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(["upgrade-core", "--apply"])
    out = capsys.readouterr().out
    assert "core chain upgraded to head on" in out
    assert "would" not in out and "dry run" not in out
    assert "s3cret-pw" not in out


@pytest.mark.parametrize(
    ("flags", "expected", "dry"),
    [
        ([], "would ensure the core infra tables on", True),
        (["--apply"], "ensured the core infra tables on", False),
    ],
)
def test_ensure_outbox_reports_its_result(
    capsys: pytest.CaptureFixture[str], flags: list[str], expected: str, dry: bool
) -> None:
    _run(["ensure-outbox", *flags])
    out = capsys.readouterr().out
    assert expected in out
    assert ("dry run:" in out) is dry
    assert "s3cret-pw" not in out


@pytest.mark.parametrize(
    ("flags", "line"),
    [
        ([], "module billing: would upgrade to head on database default"),
        (["--apply"], "module billing: upgraded to head on database default"),
    ],
)
def test_upgrade_module_names_the_module_and_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    line: str,
) -> None:
    monkeypatch.setattr(migrate, "upgrade_module", lambda *a, **k: ["default"])
    _run(["upgrade-module", "billing", *flags], descriptors=[_module()])
    assert line in capsys.readouterr().out


def test_upgrade_module_without_a_chain_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(migrate, "upgrade_module", lambda *a, **k: [])
    _run(["upgrade-module", "billing", "--apply"], descriptors=[_module()])
    out = capsys.readouterr().out
    assert "module billing: no migration chain, nothing to do" in out
    assert "upgraded" not in out


def test_upgrade_all_prints_every_database_then_every_module_in_order(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(migrate, "_hosting_databases", lambda d: ["default", "stats"])
    monkeypatch.setattr(
        migrate,
        "upgrade_all_modules",
        lambda *a, **k: {
            "myapp.modules.users": ["default"],
            "myapp.modules.hits": ["stats"],
        },
    )
    _run(["upgrade-all", "--apply"])
    lines = capsys.readouterr().out.splitlines()
    assert [line.split(" on ")[0] for line in lines[:2]] == [
        "core chain upgraded to head"
    ] * 2
    assert lines[2:] == [
        "module users: upgraded to head on database default",
        "module hits: upgraded to head on database stats",
    ]


def test_upgrade_all_dry_run_is_a_plan_and_says_it_changed_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(migrate, "_hosting_databases", lambda d: ["default"])
    monkeypatch.setattr(
        migrate,
        "upgrade_all_modules",
        lambda *a, **k: {"myapp.modules.users": ["default"]},
    )
    _run(["upgrade-all"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("would upgrade the core chain to head on")
    assert lines[1] == "module users: would upgrade to head on database default"
    assert lines[2].startswith("dry run:")


def test_the_library_reports_the_databases_it_upgraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``upgrade_module`` / ``upgrade_all_modules`` return what ran: the lines
    above are built from it, so a module that was skipped is not reported."""
    chain = tmp_path / "migrations"
    (chain / "versions").mkdir(parents=True)
    with_chain = ModuleDescriptor(
        name="m.with_chain", version="1", migrations_path=str(chain)
    )
    without = ModuleDescriptor(name="m.without", version="1")
    assert migrations.upgrade_module(with_chain, dry_run=True) == ["default"]
    assert migrations.upgrade_module(without, dry_run=True) == []
    assert migrations.upgrade_all_modules([with_chain, without], dry_run=True) == {
        "m.with_chain": ["default"]
    }
    seen: list[str] = []
    monkeypatch.setattr(migrations, "_has_env_script", lambda path: True)
    monkeypatch.setattr(migrations, "resolve_database_sync_url", lambda name: URL)
    from alembic import command

    monkeypatch.setattr(command, "upgrade", lambda cfg, rev: seen.append(rev))
    assert migrations.upgrade_module(with_chain, "abc123", dry_run=False) == ["default"]
    assert seen == ["abc123"]
