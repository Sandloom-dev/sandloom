"""``schema & deploy``'s migration-chain checks on a database that takes its
schema from the models (spec: "SQLite app gaps, as built")."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import sandloom.core.testing.static_checks as checks
from sandloom.core.testing.migrations import MigrationChain

CORE = MigrationChain("core", None, Path("/nowhere/core"), "alembic_version")
NOTES = MigrationChain("notes", "app.modules.notes", Path("/nowhere/notes"), "v_n")
TAGS = MigrationChain("tags", "app.modules.tags", Path("/nowhere/tags"), "v_t")


def _descriptor(name: str, database: str) -> Any:
    return SimpleNamespace(name=name, database_instances=lambda: {database})


@pytest.fixture
def chains(monkeypatch: pytest.MonkeyPatch) -> Any:
    """notes on ``default`` (SQLite), tags on ``reports`` (PostgreSQL); both
    chains create ``shared_rows``; tags misses its ``tags_items`` table and
    creates ``tags_orphans``, which no model declares."""
    core = SimpleNamespace(
        module_descriptors=[
            _descriptor("app.modules.notes", "default"),
            _descriptor("app.modules.tags", "reports"),
        ]
    )
    monkeypatch.setattr(
        checks,
        "list_database_instances",
        lambda: {
            "default": "sqlite:///app_test.db",
            "reports": "postgresql+psycopg2://u@h/reports",
        },
    )
    monkeypatch.setattr(checks, "migration_chains", lambda _core: [CORE, NOTES, TAGS])
    created = {
        "core": {"outbox_messages"},
        "notes": {"shared_rows", "notes_gone"},
        "tags": {"shared_rows", "tags_orphans"},
    }
    monkeypatch.setattr(checks, "tables_created_by", lambda c: created[c.key])
    monkeypatch.setattr(
        checks,
        "model_tables",
        lambda _core: {
            "outbox_messages": CORE,
            "shared_rows": NOTES,
            "tags_items": TAGS,
        },
    )

    def uncovered(_core: Any, *, chains: Any) -> list[tuple[MigrationChain, str]]:
        problems = [
            (NOTES, "table notes_new uncovered"),
            (TAGS, "table tags_items uncovered"),
        ]
        return [p for p in problems if p[0].key in chains]

    monkeypatch.setattr(checks, "uncovered_by_chain", uncovered)
    return core


def test_the_chains_of_a_models_schema_database_skip_coverage_and_ownership(
    chains: Any,
) -> None:
    skipped = checks.skipped_chains(chains)
    # the core chain goes to every hosting database, one of them SQLite
    assert skipped == frozenset({"core", "notes"})
    problems = checks.chain_problems(chains, skipped=skipped)
    # tags (PostgreSQL) is checked in full; notes (SQLite) is not
    assert "table tags_items uncovered" in problems
    assert not any("notes_new" in p or "notes_gone" in p for p in problems)
    assert any("tags_orphans" in p and "no loaded model" in p for p in problems)
    # a table created by two chains is reported whatever the database: the
    # check reads the revisions only
    assert any("shared_rows is created by two chains" in p for p in problems)
    reason = checks.chain_check_skipped(chains)
    assert reason is not None and reason.startswith("chain(s) core, notes:")
    assert "SANDLOOM-B029" in reason


def test_every_chain_is_checked_where_every_database_runs_chains(
    chains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        checks,
        "list_database_instances",
        lambda: {"default": "postgresql+psycopg2://u@h/app"},
    )
    assert checks.skipped_chains(chains) == frozenset()
    assert checks.chain_check_skipped(chains) is None
    problems = checks.chain_problems(chains)
    assert "table notes_new uncovered" in problems
    assert any("notes_gone" in p for p in problems)


def test_the_summary_line_of_a_step_that_skipped_says_so() -> None:
    from sandloom.core.cli.run_tests import Outcome, Step, summary

    notes = iter(["migration chains skipped (SANDLOOM-T002)", None])
    steps = [
        Step("schema & deploy", call=lambda: 0, note=lambda: next(notes)),
        Step("ruff check", call=lambda: 0, note=lambda: next(notes)),
    ]
    outcomes = [Outcome(s, 0, 0.1, (s.note() if s.note else None) or "") for s in steps]
    lines = summary(outcomes).splitlines()
    schema = next(line for line in lines if "schema & deploy" in line)
    assert schema.strip().startswith("PASS")
    assert schema.endswith("(migration chains skipped (SANDLOOM-T002))")
    ruff = next(line for line in lines if "ruff check" in line)
    assert ruff.endswith("s")
