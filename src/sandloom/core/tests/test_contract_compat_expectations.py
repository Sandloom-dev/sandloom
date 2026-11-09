"""The startup compatibility check reads the consumer's own expectations
(ADR-0013 decisions 3 and 4, KB-21; closes D4).

* A consumer-only install (no provider package, no ``CONTRACT_SNAPSHOT_PATHS``)
  is checked from the ``<stem>.describe.json`` its distribution ships beside
  the stub: a compatible provider passes, a breaking one is refused under
  ``strict`` and fails ``/ready`` under ``ready`` (mixed-version pair N/N+1 in
  both orders).
* The expectation beside the stub wins over ``CONTRACT_SNAPSHOT_PATHS`` and
  the provider package, and the check is narrowed to the composed
  ``consumes``.
* No expectation for a required remote contract is never a warning:
  ``strict`` refuses startup before any probe, ``ready`` fails ``/ready``
  with ``contract_unverified``; a copy that does not verify counts as none;
  an optional consumer needs none.
* The kernel families ``<m>.claims.v1`` / ``<m>.facts.v1`` are held to the
  kernel's own schema, never to a consumer copy.
* No I/O on the request path: the expectations are read once at startup,
  ``/ready`` reads no file, and a contract call sends the same requests
  with the check on as with it off.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from sandloom.core import contract_compat, rpc
from sandloom.core.config.settings import settings
from sandloom.core.contract_testing import rpc_settings
from sandloom.core.descriptors import (
    ContractRequirement,
    HealthResult,
    ModuleDescriptor,
)
from sandloom.core.descriptors import generated_provenance as provenance
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import contract_method, contract_schema
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.core.rpc import readiness as rpc_readiness
from sandloom.core.tests.test_contract_compat import V1, V2Additive, V2Breaking

_SVC = "svc"
_TOKEN = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"
_CONTRACT = "shop.items.v1"


# --------------------------------------------------------------------------- #
# A consumer-only install: the consumer's package with its own expectation
# --------------------------------------------------------------------------- #
def _expectation(impl: Any, methods: tuple[str, ...], contract: str) -> str:
    """What ``sandloom generate`` writes beside the stub (signed)."""
    describe = contract_schema(impl).describe()
    narrowed = {
        **describe,
        "methods": {k: v for k, v in describe["methods"].items() if k in methods},
    }
    doc = provenance.signed_expectation(
        {
            "format": 2,
            "contract": contract,
            "fingerprint": describe["hash"],
            "provider": {"distribution": "acme-shop", "class": "acme_shop.Items"},
            "generator_format": 2,
            "methods": sorted(methods),
            "describe": narrowed,
        }
    )
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def _consumer_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    *,
    expects: Any = None,
    methods: tuple[str, ...] = ("get", "put"),
    contract: str = _CONTRACT,
) -> Path:
    """An importable consumer distribution ``name`` (not imported) holding
    its expectation of ``contract`` built from ``expects``."""
    package = tmp_path / name
    out = package / "_generated" / "contracts"
    out.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    if expects is not None:
        stem = "_".join(contract.split("."))
        path = out / f"{stem}.describe.json"
        path.write_text(_expectation(expects, methods, contract))
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    return package


def _provider_app(impl: Any, contract: str = _CONTRACT) -> FastAPI:
    from sandloom.core.tests.test_service_rpc import _wire_fake_database

    app = FastAPI()
    core = CoreFacade(app=app)
    _wire_fake_database(core, [])
    core._registering_module = contract.split(".", 1)[0]
    core.register_service(contract, impl)
    core._registering_module = None
    app.state.core = core
    app.include_router(rpc.rpc_router(core))
    return app


class _Wire:
    """Every request a caller sends to the provider, in order."""

    def __init__(self, provider: FastAPI) -> None:
        self.requests: list[tuple[str, str]] = []
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=provider),
            base_url="http://shop",
            event_hooks={"request": [self._record]},
        )

    async def _record(self, request: httpx.Request) -> None:
        self.requests.append((request.method, request.url.path))


def _consumer_app(
    monkeypatch: pytest.MonkeyPatch,
    wire: _Wire,
    *,
    module: str,
    consumes: list[ContractRequirement],
    mode: str = "strict",
    bindings: tuple[str, ...] = (_CONTRACT,),
) -> tuple[FastAPI, CoreFacade]:
    from sandloom.core.app_factory import _install_ops_endpoints

    monkeypatch.setattr(rpc_readiness, "_rpc_client", lambda: wire.client)
    app = FastAPI()
    core = CoreFacade(app=app)

    async def ok() -> HealthResult:
        return HealthResult(status="ok")

    core.register_health_check("db", ok)
    core.loaded_modules = [module]
    core.module_descriptors = [
        ModuleDescriptor(name=module, version="1", consumes=consumes)
    ]
    for name, value in rpc_settings(_SVC, allowed_calls={_SVC: list(bindings)}).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(
        settings,
        "service_bindings",
        {b: {"base_url": "http://shop", "service_id": _SVC} for b in bindings},
    )
    monkeypatch.setattr(settings, "contract_snapshot_paths", [])
    monkeypatch.setattr(settings, "contract_compat_check", mode)
    monkeypatch.setattr(settings, "health_cache_ttl_seconds", 0.0)
    monkeypatch.setattr(settings, "module_required", [])
    monkeypatch.setattr(settings, "sandloom_modules", [module])
    monkeypatch.setattr(settings, "internal_api_token", _TOKEN)
    _install_ops_endpoints(app, core)
    return app, core


async def _ready(app: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get("/ready", headers={"X-Internal-Token": _TOKEN})


def _uses(*methods: str, required: bool = True) -> list[ContractRequirement]:
    return [ContractRequirement(_CONTRACT, required=required, methods=methods)]


def _no_provider_package() -> None:
    assert importlib.util.find_spec("shop") is None
    assert importlib.util.find_spec("acme_shop") is None


# --------------------------------------------------------------------------- #
# D4: a consumer-only install is checked from its own expectation
# --------------------------------------------------------------------------- #
async def test_consumer_only_install_is_checked_from_its_expectation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = _consumer_package(monkeypatch, tmp_path, "kb21_orders_a", expects=V1)
    _no_provider_package()
    wire = _Wire(_provider_app(V1))
    _, core = _consumer_app(
        monkeypatch, wire, module="kb21_orders_a", consumes=_uses("get", "put")
    )
    result = await rpc.check_remote_contract_compat(core)
    assert result.status == "ok"
    assert result.details == {"probed": [_CONTRACT]}
    found = contract_compat.expected_contracts(core, [_CONTRACT])[_CONTRACT]
    assert found.source == str(
        package / "_generated" / "contracts" / "shop_items_v1.describe.json"
    )
    assert found.consumer == "kb21_orders_a"


@pytest.mark.parametrize(
    ("provider", "expects", "compatible"),
    [
        (V1, V1, True),  # N / N
        (V2Additive, V1, True),  # provider upgraded first: N+1 provider, N consumer
        (V1, V2Additive, False),  # consumer upgraded first and calls count
        (V2Breaking, V1, False),  # a breaking provider
    ],
    ids=["n-n", "provider-first", "consumer-first-new-method", "breaking"],
)
async def test_mixed_version_pair_consumer_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: Any,
    expects: Any,
    compatible: bool,
) -> None:
    methods = ("count", "get", "put") if expects is V2Additive else ("get", "put")
    name = f"kb21_pair_{id(provider)}_{id(expects)}"
    _consumer_package(monkeypatch, tmp_path, name, expects=expects, methods=methods)
    wire = _Wire(_provider_app(provider))
    app, core = _consumer_app(
        monkeypatch, wire, module=name, consumes=_uses(*methods), mode="ready"
    )
    ready = await _ready(app)
    assert (ready.status_code == 200) is compatible, ready.text
    monkeypatch.setattr(settings, "contract_compat_check", "strict")
    if compatible:
        assert (await rpc.check_remote_contract_compat(core)).status == "ok"
        return
    failures = ready.json()["required_services"]["details"]["remote_services"]
    assert "contract_incompatible" in failures[_CONTRACT]
    assert failures[_CONTRACT]["expected_from"].endswith("shop_items_v1.describe.json")
    with pytest.raises(rpc.ContractIncompatibleError, match=r"expected_from"):
        await rpc.check_remote_contract_compat(core)


async def test_the_expectation_beside_the_stub_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The operator's snapshot says V2Breaking; the consumer's copy says V1.
    _consumer_package(monkeypatch, tmp_path, "kb21_orders_b", expects=V1)
    snapshot = tmp_path / "contracts.snapshot.json"
    snapshot.write_text(
        json.dumps({"contracts": {_CONTRACT: contract_schema(V2Breaking).describe()}})
    )
    wire = _Wire(_provider_app(V1))
    _, core = _consumer_app(
        monkeypatch, wire, module="kb21_orders_b", consumes=_uses("get", "put")
    )
    monkeypatch.setattr(settings, "contract_snapshot_paths", [str(snapshot)])
    assert (await rpc.check_remote_contract_compat(core)).status == "ok"
    found = contract_compat.expected_contracts(core, [_CONTRACT])[_CONTRACT]
    assert found.source.endswith(
        "kb21_orders_b/_generated/contracts/shop_items_v1.describe.json"
    )


async def test_narrowed_to_the_composed_consumes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Built against V2Additive, the consumer calls only put; the provider (V1)
    # lacks count and changed get, which it never calls.
    _consumer_package(
        monkeypatch, tmp_path, "kb21_orders_c", expects=V2Additive, methods=("put",)
    )
    wire = _Wire(_provider_app(V1))
    _, core = _consumer_app(
        monkeypatch, wire, module="kb21_orders_c", consumes=_uses("put")
    )
    assert (await rpc.check_remote_contract_compat(core)).status == "ok"
    # The same copy narrowed to every method it describes is refused.
    wide = _consumer_package(
        monkeypatch,
        tmp_path / "wide",
        "kb21_orders_c2",
        expects=V2Additive,
        methods=("count", "get", "put"),
    )
    assert wide.is_dir()
    _, core = _consumer_app(
        monkeypatch,
        wire,
        module="kb21_orders_c2",
        consumes=_uses("count", "get", "put"),
    )
    with pytest.raises(rpc.ContractIncompatibleError, match="count: method removed"):
        await rpc.check_remote_contract_compat(core)


async def test_a_consumed_method_its_copy_does_not_describe_is_unverified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A stale copy: consumes now lists put, the copy describes only get.
    _consumer_package(
        monkeypatch, tmp_path, "kb21_orders_d", expects=V1, methods=("get",)
    )
    wire = _Wire(_provider_app(V1))
    _, core = _consumer_app(
        monkeypatch, wire, module="kb21_orders_d", consumes=_uses("get", "put")
    )
    with pytest.raises(rpc.ContractUnverifiedError, match="calls put"):
        await rpc.check_remote_contract_compat(core)
    assert wire.requests == []


# --------------------------------------------------------------------------- #
# No expectation is never a warning (decision 4)
# --------------------------------------------------------------------------- #
async def test_no_expectation_refuses_strict_before_any_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _consumer_package(monkeypatch, tmp_path, "kb21_orders_e")  # no copy at all
    wire = _Wire(_provider_app(V1))
    _, core = _consumer_app(
        monkeypatch, wire, module="kb21_orders_e", consumes=_uses("get")
    )
    with pytest.raises(rpc.ContractUnverifiedError, match="no expectation") as exc:
        await rpc.check_remote_contract_compat(core)
    assert "consumed by kb21_orders_e" in str(exc.value)
    assert "sandloom generate" in str(exc.value)
    assert wire.requests == []  # a preflight: refused before any probe
    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert not any(r.levelname == "WARNING" for r in caplog.records)


async def test_no_expectation_fails_ready_with_contract_unverified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _consumer_package(monkeypatch, tmp_path, "kb21_orders_f")
    wire = _Wire(_provider_app(V1))
    app, core = _consumer_app(
        monkeypatch, wire, module="kb21_orders_f", consumes=_uses("get"), mode="ready"
    )
    result = await rpc.check_remote_contract_compat(core)  # ready: logs, starts
    assert result.status == "fail"
    ready = await _ready(app)
    assert ready.status_code == 503
    failures = ready.json()["required_services"]["details"]["remote_services"]
    assert "contract_unverified" in failures[_CONTRACT]


async def test_a_binding_nobody_consumes_needs_an_expectation_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A hand-written ContractRef / get_service with no stub and no consumes.
    _consumer_package(monkeypatch, tmp_path, "kb21_orders_g")
    wire = _Wire(_provider_app(V1))
    _, core = _consumer_app(monkeypatch, wire, module="kb21_orders_g", consumes=[])
    with pytest.raises(rpc.ContractUnverifiedError, match="bound in SERVICE_BINDINGS"):
        await rpc.check_remote_contract_compat(core)


async def test_a_hand_edited_copy_counts_as_no_expectation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = _consumer_package(monkeypatch, tmp_path, "kb21_orders_h", expects=V1)
    path = package / "_generated" / "contracts" / "shop_items_v1.describe.json"
    doc = json.loads(path.read_text())
    del doc["describe"]["methods"]["put"]  # loosen the check by hand
    path.write_text(json.dumps(doc))
    wire = _Wire(_provider_app(V1))
    _, core = _consumer_app(
        monkeypatch, wire, module="kb21_orders_h", consumes=_uses("get")
    )
    with pytest.raises(rpc.ContractUnverifiedError, match="hand-edited"):
        await rpc.check_remote_contract_compat(core)


async def test_an_optional_consumer_needs_no_expectation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _consumer_package(monkeypatch, tmp_path, "kb21_orders_i")
    wire = _Wire(_provider_app(V1))
    _, core = _consumer_app(
        monkeypatch,
        wire,
        module="kb21_orders_i",
        consumes=_uses("get", required=False),
    )
    assert (await rpc.check_remote_contract_compat(core)).status == "ok"


# --------------------------------------------------------------------------- #
# Kernel families are held to the kernel's schema
# --------------------------------------------------------------------------- #
def test_kernel_family_shapes_match_the_kernel_providers() -> None:
    from sandloom.core.facade._claims import ClaimsContributorContract
    from sandloom.shared.authz.facts_contract import FactsContract

    def methods(impl: Any) -> dict[str, Any]:
        return dict(contract_schema(impl).describe()["methods"])

    claims = ClaimsContributorContract(CoreFacade(app=FastAPI()), "grants")
    facts = FactsContract(CoreFacade(app=FastAPI()), "grants")
    assert methods(claims) == methods(contract_compat.KERNEL_FAMILIES["claims"])
    assert methods(facts) == methods(contract_compat.KERNEL_FAMILIES["facts"])
    assert contract_compat.kernel_family("grants.claims.v1") is not None
    assert contract_compat.kernel_family("grants.facts.v1") is not None
    assert contract_compat.kernel_family("grants.claims.v2") is None
    assert contract_compat.kernel_family("grants.roles.v1") is None


class _GrantsClaims:
    @contract_method
    async def contribute(self, user_id: Any) -> dict[str, Any]:
        return {}


class _GrantsClaimsBroken:
    @contract_method
    async def contribute(self, user_id: Any, scope: str) -> dict[str, Any]:
        return {}


@pytest.mark.parametrize(
    ("impl", "ok"), [(_GrantsClaims(), True), (_GrantsClaimsBroken(), False)]
)
async def test_a_remote_claims_contract_under_strict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, impl: Any, ok: bool
) -> None:
    # No copy, no snapshot, no provider package: the kernel's schema.
    _consumer_package(monkeypatch, tmp_path, "kb21_issuer")
    wire = _Wire(_provider_app(impl, "grants.claims.v1"))
    _, core = _consumer_app(
        monkeypatch,
        wire,
        module="kb21_issuer",
        consumes=[],
        bindings=("grants.claims.v1",),
    )
    found = contract_compat.expected_contracts(core, ["grants.claims.v1"])
    assert found["grants.claims.v1"].source == "kernel schema of <m>.claims.v1"
    if ok:
        assert (await rpc.check_remote_contract_compat(core)).status == "ok"
    else:
        with pytest.raises(rpc.ContractIncompatibleError, match="scope"):
            await rpc.check_remote_contract_compat(core)


# --------------------------------------------------------------------------- #
# No I/O on the request path (counts)
# --------------------------------------------------------------------------- #
def _count(
    monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, calls: list[str]
) -> None:
    real: Callable[..., Any] = getattr(owner, name)

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(name)
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, counted)


async def _call_twice(wire: _Wire) -> list[tuple[str, str]]:
    """The requests of a first and a steady call from a fresh process."""
    rpc_proxy._remote_contracts.clear()
    rpc_proxy._describe_failed.clear()
    proxy = rpc.RemoteServiceProxy(
        service_name=_CONTRACT,
        binding={"base_url": "http://shop", "service_id": _SVC},
        default_timeout_seconds=5.0,
        client=wire.client,
    )
    start = len(wire.requests)
    assert await proxy.get(org_id=uuid4()) == []  # first call
    assert await proxy.get(org_id=uuid4()) == []  # steady call
    return wire.requests[start:]


async def test_expectations_are_read_at_startup_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _consumer_package(monkeypatch, tmp_path, "kb21_orders_j", expects=V1)
    wire = _Wire(_provider_app(V1))
    app, core = _consumer_app(
        monkeypatch, wire, module="kb21_orders_j", consumes=_uses("get", "put")
    )
    reads: list[str] = []
    _count(monkeypatch, contract_compat, "read_expectation", reads)
    _count(monkeypatch, contract_compat, "module_package_dir", reads)
    _count(monkeypatch, contract_compat, "load_snapshot_contracts", reads)

    assert (await rpc.check_remote_contract_compat(core)).status == "ok"
    startup_reads = list(reads)
    assert startup_reads.count("read_expectation") == 1
    assert wire.requests == [("GET", f"/_sandloom/rpc/{_CONTRACT}")]  # one probe

    reads.clear()
    wire.requests.clear()
    for _ in range(3):
        assert (await _ready(app)).status_code == 200
    assert reads == []  # /ready reads no file
    # /ready probes as before: one describe GET per remote binding per probe.
    assert wire.requests == [("GET", f"/_sandloom/rpc/{_CONTRACT}")] * 3

    wire.requests.clear()
    calls_on = await _call_twice(wire)
    assert reads == []
    monkeypatch.setattr(settings, "contract_compat_check", "off")
    calls_off = await _call_twice(wire)
    # The first and the steady call send the same requests with the check on
    # as with it off: the check adds nothing to the request path.
    assert calls_on == calls_off
    call, describe = (
        ("POST", f"/_sandloom/rpc/{_CONTRACT}/get"),
        ("GET", f"/_sandloom/rpc/{_CONTRACT}"),
    )
    # First call: the call, then the proxy's own describe (as at base);
    # steady call: the call alone.
    assert calls_on == [call, describe, call]
    assert reads == []
