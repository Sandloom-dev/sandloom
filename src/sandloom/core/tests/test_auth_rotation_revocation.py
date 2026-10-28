"""JWT key rotation (kid / previous secrets), required ``iat``, framework-wide
token revocation, and the auth-related settings validation."""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import jwt
import pytest
from fastapi import Depends, FastAPI
from pydantic import ValidationError
from starlette.testclient import TestClient

from sandloom.core.auth import bearer, marks, revocation, tokens
from sandloom.core.config.settings import Settings, settings

_STRONG = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"
_STRONG_2 = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b"
_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
    "CACHE_URL": "redis://h:6379/0",
    "BROKER_URL": "redis://h:6379/1",
}


def _settings(**env: Any) -> Settings:
    return Settings(_env_file=None, **{**_BASE_ENV, **env})  # type: ignore[call-arg]


class _MemoryCache:
    """Dict-backed stand-in for the cache capability (get/set only)."""

    def __init__(self, *, broken: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.broken = broken
        self.gets = 0

    async def get(self, key: str) -> str | None:
        self.gets += 1
        if self.broken:
            raise ConnectionError("cache down")
        return self.store.get(key)

    async def get_many(self, keys: list[str]) -> list[str | None]:
        """One round trip (like Redis MGET): counted as a single get."""
        self.gets += 1
        if self.broken:
            raise ConnectionError("cache down")
        return [self.store.get(k) for k in keys]

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        if self.broken:
            raise ConnectionError("cache down")
        self.store[key] = value

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        if self.broken:
            raise ConnectionError("cache down")
        if key in self.store:
            return False
        self.store[key] = value
        return True

    async def compare_and_set(
        self,
        key: str,
        expected: str,
        value: str,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        if self.broken:
            raise ConnectionError("cache down")
        if self.store.get(key) != expected:
            return False
        self.store[key] = value
        return True


class _InterleavingCache(_MemoryCache):
    """Yields to the event loop after every read: forces read/write races."""

    async def get(self, key: str) -> str | None:
        value = await super().get(key)
        await asyncio.sleep(0)
        return value


@pytest.fixture
def mem_cache(monkeypatch: pytest.MonkeyPatch) -> _MemoryCache:
    import sandloom.core.infra.access as access

    fake = _MemoryCache()
    monkeypatch.setattr(access, "cache", lambda name="default": fake)
    return fake


def _token(sub: str, *, iat_offset: int = 0, **extra: Any) -> str:
    now = datetime.now(UTC) + timedelta(seconds=iat_offset)
    return tokens.create_access_token(
        {"sub": sub, "token_type": "access", "iat": now, **extra}
    )


# --------------------------------------------------------------------------- #
# S1 — key rotation + required iat
# --------------------------------------------------------------------------- #
def test_kid_is_stamped_and_previous_secret_still_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "jwt_kid", "k1")
    old_token = tokens.create_access_token({"sub": "u", "token_type": "access"})
    assert jwt.get_unverified_header(old_token)["kid"] == "k1"

    # Rotate: new secret + kid; the old secret moves to JWT_PREVIOUS_SECRETS.
    old_secret = settings.jwt_secret
    monkeypatch.setattr(settings, "jwt_secret", _STRONG)
    monkeypatch.setattr(settings, "jwt_kid", "k2")
    monkeypatch.setattr(settings, "jwt_previous_secrets", [old_secret])
    claims = tokens.decode_token(old_token)
    assert claims is not None and claims["sub"] == "u"

    new_token = tokens.create_access_token({"sub": "v", "token_type": "access"})
    assert jwt.get_unverified_header(new_token)["kid"] == "k2"
    assert tokens.decode_token(new_token) is not None

    # Retiring the previous secret ends the old token's validity.
    monkeypatch.setattr(settings, "jwt_previous_secrets", [])
    assert tokens.decode_token(old_token) is None


def test_current_kid_is_verified_with_current_secret_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token claiming the CURRENT kid but signed with a retired key fails."""
    monkeypatch.setattr(settings, "jwt_kid", "k2")
    monkeypatch.setattr(settings, "jwt_previous_secrets", [_STRONG_2])
    now = int(time.time())
    forged = jwt.encode(
        {"sub": "u", "token_type": "access", "iat": now, "exp": now + 60},
        _STRONG_2,
        algorithm=settings.jwt_algorithm,
        headers={"kid": "k2"},
    )
    assert tokens.decode_token(forged) is None
    # Without the kid (or an unknown one) the previous secret is tried.
    legacy = jwt.encode(
        {"sub": "u", "token_type": "access", "iat": now, "exp": now + 60},
        _STRONG_2,
        algorithm=settings.jwt_algorithm,
    )
    assert tokens.decode_token(legacy) is not None


def test_unknown_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_previous_secrets", [_STRONG_2])
    now = int(time.time())
    foreign = jwt.encode(
        {"sub": "u", "iat": now, "exp": now + 60},
        "a-completely-different-secret-0123456789",
        algorithm=settings.jwt_algorithm,
    )
    assert tokens.decode_token(foreign) is None


def test_decode_requires_iat() -> None:
    now = int(time.time())
    no_iat = jwt.encode(
        {"sub": "u", "token_type": "access", "exp": now + 60},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    assert tokens.decode_token(no_iat) is None


def test_prod_previous_secrets_follow_jwt_secret_rules() -> None:
    with pytest.raises(ValidationError, match=r"JWT_PREVIOUS_SECRETS\[0\]"):
        _settings(
            ENVIRONMENT="prod",
            JWT_SECRET=_STRONG,
            JWT_PREVIOUS_SECRETS=["change_me_in_production_use_openssl_rand_hex_32"],
            TWOFA_ENCRYPTION_KEY=_STRONG_2,
        )
    ok = _settings(
        ENVIRONMENT="prod",
        JWT_SECRET=_STRONG,
        JWT_PREVIOUS_SECRETS=[_STRONG_2],
        JWT_KID="2026-10",
    )
    assert ok.jwt_previous_secrets == [_STRONG_2]
    assert ok.jwt_kid == "2026-10"


def test_previous_secrets_must_not_contain_current() -> None:
    with pytest.raises(ValidationError, match="must not contain the current"):
        _settings(JWT_SECRET="x", JWT_PREVIOUS_SECRETS=["x"])


# --------------------------------------------------------------------------- #
# S7 — billing / 2FA production settings
# --------------------------------------------------------------------------- #
def test_prod_rejects_explicit_fake_psp_unless_opted_in() -> None:
    with pytest.raises(ValidationError, match="BILLING_PROVIDER=fake"):
        _settings(ENVIRONMENT="prod", JWT_SECRET=_STRONG, BILLING_PROVIDER="fake")
    demo = _settings(
        ENVIRONMENT="prod",
        JWT_SECRET=_STRONG,
        BILLING_PROVIDER="fake",
        SANDLOOM_ACCEPTED_RISKS="fake_psp_in_prod",
    )
    assert demo.accepts_risk("fake_psp_in_prod")
    # Not configured at all (a deployment without billing): the default passes;
    # the billing module's critical startup check covers it when loaded.
    assert _settings(ENVIRONMENT="prod", JWT_SECRET=_STRONG).is_prod


def test_fake_psp_risk_is_not_accepted_by_default() -> None:
    cfg = _settings(JWT_SECRET="x")
    assert not cfg.accepts_risk("fake_psp_in_prod")


def test_prod_twofa_key_must_be_strong() -> None:
    with pytest.raises(ValidationError, match="TWOFA_ENCRYPTION_KEY"):
        _settings(ENVIRONMENT="prod", JWT_SECRET=_STRONG, TWOFA_ENCRYPTION_KEY="k")
    assert _settings(
        ENVIRONMENT="prod",
        JWT_SECRET=_STRONG,
        TWOFA_ENCRYPTION_KEY=f"{_STRONG_2},{_STRONG[::-1]}",
    ).is_prod


def test_revocation_fail_closed_defaults_by_environment() -> None:
    assert _settings(JWT_SECRET="x").revocation_fail_closed is False
    assert _settings(ENVIRONMENT="prod", JWT_SECRET=_STRONG).revocation_fail_closed
    assert (
        _settings(
            ENVIRONMENT="prod", JWT_SECRET=_STRONG, AUTH_REVOCATION_FAIL_CLOSED="false"
        ).revocation_fail_closed
        is False
    )


# --------------------------------------------------------------------------- #
# S2/S3 — framework-wide revocation
# --------------------------------------------------------------------------- #
def _guarded_app() -> FastAPI:
    from sandloom.core.di import TenantScope
    from sandloom.shared.authz import Perm, require
    from sandloom.shared.guards.auth import get_current_user_id

    app = FastAPI()

    @app.get("/me")
    async def _me(user_id: UUID = Depends(get_current_user_id)) -> dict[str, str]:
        return {"id": str(user_id)}

    @app.get("/perm", dependencies=[Depends(require(Perm("x.read")))])
    async def _perm() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/tenant")
    async def _tenant(tenant: Any = TenantScope()) -> dict[str, str]:
        return {"tenant": str(tenant)}

    return app


async def test_revoked_token_is_rejected_by_every_framework_guard(
    mem_cache: _MemoryCache,
) -> None:
    uid = str(uuid.uuid4())
    tenant = str(uuid.uuid4())
    old = _token(uid, iat_offset=-30, permissions=["x.read"], tenant_ids=[tenant])
    client = TestClient(_guarded_app())
    auth = {"Authorization": f"Bearer {old}"}
    assert client.get("/me", headers=auth).status_code == 200
    assert client.get("/perm", headers=auth).status_code == 200
    assert client.get("/tenant", headers=auth).status_code == 200

    assert await revocation.revoke_user_tokens(uid) is True
    assert f"{marks.SUBJECT_KEY_PREFIX}{uid}" in mem_cache.store
    for path in ("/me", "/perm", "/tenant"):
        resp = client.get(path, headers=auth)
        assert resp.status_code == 401, path
    # A token minted after the revocation by an issuer that saw it (this
    # context revoked) is stamped past the mark — skew margin included — and
    # works (test_revocation_windows pins the precise rule).
    fresh = tokens.create_access_token(
        {
            "sub": uid,
            "token_type": "access",
            "permissions": ["x.read"],
            "tenant_ids": [tenant],
        }
    )
    assert (
        client.get("/tenant", headers={"Authorization": f"Bearer {fresh}"}).status_code
        == 200
    )


async def test_revocation_lookup_runs_once_per_request(
    mem_cache: _MemoryCache,
) -> None:
    from sandloom.core.di import ensure_token_not_revoked

    request = SimpleNamespace(state=SimpleNamespace())
    claims = {"sub": "u1", "iat": int(time.time()), "jti": "j"}
    await ensure_token_not_revoked(request, claims)
    await ensure_token_not_revoked(request, claims)
    assert mem_cache.gets == 1


async def test_revocation_mark_never_moves_backwards(mem_cache: _MemoryCache) -> None:
    now = int(time.time())
    await revocation.revoke_user_tokens("u", valid_after=now)
    await revocation.revoke_user_tokens("u", valid_after=now - 100)
    assert (
        marks._parse_mark_us(mem_cache.store[f"{marks.SUBJECT_KEY_PREFIX}u"])
        / 1_000_000
    ) == now


async def test_concurrent_revocations_never_move_the_mark_backwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get->max->set raced: a concurrent LOWER mark must not overwrite a higher."""
    import sandloom.core.infra.access as access

    fake = _InterleavingCache()
    monkeypatch.setattr(access, "cache", lambda name="default": fake)
    key = f"{marks.SUBJECT_KEY_PREFIX}race"
    now = int(time.time())
    for existing in (None, marks._format_mark(marks._seconds_to_us(now - 500))):
        if existing is None:
            fake.store.pop(key, None)
        else:
            fake.store[key] = existing
        results = await asyncio.gather(
            revocation.revoke_user_tokens("race", valid_after=now + 50),
            revocation.revoke_user_tokens("race", valid_after=now),
            revocation.revoke_user_tokens("race", valid_after=now - 100),
        )
        assert results == [True, True, True]
        assert (marks._parse_mark_us(fake.store[key]) / 1_000_000) == now + 50, existing


async def test_revoke_on_commit_logs_failures_and_returns_the_mark(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Outside a UoW the scheduled revocation runs now; failures are logged."""
    from sandloom.core.di import revoke_user_tokens_on_commit

    calls: list[tuple[Any, float | None]] = []

    async def _fail(user_id: Any, *, valid_after: float | None = None) -> bool:
        calls.append((user_id, valid_after))
        return False

    monkeypatch.setattr(revocation, "revoke_user_tokens", _fail)
    monkeypatch.setattr(settings, "auth_revocation_retry_base_seconds", 0.0)
    with caplog.at_level("WARNING", logger="sandloom.core.auth.revocation"):
        mark = await revoke_user_tokens_on_commit("u-x", reason="test.reason")
    # retried, then deferred to the durable applier (logged with the reason)
    # (the di facade returns the precise mark rounded UP to whole seconds)
    assert len(calls) == settings.auth_revocation_write_attempts
    assert {uid for uid, _ in calls} == {"u-x"}
    precise = {v for _, v in calls}
    assert len(precise) == 1 and math.ceil(precise.pop() or 0) == mark
    assert "test.reason" in caplog.text


def test_bearer_claims_is_the_single_access_token_parser() -> None:
    request = SimpleNamespace(state=SimpleNamespace(), headers={})
    access = _token("u-b")
    refresh = tokens.create_access_token({"sub": "u-b", "token_type": "refresh"})
    request.headers = {"authorization": f"bearer   {access}  "}
    claims = bearer.bearer_claims(request)
    assert claims is not None and claims["sub"] == "u-b"
    for header in (f"Bearer {refresh}", "Bearer ", "Basic abc", ""):
        req = SimpleNamespace(
            state=SimpleNamespace(), headers={"authorization": header}
        )
        assert bearer.bearer_claims(req) is None, header


def test_idempotency_and_tenant_scope_ignore_non_access_bearers() -> None:
    """Idempotency identity/tenant and TenantScope share bearer_claims."""
    from starlette.requests import Request

    from sandloom.core.http import tenant_scope as tenant
    from sandloom.core.idempotency import fingerprint as idempotency_fingerprint

    tid = str(uuid.uuid4())
    refresh = tokens.create_access_token(
        {"sub": "u-r", "token_type": "refresh", "tenant_id": tid}
    )
    access = _token("u-r", tenant_id=tid)

    def _req(token: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": [(b"authorization", f"Bearer  {token} ".encode())],
                "query_string": b"",
            }
        )

    assert idempotency_fingerprint._caller_identity(_req(access)) == "u:u-r"
    assert idempotency_fingerprint._tenant_identity(_req(access)) == f"t:{tid}"
    assert tenant._tenant_claims(_req(access))["sub"] == "u-r"
    # A refresh token is not a principal anywhere.
    assert idempotency_fingerprint._caller_identity(_req(refresh)).startswith("h:")
    assert idempotency_fingerprint._tenant_identity(_req(refresh)) == "t:none"
    assert tenant._tenant_claims(_req(refresh)) == {}


async def test_revocation_outage_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    import sandloom.core.infra.access as access

    broken = _MemoryCache(broken=True)
    monkeypatch.setattr(access, "cache", lambda name="default": broken)
    token = _token(str(uuid.uuid4()))
    client = TestClient(_guarded_app())
    auth = {"Authorization": f"Bearer {token}"}

    monkeypatch.setattr(settings, "auth_revocation_fail_closed", True)
    resp = client.get("/me", headers=auth)
    assert resp.status_code == 503
    monkeypatch.setattr(settings, "auth_revocation_fail_closed", False)
    assert client.get("/me", headers=auth).status_code == 200
    # revoke itself never raises on an outage (reported via the return value).
    assert await revocation.revoke_user_tokens("u") is False


# --------------------------------------------------------------------------- #
# S5 — client IP via sandloom.core.di
# --------------------------------------------------------------------------- #
def test_idempotency_anonymous_identity_uses_real_client_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core import di
    from sandloom.core.idempotency import fingerprint as idempotency_fingerprint

    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    monkeypatch.setattr(settings, "trusted_proxy_ips", ["10.0.0.0/8"])
    request = SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.5"),
        headers={"x-forwarded-for": "198.51.100.7, 10.0.0.9"},
        state=SimpleNamespace(),
    )
    assert di.get_client_ip(request) == "198.51.100.7"
    assert idempotency_fingerprint._caller_identity(request) == "anon:198.51.100.7"  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Revocation mark TTL is independent of the WRITER's access-token lifetime
# --------------------------------------------------------------------------- #
class _TtlRecordingCache(_MemoryCache):
    def __init__(self) -> None:
        super().__init__()
        self.ttls: list[int | None] = []

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        self.ttls.append(ttl_seconds)
        return await super().add(key, value, ttl_seconds=ttl_seconds)


@pytest.fixture
def ttl_cache(monkeypatch: pytest.MonkeyPatch) -> _TtlRecordingCache:
    import sandloom.core.infra.access as access

    fake = _TtlRecordingCache()
    monkeypatch.setattr(access, "cache", lambda name="default": fake)
    return fake


async def test_revocation_mark_outlives_other_services_tokens_by_default(
    monkeypatch: pytest.MonkeyPatch, ttl_cache: _TtlRecordingCache
) -> None:
    """A writer with a SHORT access TTL (tenancy, 5 min) must not write a mark
    that expires before tokens minted elsewhere (identity, 60 min) do — or a
    removed member's token is accepted again once the mark is gone."""
    monkeypatch.setattr(settings, "access_token_expire_minutes", 5)
    monkeypatch.setattr(settings, "auth_revocation_mark_ttl_seconds", None)
    assert await revocation.revoke_user_tokens(uuid.uuid4())
    ttl = ttl_cache.ttls[-1]
    assert ttl is not None and ttl >= 24 * 3600


async def test_revocation_mark_ttl_setting_is_honoured_but_never_too_short(
    monkeypatch: pytest.MonkeyPatch, ttl_cache: _TtlRecordingCache
) -> None:
    monkeypatch.setattr(settings, "access_token_expire_minutes", 15)
    monkeypatch.setattr(settings, "auth_revocation_mark_ttl_seconds", 7 * 24 * 3600)
    assert await revocation.revoke_user_tokens(uuid.uuid4())
    assert ttl_cache.ttls[-1] == 7 * 24 * 3600
    # Shorter than this process's own token lifetime: clamped up.
    monkeypatch.setattr(settings, "auth_revocation_mark_ttl_seconds", 60)
    assert await revocation.revoke_user_tokens(uuid.uuid4())
    # (+ the clock-skew margin: the mark itself is written that far ahead)
    margin = math.ceil(settings.auth_revocation_clock_skew_seconds)
    assert ttl_cache.ttls[-1] == 15 * 60 + 120 + margin


def test_revocation_mark_ttl_setting_parses() -> None:
    assert _settings(JWT_SECRET=_STRONG).auth_revocation_mark_ttl_seconds is None
    cfg = _settings(JWT_SECRET=_STRONG, AUTH_REVOCATION_MARK_TTL_SECONDS="172800")
    assert cfg.auth_revocation_mark_ttl_seconds == 172800
    with pytest.raises(ValidationError):
        _settings(JWT_SECRET=_STRONG, AUTH_REVOCATION_MARK_TTL_SECONDS="0")
