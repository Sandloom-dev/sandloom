"""What ``sandloom migrate new`` / ``add`` write into a chain: numbered revision
ids, the NOT NULL warning, the models package the runner walks, and the hook that
turns an empty autogenerate into a refusal."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.operations import ops

from sandloom.core.cli.migrate_checks import (
    next_revision,
    not_null_warning,
    not_null_without_default,
)
from sandloom.core.migrations import alembic_support

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
