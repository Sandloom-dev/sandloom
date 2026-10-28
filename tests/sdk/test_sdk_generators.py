"""``sandloom add`` and the 1.0 templates (spec §5.9, §9.1; ledger C-GEN-01..07,
G-SDK-04 / SANDLOOM-T009).

Every generator runs into a fresh ``sandloom new`` app; the result passes ruff,
ruff format, mypy --strict, ``sandloom check`` (the pending 1.0 checks included)
and the beginner-tier pin, and every one of the app's own tests passes.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter

from sandloom.core.archcheck import api10_rules
from sandloom.core.archcheck.manifest import load_rules
from sandloom.core.archcheck.runner import PENDING_RULES, run_checks
from sandloom.core.cli import add
from sandloom.core.cli.project import generate_project
from sandloom.sdk import Contract
from sandloom.sdk.testing import fake_contract
from tools.smoke import split_check

BACKEND = Path(__file__).resolve().parents[2]
SDK = BACKEND / "src" / "sandloom" / "sdk"
EXAMPLES = BACKEND / "examples"


def _app(tmp_path: Path, *, everything: bool = True) -> Path:
    generate_project("probe", tmp_path, sample_module="notes")
    root = tmp_path / "probe"
    if everything:
        where = add.layout(root)
        add.add_module("stats", where)
        add.add_event("notes", "NoteArchived", where, ["note_id:UUID", "reason:str"])
        add.add_contract("stats", "quota", where)
        add.add_consumer("stats", "notes.item.created.v1", where, [])
        add.add_saga("notes", "reserve", where)
        add.add_projection("stats", "board", where)
        add.add_task("stats", "purge", where, periodic="0 3 * * *")
        add.add_task("stats", "rollup", where, periodic="*/15 * * * *")
        add.add_task("notes", "reindex", where)
        code, output = add.run_generate(root)  # what `sandloom add` runs last
        assert code == 0, output
    return root


# --------------------------------------------------------------------------- #
# Behaviour of each generator
# --------------------------------------------------------------------------- #


def test_add_module_writes_only_inside_the_package(tmp_path: Path) -> None:
    root = _app(tmp_path, everything=False)
    before = {p for p in root.rglob("*") if p.is_file()}
    created = add.add_module("billing_notes", add.layout(root))
    after = {p for p in root.rglob("*") if p.is_file()}
    package = root / "probe" / "modules" / "billing_notes"
    assert after - before == set(created)
    assert all(package in p.parents for p in created)
    with pytest.raises(add.GeneratorError, match="already exists"):
        add.add_module("billing_notes", add.layout(root))
    with pytest.raises(add.GeneratorError, match="snake_case"):
        add.add_module("Bad", add.layout(root))


def test_add_module_lists_items_capped_and_tenant_scoped(tmp_path: Path) -> None:
    """``GET /items``: a capped ``limit`` (``Query(le=...)``) over the
    ``TenantScoped`` table, and a test that covers the cap and the tenant."""
    root = _app(tmp_path, everything=False)
    add.add_module("board", add.layout(root))
    package = root / "probe" / "modules" / "board"
    api = (package / "api.py").read_text()
    assert '@router.get("/items")\nasync def list_items(' in api
    assert "limit: Annotated[int, Query(ge=1, le=100)]" in api
    assert (
        "class BoardItem(TenantScoped, Base):"
        in (package / "models" / "item.py").read_text()
    )
    assert ".limit(limit)" in (package / "services.py").read_text()
    test = (package / "tests" / "test_board.py").read_text()
    assert "async def test_list_items_is_capped_and_tenant_scoped(" in test
    assert 'params={"limit": 101}' in test and "== 422" in test
    assert "foreign.json() == []" in test


def test_add_module_lists_newest_first_deterministically_in_one_transaction(
    tmp_path: Path,
) -> None:
    """Rows created in ONE transaction (the ``client`` fixture shares one) get
    distinct ``created_at`` and time-ordered ids: the model uses the
    framework's idiom (``created_at: Mapped[CreatedAt]``, which defaults to
    ``clock_timestamp()``, not the transaction timestamp ``now()``, and
    ``default=new_id``, a UUIDv7), the migration says ``clock_timestamp()``;
    the list orders by ``created_at`` then ``id``, and the generated test
    asserts the order."""
    root = _app(tmp_path, everything=False)
    add.add_module("board", add.layout(root))
    package = root / "probe" / "modules" / "board"
    model = (package / "models" / "item.py").read_text()
    assert "from sandloom.sdk import Base, CreatedAt, TenantScoped, new_id" in model
    assert "created_at: Mapped[CreatedAt]" in model
    assert "mapped_column(primary_key=True, default=new_id)" in model
    assert "func.now()" not in model and "uuid4" not in model
    (baseline,) = (package / "migrations" / "versions").glob("*.py")
    assert 'server_default=sa.text("clock_timestamp()")' in baseline.read_text()
    services = (package / "services.py").read_text()
    assert "newest = BoardItem.created_at.desc()" in services
    assert "tie = BoardItem.id.desc()" in services
    assert ".order_by(newest, tie)" in services
    test = (package / "tests" / "test_board.py").read_text()
    assert '[item["title"] for item in listed.json()] == ["c", "b", "a"]' in test


def test_add_module_in_the_framework_tree_is_wired_and_gets_the_kit(
    tmp_path: Path,
) -> None:
    """In the framework tree nothing is left to wire: the module joins
    LOCAL_MODULES and the monolith profile, and its tests get the kit's
    fixtures (client, published) through a one-line conftest."""
    backend = tmp_path
    (backend / "src" / "sandloom").mkdir(parents=True)
    (backend / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    main = backend / "src" / "sandloom" / "main.py"
    main.write_text('LOCAL_MODULES: list[str] = ["gadgets"]\n')
    (backend / ".env.example").write_text('SANDLOOM_MODULES=["sandloom_identity"]\n')
    where = add.Layout(
        modules_dir=backend / "src",
        modules_package="",
        shared_dir=backend / "src" / "sandloom" / "shared",
        shared_package="sandloom.shared",
        source_root=backend / "src",
    )
    touched = add.add_module("gizmos", where)
    assert 'LOCAL_MODULES: list[str] = ["gadgets", "gizmos"]' in main.read_text()
    assert '"gizmos"' in (backend / ".env.example").read_text()
    assert {main, backend / ".env.example"} <= set(touched)
    conftest = backend / "src" / "gizmos" / "tests" / "conftest.py"
    assert "from sandloom.sdk.testing.fixtures import *" in conftest.read_text()
    from sandloom.sdk.testing import fixtures

    assert "_sandloom_sdk_kit" in fixtures.__all__  # published() records


def test_add_task_writes_an_idempotent_task_and_its_schedule(tmp_path: Path) -> None:
    root = _app(tmp_path, everything=False)
    where = add.layout(root)
    created, note = add.add_task("notes", "purge", where, periodic="0 3 * * *")
    tasks = root / "probe" / "modules" / "notes" / "tasks.py"
    assert created == [tasks]
    add.add_task("notes", "rollup", where, periodic="*/15 * * * *")
    source = tasks.read_text()
    assert "from sandloom.sdk.tasks import cron, shared_task" in source
    assert "acks_late=True, reject_on_worker_lost=True" in source
    assert source.count("def setup_periodic_tasks(") == 1
    assert 'cron("0 3 * * *"), purge.s()' in source
    assert 'cron("*/15 * * * *"), rollup.s()' in source
    assert note and "sandloom add deploy" in note  # no microservices profile here
    with pytest.raises(add.GeneratorError, match="already defines"):
        add.add_task("notes", "purge", where)
    with pytest.raises(add.GeneratorError, match="5 cron fields"):
        add.add_task("notes", "nightly", where, periodic="daily")


def test_add_event_appends_and_refuses_an_unparsable_file(tmp_path: Path) -> None:
    root = _app(tmp_path, everything=False)
    where = add.layout(root)
    events = root / "probe" / "modules" / "notes" / "events.py"
    original = events.read_text()
    add.add_event("notes", "NoteShared", where, [])
    text = events.read_text()
    assert text.startswith(original.rstrip("\n"))
    assert 'class NoteShared(Event, channel="notes.note.shared.v1"):' in text
    assert "    note_id: UUID\n    tenant_id: UUID\n" in text
    with pytest.raises(add.GeneratorError, match="lacks"):
        add.add_event("notes", "NoteDated", where, ["on:datetime"])
    events.write_text("class Broken(\n")
    with pytest.raises(add.GeneratorError, match="does not parse"):
        add.add_event("notes", "NoteShared", where, [])
    with pytest.raises(add.GeneratorError, match="CamelCase"):
        add.add_event("notes", "note_shared", where, [])
    with pytest.raises(add.GeneratorError, match="name:type"):
        add.parse_fields(["bad:object"])


def test_add_consumer_takes_the_producer_schema_or_marks_unverified(
    tmp_path: Path,
) -> None:
    root = _app(tmp_path, everything=False)
    where = add.layout(root)
    add.add_module("stats", where)
    add.add_consumer("stats", "notes.item.created.v1", where, [])
    consumers = (root / "probe" / "modules" / "stats" / "consumers.py").read_text()
    assert "fields of the producer (notes/events.py)" in consumers
    assert "    item_id: UUID\n    tenant_id: UUID\n" in consumers
    assert (
        "@on(ItemCreated)\nasync def on_item_created(event: ItemCreated)" in consumers
    )
    with pytest.raises(add.GeneratorError, match="--field"):
        add.add_consumer("stats", "billing.payment.done.v1", where, [])
    add.add_consumer("stats", "billing.payment.done.v1", where, ["amount:int"])
    text = (root / "probe" / "modules" / "stats" / "consumers.py").read_text()
    assert "# unverified: no producer schema was found" in text


def test_an_existing_target_is_never_overwritten(tmp_path: Path) -> None:
    root = _app(tmp_path, everything=False)
    where = add.layout(root)
    sagas = root / "probe" / "modules" / "notes" / "sagas.py"
    sagas.write_text("# hand-written\n")
    created = add.add_saga("notes", "reserve", where)
    assert sagas.read_text() == "# hand-written\n"
    assert sagas.with_name("sagas.py.new") in created


async def test_split_smoke_calls_the_scaffolded_contract(tmp_path: Path) -> None:
    root = _app(tmp_path, everything=False)
    add.add_contract("notes", "lookup", add.layout(root))
    provider = importlib.import_module("probe.modules.notes.contracts").Lookup()
    ref = importlib.import_module(
        "probe.shared.contracts.notes_lookup_v1"
    ).NOTES_LOOKUP_V1
    namespace = {
        "__name__": __name__,
        "router": APIRouter(),
        "CurrentUser": Any,
        "lookup": Contract(ref),
    }
    exec(  # noqa: S102 - execute the repository-owned route fragment the split smoke installs
        split_check._ORDERS_CALL, namespace
    )
    with fake_contract(ref, provider):
        assert await namespace["catalog_ping"](None) == {"catalog": True}


def test_add_contract_writes_the_generated_stub(tmp_path: Path) -> None:
    """The provider in contracts.py, its DTO in the module's schemas.py (the
    provider authors it); ``sandloom generate`` (run by ``sandloom add``)
    writes the stub and the DTO's copy in the shared DTO package."""
    root = _app(tmp_path, everything=False)
    created = add.add_contract("notes", "lookup", add.layout(root))
    package = root / "probe" / "modules" / "notes"
    assert set(created) == {package / "schemas.py", package / "contracts.py"}
    assert "class LookupStatus(BaseModel):" in (package / "schemas.py").read_text()
    assert "from .schemas import LookupStatus" in (package / "contracts.py").read_text()
    stub = root / "probe" / "shared" / "contracts" / "notes_lookup_v1.py"
    code, output = add.run_generate(root)
    assert code == 0, output
    assert "notes_lookup_v1" not in output  # bootstrapped exactly as generated
    text = stub.read_text()
    assert "class NotesLookupV1(Protocol):" in text
    assert "async def record(self, amount: int, operation_key: str) -> int: ..." in text
    assert "async def status(self) -> LookupStatus: ..." in text
    assert "from probe.shared.contracts.notes_dto import LookupStatus" in text
    assert (
        "NOTES_LOOKUP_V1: ContractRef[NotesLookupV1] = ContractRef(\n"
        '    "notes.lookup.v1", NotesLookupV1\n)' in text
    )
    copy = (root / "probe" / "shared" / "contracts" / "notes_dto.py").read_text()
    assert copy.startswith("# Generated by `sandloom generate`")
    assert '@dto_copy("probe.modules.notes.schemas:LookupStatus")' in copy
    # A second contract of the module reuses the file and the import.
    add.add_contract("notes", "ledger", add.layout(root))
    contracts = (package / "contracts.py").read_text()
    assert "from .schemas import LedgerStatus, LookupStatus" in contracts
    assert contracts.count("from sandloom.sdk import command, contract") == 1
    code, output = add.run_generate(root)
    assert code == 0, output
    copy = (root / "probe" / "shared" / "contracts" / "notes_dto.py").read_text()
    assert "class LookupStatus(BaseModel):" in copy
    assert "class LedgerStatus(BaseModel):" in copy


def test_sandloom_add_runs_generate_at_the_end(tmp_path: Path) -> None:
    """``sandloom add`` leaves the generated files current (C-GEN-15)."""
    root = _app(tmp_path, everything=False)
    assert add.main(["--root", str(root), "contract", "notes", "lookup"]) == 0
    assert (root / "probe" / "shared" / "contracts" / "notes_lookup_v1.py").is_file()
    assert (root / "probe" / "shared" / "contracts" / "notes_dto.py").is_file()
    code, output = add.run_generate(root)
    assert code == 0 and "wrote" not in output, output  # nothing left to write


def test_add_projection_chains_its_migration_after_the_head(tmp_path: Path) -> None:
    root = _app(tmp_path, everything=False)
    created = add.add_projection("notes", "board", add.layout(root))
    migration = next(p for p in created if "versions" in p.parts)
    assert (
        'down_revision: str | Sequence[str] | None = "0001_notes"'
        in migration.read_text()
    )


def test_migrate_new_adds_row_level_security_to_new_tenant_tables(
    tmp_path: Path,
) -> None:
    """``sandloom migrate new`` post-processes the autogenerated revision (§5.6)."""
    from sandloom.core.cli import migrate_rls

    revision = tmp_path / "0002_x.py"
    revision.write_text(
        '"""x"""\n'
        "import sqlalchemy as sa\n"
        "from alembic import op\n\n\n"
        "def upgrade() -> None:\n"
        '    op.create_table("m_rows", sa.Column("id", sa.Uuid()))\n'
        '    op.create_table("m_global", sa.Column("id", sa.Uuid()))\n'
        '    op.create_table("m_sys", sa.Column("id", sa.Uuid()))\n\n\n'
        "def downgrade() -> None:\n"
        '    """Downgrade."""\n'
        '    op.drop_table("m_sys")\n'
        '    op.drop_table("m_global")\n'
        '    op.drop_table("m_rows")\n'
    )
    covered = migrate_rls.add_tenant_rls(revision, {"m_rows": False, "m_sys": True})
    assert covered == ["m_rows", "m_sys"]
    text = revision.read_text()
    compile(text, str(revision), "exec")
    assert migrate_rls.RLS_IMPORT in text
    assert 'enable_tenant_rls(op, "m_sys", system_access=True)' in text
    upgrade, downgrade = text.split("def downgrade")
    assert 'enable_tenant_rls(op, "m_rows")' in upgrade
    assert "m_global" not in text.split("enable_tenant_rls", 1)[1].split("def")[0]
    assert downgrade.index("disable_tenant_rls") < downgrade.index("op.drop_table")
    assert migrate_rls.add_tenant_rls(revision, {}) == []


# --------------------------------------------------------------------------- #
# The beginner-tier pin (SANDLOOM-T009)
# --------------------------------------------------------------------------- #


def _tier_problems(paths: list[Path]) -> list[str]:
    beginner = api10_rules.beginner_names(SDK)
    return [
        f"{py}: {problem}"
        for py in api10_rules.iter_python(paths)
        for problem in api10_rules.find_beginner_tier_problems(py, beginner)
    ]


def test_the_beginner_tier_is_the_29_names() -> None:
    """27 names of the 1.0 spec, plus ``new_id`` and ``CreatedAt``: the id
    and timestamp idiom every generated model uses (owner, 2026-10-27)."""
    beginner = api10_rules.beginner_names(SDK)
    code = beginner["sandloom.sdk"]
    testing = beginner["sandloom.sdk.testing"] | beginner["sandloom.sdk.testing.plugin"]
    assert len(code) == 23 and len(testing) == 6
    assert {"new_id", "CreatedAt"} <= code


def test_generated_code_uses_only_the_beginner_tier(tmp_path: Path) -> None:
    root = _app(tmp_path)
    assert _tier_problems([root]) == []


def test_examples_importing_the_sdk_stay_in_the_beginner_tier() -> None:
    """The pin covers every example file that imports ``sandloom.sdk`` (the
    examples migrate to it at hand-off H3)."""
    files = [
        py
        for py in api10_rules.iter_python([EXAMPLES])
        if "sandloom.sdk" in py.read_text(encoding="utf-8")
    ]
    assert _tier_problems(files) == []


def test_the_pin_reports_an_unmarked_advanced_name(tmp_path: Path) -> None:
    py = tmp_path / "m.py"
    py.write_text(
        "from sandloom.sdk import db, define_module\n"
        "from sandloom.sdk.web import DbSession\n"
        "from sandloom.sdk.data import open_uow  # sandloom: advanced a block unit\n"
        "import sandloom.sdk.tenant\n"
    )
    problems = _tier_problems([py])
    assert len(problems) == 2
    assert "DbSession" in problems[0] and "sandloom.sdk.tenant" in problems[1]


# --------------------------------------------------------------------------- #
# Generated apps are green
# --------------------------------------------------------------------------- #


def _run(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


@pytest.mark.slow
def test_a_fully_generated_app_passes_the_static_gates(tmp_path: Path) -> None:
    root = _app(tmp_path)
    python = sys.executable
    _run([python, "-m", "ruff", "check", "--no-fix", "--no-cache", "."], root)
    _run([python, "-m", "ruff", "format", "--check", "--no-cache", "."], root)
    env = {**os.environ, "MYPYPATH": str(root)}
    _run([python, "-m", "mypy", "--no-incremental", "probe"], root, env)
    rules = load_rules(root / "architecture_rules.yml")
    errors = [
        v.format(root) for v in run_checks(rules, pending=True) if v.severity == "error"
    ]
    assert errors == [], errors
    assert set(PENDING_RULES) <= {*api10_rules.SEVERITY}


@pytest.mark.slow
@pytest.mark.integration
def test_a_fully_generated_app_passes_its_tests_up_to_the_h1_stubs(
    tmp_path: Path,
) -> None:
    """Migrations apply on a fresh database and every test of the app passes
    (H1 landed: ``db()`` and model E are the kernel's; no failure allowed).
    The name is kept for the ledger (C-GEN-06)."""
    import psycopg2
    from sqlalchemy.engine import make_url

    from sandloom.core.testing.ownership import OWNER_ENV, current_owner
    from sandloom.core.testing.workers import worker_env_values

    env_test = worker_env_values(BACKEND / ".env.test")
    test_env = {**env_test, **os.environ}
    test_url = str(test_env.get("DATABASE_URL") or "")
    base = make_url(test_url.replace("+asyncpg", ""))
    if base.get_backend_name() != "postgresql":
        pytest.skip("needs the Postgres test service")
    dbname = f"{base.database}_gen_{uuid.uuid4().hex[:8]}"
    admin = {
        "host": base.host,
        "port": base.port,
        "user": base.username,
        "password": base.password,
        "dbname": base.database,
    }
    try:
        conn = psycopg2.connect(**admin)
    except psycopg2.OperationalError:
        pytest.skip("Postgres test service not reachable")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{dbname}"')
    conn.close()
    try:
        root = _app(tmp_path)
        url = base.set(database=dbname, drivername="postgresql+asyncpg")
        async_url = url.render_as_string(hide_password=False)
        lines = []
        for line in (root / ".env.test").read_text().splitlines():
            key = line.split("=", 1)[0]
            if key == "DATABASE_URL":
                line = f"DATABASE_URL={async_url}"
            elif key in ("CACHE_URL", "BROKER_URL") and test_env.get(key):
                line = f"{key}={test_env[key]}"
            lines.append(line)
        (root / ".env.test").write_text("\n".join(lines) + "\n")
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SANDLOOM_", "PYTEST_")) and k != "INFRA"
        }
        env[OWNER_ENV] = current_owner()
        migrate_env = {
            **env,
            "PYTHONPATH": str(root),
            "ENVIRONMENT": "test",
            "DATABASE_URL": async_url,
            "CACHE_URL": str(test_env.get("CACHE_URL") or ""),
            "BROKER_URL": str(test_env.get("BROKER_URL") or ""),
            "JWT_SECRET": "x" * 40,
            "SANDLOOM_COMPOSITION_ROOT": "probe.asgi",
            "CONTRACT_DTO_PACKAGES": json.dumps(["probe.shared"]),
            "EVENT_BUS_BACKEND": "memory",
        }
        _run(
            [
                sys.executable,
                "-m",
                "sandloom.core.cli.migrate",
                "upgrade-all",
                "--apply",
            ],
            root,
            migrate_env,
        )
        report = tmp_path / "report.xml"
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"--junitxml={report}"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )  # fmt: skip
        cases = re.findall(
            r"<testcase (.*?)(?:/>|>(.*?)</testcase>)", report.read_text(), re.S
        )
        passed = sum(
            1 for _, body in cases if "<failure" not in body and "<error" not in body
        )
        unexpected = [
            f"{attrs}: {body[:1500]}"
            for attrs, body in cases
            if "<failure" in body or "<error" in body
        ]
        assert unexpected == [], "\n".join(unexpected)
        assert passed == len(cases) and passed >= 4
    finally:
        conn = psycopg2.connect(**admin)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        conn.close()


@pytest.mark.slow
@pytest.mark.integration
def test_migration_drift_names_the_module_and_the_fix(tmp_path: Path) -> None:
    """``sandloom migrate drift`` (SANDLOOM-T006): a generated app is at its models; a
    model column without its revision fails naming the module and the fix.
    Only a scratch database is written (created and dropped by the check)."""
    from sqlalchemy.engine import make_url

    from sandloom.core.testing.ownership import OWNER_ENV, current_owner
    from sandloom.core.testing.workers import worker_env_values

    env_test = worker_env_values(BACKEND / ".env.test")
    test_env = {**env_test, **os.environ}
    test_url = str(test_env.get("DATABASE_URL") or "")
    if make_url(test_url or "sqlite://").get_backend_name() != "postgresql":
        pytest.skip("needs the Postgres test service")
    root = _app(tmp_path, everything=False)
    env = {
        **{
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SANDLOOM_", "PYTEST_")) and k != "INFRA"
        },
        OWNER_ENV: current_owner(),
        "PYTHONPATH": str(root),
        "ENVIRONMENT": "test",
        "DATABASE_URL": test_url,
        "CACHE_URL": str(test_env.get("CACHE_URL") or ""),
        "BROKER_URL": str(test_env.get("BROKER_URL") or ""),
        "JWT_SECRET": "x" * 40,
        "SANDLOOM_COMPOSITION_ROOT": "probe.asgi",
        "CONTRACT_DTO_PACKAGES": json.dumps(["probe.shared"]),
        "EVENT_BUS_BACKEND": "memory",
    }
    drift = [sys.executable, "-m", "sandloom.core.cli.migrate", "drift"]
    assert "migration drift: none (1 module chain(s)" in _run(drift, root, env)

    model = root / "probe" / "modules" / "notes" / "models" / "item.py"
    text = model.read_text()
    column = "    title: Mapped[str] = mapped_column(String(255))\n"
    assert column in text
    model.write_text(
        text.replace(
            column, column + "    color: Mapped[str] = mapped_column(String(16))\n"
        )
    )
    proc = subprocess.run(
        drift, cwd=root, env=env, capture_output=True, text=True, timeout=600
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "probe.modules.notes: models differ from the migration chain head" in out
    assert "[SANDLOOM-T006]" in out and "add_column" in out
    assert 'sandloom migrate new probe.modules.notes "<message>"' in out
