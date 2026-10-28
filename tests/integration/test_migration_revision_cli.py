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

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandloom.core.cli import add
from sandloom.core.migrations.urls import to_sync_url

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


def _env(tmp_path: Path, database_url: str, **extra: str) -> dict[str, str]:
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
            "CACHE_URL": os.getenv("CACHE_URL") or "redis://127.0.0.1:6379/0",
            "BROKER_URL": os.getenv("BROKER_URL") or "redis://127.0.0.1:6379/1",
            "JWT_SECRET": "revision-cli-test-secret-not-real-0123456789abcdef",
            "EVENT_BUS_BACKEND": "memory",
            "SANDLOOM_MODULES": '["revapp.revprobe"]',
            "PYTHONPATH": os.pathsep.join([str(tmp_path), *sys.path]),
        }
    )
    env.update(extra)
    return env


def _migrate(tmp_path: Path, env: dict[str, str], *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "sandloom.core.cli.migrate", *args],
        cwd=tmp_path,  # no backend/.env: the env above is the whole config
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
    return proc.stdout


def _scaffold(tmp_path: Path) -> Path:
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
    add.add_module("revprobe", where)
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
        "--rev-id",
        "0002_revprobe",
    )
    created = module_dir / "migrations" / "versions" / "0002_revprobe_add_note.py"
    assert str(created) in out, out
    text = created.read_text()
    assert "down_revision" in text and "0001_revprobe" in text
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
