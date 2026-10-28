"""Audit trail (record_audit) + file-based secrets (secrets_dir)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import Table, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sandloom.core import di
from sandloom.core.config.settings import Settings
from sandloom.core.sql_alchemy.models.audit import AuditLogEntry
from sandloom.core.sql_alchemy.outbox_store import sql_alchemy_audit_store
from sandloom.core.testing import reload_test_env
from sandloom.core.unit_of_work import scope as uow_mod_scope


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    backend_dir = Path(__file__).resolve().parents[4]
    env_test = backend_dir / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture
async def audit_session_factory() -> AsyncGenerator[
    async_sessionmaker[AsyncSession], None
]:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not configured")
    engine: AsyncEngine = create_async_engine(url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: cast(Table, AuditLogEntry.__table__).create(
                sync_conn, checkfirst=True
            )
        )
        await conn.execute(text("DELETE FROM audit_log"))
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM audit_log"))
    await engine.dispose()


@pytest.mark.asyncio
async def test_record_audit_writes_in_uow(
    audit_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    actor = uuid.uuid4()
    async with audit_session_factory() as session:
        session.info[uow_mod_scope._AUDIT_STORE_KEY] = sql_alchemy_audit_store()
        token = uow_mod_scope._current_uow_session.set(session)
        try:
            di.record_audit(
                "tenancy.member.role_changed",
                actor_id=actor,
                resource_type="organization_member",
                resource_id="org-123",
                data={"from": "member", "to": "admin"},
            )
            await session.commit()
        finally:
            uow_mod_scope._current_uow_session.reset(token)

        rows = (await session.execute(select(AuditLogEntry))).scalars().all()
        assert len(rows) == 1
        entry = rows[0]
        assert entry.action == "tenancy.member.role_changed"
        assert entry.actor_id == actor
        assert entry.resource_id == "org-123"
        assert entry.data == {"from": "member", "to": "admin"}


def test_record_audit_outside_uow_raises_by_default() -> None:
    # 0.7: required is the default — no UoW must fail loudly, naming the fix.
    with pytest.raises(di.AuditUnavailableError, match="required=False"):
        di.record_audit("x.y.z")


def test_record_audit_best_effort_outside_uow_is_skipped() -> None:
    # Best-effort is explicit: no active UoW session -> skipped, never raises.
    di.record_audit("x.y.z", required=False)


def test_record_audit_required_outside_uow_raises() -> None:
    # A mandatory audit must fail loudly rather than silently skip.
    with pytest.raises(di.AuditUnavailableError):
        di.record_audit("security.sensitive", required=True)


def test_record_audit_default_rejects_invalid_actor() -> None:
    # The default is required: a non-UUID actor raises instead of becoming None.
    token = uow_mod_scope._current_uow_session.set(cast("AsyncSession", object()))
    try:
        with pytest.raises(di.AuditUnavailableError):
            di.record_audit("security.sensitive", actor_id="not-a-uuid")
    finally:
        uow_mod_scope._current_uow_session.reset(token)


def test_record_audit_required_invalid_actor_raises() -> None:
    # A non-UUID actor on a mandatory audit must raise, never silently become
    # None (which would corrupt the trail). This branch fires before any store
    # access, so a placeholder session is enough.
    token = uow_mod_scope._current_uow_session.set(cast("AsyncSession", object()))
    try:
        with pytest.raises(di.AuditUnavailableError):
            di.record_audit("security.sensitive", actor_id="not-a-uuid", required=True)
    finally:
        uow_mod_scope._current_uow_session.reset(token)


def test_secrets_dir_reads_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Provide the JWT secret as a Docker/K8s-style secret file.
    (tmp_path / "jwt_secret").write_text("file-sourced-secret-value-1234567890")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("CACHE_URL", "redis://h:6379/0")
    monkeypatch.setenv("BROKER_URL", "redis://h:6379/1")
    monkeypatch.delenv("JWT_SECRET", raising=False)

    s = Settings(_env_file=None, _secrets_dir=str(tmp_path))  # type: ignore[call-arg]
    assert s.jwt_secret == "file-sourced-secret-value-1234567890"
