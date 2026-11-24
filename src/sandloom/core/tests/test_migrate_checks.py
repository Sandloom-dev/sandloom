"""What ``sandloom migrate new`` / ``add`` write into a chain: numbered revision
ids, the NOT NULL warning, the models package the runner walks, and the hook that
turns an empty autogenerate into a refusal."""

from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest
from alembic import util as alembic_util
from alembic.config import Config
from alembic.operations import ops

from sandloom.core.cli.migrate import check_revision_ids
from sandloom.core.cli.migrate_checks import (
    duplicate_revision_problem,
    next_revision,
    not_null_warning,
    not_null_without_default,
)
from sandloom.core.cli.migrate_drift import find_drift
from sandloom.core.composition import load_composition_core
from sandloom.core.descriptors import ModuleDescriptor
from sandloom.core.migrations import alembic_support
from sandloom.core.migrations.migrations import MigrationDependencyError
from sandloom.core.testing.migrations import runs_no_chains

# --------------------------------------------------------------------------- #
# Numbered revision ids
# --------------------------------------------------------------------------- #


def test_next_revision_follows_the_highest_numbered_file(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    assert next_revision(versions, "orders") == "0001_orders"  # no directory yet
    versions.mkdir()
    assert next_revision(versions, "orders") == "0001_orders"  # an empty chain
    (versions / "0001_orders_baseline.py").write_text("")
    assert next_revision(versions, "orders") == "0002_orders"
    (versions / "0002_orders_add_status.py").write_text("")
    (versions / "0005_orders_gap.py").write_text("")
    assert next_revision(versions, "orders") == "0006_orders"
    (versions / "__init__.py").write_text("")  # not a revision
    assert next_revision(versions, "orders") == "0006_orders"


def test_next_revision_counts_the_files_of_a_hash_chain(tmp_path: Path) -> None:
    """A chain written with random ids (before the numbered convention) goes on
    with the next number, never with a number that collides."""
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "32513109912d_add_status.py").write_text("")
    (versions / "9f0c1e7ab4d2_add_note.py").write_text("")
    assert next_revision(versions, "orders") == "0003_orders"


def _revision_file(versions: Path, name: str, revision: str, down: str | None) -> None:
    """A revision file the way alembic's template writes it (annotated)."""
    versions.mkdir(parents=True, exist_ok=True)
    (versions / name).write_text(
        f'"""{name}"""\n'
        "from typing import Union\n\n"
        f'revision: str = "{revision}"\n'
        f"down_revision: Union[str, None] = {down!r}\n\n\n"
        "def upgrade() -> None:\n    pass\n"
    )


def test_two_files_with_one_revision_id_are_refused_with_the_renumbering(
    tmp_path: Path,
) -> None:
    """Two branches that each added ``0002_orders``: Alembic keeps one file."""
    versions = tmp_path / "versions"
    _revision_file(versions, "0001_orders_baseline.py", "0001_orders", None)
    _revision_file(versions, "0002_orders_add_status.py", "0002_orders", "0001_orders")
    assert duplicate_revision_problem(versions, "orders") is None
    _revision_file(versions, "0002_orders_add_note.py", "0002_orders", "0001_orders")
    problem = duplicate_revision_problem(versions, "orders")
    assert problem is not None
    assert "'0002_orders' in 2 files" in problem
    # The later file by name is the one to renumber, after the chain's highest.
    assert "renumber 0002_orders_add_status.py to 0003_orders" in problem
    assert 'revision = "0003_orders" and down_revision = "0002_orders"' in problem
    assert "renumber 0002_orders_add_note.py" not in problem


def test_three_files_with_one_id_are_renumbered_one_after_the_other(
    tmp_path: Path,
) -> None:
    versions = tmp_path / "versions"
    for name in ("a", "b", "c"):
        _revision_file(versions, f"0001_orders_{name}.py", "0001_orders", None)
    problem = duplicate_revision_problem(versions, "orders")
    assert problem is not None
    assert "renumber 0001_orders_b.py to 0002_orders" in problem
    assert 'down_revision = "0001_orders")' in problem
    assert "renumber 0001_orders_c.py to 0003_orders" in problem
    assert 'down_revision = "0002_orders")' in problem


def test_a_plain_assignment_and_a_non_revision_file_are_read_the_same(
    tmp_path: Path,
) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "__init__.py").write_text("")
    (versions / "0001_orders_a.py").write_text('revision = "0001_orders"\n')
    (versions / "0001_orders_b.py").write_text('revision = "0001_orders"\n')
    problem = duplicate_revision_problem(versions, "orders")
    assert problem is not None and "renumber 0001_orders_b.py" in problem
    (versions / "0001_orders_b.py").write_text('revision = "0002_orders"\n')
    assert duplicate_revision_problem(versions, "orders") is None
    assert duplicate_revision_problem(tmp_path / "absent", "orders") is None


def test_the_migrate_command_refuses_a_chain_with_a_duplicate_revision_id(
    tmp_path: Path,
) -> None:
    """The check ``sandloom migrate`` (and the migration step of ``sandloom test``,
    which runs ``migrate drift``) runs before any command: a chain with a
    duplicate id raises, a sound one does not."""
    versions = tmp_path / "migrations" / "versions"
    _revision_file(versions, "0001_orders_baseline.py", "0001_orders", None)
    descriptor = ModuleDescriptor(
        name="orders", version="1", migrations_path=str(tmp_path / "migrations")
    )
    check_revision_ids([descriptor])
    _revision_file(versions, "0001_orders_other.py", "0001_orders", None)
    with pytest.raises(MigrationDependencyError, match="renumber 0001_orders_other.py"):
        check_revision_ids([descriptor])
    # A module with no chain is not looked at.
    check_revision_ids([ModuleDescriptor(name="plain", version="1")])


# --------------------------------------------------------------------------- #
# NOT NULL without a server default
# --------------------------------------------------------------------------- #

_REVISION = '''"""add columns"""
import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.add_column("orders", sa.Column("status", sa.String(20), nullable=False))
    op.add_column(
        "orders",
        sa.Column("kind", sa.String(20), server_default="plain", nullable=False),
    )
    op.add_column("orders", sa.Column("note", sa.String(20), nullable=True))
    op.add_column("orders", sa.Column("flag", sa.Boolean()))
    op.add_column("orders", sa.Column("paid", sa.Boolean(), nullable=False))
    op.create_table("fresh", sa.Column("c", sa.Integer(), nullable=False))


def downgrade() -> None:
    op.add_column("orders", sa.Column("gone", sa.String(20), nullable=False))
    op.drop_column("orders", "status")
'''


def test_not_null_without_default_names_only_the_columns_that_fail(
    tmp_path: Path,
) -> None:
    """A table with rows rejects ``ADD COLUMN ... NOT NULL`` with no SQL default:
    the NOT NULL columns of ``upgrade()`` with no ``server_default`` are named;
    a nullable one, one with a default, a new table's column and what
    ``downgrade()`` re-adds are not."""
    revision = tmp_path / "0002_orders_add.py"
    revision.write_text(_REVISION)
    assert not_null_without_default(revision) == ["orders.status", "orders.paid"]


def test_a_revision_without_such_a_column_has_no_warning(tmp_path: Path) -> None:
    revision = tmp_path / "0002_orders_drop.py"
    revision.write_text(
        "import sqlalchemy as sa\nfrom alembic import op\n\n\n"
        "def upgrade() -> None:\n"
        '    op.drop_column("orders", "status")\n\n\n'
        "def downgrade() -> None:\n"
        '    op.add_column("orders", sa.Column("status", sa.String(20)))\n'
    )
    assert not_null_without_default(revision) == []


def test_the_warning_names_the_column_and_both_fixes() -> None:
    warning = not_null_warning("orders.status")
    assert warning.startswith("orders.status is added NOT NULL")
    assert "server_default" in warning and "backfill" in warning


# --------------------------------------------------------------------------- #
# The runner walks the models package
# --------------------------------------------------------------------------- #


def _forget(prefix: str) -> None:
    for name in [m for m in sys.modules if m == prefix or m.startswith(prefix + ".")]:
        del sys.modules[name]


class _ReachedConfig(Exception):
    """``run_migrations`` imports the models first, then reads the alembic config."""


class _ContextStoppingAtConfig:
    @property
    def config(self) -> Any:
        raise _ReachedConfig


def _import_the_models_of(monkeypatch: pytest.MonkeyPatch, *model_modules: str) -> None:
    """Run the model-import step of ``run_migrations``, stopped before it needs
    a database."""
    monkeypatch.setattr(alembic_support, "context", _ContextStoppingAtConfig())
    with pytest.raises(_ReachedConfig):
        alembic_support.run_migrations(model_modules=model_modules)


def test_the_runner_walks_every_module_below_the_models_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``models/__init__.py`` imports nothing; a model file anywhere below it
    still joins the metadata, as ``define_module`` walks the package."""
    models = tmp_path / "walkedpkg" / "models"
    (models / "nested").mkdir(parents=True)
    for init in (tmp_path / "walkedpkg", models, models / "nested"):
        (init / "__init__.py").write_text("")
    (models / "item.py").write_text("")
    (models / "nested" / "deep.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        _import_the_models_of(monkeypatch, "walkedpkg.models")
        assert "walkedpkg.models.item" in sys.modules
        assert "walkedpkg.models.nested.deep" in sys.modules
    finally:
        _forget("walkedpkg")


def test_the_runner_skips_tests_and_migrations_below_the_models_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same two skips as ``define_module``'s walk: a ``tests`` or a
    ``migrations`` package below ``models`` is not a model module."""
    models = tmp_path / "skippkg" / "models"
    for directory in ("tests", "migrations", "nested/tests", "nested"):
        (models / directory).mkdir(parents=True, exist_ok=True)
    for init in (tmp_path / "skippkg", models, *models.rglob("*")):
        if init.is_dir():
            (init / "__init__.py").write_text("")
    (models / "item.py").write_text("")
    (models / "nested" / "deep.py").write_text("")
    for skipped in (
        models / "tests" / "test_item.py",
        models / "migrations" / "env.py",
        models / "nested" / "tests" / "test_deep.py",
    ):
        skipped.write_text("raise RuntimeError('imported by the runner')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        _import_the_models_of(monkeypatch, "skippkg.models")
        assert "skippkg.models.item" in sys.modules
        assert "skippkg.models.nested.deep" in sys.modules
        # (the walk itself imports a package to look inside it, like define_module's;
        # the skipped files raise on import, so the run above would have failed)
        for skipped in (
            "skippkg.models.tests.test_item",
            "skippkg.models.migrations.env",
            "skippkg.models.nested.tests.test_deep",
        ):
            assert skipped not in sys.modules
    finally:
        _forget("skippkg")


def test_the_runner_takes_a_plain_models_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "plainmodels.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        _import_the_models_of(monkeypatch, "plainmodels")
        assert "plainmodels" in sys.modules
    finally:
        _forget("plainmodels")


def test_a_missing_models_module_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ModuleNotFoundError):
        _import_the_models_of(monkeypatch, "sandloom_no_such_models")


# --------------------------------------------------------------------------- #
# An autogenerate with nothing to write
# --------------------------------------------------------------------------- #


#: The hook ignores the migration context; alembic passes it, the test has none.
_NO_CONTEXT: Any = None


def _script(*operations: Any) -> ops.MigrationScript:
    return ops.MigrationScript(
        "rev", ops.UpgradeOps(ops=list(operations)), ops.DowngradeOps(ops=[])
    )


def test_an_empty_autogenerate_writes_no_revision_when_refusing() -> None:
    config = Config()
    config.attributes[alembic_support.REFUSE_EMPTY_AUTOGENERATE] = True
    hook = alembic_support._drop_empty_autogenerate(config)
    directives = [_script()]
    hook(_NO_CONTEXT, None, directives)
    assert directives == []
    assert config.attributes[alembic_support.EMPTY_AUTOGENERATE] is True


def _alembic_operator_clause_warning(expression: str) -> None:
    """Alembic's own emission (``alembic/ddl/postgresql.py``), as the index
    comparison makes it for a ``search_index`` operand."""
    alembic_util.warn(
        f"Expression #1 {expression!r} in index 'ix_users_search_trgm' detected "
        "to include an operator clause. Expression compare cannot proceed. "
        "Please move the operator clause to the ``postgresql_ops`` dict to "
        "enable proper compare of the index expressions"
    )


def test_only_the_operator_clause_warning_is_ignored() -> None:
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        with alembic_support.ignore_operator_clause_warning():
            _alembic_operator_clause_warning("full_name gin_trgm_ops")
            _alembic_operator_clause_warning("(email::text) gin_trgm_ops")
            alembic_util.warn("some other alembic warning")
        _alembic_operator_clause_warning("outside gin_trgm_ops")
    assert [str(w.message).split(" in index")[0] for w in seen] == [
        "some other alembic warning",
        "Expression #1 'outside gin_trgm_ops'",
    ]


@pytest.mark.skipif(
    runs_no_chains(os.environ.get("DATABASE_URL", "")),
    reason="needs PostgreSQL: SQLite runs no chain (SANDLOOM-B028)",
)
def test_the_chain_check_of_a_search_index_prints_no_warning() -> None:
    """``sandloom migrate drift`` (``alembic check`` per chain) over identity,
    whose ``ix_users_search_trgm`` is a three-operand ``search_index``: Alembic
    warned once per operand."""
    loaded = load_composition_core().module_descriptors
    descriptors = [d for d in loaded if d.name == "sandloom_identity"]
    assert descriptors, f"identity is not loaded; loaded: {[d.name for d in loaded]}"
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        drifts = find_drift(descriptors)
    assert drifts == []
    assert [str(w.message) for w in seen if "operator clause" in str(w.message)] == []


def test_a_revision_with_changes_is_kept() -> None:
    config = Config()
    config.attributes[alembic_support.REFUSE_EMPTY_AUTOGENERATE] = True
    hook = alembic_support._drop_empty_autogenerate(config)
    script = _script(ops.DropTableOp("gone"))
    directives = [script]
    hook(_NO_CONTEXT, None, directives)
    assert directives == [script]
    assert not config.attributes.get(alembic_support.EMPTY_AUTOGENERATE, False)


def test_a_hand_written_revision_stays_an_empty_template() -> None:
    """Not an autogenerate (``sandloom migrate revision`` without
    ``--autogenerate``): the empty template is what was asked for."""
    config = Config()
    hook = alembic_support._drop_empty_autogenerate(config)
    script = _script()
    directives = [script]
    hook(_NO_CONTEXT, None, directives)
    assert directives == [script]
    assert not config.attributes.get(alembic_support.EMPTY_AUTOGENERATE, False)
