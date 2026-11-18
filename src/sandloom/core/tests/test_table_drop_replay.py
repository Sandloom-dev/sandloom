"""SANDLOOM-T002 reads a chain as Alembic runs it: ``drop_table`` and
``rename_table`` of a later revision are honoured, in ``down_revision`` order."""

from pathlib import Path

import pytest

from sandloom.core.testing import static_checks as checks
from sandloom.core.testing.migrations import MigrationChain, tables_created_by


def _chain(tmp_path: Path, revisions: dict[str, str]) -> MigrationChain:
    versions = tmp_path / "versions"
    versions.mkdir(parents=True)
    for name, source in revisions.items():
        (versions / name).write_text(source)
    return MigrationChain("notes", "app.notes", tmp_path, "alembic_version_notes")


def _revision(rev: str, down: str | None, upgrade: str, downgrade: str = "pass") -> str:
    return (
        f"revision = {rev!r}\ndown_revision = {down!r}\n"
        f"def upgrade():\n    {upgrade}\n"
        f"def downgrade():\n    {downgrade}\n"
    )


def test_a_later_drop_table_removes_the_table(tmp_path: Path) -> None:
    chain = _chain(
        tmp_path,
        {
            "0001_create.py": _revision(
                "r1",
                None,
                "op.create_table('notes_items')\n    op.create_table('keep')",
            ),
            "0002_drop.py": _revision(
                "r2",
                "r1",
                "op.drop_table('notes_items')",
                "op.create_table('notes_items')",
            ),
        },
    )
    assert tables_created_by(chain) == {"keep"}


def test_revisions_replay_in_down_revision_order_not_name_order(
    tmp_path: Path,
) -> None:
    """The drop sorts first by file name but runs second: the table is gone;
    a create that runs after the drop brings it back."""
    dropped = _chain(
        tmp_path / "a",
        {
            "0001_drop.py": _revision("r2", "r1", "op.drop_table('t')"),
            "0002_create.py": _revision("r1", None, "op.create_table('t')"),
        },
    )
    assert tables_created_by(dropped) == set()
    recreated = _chain(
        tmp_path / "b",
        {
            "0001_create.py": _revision("r2", "r1", "op.create_table('t')"),
            "0002_drop.py": _revision("r1", None, "op.drop_table('t')"),
        },
    )
    assert tables_created_by(recreated) == {"t"}


def test_rename_table_moves_the_name(tmp_path: Path) -> None:
    chain = _chain(
        tmp_path,
        {
            "0001.py": _revision("r1", None, "op.create_table('old_name')"),
            "0002.py": _revision(
                "r2", "r1", "op.rename_table('old_name', new_table_name='new_name')"
            ),
        },
    )
    assert tables_created_by(chain) == {"new_name"}


def test_a_merge_revision_runs_after_both_parents(tmp_path: Path) -> None:
    chain = _chain(
        tmp_path,
        {
            "0001.py": _revision("r1", None, "op.create_table('t')"),
            "0002.py": _revision("r2", "r1", "op.create_table('u')"),
            "0003.py": _revision("r3", "r1", "op.drop_table('t')"),
            "0004.py": "revision = 'r4'\ndown_revision = ('r2', 'r3')\n"
            "def upgrade():\n    pass\n",
        },
    )
    assert tables_created_by(chain) == {"u"}


def test_a_dropped_table_no_model_declares_is_not_a_t002_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The advice of T002 ("add a revision that drops it") clears it."""
    chain = _chain(
        tmp_path,
        {"0001.py": _revision("r1", None, "op.create_table('servers_items')")},
    )
    monkeypatch.setattr(checks, "migration_chains", lambda _core: [chain])
    monkeypatch.setattr(checks, "model_tables", lambda _core: {})
    monkeypatch.setattr(checks, "uncovered_by_chain", lambda _core, *, chains: [])
    [problem] = checks.chain_problems(object())
    assert "servers_items" in problem and "SANDLOOM-T002" in problem

    (tmp_path / "versions" / "0002.py").write_text(
        _revision("r2", "r1", "op.drop_table('servers_items')")
    )
    assert checks.chain_problems(object()) == []


def test_an_unreadable_revision_raises(tmp_path: Path) -> None:
    """A revision that is not Python is an error, never an empty chain."""
    chain = _chain(tmp_path, {"0001.py": "def upgrade(:\n"})
    with pytest.raises(SyntaxError):
        tables_created_by(chain)
