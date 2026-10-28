"""Regression tests for the second production-readiness review.

Covers: event schema_version normalization, multi-database publish_event
disambiguation, strict infra startup, atomic module-registration rollback, and
the enriched JWT claims.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core.descriptors import HealthResult
from sandloom.core.eventing.envelope import normalize_schema_version
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import CacheProvider
from sandloom.core.infra.registry import InfraRegistry, InfraStartupError
from sandloom.core.unit_of_work import scope as uow_mod_scope


# --------------------------------------------------------------------------- #
# #4 schema_version normalization
# --------------------------------------------------------------------------- #
def test_normalize_schema_version_derives_from_channel_suffix() -> None:
    from sandloom.core.contracts.validators import ContractValidationError

    # A versioned channel is authoritative: the default "v1" is overridden.
    assert normalize_schema_version("billing.invoice.created.v2") == "v2"
    assert normalize_schema_version("billing.invoice.created.v2", "v1") == "v2"
    # An explicit version that MATCHES the suffix is fine.
    assert normalize_schema_version("billing.invoice.created.v2", "v2") == "v2"
    # An explicit version that CONTRADICTS a versioned suffix is rejected.
    with pytest.raises(ContractValidationError, match="contradicts"):
        normalize_schema_version("billing.invoice.created.v2", "v3")
    # An unversioned channel keeps the given (explicit) version.
    assert normalize_schema_version("billing.invoice.created") == "v1"
    assert normalize_schema_version("billing.invoice.created", "v5") == "v5"


# --------------------------------------------------------------------------- #
# #2 multi-database publish_event disambiguation
# --------------------------------------------------------------------------- #
class _FakeSession:
    def __init__(self, name: str) -> None:
        self.info: dict[str, Any] = {uow_mod_scope._DB_NAME_KEY: name}


def _bind(active: tuple[Any, ...], current: Any):
    st = uow_mod_scope._active_uow_sessions.set(active)
    ct = uow_mod_scope._current_uow_session.set(current)
    return st, ct


def test_resolve_scoped_session_single_database() -> None:
    s1 = _FakeSession("default")
    st, ct = _bind((s1,), s1)
    try:
        assert uow_mod_scope._resolve_scoped_session(None, op="publish_event") is s1
        assert (
            uow_mod_scope._resolve_scoped_session("default", op="publish_event") is s1
        )
    finally:
        uow_mod_scope._active_uow_sessions.reset(st)
        uow_mod_scope._current_uow_session.reset(ct)


def test_resolve_scoped_session_ambiguous_without_database() -> None:
    s1, s2 = _FakeSession("default"), _FakeSession("analytics")
    st, ct = _bind((s1, s2), s2)
    try:
        with pytest.raises(RuntimeError, match="ambiguous"):
            uow_mod_scope._resolve_scoped_session(None, op="publish_event")
        # Explicit database picks the right session deterministically — for both
        # publish_event and record_audit (same resolver).
        assert (
            uow_mod_scope._resolve_scoped_session("analytics", op="publish_event") is s2
        )
        assert uow_mod_scope._resolve_scoped_session("default", op="record_audit") is s1
        with pytest.raises(RuntimeError, match="no open Unit of Work"):
            uow_mod_scope._resolve_scoped_session("nonexistent", op="record_audit")
    finally:
        uow_mod_scope._active_uow_sessions.reset(st)
        uow_mod_scope._current_uow_session.reset(ct)


def test_resolve_scoped_session_outside_uow_is_none() -> None:
    assert uow_mod_scope._resolve_scoped_session(None, op="publish_event") is None


# --------------------------------------------------------------------------- #
# #8 strict infra startup
# --------------------------------------------------------------------------- #
class _BoomCache(CacheProvider):
    """A cache provider whose connection open fails."""

    def __init__(self, *, name: str, driver: str, options: dict[str, Any]) -> None:
        super().__init__(name=name, driver=driver, options=options)

    async def aopen(self) -> None:
        raise ConnectionError("cannot reach cache")

    async def get(self, key: str) -> str | None:  # pragma: no cover - unused
        return None

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:  # pragma: no cover - unused
        return None

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:  # pragma: no cover - unused
        return True

    async def delete(self, key: str) -> None:  # pragma: no cover - unused
        return None

    async def incr(
        self, key: str, *, ttl_seconds: int | None = None
    ) -> int:  # pragma: no cover - unused
        return 1

    async def health(self) -> HealthResult:  # pragma: no cover - unused
        return HealthResult(status="fail")


def _boom_registry() -> InfraRegistry:
    reg = InfraRegistry(
        config={
            "cache": {
                "default": InstanceConfig(
                    capability="cache", name="default", driver="boom", options={}
                )
            }
        }
    )
    reg.register_driver(
        "cache",
        "boom",
        lambda cfg: _BoomCache(
            name=cfg.name, driver=cfg.driver, options=dict(cfg.options)
        ),
    )
    return reg


@pytest.mark.asyncio
async def test_aopen_all_strict_aborts_on_failure() -> None:
    reg = _boom_registry()
    with pytest.raises(InfraStartupError, match="cache/default"):
        await reg.aopen_all(strict=True)


@pytest.mark.asyncio
async def test_aopen_all_best_effort_skips_failure() -> None:
    reg = _boom_registry()
    # Dev default: a failure is logged and skipped, never raised.
    await reg.aopen_all(strict=False)


# --------------------------------------------------------------------------- #
# #6 atomic module-registration rollback
# --------------------------------------------------------------------------- #
def test_module_apply_rolls_back_when_a_step_fails() -> None:
    from sandloom.core.facade import CoreFacade
    from sandloom.core.module_api import ModuleRegistrationDraft
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    app = FastAPI()
    facade = CoreFacade(app=app)
    facade.register_schema_registry(InMemorySchemaRegistry())

    routes_before = len(app.router.routes)

    draft = ModuleRegistrationDraft()
    draft.services.append(("demo.svc.v1", object()))

    # Force the LAST apply step (router include) to fail; everything applied
    # before it must be rewound.
    class _BadRouter:
        pass

    def _boom_include(_router: Any, **_kwargs: Any) -> None:
        raise RuntimeError("router include failed")

    draft.routers.append((_BadRouter(), {}))
    app.include_router = _boom_include  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="router include failed"):
        draft.apply(facade)

    # The service registered earlier in apply() was rolled back — no orphan.
    assert not facade.service_registry.contains("demo.svc.v1")
    assert len(app.router.routes) == routes_before


# --------------------------------------------------------------------------- #
# #9 enriched JWT claims
# --------------------------------------------------------------------------- #
def test_jwt_carries_registered_claims_by_default() -> None:
    from sandloom.core.auth import tokens

    token = tokens.create_access_token({"sub": "user-1", "token_type": "access"})
    claims = tokens.decode_token(token)
    assert claims is not None
    assert claims["sub"] == "user-1"
    # iat / nbf / jti are always present; exp too.
    for field in ("iat", "nbf", "jti", "exp"):
        assert field in claims
    # No issuer/audience emitted when unconfigured.
    assert "iss" not in claims
    assert "aud" not in claims


def test_jwt_issuer_audience_enforced_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.auth import tokens
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(settings, "jwt_issuer", "sandloom", raising=False)
    monkeypatch.setattr(settings, "jwt_audience", "sandloom-clients", raising=False)

    token = tokens.create_access_token({"sub": "user-2", "token_type": "access"})
    claims = tokens.decode_token(token)
    assert claims is not None
    assert claims["iss"] == "sandloom"
    assert claims["aud"] == "sandloom-clients"

    # A token minted for a DIFFERENT audience must be rejected.
    monkeypatch.setattr(settings, "jwt_audience", "someone-else", raising=False)
    other = tokens.create_access_token({"sub": "user-2", "token_type": "access"})
    monkeypatch.setattr(settings, "jwt_audience", "sandloom-clients", raising=False)
    assert tokens.decode_token(other) is None
