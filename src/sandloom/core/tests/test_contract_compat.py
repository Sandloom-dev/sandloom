"""Rolling-upgrade contract compatibility (sandloom.core.contract_compat).

* ``compare_contract`` accepts additive changes and reports breaking ones
  precisely (removed method / parameter, newly required parameter, changed
  kind / idempotency key, incompatible type, reordered positional parameter,
  model field changes);
* the committed per-module ``contracts.snapshot.json`` files match what the
  modules register (the CI diff gate);
* a caller's ``/ready`` fails with a ``contract_incompatible`` diagnostic when
  a bound provider's live describe document breaks the caller's snapshot,
  and passes for an additive provider change;
* ``sandloom contracts check --against <snapshot>`` exits 1 on a breaking change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from sandloom.core import contract_compat, rpc
from sandloom.core.cli import contracts as contracts_cli
from sandloom.core.config.settings import settings
from sandloom.core.contract_compat import compare_contract
from sandloom.core.contract_testing import rpc_settings
from sandloom.core.descriptors import (
    ContractRequirement,
    HealthResult,
    ModuleDescriptor,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import contract_method, contract_schema
from sandloom.core.rpc import readiness as rpc_readiness

#: The consumer and the provider share this process's settings: the consumer
#: describes the provider keyed as ``svc`` calling itself.
_SVC = "svc"
#: Ops-endpoint token (``/ready`` details), not an RPC credential.
_TOKEN = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"


# --------------------------------------------------------------------------- #
# Provider versions
# --------------------------------------------------------------------------- #
class Item(BaseModel):
    sku: str
    qty: int


class ItemV2(BaseModel):
    """``Item`` + an optional field: additive."""

    sku: str
    qty: int
    note: str | None = None


class ItemNeedsPrice(BaseModel):
    """``Item`` + a REQUIRED field: breaking as a parameter."""

    sku: str
    qty: int
    price: int


class ItemNoQty(BaseModel):
    """``Item`` without ``qty``: breaking as a result."""

    sku: str


class V1:
    @classmethod
    @contract_method
    async def get(cls, org_id: UUID, limit: int = 10) -> list[Item]:
        return []

    @classmethod
    @contract_method(command=True, idempotency_key="op")
    async def put(cls, item: Item, op: str) -> int:
        return 1


class V2Additive:
    @classmethod
    @contract_method
    async def get(
        cls, org_id: UUID, limit: int | None = 10, verbose: bool = False
    ) -> list[ItemV2]:
        return []

    @classmethod
    @contract_method(command=True, idempotency_key="op")
    async def put(cls, item: ItemV2, op: str) -> int:
        return 1

    @classmethod
    @contract_method
    async def count(cls) -> int:
        return 0


class V2Breaking:
    @classmethod
    @contract_method
    async def get(cls, org_id: UUID, region: str, limit: int = 10) -> list[ItemNoQty]:
        return []

    @classmethod
    @contract_method
    async def put(cls, item: ItemNeedsPrice, op: str) -> int:
        return 1


class V2TypeChange:
    @classmethod
    @contract_method
    async def get(cls, org_id: str, limit: int = 10) -> list[Item] | None:
        return []

    @classmethod
    @contract_method(command=True, idempotency_key="op")
    async def put(cls, op: str, item: Item) -> int:
        return 1


def _doc(impl: Any) -> dict[str, Any]:
    return contract_schema(impl).describe()


# --------------------------------------------------------------------------- #
# compare_contract
# --------------------------------------------------------------------------- #
def test_identical_contract_is_compatible() -> None:
    report = compare_contract("shop.items.v1", _doc(V1), _doc(V1))
    assert report.compatible and not report.warnings


def test_additive_changes_are_compatible() -> None:
    report = compare_contract("shop.items.v1", _doc(V1), _doc(V2Additive))
    assert report.errors == []


def test_breaking_changes_are_reported_precisely() -> None:
    report = compare_contract("shop.items.v1", _doc(V1), _doc(V2Breaking))
    errors = "\n".join(report.errors)
    assert "shop.items.v1.get(region): new required parameter" in errors
    assert "shop.items.v1.get(return)[].qty: required field missing" in errors
    assert "shop.items.v1.put: kind changed 'command' -> 'read'" in errors
    assert "idempotency key changed 'op' -> None" in errors
    assert "shop.items.v1.put(item).price: required field missing" in errors


def test_type_and_order_changes_are_reported() -> None:
    report = compare_contract("shop.items.v1", _doc(V1), _doc(V2TypeChange))
    errors = "\n".join(report.errors)
    assert "get(org_id): uuid is not accepted by str" in errors
    assert "get(return): none is not accepted by list[Item]" in errors
    assert "put(item): positional order changed" in errors
    assert "put(op): positional order changed" in errors


def test_narrowing_a_parameter_type_is_breaking() -> None:
    report = compare_contract("shop.items.v1", _doc(V2TypeChange), _doc(V1))
    assert any("get(org_id): str is not accepted by uuid" in e for e in report.errors)


def test_removed_method_and_missing_contract() -> None:
    report = compare_contract("shop.items.v1", _doc(V2Additive), _doc(V1))
    assert "shop.items.v1.count: method removed by the provider" in report.errors
    gone = compare_contract("shop.items.v1", _doc(V1), None)
    assert gone.errors == ["shop.items.v1: not served by the provider"]


def test_methods_narrow_the_check() -> None:
    report = compare_contract(
        "shop.items.v1", _doc(V2Additive), _doc(V1), methods=["put"]
    )
    assert report.compatible
    full = compare_contract("shop.items.v1", _doc(V2Additive), _doc(V1))
    assert "shop.items.v1.get(verbose): parameter removed by the provider" in (
        full.errors
    )


def test_model_without_field_info_is_a_warning_not_an_error() -> None:
    old_peer = json.loads(json.dumps(_doc(V2Additive)))
    for param in old_peer["methods"]["put"]["params"]:
        param["type"].pop("fields", None)
    report = compare_contract("shop.items.v1", _doc(V1), old_peer, methods=["put"])
    assert report.compatible
    assert any("cannot verify" in w for w in report.warnings)


# --------------------------------------------------------------------------- #
# Committed snapshots are current (CI diff gate)
# --------------------------------------------------------------------------- #
def test_committed_module_snapshots_match_registered_contracts() -> None:
    from sandloom.main import core

    docs = contract_compat.module_snapshots(core)
    assert {"sandloom_identity", "sandloom_tenancy", "sandloom_billing"} <= set(docs)
    for module, doc in docs.items():
        path = contract_compat.snapshot_path(module)
        assert path is not None and path.is_file(), f"{module}: no snapshot"
        assert path.read_text(encoding="utf-8") == contract_compat.render_snapshot(
            doc
        ), f"{path} is stale: run `sandloom contracts snapshot --write`"


def test_expectation_is_found_in_the_provider_package_without_importing_it() -> None:
    core = CoreFacade(app=FastAPI())
    core.loaded_modules = ["sandloom_identity"]
    found = contract_compat.expected_contracts(core, ["billing.credits.v1"])
    assert found["billing.credits.v1"].source.endswith(
        "sandloom_billing/contracts.snapshot.json"
    )
    assert "spend" in found["billing.credits.v1"].describe["methods"]


# --------------------------------------------------------------------------- #
# Readiness + startup against a live provider
# --------------------------------------------------------------------------- #
def _provider_app(impl: Any) -> FastAPI:
    app = FastAPI()
    core = CoreFacade(app=app)
    core.register_service("shop.items.v1", impl)
    app.state.core = core
    app.include_router(rpc.rpc_router(core))
    return app


def _consumer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: Any,
    *,
    expected: Any = V1,
    methods: tuple[str, ...] = (),
) -> FastAPI:
    from sandloom.core.app_factory import _install_ops_endpoints

    snapshot = tmp_path / "contracts.snapshot.json"
    snapshot.write_text(
        contract_compat.render_snapshot(
            {
                "format": 1,
                "module": "sandloom.shop",
                "contracts": {"shop.items.v1": contract_schema(expected).describe()},
            }
        )
    )
    provider_app = _provider_app(provider)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=provider_app))
    monkeypatch.setattr(rpc_readiness, "_rpc_client", lambda: client)
    app = FastAPI()
    core = CoreFacade(app=app)

    async def ok() -> HealthResult:
        return HealthResult(status="ok")

    core.register_health_check("db", ok)
    core.loaded_modules = ["sandloom.orders"]
    core.module_descriptors = [
        ModuleDescriptor(
            name="sandloom.orders",
            version="1",
            consumes=[ContractRequirement("shop.items.v1", methods=methods)],
        )
    ]
    for name, value in rpc_settings(
        _SVC, allowed_calls={_SVC: ["shop.items.v1"]}
    ).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(
        settings,
        "service_bindings",
        {"shop.items.v1": {"base_url": "http://shop", "service_id": _SVC}},
    )
    monkeypatch.setattr(settings, "contract_snapshot_paths", [str(snapshot)])
    monkeypatch.setattr(settings, "contract_compat_check", "ready")
    monkeypatch.setattr(settings, "health_cache_ttl_seconds", 0.0)
    monkeypatch.setattr(settings, "module_required", [])
    monkeypatch.setattr(settings, "sandloom_modules", ["sandloom.orders"])
    monkeypatch.setattr(settings, "internal_api_token", _TOKEN)
    _install_ops_endpoints(app, core)
    app.state.test_core = core
    return app


async def _ready(app: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get("/ready", headers={"X-Internal-Token": _TOKEN})


async def test_ready_fails_on_an_incompatible_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _consumer(monkeypatch, tmp_path, V2Breaking)
    response = await _ready(app)
    assert response.status_code == 503, response.text
    body = json.dumps(response.json())
    assert "contract_incompatible" in body
    assert "shop.items.v1.get(region): new required parameter" in body


async def test_ready_passes_on_an_additive_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _consumer(monkeypatch, tmp_path, V2Additive)
    response = await _ready(app)
    assert response.status_code == 200, response.text


async def test_methods_the_consumer_does_not_call_do_not_fail_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The provider dropped `count`, which this consumer never calls.
    app = _consumer(monkeypatch, tmp_path, V1, expected=V2Additive, methods=("put",))
    assert (await _ready(app)).status_code == 200
    app = _consumer(monkeypatch, tmp_path, V1, expected=V2Additive)
    assert (await _ready(app)).status_code == 503


async def test_ready_fails_on_access_token_lifetime_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Review hand-off: ACCESS_TOKEN_EXPIRE_MINUTES must be identical in every
    # service (revocation-mark TTLs are derived from it). The provider now
    # publishes its value in the describe document and a bound caller with
    # another value fails /ready with a clear reason.
    real = _provider_app

    def drifting(impl: Any) -> FastAPI:
        app = real(impl)
        app.state.core.settings = settings.model_copy(
            update={
                # Keyed like the consumer: built before _consumer keys it.
                **rpc_settings(_SVC, allowed_calls={_SVC: ["shop.items.v1"]}),
                "access_token_expire_minutes": settings.access_token_expire_minutes + 5,
            }
        )
        return app

    monkeypatch.setitem(globals(), "_provider_app", drifting)
    app = _consumer(monkeypatch, tmp_path, V1)
    response = await _ready(app)
    assert response.status_code == 503, response.text
    body = json.dumps(response.json())
    assert "access_token_expire_minutes" in body


async def test_compat_check_off_restores_reachability_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _consumer(monkeypatch, tmp_path, V2Breaking)
    monkeypatch.setattr(settings, "contract_compat_check", "off")
    assert (await _ready(app)).status_code == 200


async def test_startup_check_strict_raises_ready_only_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _consumer(monkeypatch, tmp_path, V2Breaking)
    core = app.state.test_core
    result = await rpc.check_remote_contract_compat(core)
    assert result.status == "fail"
    monkeypatch.setattr(settings, "contract_compat_check", "strict")
    with pytest.raises(rpc.ContractIncompatibleError, match="new required parameter"):
        await rpc.check_remote_contract_compat(core)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli_core(impl: Any) -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core._registering_module = "sandloom.shop"
    core.register_service("shop.items.v1", impl)
    core._registering_module = None
    return core


def test_cli_check_against_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    released = tmp_path / "contracts.snapshot.json"
    released.write_text(
        contract_compat.render_snapshot(
            contract_compat.module_snapshots(_cli_core(V1))["sandloom.shop"]
        )
    )
    monkeypatch.setattr(contracts_cli, "_build_core", lambda: _cli_core(V2Additive))
    assert contracts_cli.main(["check", "--against", str(released)]) == 0
    monkeypatch.setattr(contracts_cli, "_build_core", lambda: _cli_core(V2Breaking))
    assert contracts_cli.main(["check", "--against", str(released)]) == 1
    out = capsys.readouterr().out
    assert "INCOMPATIBLE: shop.items.v1.get(region): new required parameter" in out
    # As a consumer of the released provider, the additive build is fine for
    # what both have, but its new method is missing there.
    monkeypatch.setattr(contracts_cli, "_build_core", lambda: _cli_core(V2Additive))
    assert (
        contracts_cli.main(["check", "--against", str(released), "--as", "consumer"])
        == 1
    )
    assert "count: method removed by the provider" in capsys.readouterr().out
