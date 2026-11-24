"""`sandloom migrate revision <module>` creates revisions in the module's OWN chain.

End to end against a throwaway Postgres database, in a subprocess so the
scaffolded module never touches this process's shared ``Base.metadata``:

1. generate a module (`sandloom add module`) into an importable temp package (an *app* module,
   ``revapp.revprobe`` — not under ``sandloom``), load it via ``SANDLOOM_MODULES``;
2. ``upgrade-module`` applies its generated baseline;
3. add a column to the model, ``revision --autogenerate`` writes a revision
   into the module's ``migrations/versions`` chained on the baseline, scoped to
   the module's tables only;
4. ``upgrade-module`` applies it: the column exists and the module's own
   version table (``alembic_version_revprobe``) is at the new head;
5. the same works for an app composition root (``SANDLOOM_COMPOSITION_ROOT``).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandloom.core.cli import add
from sandloom.core.migrations.urls import to_sync_url
from sandloom.core.tests._root_support import lean_root_infra

# Slow (alembic autogenerate + upgrade in subprocesses): skipped by the fast dev loop `pytest -m "not slow"`.
pytestmark = pytest.mark.slow

_DEFAULT_URL = (
    "postgresql+asyncpg://test_user:test_password@localhost:5433/sandloom_test"
)


def _async_url() -> str:
    raw = os.getenv("DATABASE_URL") or _DEFAULT_URL
    return raw.replace("@localhost:", "@127.0.0.1:")


@pytest.fixture()
def scratch_db() -> Iterator[str]:
    """A fresh database (async URL) dropped afterwards."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    base = make_url(_async_url()).set(drivername="postgresql+psycopg2")
    name = f"{base.database}_revcli"
    admin = create_engine(
        base.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    except Exception as exc:  # pragma: no cover  # noqa: BLE001 - test Postgres unreachable: skip
        pytest.skip(f"Postgres not reachable: {exc}")
    finally:
        admin.dispose()
    yield (
        base.set(drivername="postgresql+asyncpg", database=name).render_as_string(
            hide_password=False
        )
    )
    admin = create_engine(
        base.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    finally:
        admin.dispose()


def _env(
    tmp_path: Path, database_url: str, *, tenant_rls: bool = False, **extra: str
) -> dict[str, str]:
    """The child's environment; ``tenant_rls`` sets the option of the INFRA
    database instance the chain runner reads to decide on row-level security."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SANDLOOM_", "INFRA", "DATABASE_URL", "SERVICE_BINDINGS"))
    }
    env.update(
        {
            "ENVIRONMENT": "dev",
            "DATABASE_URL": database_url,
            # Never contacted by migrations; any well-formed DSN will do.
            "INFRA": json.dumps(
                {
                    "cache": {"default": {"url": "redis://127.0.0.1:6379/0"}},
                    "broadcast": {"default": {"url": "redis://127.0.0.1:6379/0"}},
                    "atomic_store": {"security": {"url": "redis://127.0.0.1:6379/0"}},
                    "taskqueue": {"default": {"broker": "redis://127.0.0.1:6379/1"}},
                }
            ),
            "JWT_SECRET": "revision-cli-test-secret-not-real-0123456789abcdef",
            "EVENT_BUS_BACKEND": "memory",
            "SANDLOOM_MODULES": '["revapp.revprobe"]',
            "PYTHONPATH": os.pathsep.join([str(tmp_path), *sys.path]),
        }
    )
    env.update(extra)
    if tenant_rls:
        infra = json.loads(env["INFRA"])
        infra["database"] = {"default": {"tenant_rls": True}}
        env["INFRA"] = json.dumps(infra)
    # A root that selects no driver (the lean one) gets them from INFRA.
    env["INFRA"] = lean_root_infra(env)
    return env


def _migrate_proc(
    tmp_path: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sandloom.core.cli.migrate", *args],
        cwd=tmp_path,  # no backend/.env: the env above is the whole config
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _migrate(tmp_path: Path, env: dict[str, str], *args: str) -> str:
    proc = _migrate_proc(tmp_path, env, *args)
    assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
    return proc.stdout


def _scaffold(tmp_path: Path, *, empty: bool = False) -> Path:
    pkg = tmp_path / "revapp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    where = add.Layout(
        modules_dir=pkg,
        modules_package="revapp",
        shared_dir=pkg / "shared",
        shared_package="revapp.shared",
        source_root=tmp_path,
    )
    add.add_module("revprobe", where, empty=empty)
    return pkg / "revprobe"


def _add_note_column(module_dir: Path) -> None:
    model = module_dir / "models" / "item.py"
    source = model.read_text()
    anchor = "    title: Mapped[str] = mapped_column(String(255))\n"
    assert anchor in source
    model.write_text(
        source.replace(
            anchor,
            anchor
            + "    note: Mapped[str | None] = mapped_column(String(50), nullable=True)\n",
        )
    )


def _columns_and_version(database_url: str) -> tuple[set[str], str]:
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(to_sync_url(database_url))
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("revprobe_items")}
        with engine.connect() as conn:
            version = conn.execute(
                text("SELECT version_num FROM alembic_version_revprobe")
            ).scalar_one()
        return cols, version
    finally:
        engine.dispose()


def _run_revision_flow(tmp_path: Path, env: dict[str, str], database_url: str) -> None:
    module_dir = _scaffold(tmp_path)
    _migrate(tmp_path, env, "upgrade-module", "revprobe", "--apply")
    cols, version = _columns_and_version(database_url)
    assert "note" not in cols and version == "0001_revprobe"

    _add_note_column(module_dir)
    out = _migrate(
        tmp_path,
        env,
        "revision",
        "revprobe",
        "-m",
        "add note",
        "--autogenerate",
    )
    # No --rev-id: the next number of the chain, not a random hash.
    created = module_dir / "migrations" / "versions" / "0002_revprobe_add_note.py"
    assert str(created) in out, out
    text = created.read_text()
    assert "down_revision" in text and "0001_revprobe" in text
    assert "0002_revprobe" in text
    assert "add_column" in text and "revprobe_items" in text and "note" in text
    # Scoped to the module's own tables: nothing about other chains' tables.
    assert "outbox_messages" not in text and "identity_" not in text

    _migrate(tmp_path, env, "upgrade-module", "revprobe", "--apply")
    cols, version = _columns_and_version(database_url)
    assert "note" in cols
    assert version == "0002_revprobe"


def test_revision_autogenerate_and_upgrade_for_app_module(
    tmp_path: Path, scratch_db: str
) -> None:
    _run_revision_flow(tmp_path, _env(tmp_path, scratch_db), scratch_db)


def test_revision_respects_composition_root(tmp_path: Path, scratch_db: str) -> None:
    """A downstream app's composition root (SANDLOOM_COMPOSITION_ROOT) is honoured."""
    (tmp_path / "revroot.py").write_text(
        "from sandloom.main import create_app\n\napp = create_app()\ncore = app.state.core\n"
    )
    env = _env(tmp_path, scratch_db, SANDLOOM_COMPOSITION_ROOT="revroot")
    _run_revision_flow(tmp_path, env, scratch_db)


def test_revision_unknown_module_fails_cleanly(tmp_path: Path) -> None:
    env = _env(tmp_path, _async_url(), SANDLOOM_MODULES="[]")
    if importlib.util.find_spec("sandloom_standard") is not None:
        # Every installed module composes: with the batteries (identity
        # issues tokens) only a root with a claim authorities map boots
        # (SANDLOOM-B035), the standard one.
        env["SANDLOOM_COMPOSITION_ROOT"] = "sandloom_standard.main"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "sandloom.core.cli.migrate",
            "revision",
            "nope",
            "-m",
            "x",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 1
    assert "Module not found: nope" in proc.stderr


def test_new_autogenerates_against_a_scratch_database(
    tmp_path: Path, scratch_db: str
) -> None:
    """`sandloom migrate new` needs no database at head: it compares the models
    with a scratch database where every chain is applied (G9). The configured
    database (here: empty, never migrated) is left untouched."""
    from sqlalchemy import create_engine, inspect

    module_dir = _scaffold(tmp_path)
    _add_note_column(module_dir)
    out = _migrate(
        tmp_path,
        _env(tmp_path, scratch_db),
        "new",
        "revprobe",
        "add note",
        "--rev-id",
        "0002_revprobe",
    )
    created = module_dir / "migrations" / "versions" / "0002_revprobe_add_note.py"
    assert str(created) in out, out
    text = created.read_text()
    assert "add_column" in text and "note" in text
    assert "create_table" not in text  # the baseline was applied to the scratch
    engine = create_engine(to_sync_url(scratch_db))
    try:
        assert "revprobe_items" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def _versions(module_dir: Path) -> list[str]:
    return sorted(p.name for p in (module_dir / "migrations" / "versions").glob("*.py"))


def test_new_refuses_when_no_model_changed(tmp_path: Path, scratch_db: str) -> None:
    """Nothing to write is not a revision: no empty file, a non-zero exit."""
    module_dir = _scaffold(tmp_path)
    proc = _migrate_proc(
        tmp_path, _env(tmp_path, scratch_db), "new", "revprobe", "nothing changed"
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "no changes detected" in proc.stderr
    assert _versions(module_dir) == ["0001_revprobe_baseline.py"]


def test_revision_autogenerate_refuses_when_no_model_changed(
    tmp_path: Path, scratch_db: str
) -> None:
    module_dir = _scaffold(tmp_path)
    env = _env(tmp_path, scratch_db)
    _migrate(tmp_path, env, "upgrade-module", "revprobe", "--apply")
    proc = _migrate_proc(
        tmp_path, env, "revision", "revprobe", "-m", "empty", "--autogenerate"
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "no changes detected" in proc.stderr
    assert _versions(module_dir) == ["0001_revprobe_baseline.py"]
    # Without --autogenerate the empty template is what was asked for.
    _migrate(tmp_path, env, "revision", "revprobe", "-m", "by hand")
    assert _versions(module_dir) == [
        "0001_revprobe_baseline.py",
        "0002_revprobe_by_hand.py",
    ]


def test_new_numbers_the_revision_and_warns_about_a_not_null_column(
    tmp_path: Path, scratch_db: str
) -> None:
    """A Python ``default=`` autogenerates ``add_column(nullable=False)`` with no
    SQL default: it fails on a table with rows while the drift gate passes."""
    module_dir = _scaffold(tmp_path)
    model = module_dir / "models" / "item.py"
    anchor = "    title: Mapped[str] = mapped_column(String(255))\n"
    status = '    status: Mapped[str] = mapped_column(String(20), default="new")\n'
    model.write_text(model.read_text().replace(anchor, anchor + status))
    proc = _migrate_proc(
        tmp_path, _env(tmp_path, scratch_db), "new", "revprobe", "add status"
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    created = module_dir / "migrations" / "versions" / "0002_revprobe_add_status.py"
    assert str(created) in proc.stdout
    assert "0002_revprobe" in created.read_text()
    assert (
        "warning: revprobe_items.status is added NOT NULL with no server_default"
        in proc.stderr
    )
    # A nullable column is no warning.
    _add_note_column(module_dir)
    proc = _migrate_proc(
        tmp_path, _env(tmp_path, scratch_db), "new", "revprobe", "add note"
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "0003_revprobe_add_note.py" in proc.stdout
    assert "warning" not in proc.stderr


_NOTE_MODEL = '''"""A table of the module's own."""

import uuid

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, TenantScoped, new_id


class RevprobeNote(TenantScoped, Base):
    __tablename__ = "revprobe_notes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255))
'''


def test_an_empty_module_gets_its_first_revision_from_migrate_new(
    tmp_path: Path, scratch_db: str
) -> None:
    """``add module --empty``, then a model, then ``migrate new``: the chain's
    first revision (``0001_<module>``) with the table and its row-level security;
    ``models/__init__.py`` imports nothing, the runner finds the new file. The
    revision applies, and a second ``migrate new`` has nothing to write."""
    from sqlalchemy import create_engine, inspect

    module_dir = _scaffold(tmp_path, empty=True)
    assert _versions(module_dir) == []
    (module_dir / "models" / "note.py").write_text(_NOTE_MODEL)
    env = _env(tmp_path, scratch_db)
    out = _migrate(tmp_path, env, "new", "revprobe", "add notes")
    created = module_dir / "migrations" / "versions" / "0001_revprobe_add_notes.py"
    assert str(created) in out, out
    text = created.read_text()
    assert 'revision: str = "0001_revprobe"' in text or "'0001_revprobe'" in text
    assert "down_revision" in text and "None" in text
    assert "create_table" in text and "revprobe_notes" in text
    assert 'enable_tenant_rls(op, "revprobe_notes", if_configured=True)' in text
    compile(text, str(created), "exec")
    _migrate(tmp_path, env, "upgrade-module", "revprobe", "--apply")
    engine = create_engine(to_sync_url(scratch_db))
    try:
        assert "revprobe_notes" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    proc = _migrate_proc(tmp_path, env, "new", "revprobe", "again")
    assert proc.returncode == 1 and "no changes detected" in proc.stderr
    assert _versions(module_dir) == ["0001_revprobe_add_notes.py"]


@pytest.mark.parametrize("tenant_rls", [True, False])
def test_the_baseline_enables_row_level_security_where_the_database_is_configured(
    tmp_path: Path, scratch_db: str, tenant_rls: bool
) -> None:
    """The scaffold's one-call baseline follows the INFRA database instance:
    ``tenant_rls`` on enables and forces row security, off leaves the table plain."""
    from sqlalchemy import create_engine, text

    _scaffold(tmp_path)
    env = _env(tmp_path, scratch_db, tenant_rls=tenant_rls)
    _migrate(tmp_path, env, "upgrade-module", "revprobe", "--apply")
    engine = create_engine(to_sync_url(scratch_db))
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'revprobe_items'"
                )
            ).one()
    finally:
        engine.dispose()
    assert tuple(row) == (tenant_rls, tenant_rls)
