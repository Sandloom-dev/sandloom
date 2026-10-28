"""PRODUCTION_PROFILE=multi_tenant_strict — the trust model, enforced.

* configuration: in prod the profile refuses every setting that leaves a layer
  of the multi-tenant trust model off (RLS per database, role verification,
  fail-closed revocation, a separate security cache on its own Redis, the
  contract compatibility check when split) and turns fail-closed revocation
  on; outside prod, or with ``standard``, nothing changes;
* security-critical cache state goes to ``SECURITY_CACHE_INSTANCE``;
* at startup every ``TenantScoped`` table must carry the forced tenant policy
  (real PostgreSQL).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from sandloom.core import production_profile
from sandloom.core.app_factory import ConfigurationError, enforce_configuration
from sandloom.core.auth import revocation, tokens
from sandloom.core.config.common import strict_profile_active
from sandloom.core.config.settings import Settings, settings
from sandloom.core.idempotency import cache_store as idempotency_cache_store
from sandloom.core.infra import access
from sandloom.core.sql_alchemy.rls import enable_tenant_rls_statements
from sandloom.core.sql_alchemy.tenant_rls import tenant_rls_coverage
from sandloom.core.testing import reload_test_env

_STRONG = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"
_STRONG_2 = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b"
_COMPLIANT_INFRA = {
    "database": {"default": {"tenant_rls": True}},
    "cache": {"security": {"driver": "redis", "url": "redis://sec-redis:6379/0"}},
}

#: A configuration is refused at load (``Settings``) or at boot
#: (:func:`sandloom.core.app_factory.enforce_configuration`).
_REFUSED = (ValidationError, ConfigurationError)


def _split_env() -> dict[str, Any]:
    """A keyed billing process binding tenancy (internal RPC is pair-MAC)."""
    from sandloom.core.contract_testing import rpc_env

    return {
        "SERVICE_BINDINGS": {
            "tenancy.membership.v1": {
                "base_url": "http://tenancy:2222",
                "service_id": "tenancy",
            }
        },
        **rpc_env(
            "billing",
            peers=["tenancy"],
            allowed_calls={"billing": ["tenancy.membership.v1"]},
        ),
    }


_BASE = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
    "CACHE_URL": "redis://cache-redis:6379/0",
    "BROKER_URL": "redis://cache-redis:6379/1",
    "JWT_SECRET": _STRONG,
    "ENVIRONMENT": "prod",
    "PRODUCTION_PROFILE": "multi_tenant_strict",
    "INFRA": _COMPLIANT_INFRA,
    "SECURITY_CACHE_INSTANCE": "security",
}


def _settings(**env: Any) -> Settings:
    cfg = Settings(_env_file=None, **{**_BASE, **env})  # type: ignore[call-arg]
    enforce_configuration(cfg)  # the checks a process runs at boot
    return cfg


# --------------------------------------------------------------------------- #
# Configuration half
# --------------------------------------------------------------------------- #
def test_compliant_strict_profile_starts_and_forces_fail_closed() -> None:
    cfg = _settings()
    assert cfg.production_profile == "multi_tenant_strict"
    assert cfg.auth_revocation_fail_closed is True
    assert strict_profile_active(cfg)


def test_default_production_config_is_refused_with_every_reason() -> None:
    with pytest.raises(_REFUSED) as exc_info:
        _settings(
            INFRA={},
            SECURITY_CACHE_INSTANCE="default",
            AUTH_REVOCATION_FAIL_CLOSED="false",
        )
    message = str(exc_info.value)
    assert "PRODUCTION_PROFILE=multi_tenant_strict is not met" in message
    assert "database/default: tenant_rls is off" in message
    assert "SECURITY_CACHE_INSTANCE is 'default'" in message
    assert "AUTH_REVOCATION_FAIL_CLOSED=false" in message


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"TENANCY_MODE": "single", "SINGLE_TENANT_ID": str(uuid.uuid4())},
            "TENANCY_MODE must be 'multi'",
        ),
        (
            {
                "INFRA": {
                    **_COMPLIANT_INFRA,
                    "database": {
                        "default": {"tenant_rls": True},
                        "analytics": {"url": "postgresql+asyncpg://u:p@a/a"},
                    },
                }
            },
            "database/analytics: tenant_rls is off",
        ),
        (
            {
                "INFRA": {
                    **_COMPLIANT_INFRA,
                    "cache": {"security": {"url": "redis://cache-redis:6379/5"}},
                }
            },
            "same Redis server as the default cache",
        ),
        (
            {"SECURITY_CACHE_INSTANCE": "sessions"},
            "SECURITY_CACHE_INSTANCE='sessions' is not declared",
        ),
        (
            {**_split_env(), "CONTRACT_COMPAT_CHECK": "off"},
            "CONTRACT_COMPAT_CHECK=off",
        ),
    ],
)
def test_each_trust_layer_is_required(overrides: dict[str, Any], reason: str) -> None:
    with pytest.raises(_REFUSED, match="multi_tenant_strict is not met") as exc:
        _settings(**overrides)
    assert reason in str(exc.value)


def _ed25519_pair() -> tuple[str, str]:
    from sandloom.core.signing_keys import generate_private_key, private_pem, public_pem

    key = generate_private_key("EdDSA")
    return private_pem(key), public_pem(key)


_SPLIT = _split_env()


def test_split_requires_asymmetric_jwt() -> None:
    with pytest.raises(_REFUSED, match="shared HS256 secret"):
        _settings(**_SPLIT)
    with pytest.raises(_REFUSED, match="shared_hs_secret_split"):
        _settings(**_SPLIT, SANDLOOM_ACCEPTED_RISKS="shared_hs_secret_split")
    priv, pub = _ed25519_pair()
    # A verify-only split service and the issuer both meet the profile.
    verifier = _settings(
        **_SPLIT, JWT_SECRET="", JWT_ALGORITHM="EdDSA", JWT_PUBLIC_KEYS={"k1": pub}
    )
    issuer = _settings(
        **_SPLIT,
        JWT_ALGORITHM="EdDSA",
        JWT_PRIVATE_KEY=priv,
        JWT_KID="k1",
        JWT_PUBLIC_KEYS={"k1": pub},
    )
    for cfg in (verifier, issuer):
        assert production_profile.config_problems(cfg) == ([], [])
    assert tokens.token_signing_problem(issuer_loaded=False, cfg=verifier) is None
    assert tokens.token_signing_problem(issuer_loaded=True, cfg=issuer) is None


def test_startup_role_check_has_no_shared_secret_opt_out_under_the_profile() -> None:
    """A process loading no issuer is split even without bindings: the kernel
    role check refuses its HS secret under the profile, opt-out or not."""
    cfg = _settings()  # no bindings: passes the Settings half
    object.__setattr__(cfg, "sandloom_accepted_risks", ["shared_hs_secret_split"])
    problem = tokens.token_signing_problem(issuer_loaded=False, cfg=cfg)
    assert problem is not None and "multi_tenant_strict" in problem
    # The monolith (issuer loaded, no bindings) keeps its HS secret.
    assert tokens.token_signing_problem(issuer_loaded=True, cfg=_settings()) is None


def test_profile_is_inert_outside_prod_and_for_standard() -> None:
    dev = _settings(ENVIRONMENT="dev", INFRA={}, SECURITY_CACHE_INSTANCE="default")
    assert not strict_profile_active(dev)
    assert dev.auth_revocation_fail_closed is None
    standard = _settings(
        PRODUCTION_PROFILE="standard", INFRA={}, SECURITY_CACHE_INSTANCE="default"
    )
    assert not strict_profile_active(standard)


# --------------------------------------------------------------------------- #
# Security cache routing
# --------------------------------------------------------------------------- #
class _Recorder:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.store: dict[str, str] = {}

    def __call__(self, name: str = "default") -> Any:
        self.names.append(name)
        return self

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def add(self, key: str, value: str, ttl_seconds: int | None = None) -> bool:
        if key in self.store:
            return False
        self.store[key] = value
        return True

    async def compare_and_set(
        self, key: str, expected: str | None, value: str, ttl_seconds: int | None = None
    ) -> bool:
        if self.store.get(key) != expected:
            return False
        self.store[key] = value
        return True


async def test_security_state_uses_the_security_cache_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(access, "cache", recorder)
    monkeypatch.setattr(idempotency_cache_store, "cache", recorder)
    monkeypatch.setattr(settings, "security_cache_instance", "security")
    assert access.security_cache_name() == "security"
    await revocation.revoke_user_tokens(uuid.uuid4())
    await idempotency_cache_store._store_add("idem:k", "v", 60)
    assert recorder.names and set(recorder.names) == {"security"}
    # The default stays exactly `cache()` (no name) — backward compatible.
    recorder.names.clear()
    monkeypatch.setattr(settings, "security_cache_instance", "default")
    await idempotency_cache_store._store_add("idem:k2", "v", 60)
    assert recorder.names == ["default"]


# --------------------------------------------------------------------------- #
# Startup half: RLS covers every tenant table (real PostgreSQL)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


async def test_rls_coverage_reports_unprotected_tenant_tables() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    suffix = uuid.uuid4().hex[:10]
    covered, bare, unforced = (
        f"test_prof_cov_{suffix}",
        f"test_prof_bare_{suffix}",
        f"test_prof_nf_{suffix}",
    )
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            for table in (covered, bare, unforced):
                await conn.execute(
                    text(f"CREATE TABLE {table} (id uuid PRIMARY KEY, tenant_id uuid)")
                )
            for sql in enable_tenant_rls_statements(covered):
                await conn.execute(text(sql))
            for sql in enable_tenant_rls_statements(unforced):
                await conn.execute(text(sql))
            await conn.execute(
                text(f"ALTER TABLE {unforced} NO FORCE ROW LEVEL SECURITY")
            )
        async with engine.connect() as conn:
            present, problems = await tenant_rls_coverage(
                conn, [covered, bare, unforced, f"test_prof_absent_{suffix}"]
            )
        assert sorted(present) == sorted([covered, bare, unforced])
        joined = "\n".join(problems)
        assert covered not in joined
        assert f"{bare}: ROW LEVEL SECURITY not enabled" in joined
        assert f"policy '{bare}_tenant_isolation' missing" in joined
        assert f"{unforced}: not FORCEd" in joined
        assert "enable_tenant_rls(op," in joined
    finally:
        async with engine.begin() as conn:
            for table in (covered, bare, unforced):
                await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        await engine.dispose()


class _Provider:
    def __init__(self, present: list[str], problems: list[str]) -> None:
        self._result = (present, problems)

    async def tenant_rls_coverage(
        self, tables: list[str]
    ) -> tuple[list[str], list[str]]:
        return [t for t in self._result[0] if t in tables], self._result[1]


class _Infra:
    def __init__(self, providers: dict[str, Any]) -> None:
        self._providers = providers

    def instances(self, capability: str) -> list[str]:
        return list(self._providers)

    def provider(self, capability: str, name: str) -> Any:
        return self._providers[name]


class _Core:
    def __init__(self, infra: _Infra, cfg: Any) -> None:
        self.infra = infra
        self.settings = cfg


async def test_startup_enforcement_fails_on_an_uncovered_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _settings()
    ok = _Core(_Infra({"default": _Provider(["notes", "tasks"], [])}), cfg)
    assert (
        await production_profile.verify_tenant_rls_coverage(ok, ["notes", "tasks"])
        == []
    )
    bad = _Core(
        _Infra(
            {
                "default": _Provider(["notes"], []),
                "analytics": _Provider(["tasks"], ["tasks: not FORCEd"]),
            }
        ),
        cfg,
    )
    problems = await production_profile.verify_tenant_rls_coverage(
        bad, ["notes", "tasks", "ghost"]
    )
    assert "database/analytics: tasks: not FORCEd" in problems
    assert any("['ghost'] not found" in p for p in problems)
    # enforce_runtime raises the precise error (strict profile active).
    monkeypatch.setattr(
        production_profile, "tenant_scoped_tables", lambda: ["notes", "tasks"]
    )
    with pytest.raises(production_profile.ProductionProfileError, match="not FORCEd"):
        await production_profile.enforce_runtime(bad)
