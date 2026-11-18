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


def _keyed(tmp_path: Path, key: str, revisions: dict[str, str]) -> MigrationChain:
    versions = tmp_path / key / "versions"
    versions.mkdir(parents=True)
    for name, source in revisions.items():
        (versions / name).write_text(source)
    return MigrationChain(key, f"app.{key}", tmp_path / key, f"alembic_version_{key}")


def _two_chains(
    monkeypatch: pytest.MonkeyPatch, a: MigrationChain, b: MigrationChain
) -> list[str]:
    monkeypatch.setattr(checks, "migration_chains", lambda _core: [a, b])
    monkeypatch.setattr(checks, "model_tables", lambda _core: {})
    monkeypatch.setattr(checks, "uncovered_by_chain", lambda _core, *, chains: [])
    return checks.chain_problems(object())


@pytest.mark.parametrize(
    "second",
    ["op.drop_table('t')", "op.rename_table('t', 't_old')"],
    ids=["dropped", "renamed away"],
)
def test_a_table_moved_away_from_one_chain_is_still_created_by_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second: str
) -> None:
    """Chain a creates ``t`` and then drops (or renames) it; chain b creates
    ``t``: which of the two runs first decides whether a fresh database
    migrates, so T002 reports it though a's end state has no ``t``."""
    a = _keyed(
        tmp_path,
        "a",
        {
            "0001.py": _revision("a1", None, "op.create_table('t')"),
            "0002.py": _revision("a2", "a1", second),
        },
    )
    b = _keyed(
        tmp_path, "b", {"0001.py": _revision("b1", None, "op.create_table('t')")}
    )
    assert "t" not in tables_created_by(a) and tables_created_by(b) == {"t"}
    problems = _two_chains(monkeypatch, a, b)
    assert any("table t is created by two chains" in p for p in problems), problems


def test_a_downgrade_that_recreates_a_moved_table_is_not_a_second_creator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only ``upgrade()`` counts for the disjointness check: a ``downgrade()``
    that puts back a table which moved to another module's chain is not a
    second creator."""
    a = _keyed(
        tmp_path,
        "a",
        {
            "0001.py": _revision("a1", None, "op.create_table('keep')"),
            "0002.py": _revision(
                "a2", "a1", "op.drop_table('t')", "op.create_table('t')"
            ),
        },
    )
    b = _keyed(
        tmp_path, "b", {"0001.py": _revision("b1", None, "op.create_table('t')")}
    )
    assert not [p for p in _two_chains(monkeypatch, a, b) if "two chains" in p]


def test_a_conditional_drop_table_does_not_drop(tmp_path: Path) -> None:
    """A ``drop_table`` under an ``if`` may not run, so it never clears the
    table; an unconditional top-level one does."""
    chain = _chain(
        tmp_path,
        {
            "0001.py": _revision("r1", None, "op.create_table('t')"),
            "0002.py": _revision(
                "r2",
                "r1",
                "if op.get_bind().dialect.name == 'sqlite':\n"
                "        op.drop_table('t')",
            ),
        },
    )
    assert tables_created_by(chain) == {"t"}


def test_a_list_form_down_revision_is_a_merge_not_a_root(tmp_path: Path) -> None:
    """``down_revision = ['r2', 'r3']`` merges like the tuple form: the merge
    sorts first by name but runs last, so its create comes after r2's drop."""
    chain = _chain(
        tmp_path,
        {
            "0_merge.py": _revision("r4", ["r2", "r3"], "op.create_table('t')"),
            "1.py": _revision("r1", None, "op.create_table('t')"),
            "2.py": _revision("r2", "r1", "op.drop_table('t')"),
            "3.py": _revision("r3", "r1", "op.create_table('u')"),
        },
    )
    assert tables_created_by(chain) == {"t", "u"}


def test_a_long_linear_chain_does_not_exhaust_the_recursion_limit(
    tmp_path: Path,
) -> None:
    count = 1500  # past the interpreter's default limit of 1000 frames
    revisions = {
        f"{n:05d}.py": _revision(
            f"r{n}", f"r{n - 1}" if n else None, f"op.create_table('t{n}')"
        )
        for n in range(count)
    }
    revisions["99999.py"] = _revision("last", f"r{count - 1}", "op.drop_table('t0')")
    found = tables_created_by(_chain(tmp_path, revisions))
    assert len(found) == count - 1 and "t0" not in found
