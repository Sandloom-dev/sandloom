"""The 1.0 testing kit (spec §2.3; ledger C-TEST-07/08/09/10).

``as_user`` is ``make_user_headers`` (same parameters, same result);
``deliver`` drives the real consumer runtime (schema preflight, the durable
claim, the tenant binding) and accepts an ``Event``; ``published`` lists what
``Event.publish()`` published; ``fake_contract`` serves a contract for a block.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from sdkfix_shared.contracts.sdkfix_stats_quota_v1 import SDKFIX_STATS_QUOTA_V1

from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.facade import CoreFacade
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.testing import make_user_headers
from sandloom.sdk import Contract, CurrentUser
from sandloom.sdk.module import ModuleAPI
from sandloom.sdk.security import decode_token
from sandloom.sdk.testing import (
    as_user,
    assert_route_snapshot,
    deliver,
    fake_contract,
    route_policy,
)


def test_as_user_is_make_user_headers() -> None:
    assert inspect.signature(as_user).parameters.keys() == (
        inspect.signature(make_user_headers).parameters.keys()
    )
    user, tenant = uuid4(), uuid4()
    uid, headers = as_user(user, tenant, roles=["admin"])
    assert uid == user
    claims = decode_token(headers["Authorization"].removeprefix("Bearer "))
    assert claims["sub"] == str(user)
    assert claims["tenant_ids"] == [str(tenant)]
    assert claims["roles"] == ["admin"]
    _, tenantless = as_user(tenant=False)
    assert "tenant_id" not in decode_token(
        tenantless["Authorization"].removeprefix("Bearer ")
    )


@pytest.fixture
def stats_core() -> CoreFacade:
    import sdkfix_stats

    registry = InMemorySchemaRegistry()
    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(registry)
    core.set_event_bus(InMemoryEventBus(registry))
    sdkfix_stats.register(ModuleAPI(core))
    return core


async def test_deliver_drives_the_real_consumer_once(
    stats_core: CoreFacade, db: Any
) -> None:
    from sdkfix_stats.consumers import COUNTS, NoteCreated

    tenant = uuid4()
    applied = await deliver(
        NoteCreated(tenant_id=tenant), times=2, session=db, core=stats_core
    )
    assert applied == 1
    assert COUNTS[tenant] == 1  # durable=True: the redelivery is claimed once


async def test_deliver_keeps_the_channel_form(stats_core: CoreFacade, db: Any) -> None:
    from sdkfix_stats.consumers import COUNTS, NoteCreated

    tenant = uuid4()
    await deliver(NoteCreated(tenant_id=uuid4()), session=db, core=stats_core)
    await deliver(
        "sdkfix_notes.note.created.v1",
        {"tenant_id": str(tenant)},
        session=db,
        core=stats_core,
    )
    assert COUNTS[tenant] == 1
    with pytest.raises(TypeError, match="payload is required"):
        await deliver("sdkfix_notes.note.created.v1")
    with pytest.raises(TypeError, match="takes no payload"):
        await deliver(NoteCreated(tenant_id=tenant), {"x": 1})


async def test_an_aliased_field_survives_the_real_consumer_runtime(
    db: Any,
) -> None:
    """A producer field aliased to the wire key ``channel`` (the Event API
    reserves the name) is published under the alias and reaches a view's
    handler through the real runtime: schema preflight, durable claim, the
    tenant bound from the payload."""
    from pydantic import Field

    from sandloom.sdk import Event, on
    from sandloom.sdk._decl.events import consumer_callback, to_wire
    from sandloom.sdk.tenant import current_tenant_id_optional

    class Sent(Event, channel="sdkfix_notes.notice.sent.v1", role="producer"):
        tenant_id: UUID
        via: str = Field(alias="channel")

    class SentView(Event, channel="sdkfix_notes.notice.sent.v1", role="view"):
        tenant_id: UUID
        delivery: str = Field(alias="channel")

    seen: list[tuple[str, Any]] = []

    @on(SentView)
    async def record(event: SentView) -> None:
        seen.append((event.delivery, current_tenant_id_optional()))

    registry = InMemorySchemaRegistry()
    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(registry)
    core.set_event_bus(InMemoryEventBus(registry))
    (spec,) = record.__sandloom_consumers__  # type: ignore[attr-defined]
    ModuleAPI(core).register_event_consumer(
        SentView.channel,
        consumer_callback(spec),
        name="record",
        bind_tenant_from=spec.bind_tenant_from,
    )
    tenant = uuid4()
    event = Sent(tenant_id=tenant, via="sms")
    assert to_wire(event) == {"tenant_id": str(tenant), "channel": "sms"}
    assert await deliver(event, session=db, core=core) == 1
    assert seen == [("sms", tenant)]


async def test_fake_contract_nests_and_restores() -> None:
    quota = Contract(SDKFIX_STATS_QUOTA_V1)

    class Deny:
        async def may_create_note(self) -> bool:
            return False

    class Allow:
        async def may_create_note(self) -> bool:
            return True

    with fake_contract(SDKFIX_STATS_QUOTA_V1, Deny()):
        assert await quota.may_create_note() is False
        with fake_contract("sdkfix_stats.quota.v1", Allow()):
            assert await quota.may_create_note() is True
        assert await quota.may_create_note() is False
    with pytest.raises(TypeError):
        fake_contract(object(), Allow())


def _notes_app() -> FastAPI:
    """An app with the L1 fixture modules loaded by the module loader (their
    routes stamped with the module, as ``create_app`` does)."""
    from sandloom.core.loader.plugin_loader import load_modules_with_summary

    app = FastAPI()
    core = CoreFacade(app=app)
    core.register_schema_registry(InMemorySchemaRegistry())
    summary = load_modules_with_summary(core, ["sdkfix_stats", "sdkfix_notes"])
    assert [d.name for d in summary.loaded] == ["sdkfix_stats", "sdkfix_notes"]
    return app


def test_route_policy_reads_auth_risk_and_the_public_reason() -> None:
    """``route_policy`` (C-TEST-07): deny-by-default is assertable from an
    app's suite, without the kernel's route-policy internals."""
    from fastapi import APIRouter, Depends

    from sandloom.sdk.authz import Role, require
    from sandloom.sdk.web import sensitive

    app = _notes_app()
    by_path = {
        (r.path, m): route_policy(r)
        for r in app.routes
        if isinstance(r, APIRoute)
        for m in r.methods
    }
    create = by_path[("/api/v1/sdkfix_notes/", "POST")]
    assert (create.auth, create.effect, create.public_reason) == ("user", "write", None)
    health = by_path[("/api/v1/sdkfix_notes/health", "GET")]
    assert (health.auth, health.effect) == ("public", "read")
    assert health.public_reason == "liveness probe"
    assert all(p.always_strict and not p.declares_budget for p in by_path.values())

    router = APIRouter()

    @router.delete("/x", dependencies=[Depends(require(Role("admin")))])
    async def admin_only() -> None: ...

    @router.post("/y", dependencies=[sensitive("security")])
    async def hand_rolled(user: CurrentUser) -> None: ...

    risks = {getattr(r, "path", ""): route_policy(r).risk for r in router.routes}
    assert risks == {"/x": ("admin",), "/y": ("security",)}


def test_assert_route_snapshot_pins_the_module_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``assert_route_snapshot`` (C-TEST-07, SANDLOOM-T001): the committed snapshot
    passes; a stale or missing one fails naming the fix."""
    import sandloom.core.route_snapshots as snapshots

    app = _notes_app()
    assert_route_snapshot(app, "sdkfix_notes")

    committed = snapshots.snapshot_path("sdkfix_notes")
    assert committed is not None
    stale = tmp_path / "routes.snapshot.json"
    doc = json.loads(committed.read_text())
    doc["routes"] = [e for e in doc["routes"] if e["path"].endswith("/health")]
    stale.write_text(json.dumps(doc))
    monkeypatch.setattr(snapshots, "snapshot_path", lambda _module: stale)
    with pytest.raises(AssertionError, match=r"SANDLOOM-T001"):
        assert_route_snapshot(app, "sdkfix_notes")
    stale.unlink()
    with pytest.raises(AssertionError, match="is missing"):
        assert_route_snapshot(app, "sdkfix_notes")
