"""Declared planner operations are planned at startup, logged and diagnosable.

``ModuleAPI.register_operations`` adds a CRITICAL startup task that plans each
operation (a declaration that does not match the schema fails the boot), logs
the executor the kernel picked and why, and lists the operation in
``/diagnostics/core`` under ``planner`` with its CURRENT plan.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import BaseModel

from sandloom.sdk.data import ReadOperation
from sandloom.sdk.infra import settings
from sandloom.sdk.module import ModuleAPI
from sandloom_tenancy.models import Organization
from sandloom_tenancy.services.operations import member_role_in_active_org

TOKEN = "planner-diag-internal-token-0123456789abcdef"


def _prepare_tasks(facade: Any) -> list[Any]:
    return [
        (task, critical)
        for task, critical in facade.startup_task_specs
        if getattr(task, "__name__", "") == "prepare_declared_operations"
    ]


async def _run_prepare_tasks(facade: Any) -> None:
    for task, critical in _prepare_tasks(facade):
        assert critical, "operation planning must be a critical startup task"
        await task()


@pytest.mark.parametrize("optimized", [False, True])
async def test_composition_root_registers_the_tenancy_operation(
    sandloom_app: Any,
    sandloom_core: Any,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    optimized: bool,
) -> None:
    """The tenancy module declares its membership JoinRead; startup plans it
    and logs the executor and the reason."""
    assert _prepare_tasks(sandloom_core), "tenancy registers its declared operations"
    monkeypatch.setattr(settings, "planner_optimized_executor", optimized)
    with caplog.at_level(logging.INFO, logger="sandloom.core.planner"):
        await _run_prepare_tasks(sandloom_core)
    lines = [
        r.getMessage() for r in caplog.records if r.name == "sandloom.core.planner"
    ]
    assert any(
        "tenancy.member.role_in_active_org" in m
        and ("optimized executor" if optimized else "general executor") in m
        and ("FK join, equality keys" if optimized else "optimized executor disabled")
        in m
        for m in lines
    ), lines


async def test_diagnostics_lists_operations_with_executor_and_reason(
    sandloom_app: Any,
    sandloom_core: Any,
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "planner_optimized_executor", False)
    await _run_prepare_tasks(sandloom_core)
    monkeypatch.setattr(settings, "internal_api_token", TOKEN)
    resp = await client.get("/diagnostics/core", headers={"X-Internal-Token": TOKEN})
    assert resp.status_code == 200, resp.text
    block = resp.json()["planner"]
    assert block["optimized_executor_enabled"] is False
    ops = {o["name"]: o for o in block["operations"]}
    entry = ops["tenancy.member.role_in_active_org"]
    assert entry["executor"] == "general"
    assert entry["reason"] == "optimized executor disabled"
    assert entry["kind"] == "JoinRead"
    assert entry["module"].startswith("sandloom_tenancy")
    # with the optimized executor enabled the CURRENT plan is reported
    monkeypatch.setattr(settings, "planner_optimized_executor", True)
    resp = await client.get("/diagnostics/core", headers={"X-Internal-Token": TOKEN})
    ops = {o["name"]: o for o in resp.json()["planner"]["operations"]}
    entry = ops["tenancy.member.role_in_active_org"]
    assert entry["executor"] == "optimized"
    assert entry["reason"] == "FK join, equality keys"


class _Labelled(BaseModel):
    id: Any
    label: str


async def test_unsupported_operation_is_general_with_the_reason_logged(
    sandloom_core: Any,
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Optimized executor ON, an operation it cannot run: the kernel picks the
    general executor explicitly and says why (log + diagnostics)."""
    monkeypatch.setattr(settings, "planner_optimized_executor", True)
    monkeypatch.setattr(
        Organization, "label", property(lambda o: o.name.upper()), raising=False
    )
    op = ReadOperation(entity=Organization, result=_Labelled, name="t.org.labelled")
    before = len(_prepare_tasks(sandloom_core))
    ModuleAPI(sandloom_core).register_operations(op)
    task, _critical = _prepare_tasks(sandloom_core)[before]
    with caplog.at_level(logging.INFO, logger="sandloom.core.planner"):
        await task()
    msg = next(
        r.getMessage() for r in caplog.records if "t.org.labelled" in r.getMessage()
    )
    assert "general executor" in msg and "'label' is not a plain column" in msg
    monkeypatch.setattr(settings, "internal_api_token", TOKEN)
    resp = await client.get("/diagnostics/core", headers={"X-Internal-Token": TOKEN})
    assert resp.status_code == 200, resp.text
    ops = {o["name"]: o for o in resp.json()["planner"]["operations"]}
    entry = ops[op.name]
    assert entry["executor"] == "general"
    assert entry["module"].startswith("sandloom_tenancy")  # the entity's module
    assert "not a plain column" in entry["reason"]


async def test_a_bad_declaration_fails_the_startup_task(sandloom_core: Any) -> None:
    class _Missing(BaseModel):
        nope: int

    api = ModuleAPI(sandloom_core)
    before = len(_prepare_tasks(sandloom_core))
    api.register_operations(ReadOperation(entity=Organization, result=_Missing))
    task, critical = _prepare_tasks(sandloom_core)[before]
    assert critical
    with pytest.raises(Exception, match="nope") as exc:
        await task()
    assert type(exc.value).__name__ == "PlanningError"


def test_register_operations_refuses_non_operations(sandloom_core: Any) -> None:
    with pytest.raises(TypeError):
        ModuleAPI(sandloom_core).register_operations(
            member_role_in_active_org, object()
        )


async def test_unit_read_reloads_expired_columns_of_an_identity_map_hit(
    db: Any,
) -> None:
    """A declared read ON a Unit of Work (``UowReads`` / ``session_reads``)
    whose row is already in the identity map with EXPIRED attributes — a
    flushed server-side ``onupdate`` column, or everything after a commit —
    loads them (as a SELECT would) instead of lazy-loading outside the
    greenlet (``MissingGreenlet`` -> 500)."""
    import uuid

    from sandloom.sdk.data import session_reads
    from sandloom_tenancy.services.operations import org_by_id

    org = Organization(
        name="Before", slug=f"exp-{uuid.uuid4().hex[:8]}", owner_id=uuid.uuid4()
    )
    db.add(org)
    await db.flush()
    org.name = "After"
    await db.flush()  # updated_at (server onupdate) is now expired
    org_id = org.id
    out = await session_reads(db).one(org_by_id, id=org_id)
    assert out is not None and out.name == "After"
    assert out.updated_at is not None
    db.expire_all()  # e.g. a committed unit with expire_on_commit
    again = await session_reads(db).one(org_by_id, id=org_id)
    assert again is not None and again.name == "After"
