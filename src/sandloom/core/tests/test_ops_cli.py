"""Operator tooling: migration-lock diagnostics (C-05), ``sandloom config dump``
(H-22) and ``sandloom doctor`` (H-08)."""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from sandloom.core.cli import config as config_cli
from sandloom.core.cli import doctor
from sandloom.core.config.settings import Settings
from sandloom.core.descriptors import ContractRequirement, ModuleDescriptor
from sandloom.core.migrations import migrations
from sandloom.core.migrations.migrations import (
    MIGRATION_LOCK_KEY,
    MigrationDependencyError,
    migration_lock,
    migration_lock_holders,
)
from sandloom.core.migrations.urls import to_sync_url


def _test_database_url() -> str:
    from sandloom.core.testing.workers import worker_env_values

    values = worker_env_values(Path(__file__).resolve().parents[4] / ".env.test")
    return os.environ.get("SANDLOOM_TEST_DATABASE_URL") or values["DATABASE_URL"]


# ------------------------------------------------------- migration lock ----


def test_lock_wait_names_the_holder_and_honours_the_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    url = to_sync_url(_test_database_url())
    engine = create_engine(url, connect_args={"application_name": "ops-test-holder"})
    caplog.set_level(logging.WARNING, logger="sandloom.core.migrations.migrations")
    try:
        with engine.connect() as holder:
            holder.execute(
                text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY}
            )
            holder.commit()
            pid = holder.execute(text("SELECT pg_backend_pid()")).scalar_one()
            try:
                with pytest.raises(MigrationDependencyError) as exc:
                    with migration_lock([url], timeout=0.3):
                        pytest.fail("the lock is held by another session")
            finally:
                holder.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_KEY}
                )
                holder.commit()
    finally:
        engine.dispose()
    message = str(exc.value)
    assert "Timed out after 0.3s" in message
    assert f"pid {pid}" in message
    assert "application_name='ops-test-holder'" in message
    assert "test_password" not in message  # DSN credentials masked
    waiting = [
        r.getMessage() for r in caplog.records if "waiting for" in r.getMessage()
    ]
    assert waiting and f"pid {pid}" in waiting[0]


def test_lock_holders_reports_our_own_application_name() -> None:
    url = to_sync_url(_test_database_url())
    with migration_lock([url], timeout=5):
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                holders = migration_lock_holders(conn)
        finally:
            engine.dispose()
    assert [h["application_name"] for h in holders] == ["sandloom-migrate"]


def test_lock_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="> 0"):
        with migration_lock([], timeout=0):
            pass


def test_cli_lock_timeout_flag_reaches_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    from sandloom.core.cli import migrate

    seen: list[float | None] = []

    @contextmanager
    def fake_lock(urls: Any, *, timeout: float | None = None) -> Any:
        seen.append(timeout)
        yield

    core = SimpleNamespace(module_descriptors=[])
    monkeypatch.setattr(migrate, "_load_core", lambda: core)
    monkeypatch.setattr(migrate, "migration_lock", fake_lock)
    monkeypatch.setattr(migrate, "resolve_database_sync_url", lambda n: "x://")
    monkeypatch.setattr(migrate, "run_core_upgrade", lambda **_: None)

    assert migrate.main(["upgrade-core", "--lock-timeout", "12.5"]) == 0
    assert migrate.main(["upgrade-core"]) == 0
    assert seen == [12.5, None]
    with pytest.raises(SystemExit):
        migrate.main(["upgrade-core", "--lock-timeout", "0"])


# ------------------------------------------------------ migration status ----


def test_migration_status_compares_applied_with_code_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "versions").mkdir()
    chain = f"opstest{uuid.uuid4().hex[:8]}"
    descriptor = ModuleDescriptor(
        name="opstest.mod",
        version="0.1.0",
        migrations_path=str(tmp_path),
        migrations_chain=chain,
    )
    url = to_sync_url(_test_database_url())
    monkeypatch.setattr(migrations, "list_database_instances", lambda: {"default": url})
    monkeypatch.setattr(migrations, "_chain_heads", lambda _loc: ("b",))
    monkeypatch.setattr(migrations, "_chain_revisions", lambda _loc: {"a", "b"})
    table = f"alembic_version_{chain}"

    def status() -> str:
        rows = migrations.migration_status([descriptor], [])
        assert [r["chain"] for r in rows] == ["opstest.mod"]
        return str(rows[0]["status"])

    engine = create_engine(url)
    try:
        assert status() == "missing"
        with engine.begin() as conn:
            conn.execute(text(f'CREATE TABLE "{table}" (version_num varchar(32))'))
            conn.execute(text(f"INSERT INTO \"{table}\" VALUES ('a')"))
        assert status() == "behind"
        with engine.begin() as conn:
            conn.execute(text(f"UPDATE \"{table}\" SET version_num = 'b'"))
        assert status() == "ok"
        with engine.begin() as conn:
            conn.execute(text(f"UPDATE \"{table}\" SET version_num = 'zzz'"))
        assert status() == "unknown"
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP TABLE IF EXISTS "{table}"'))
        engine.dispose()


def test_migration_status_reports_unreachable_databases_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "postgresql+psycopg2://user:hunter2-secret@127.0.0.1:1/nope"
    monkeypatch.setattr(migrations, "list_database_instances", lambda: {"default": url})
    rows = migrations.migration_status([], ["default"], connect_timeout=1)
    assert rows[0]["chain"] == "core" and rows[0]["status"] == "error"
    assert "hunter2" not in json.dumps(rows, default=str)


# ------------------------------------------------------------ config dump ----


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("JWT_SECRET", True),
        ("JWT_PREVIOUS_SECRETS", True),
        ("SMTP_PASSWORD", True),
        ("INTERNAL_API_TOKEN", True),
        ("TELEGRAM_BOT_TOKEN", True),
        ("JWT_PRIVATE_KEY", True),
        ("RPC_DH_EXTRA_PRIVATE_KEYS", True),
        ("TWOFA_ENCRYPTION_KEY", True),
        ("STRIPE_SECRET_KEY", True),
        ("S3_ACCESS_KEY_ID", True),
        ("SENTRY_DSN", True),
        ("JWT_PUBLIC_KEYS", False),
        ("RPC_SERVICE_KEYS", False),
        ("PASSWORD_HASH_PROFILE", False),
        ("RPC_KEY_ID", False),
        ("JWT_KID", False),
        ("DATABASE_URL", False),
        ("LOG_LEVEL", False),
    ],
)
def test_secret_names(name: str, secret: bool) -> None:
    assert config_cli.is_secret_name(name) is secret


def test_redaction_is_recursive_and_masks_url_credentials() -> None:
    infra = {
        "cache": {"s": {"url": "redis://:pw1@h:6379/0", "password": "pw2"}},
        "database": {"a": {"url": "postgresql+asyncpg://u:pw3@db/a"}},
    }
    out = json.dumps(config_cli.redact_value("INFRA", infra))
    assert "pw1" not in out and "pw2" not in out and "pw3" not in out
    assert "postgresql+asyncpg://u:***@db/a" in out
    nested = config_cli.redact_value("RPC_DH_EXTRA_PRIVATE_KEYS", {"kid1": "PEM"})
    assert nested == {"kid1": config_cli.REDACTED}
    assert config_cli.redact_value("JWT_SECRET", None) is None  # unset stays unset
    assert config_cli.redact_value("ACCESS_TOKEN_EXPIRE_MINUTES", 15) == 15


def test_effective_config_reports_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "internal_api_token").write_text("from-secrets-dir-token-0123456789")
    (tmp_path / ".env").write_text("LOG_LEVEL=debug\nJWT_SECRET=dotenv-secret-value\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)
    monkeypatch.setenv("METRICS_TOKEN", "env-metrics-token-value-0123456789")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:dbpass@h:5432/d")
    monkeypatch.setenv("CACHE_URL", "redis://h:6379/0")
    monkeypatch.setenv("BROKER_URL", "redis://h:6379/1")

    class _Cfg(Settings):
        model_config = {**Settings.model_config, "secrets_dir": str(secrets)}

    cfg = _Cfg()
    entries = {e.name: e for e in config_cli.effective_config(cfg)}

    assert (entries["LOG_LEVEL"].value, entries["LOG_LEVEL"].source) == (
        "DEBUG",
        "dotenv",
    )
    assert entries["JWT_SECRET"].source == "dotenv"
    assert entries["JWT_SECRET"].value == config_cli.REDACTED
    assert entries["INTERNAL_API_TOKEN"].source == "secrets_dir"
    assert entries["METRICS_TOKEN"].source == "env"
    assert entries["SHUTDOWN_BUDGET_SECONDS"].source == "default"
    dumped = json.dumps([e.value for e in entries.values()], default=str)
    assert entries["DATABASE_URL"].value == "postgresql+asyncpg://u:***@h:5432/d"
    for secret in (
        "from-secrets-dir",
        "dotenv-secret-value",
        "env-metrics-token",
        "dbpass",
    ):
        assert secret not in dumped
    clear = {e.name: e for e in config_cli.effective_config(cfg, redact=False)}
    assert clear["JWT_SECRET"].value == "dotenv-secret-value"


def test_config_dump_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert config_cli.main(["dump", "--json", "--only", "jwt_,log_level"]) == 0
    rows = json.loads(capsys.readouterr().out)
    names = {r["name"] for r in rows}
    assert "LOG_LEVEL" in names and "JWT_SECRET" in names
    assert all(n.startswith(("JWT_", "LOG_LEVEL")) for n in names)
    assert all(
        r["value"] in (None, config_cli.REDACTED, [])
        for r in rows
        if r["name"] in ("JWT_SECRET", "JWT_PREVIOUS_SECRETS")
    )
    assert config_cli.main(["dump", "--only", "LOG_"]) == 0
    assert "LOG_LEVEL" in capsys.readouterr().out
    assert config_cli.main([]) == 2


def test_show_secrets_is_refused_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.core.config.common import Environment
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(settings, "environment", Environment.prod)
    assert config_cli.main(["dump", "--show-secrets"]) == 2


# ----------------------------------------------------------------- doctor ----


class _Result:
    def __init__(self, status: str, **details: Any) -> None:
        self.status = status
        self.details = details


class _Infra:
    def __init__(self, results: dict[str, _Result]) -> None:
        self.results = results
        self.closed = False

    async def health_all(self) -> dict[str, _Result]:
        return self.results

    def describe(self) -> dict[str, Any]:
        return {"instances": {"database": {"default": {"driver": "sqlalchemy"}}}}

    def instances(self, capability: str) -> list[str]:
        return []

    async def aclose_all(self) -> None:
        self.closed = True


class _Transport:
    async def group_info(self, stream: str) -> list[dict[str, Any]]:
        return [{"group": "billing:seen", "pending": 0, "lag": 5000}]


def _core(**infra: _Result) -> Any:
    return SimpleNamespace(
        loaded_modules=["sandloom_billing"],
        module_descriptors=[
            ModuleDescriptor(
                name="sandloom_billing",
                version="1",
                consumes=[ContractRequirement("tenancy.membership.v1", required=False)],
            )
        ],
        service_registry=SimpleNamespace(list=lambda: []),
        infra=_Infra(dict(infra)),
        event_consumers=[
            SimpleNamespace(channel="a.b.v1", group_name="billing:seen", required=True),
            SimpleNamespace(channel="a.b.v1", group_name="billing:new", required=True),
        ],
        projection_registry=None,
        event_bus=SimpleNamespace(transport=_Transport()),
        settings=SimpleNamespace(production_profile="standard", is_prod=False),
    )


async def test_doctor_reports_each_section_and_exit_code() -> None:
    core = _core(
        **{
            "database:default": _Result("ok"),
            "cache:default": _Result(
                "fail", error="connect to redis://:topsecret@cache:6379 refused"
            ),
        }
    )
    report = await doctor.run_doctor(
        skip=frozenset({"migrations"}), timeout=2, core_loader=lambda: core
    )
    by_name = {(c.section, c.name): c for c in report.checks}

    assert by_name[("infra", "database:default")].status == doctor.OK
    failed = by_name[("infra", "cache:default")]
    assert failed.status == doctor.FAIL
    assert (
        by_name[("modules", "sandloom_billing -> tenancy.membership.v1")].status
        == doctor.WARN
    )
    assert by_name[("consumers", "a.b.v1 -> billing:seen")].status == doctor.WARN  # lag
    assert (
        by_name[("consumers", "a.b.v1 -> billing:new")].status == doctor.WARN
    )  # missing
    assert by_name[("rls", "tenant_rls")].status == doctor.SKIP
    assert report.exit_code == doctor.EXIT_FAIL
    assert core.infra.closed
    rendered = report.render() + json.dumps(report.to_dict(), default=str)
    assert "topsecret" not in rendered
    assert "FAIL" in report.render()


async def test_doctor_exit_codes() -> None:
    healthy = _core(**{"database:default": _Result("ok")})
    healthy.event_consumers = []
    healthy.module_descriptors = []
    report = await doctor.run_doctor(
        skip=frozenset({"migrations", "config"}), core_loader=lambda: healthy
    )
    assert report.exit_code == doctor.EXIT_OK

    healthy.module_descriptors = _core().module_descriptors  # optional binding gap
    report = await doctor.run_doctor(
        skip=frozenset({"migrations", "config"}), core_loader=lambda: healthy
    )
    assert report.exit_code == doctor.EXIT_WARN


async def test_doctor_reports_a_broken_composition_root() -> None:
    def boom() -> Any:
        raise RuntimeError("module sandloom.x failed: postgresql://u:pw9@h/db")

    report = await doctor.run_doctor(core_loader=boom)
    check = report.checks[-1]
    assert (check.section, check.status) == ("modules", doctor.FAIL)
    assert "pw9" not in check.message
    assert report.exit_code == doctor.EXIT_FAIL


async def test_doctor_flags_strict_profile_problems(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(settings, "production_profile", "multi_tenant_strict")
    report = await doctor.run_doctor(
        skip=frozenset(doctor.SECTIONS) - {"config"}, core_loader=lambda: None
    )
    strict = [c for c in report.checks if c.name == "multi_tenant_strict"]
    assert any(c.status == doctor.FAIL and "tenant_rls" in c.message for c in strict)
    assert report.exit_code == doctor.EXIT_FAIL


def test_doctor_cli_rejects_unknown_sections() -> None:
    with pytest.raises(SystemExit) as exc:
        doctor.main(["--skip", "nope"])
    assert exc.value.code == 2
