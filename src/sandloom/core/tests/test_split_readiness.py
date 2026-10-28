"""Split-deployment probe semantics (/health, /ready) and module requirements.

Pins the review findings of the microservices profile:

- a DOWN PEER never fails liveness (``/health``) and fails readiness only for
  bindings explicitly marked ``"required": true``; required probes run
  concurrently with a short cap and are pair-MAC authenticated like calls;
- a service whose modules all failed to load is never ready, and modules an
  operator lists explicitly in ``SANDLOOM_MODULES`` are required by default;
- ``SERVICE_BINDINGS`` is validated strictly at startup.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from sandloom.core.app_factory import ConfigurationError, enforce_configuration
from sandloom.core.config.settings import Settings, settings
from sandloom.core.descriptors import HealthResult
from sandloom.core.facade import CoreFacade
from sandloom.core.loader import plugin_loader
from sandloom.core.loader.plugin_loader import (
    ModuleLoadError,
    apply_module_defaults,
    load_modules_with_summary,
)
from sandloom.core.rpc import check_remote_service_bindings
from sandloom.core.schema.registry import InMemorySchemaRegistry

_TOKEN = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"
_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
    "CACHE_URL": "redis://localhost:6379/0",
    "BROKER_URL": "redis://localhost:6379/1",
    "JWT_SECRET": _TOKEN,
}

#: A configuration is refused at load (``Settings``) or at boot
#: (:func:`sandloom.core.app_factory.enforce_configuration`).
_REFUSED = (ValidationError, ConfigurationError)


def _closed_port_url() -> str:
    """A loopback URL nobody listens on (connection refused, instantly)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


def _ops_app(
    monkeypatch: pytest.MonkeyPatch,
    bindings: dict[str, dict[str, object]],
    *,
    loaded: list[str] | None = None,
    requested: list[str] | None = None,
) -> FastAPI:
    from sandloom.core.app_factory import _install_ops_endpoints

    app = FastAPI()
    core = CoreFacade(app=app)

    async def ok() -> HealthResult:
        return HealthResult(status="ok")

    core.register_health_check("db", ok)
    core.loaded_modules = ["sandloom_identity"] if loaded is None else loaded
    monkeypatch.setattr(settings, "service_bindings", bindings)
    monkeypatch.setattr(settings, "health_cache_ttl_seconds", 0.0)
    monkeypatch.setattr(settings, "module_required", [])
    monkeypatch.setattr(
        settings,
        "sandloom_modules",
        ["sandloom_identity"] if requested is None else requested,
    )
    monkeypatch.setattr(settings, "internal_api_token", _TOKEN)
    monkeypatch.setattr(settings, "service_probe_timeout_seconds", 0.5)
    _install_ops_endpoints(app, core)
    return app


# --------------------------------------------------------------------------- #
# #2 — a down peer never fails liveness; readiness only when opted in
# --------------------------------------------------------------------------- #
async def test_down_peer_does_not_fail_health_or_ready_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    down = _closed_port_url()
    app = _ops_app(
        monkeypatch,
        {
            "billing.entitlements.v1": {"base_url": down},
            "billing.claims.v1": {"base_url": down, "critical": False},
        },
    )
    async with await _client(app) as client:
        health = await client.get("/health")
        ready = await client.get("/ready")
    assert health.status_code == 200, health.json()
    assert "services" not in health.json()["checks"]
    assert ready.status_code == 200, ready.json()


async def test_required_peer_down_fails_ready_but_never_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    down = _closed_port_url()
    app = _ops_app(
        monkeypatch,
        {
            "tenancy.membership.v1": {"base_url": down, "required": True},
            "billing.credits.v1": {"base_url": down},
        },
    )
    async with await _client(app) as client:
        health = await client.get("/health")
        ready = await client.get("/ready", headers={"X-Internal-Token": _TOKEN})
    assert health.status_code == 200
    assert ready.status_code == 503
    failures = ready.json()["required_services"]["details"]["remote_services"]
    assert set(failures) == {"tenancy.membership.v1"}  # optional one not probed


class _SlowClient:
    """Stands in for the pooled RPC client: every probe takes ``delay`` and is
    answered by ``provider`` (keyed like a real provider's response)."""

    def __init__(self, delay: float, provider: Any) -> None:
        self.delay = delay
        self.provider = provider
        self.calls: list[tuple[str, dict[str, str], float]] = []

    async def get(self, url: str, *, headers: dict[str, str], timeout: float) -> Any:
        from sandloom.core.contract_testing import rpc_response_headers

        self.calls.append((url, dict(headers), timeout))
        await asyncio.sleep(self.delay)
        return httpx.Response(
            200,
            headers=rpc_response_headers(self.provider, headers, 200, b""),
            request=httpx.Request("GET", url),
        )


async def test_required_probes_run_concurrently_capped_and_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.contract_testing import rpc_settings
    from sandloom.core.rpc import readiness as rpc_readiness

    billing = settings.model_copy(update=rpc_settings("billing", peers=["svc"]))
    fake = _SlowClient(delay=0.3, provider=billing)
    monkeypatch.setattr(rpc_readiness, "_rpc_client", lambda: fake)
    core = CoreFacade(app=FastAPI())
    for name, value in rpc_settings("svc", peers=["billing"]).items():
        monkeypatch.setattr(settings, name, value)
    pinned = {"base_url": "http://billing:2222", "service_id": "billing"}
    monkeypatch.setattr(
        settings,
        "service_bindings",
        {
            "billing.a.v1": {**pinned, "required": True},
            "billing.b.v1": {**pinned, "required": True},
            "billing.c.v1": {**pinned, "required": True, "timeout_seconds": 30},
            "billing.optional.v1": pinned,
        },
    )
    monkeypatch.setattr(settings, "service_probe_timeout_seconds", 0.75)
    monkeypatch.setattr(core, "settings", settings, raising=False)

    started = time.perf_counter()
    result = await check_remote_service_bindings(core)
    elapsed = time.perf_counter() - started

    assert result.status == "ok"
    assert len(fake.calls) == 3  # the optional binding is not probed
    assert elapsed < 0.6, f"probes ran sequentially ({elapsed:.2f}s)"
    by_url = {url.rsplit("/", 1)[1]: (hdrs, t) for url, hdrs, t in fake.calls}
    for hdrs, _ in by_url.values():
        assert ";aud=billing;" in hdrs["X-SANDLOOM-RPC-Auth"]  # MACed like a call
        assert "X-Internal-Token" not in hdrs
    assert all(t <= 0.75 for _, t in by_url.values())  # capped per probe


# --------------------------------------------------------------------------- #
# #3 — a service serving no module is never ready; listed modules required
# --------------------------------------------------------------------------- #
async def test_ready_fails_when_no_requested_module_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ops_app(monkeypatch, {}, loaded=[], requested=["sandloom.biling"])
    async with await _client(app) as client:
        ready = await client.get("/ready", headers={"X-Internal-Token": _TOKEN})
        health = await client.get("/health")
    assert ready.status_code == 503
    assert ready.json()["no_modules_loaded"] is True
    assert health.status_code == 200  # liveness: a restart cannot fix config


async def test_kernel_only_app_without_requested_modules_stays_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ops_app(monkeypatch, {}, loaded=[], requested=[])
    async with await _client(app) as client:
        assert (await client.get("/ready")).status_code == 200


@pytest.fixture
def _module_settings(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    monkeypatch.setattr(settings, "module_fail_fast", False)
    monkeypatch.setattr(plugin_loader, "_defaulted_modules", None)
    return monkeypatch


def test_explicitly_listed_modules_are_required_by_default(
    _module_settings: pytest.MonkeyPatch,
) -> None:
    _module_settings.setattr(settings, "sandloom_modules", ["sandloom.biling"])
    _module_settings.setattr(settings, "module_required", [])
    apply_module_defaults(
        ["sandloom_identity", "sandloom_billing"], ["sandloom_identity"]
    )
    assert settings.module_required == ["sandloom.biling"]

    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(InMemorySchemaRegistry())
    with pytest.raises(ModuleLoadError, match="sandloom.biling"):
        load_modules_with_summary(core)


def test_explicit_module_required_wins(_module_settings: pytest.MonkeyPatch) -> None:
    _module_settings.setattr(
        settings, "sandloom_modules", ["sandloom_identity", "sandloom_billing"]
    )
    _module_settings.setattr(settings, "module_required", ["sandloom_identity"])
    apply_module_defaults([], [])
    assert settings.module_required == ["sandloom_identity"]


def test_default_implied_modules_are_required_unless_declared_optional(
    _module_settings: pytest.MonkeyPatch,
) -> None:
    _module_settings.setattr(settings, "sandloom_modules", [])
    _module_settings.setattr(settings, "module_required", [])
    defaults = ["sandloom_identity", "sandloom_tenancy", "sandloom_billing"]
    apply_module_defaults(defaults, ["sandloom_billing"])
    assert settings.sandloom_modules == defaults
    assert settings.module_required == ["sandloom_identity", "sandloom_tenancy"]
    # A second composition in the same process must not mistake the filled
    # defaults for an explicit SANDLOOM_MODULES choice.
    _module_settings.setattr(settings, "module_required", [])
    apply_module_defaults(defaults, ["sandloom_billing"])
    assert settings.module_required == ["sandloom_identity", "sandloom_tenancy"]


def test_a_defaulted_module_that_fails_to_load_aborts_startup(
    _module_settings: pytest.MonkeyPatch,
) -> None:
    """Probe 6a/6b: a module of the root's defaults that fails to register
    must abort startup, not boot without it (its routes would answer 404)."""
    _module_settings.setattr(settings, "sandloom_modules", [])
    _module_settings.setattr(settings, "module_required", [])
    apply_module_defaults(["sandloom_identity", "sandloom.biling"], [])
    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(InMemorySchemaRegistry())
    with pytest.raises(ModuleLoadError, match="sandloom.biling"):
        load_modules_with_summary(core)


# --------------------------------------------------------------------------- #
# Low — SERVICE_BINDINGS validated at startup
# --------------------------------------------------------------------------- #
def _settings(**env: Any) -> Settings:
    cfg = Settings(_env_file=None, **{**_BASE_ENV, **env})  # type: ignore[call-arg]
    enforce_configuration(cfg)  # the checks a process runs at boot
    return cfg


@pytest.mark.parametrize(
    "binding,match",
    [
        ({"base_url": "http://billing:2222", "requried": True}, "unknown key"),
        ({"base_url": "billing:2222"}, "http"),
        ({"base_url": "ftp://billing"}, "http"),
        ({}, "base_url"),
        ({"base_url": "http://billing:2222", "timeout_seconds": 0}, "timeout"),
        ({"base_url": "http://billing:2222", "required": "yes"}, "required"),
        ({"base_url": "http://billing:2222", "critical": 0}, "critical"),
    ],
)
def test_invalid_service_binding_is_rejected(binding: object, match: str) -> None:
    with pytest.raises(_REFUSED, match=match):
        _settings(
            SERVICE_BINDINGS={"billing.entitlements.v1": binding},
            INTERNAL_API_TOKEN=_TOKEN,
        )


def _caller_keys() -> dict[str, str]:
    from sandloom.core.contract_testing import rpc_env

    return rpc_env(
        "identity",
        peers=["tenancy", "billing"],
        allowed_calls={
            "identity": [
                "billing.claims.v1",
                "billing.entitlements.v1",
                "tenancy.membership.v1",
            ]
        },
    )


def test_service_bindings_require_the_callers_rpc_keys() -> None:
    bindings = {
        "billing.entitlements.v1": {
            "base_url": "http://billing:2222",
            "service_id": "billing",
        }
    }
    # No X25519 key: no call could ever authenticate (an ops token or the
    # dev opt-in never stands in for it).
    with pytest.raises(_REFUSED, match="RPC_DH_PRIVATE_KEY"):
        _settings(SERVICE_BINDINGS=bindings, INTERNAL_API_TOKEN=_TOKEN)
    with pytest.raises(_REFUSED, match="RPC_DH_PRIVATE_KEY"):
        _settings(SERVICE_BINDINGS=bindings, INTERNAL_ENDPOINTS_OPEN_IN_DEV=True)
    _settings(SERVICE_BINDINGS=bindings, **_caller_keys())


def test_valid_service_bindings_accepted() -> None:
    cfg = _settings(
        SERVICE_BINDINGS={
            "tenancy.membership.v1": {
                "base_url": "https://tenancy.internal:2222",
                "required": True,
                "timeout_seconds": 0.5,
                "service_id": "tenancy",
            },
            "billing.claims.v1": {
                "url": "http://billing:2222",
                "critical": False,
                "service_id": "billing",
            },
        },
        **_caller_keys(),
    )
    assert cfg.service_bindings["tenancy.membership.v1"]["required"] is True
