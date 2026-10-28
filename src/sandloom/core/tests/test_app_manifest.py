"""The application manifest (Public API 1.0, spec §5.7): interface and the
construction from today's loader."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import APIRouter

from sandloom.core.descriptors import ContractRequirement, ModuleDescriptor
from sandloom.core.loader.manifest import (
    AppManifest,
    ChainEntry,
    ConsumerEntry,
    ContractUse,
    EventEntry,
    ModuleManifest,
    RouteEntry,
    module_manifest,
)
from sandloom.core.module_api import ModuleRegistrationDraft, contract_method
from sandloom.main import app


class _Provider:
    @classmethod
    @contract_method
    async def ping(cls) -> str:
        return "pong"


async def _handler(session: Any, envelope: Any) -> None:
    return None


def _draft() -> ModuleRegistrationDraft:
    router = APIRouter()

    @router.get("/things")
    async def list_things() -> list[str]:
        return []

    @router.post("/things")
    async def create_thing() -> dict[str, str]:
        return {}

    draft = ModuleRegistrationDraft()
    draft.routers.append((router, {"prefix": "/api/v1/things"}))
    draft.event_schemas.append(
        ("things.thing.created.v1", "ThingCreated", {"type": "object"})
    )
    draft.services.append(("things.lookup.v1", _Provider))
    draft.service_databases["things.lookup.v1"] = "things"
    draft.event_consumers.append(
        SimpleNamespace(
            channel="other.x.v1",
            name="count_x",
            durable=True,
            required=False,
            dead_letter=False,
            database="default",
            group=None,
            start_from="latest",
            bind_tenant_from="tenant_id",
        )
    )
    return draft


def _descriptor() -> ModuleDescriptor:
    return ModuleDescriptor(
        name="things",
        version="1.2.0",
        consumes=[
            ContractRequirement("other.read.v1", required=False, methods=("get",))
        ],
        migrations_chain="things",
    )


def test_module_manifest_reads_the_loader_draft() -> None:
    entry = module_manifest(_descriptor(), _draft())
    assert entry.name == "things" and entry.version == "1.2.0"
    assert {(r.method, r.path) for r in entry.routes} == {
        ("GET", "/api/v1/things/things"),
        ("POST", "/api/v1/things/things"),
    }
    assert entry.events == (
        EventEntry(
            module="things",
            channel="things.thing.created.v1",
            name="ThingCreated",
            schema={"type": "object"},
        ),
    )
    assert entry.contracts_provided[0].name == "things.lookup.v1"
    assert entry.contracts_provided[0].database == "things"
    assert entry.contracts_provided[0].methods == ("ping",)
    assert entry.contracts_consumed == (
        ContractUse(
            module="things", name="other.read.v1", required=False, methods=("get",)
        ),
    )
    assert entry.consumers == (
        ConsumerEntry(
            module="things",
            channel="other.x.v1",
            name="count_x",
            required=False,
            dead_letter=False,
            start_from="latest",
            bind_tenant_from="tenant_id",
        ),
    )
    assert entry.chain == ChainEntry(
        module="things", database=None, chain="things", path=None
    )
    # Fields of later hand-offs exist now (frozen interface) and start empty.
    assert all(r.policy == {} for r in entry.routes)
    assert entry.deviations == ()


def test_manifest_is_frozen_and_aggregates_in_load_order() -> None:
    first = ModuleManifest(
        name="a",
        version="1",
        routes=(RouteEntry(module="a", method="GET", path="/a", name="a"),),
    )
    second = ModuleManifest(name="b", version="1")
    manifest = AppManifest.of([first, second])
    assert manifest.module("b") is second
    assert manifest.routes == first.routes
    with pytest.raises(KeyError):
        manifest.module("c")
    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.modules = ()  # type: ignore[misc]
    summary = SimpleNamespace(
        loaded=[SimpleNamespace(name="b"), SimpleNamespace(name="a")],
        manifests={"a": first, "b": second},
    )
    assert [m.name for m in AppManifest.from_summary(summary).modules] == ["b", "a"]


def test_create_app_publishes_the_manifest() -> None:
    manifest: AppManifest = app.state.app_manifest
    summary = app.state.module_summary
    assert [m.name for m in manifest.modules] == [d.name for d in summary.loaded]
    identity = manifest.module("sandloom_identity")
    assert any(r.path.startswith("/api/v1/") for r in identity.routes)
    assert "refresh_tokens" in {m.table for m in identity.models}
