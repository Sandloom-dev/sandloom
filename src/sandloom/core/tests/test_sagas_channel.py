"""Cross-tenant access to saga state (ADR-0008 §2 RLS, amendment A6) and the
``/diagnostics/core`` listing — no database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from sandloom.core.app_factory import _saga_diagnostics
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import Saga, local_step
from sandloom.core.sagas import install_sagas
from sandloom.core.sagas.channel import (
    SystemChannel,
    cross_tenant_session,
    system_channel_problem,
    tenant_rls_enabled,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry


class _Provider:
    def __init__(self, rls: bool) -> None:
        self._rls = rls

    def features(self) -> frozenset[str]:
        return frozenset({"tenant_rls"} if self._rls else set())


def _core(rls: bool) -> Any:
    return SimpleNamespace(database=lambda name: _Provider(rls))


def _channel(configured: bool, opened: list[dict[str, Any]]) -> SystemChannel:
    @asynccontextmanager
    async def open_(**kw: Any) -> AsyncIterator[str]:
        opened.append(kw)
        yield "system-session"

    return SystemChannel(open=open_, configured=lambda database: configured)


def test_problem_only_for_rls_without_a_system_channel() -> None:
    assert tenant_rls_enabled(_core(True), "default")
    assert system_channel_problem(_core(False), "default", None) is None
    assert system_channel_problem(_core(True), "default", _channel(True, [])) is None
    problem = system_channel_problem(_core(True), "default", _channel(False, []))
    assert problem is not None and "DATABASE_SYSTEM_URL" in problem
    assert system_channel_problem(_core(True), "default", None) is not None


async def test_rls_databases_go_through_the_system_channel() -> None:
    opened: list[dict[str, Any]] = []
    async with cross_tenant_session(
        _core(True),
        "default",
        actor="kernel:x",
        reason="saga test read",
        channel=_channel(True, opened),
    ) as cts:
        assert cts.session == "system-session" and cts.uow is False
    assert opened == [
        {"actor": "kernel:x", "reason": "saga test read", "database": "default"}
    ]
    with pytest.raises(RuntimeError, match="system channel"):
        async with cross_tenant_session(
            _core(True), "default", actor="a", reason="no channel", channel=None
        ):
            pass


class _In(BaseModel):
    n: int


async def _noop(db: Any, c: Any) -> None:
    return None


def test_definitions_are_listed_in_core_diagnostics() -> None:
    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(InMemorySchemaRegistry())
    saga = Saga[_In](
        "diag",
        version=2,
        input=_In,
        deadline=timedelta(minutes=1),
        steps=[local_step("only", kind="pivot", apply=_noop)],
    )
    core.register_saga(saga, "sandloom.diagmod")
    install_sagas(core)
    (row,) = _saga_diagnostics(core)
    assert row["saga_type"] == "diagmod.diag" and row["version"] == 2
    assert row["module"] == "sandloom.diagmod" and row["steps"][0]["kind"] == "pivot"
    # the advance consumer of the module is installed, durable=False / no DLQ
    (consumer,) = [
        c for c in core.event_consumers if c.channel == "diagmod.saga_advance.v1"
    ]
    assert consumer.group_name == "sandloom.diagmod:sagas@advance"
    assert consumer.durable is False and consumer.dead_letter is False
