"""Security/ops hardening: settings, JWT, HTTP error mapping, health, logging."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ValidationError

from sandloom.core.config.settings import Settings, settings
from sandloom.core.descriptors import HealthResult
from sandloom.core.facade import CoreFacade

_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
    "CACHE_URL": "redis://h:6379/0",
    "BROKER_URL": "redis://h:6379/1",
}
_STRONG = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"


def _settings(**env: Any) -> Settings:
    return Settings(_env_file=None, **{**_BASE_ENV, **env})  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "secret",
    [
        "change_me_in_production_use_openssl_rand_hex_32",
        "my-example-secret-value-0123456789abcdef",
        "placeholder-placeholder-placeholder-12345",
        "short",
    ],
)
def test_prod_rejects_weak_or_placeholder_jwt_secret(secret: str) -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _settings(ENVIRONMENT="prod", JWT_SECRET=secret)


def test_prod_accepts_random_jwt_secret() -> None:
    assert _settings(ENVIRONMENT="prod", JWT_SECRET=_STRONG).is_prod


def test_prod_validates_internal_and_metrics_tokens() -> None:
    with pytest.raises(ValidationError, match="INTERNAL_API_TOKEN"):
        _settings(ENVIRONMENT="prod", JWT_SECRET=_STRONG, INTERNAL_API_TOKEN="abc")
    with pytest.raises(ValidationError, match="METRICS_TOKEN must differ"):
        _settings(
            ENVIRONMENT="prod",
            JWT_SECRET=_STRONG,
            INTERNAL_API_TOKEN=_STRONG[::-1],
            METRICS_TOKEN=_STRONG[::-1],
        )
    # Dev keeps short tokens working.
    assert _settings(JWT_SECRET="x", INTERNAL_API_TOKEN="abc").internal_api_token


@pytest.mark.parametrize("alg", ["none", "RS256", "ES256", "hs256"])
def test_jwt_algorithm_restricted_to_hmac(alg: str) -> None:
    with pytest.raises(ValidationError, match="JWT_ALGORITHM"):
        _settings(JWT_SECRET="x", JWT_ALGORITHM=alg)


def test_decode_token_never_raises_on_key_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.auth import tokens

    token = tokens.create_access_token({"sub": "u", "token_type": "access"})
    # A PEM-looking secret makes PyJWT raise InvalidKeyError (not an
    # InvalidTokenError) — previously a 500 at every guard.
    monkeypatch.setattr(
        settings,
        "jwt_secret",
        "-----BEGIN PUBLIC KEY-----\nMIIB\n-----END PUBLIC KEY-----",
    )
    assert tokens.decode_token(token) is None
    assert tokens.decode_token("not.a.jwt") is None
    unsigned = jwt.encode({"sub": "u", "exp": 9999999999}, None, algorithm="none")
    assert tokens.decode_token(unsigned) is None


def test_s3_storage_fails_fast_at_startup() -> None:
    from sandloom.core.drivers.bootstrap import build_infra_registry
    from sandloom.core.infra.registry import InfraError
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    cfg = _settings(
        JWT_SECRET="x",
        STORAGE_TYPE="s3",
        S3_BUCKET="b",
        S3_REGION="r",
        S3_ACCESS_KEY_ID="k",
        S3_SECRET_ACCESS_KEY="s",
    )
    with pytest.raises(InfraError, match="no registered driver"):
        build_infra_registry(InMemorySchemaRegistry(), settings=cfg, load_plugins=False)


# --------------------------------------------------------------------------- #
# HTTP: 413, CORS on 500, http_status protocol
# --------------------------------------------------------------------------- #
class _Body(BaseModel):
    text: str


class AccessDeniedLike(Exception):
    """Mimics sandloom.shared.authz.AccessDenied via the protocol only."""

    http_status = 403
    code = "access_denied"
    detail = "Forbidden by policy"


def _app() -> FastAPI:
    from sandloom.core.http.error_handlers import register_error_handlers
    from sandloom.core.http.middlewares import register_middlewares

    app = FastAPI()
    register_middlewares(app)
    register_error_handlers(app)

    @app.post("/echo")
    async def echo(body: _Body) -> dict[str, str]:
        return {"text": body.text}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("password=hunter2 leaked?")

    @app.get("/denied")
    async def denied() -> None:
        raise AccessDeniedLike()

    return app


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_chunked_oversize_json_body_is_413(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "max_request_body_bytes", 64)

    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"text": "'
        for _ in range(20):
            yield b"x" * 16
        yield b'"}'

    async with await _client(_app()) as client:
        resp = await client.post(
            "/echo", content=chunks(), headers={"content-type": "application/json"}
        )
    assert resp.status_code == 413
    assert resp.json()["code"] == "request_entity_too_large"


@pytest.mark.asyncio
async def test_masked_500_carries_cors_headers() -> None:
    origin = (
        settings.cors_origins[0] if settings.cors_origins else settings.frontend_url
    )
    async with await _client(_app()) as client:
        resp = await client.get("/boom", headers={"Origin": origin})
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal server error", "code": "internal_error"}
    assert resp.headers.get("access-control-allow-origin") == origin
    assert resp.headers.get("x-request-id")


@pytest.mark.asyncio
async def test_http_status_protocol_exception_maps_to_status() -> None:
    async with await _client(_app()) as client:
        resp = await client.get("/denied")
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Forbidden by policy", "code": "access_denied"}
    assert resp.headers.get("x-request-id")  # rendered inside the middleware stack


# --------------------------------------------------------------------------- #
# /health + /ready
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_public_health_probes_are_cached_and_topology_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.app_factory import _install_ops_endpoints

    calls = {"n": 0}

    async def probe() -> HealthResult:
        calls["n"] += 1
        await asyncio.sleep(0)
        return HealthResult(status="ok")

    app = FastAPI()
    core = CoreFacade(app=app)
    core.register_health_check("db", probe)
    core.loaded_modules = ["sandloom.secret_module"]
    monkeypatch.setattr(settings, "health_cache_ttl_seconds", 30.0)
    monkeypatch.setattr(settings, "module_required", [])
    monkeypatch.setattr(settings, "internal_api_token", _STRONG)
    _install_ops_endpoints(app, core)

    async with await _client(app) as client:
        results = await asyncio.gather(*(client.get("/ready") for _ in range(10)))
        await client.get("/health")
        internal = await client.get("/ready", headers={"X-Internal-Token": _STRONG})

    assert calls["n"] == 1  # single-flight + TTL
    for resp in results:
        assert resp.status_code == 200
        assert "loaded_modules" not in resp.json()
    assert internal.json()["loaded_modules"] == ["sandloom.secret_module"]


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def _format(record: logging.LogRecord, *, json_mode: bool) -> str:
    from sandloom.core.observability.logging import JsonFormatter, RedactingFormatter

    fmt = JsonFormatter() if json_mode else RedactingFormatter("%(message)s")
    return fmt.format(record)


@pytest.mark.parametrize("json_mode", [True, False])
def test_log_extras_redacted_by_substring(json_mode: bool) -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "login", None, None)
    record.refresh_token = "rt-SECRET"  # type: ignore[attr-defined]
    record.db_password = "pw-SECRET"  # type: ignore[attr-defined]
    record.user_id = "u-1"  # type: ignore[attr-defined]
    out = _format(record, json_mode=json_mode)
    if json_mode:
        assert "rt-SECRET" not in out and "pw-SECRET" not in out
        assert "u-1" in out


@pytest.mark.parametrize("json_mode", [True, False])
def test_free_text_messages_are_scrubbed(json_mode: bool) -> None:
    msg = (
        "connect postgresql+asyncpg://app:s3cr3tpw@db:5432/x failed; "
        "Authorization: Bearer eyJabc.def.ghi api_key=AKIA123\n"
        "[SQL: INSERT INTO users (email, hash) VALUES (%(e)s, %(h)s)]\n"
        "[parameters: {'e': 'a@b.c', 'h': '$argon2$xyz'}]\n"
        "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
    )
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, msg, None, None)
    out = _format(record, json_mode=json_mode)
    for leaked in ("s3cr3tpw", "eyJabc.def.ghi", "AKIA123", "a@b.c", "$argon2$xyz"):
        assert leaked not in out, (leaked, out)
    assert "INSERT INTO users" in out  # the statement shape stays debuggable


def test_describe_exception_drops_statement_params() -> None:
    from sandloom.core.http.error_handlers import describe_exception

    class StatementError(Exception):
        statement = "INSERT ..."
        params = {"password": "hunter2"}
        orig = ValueError("dup")

    text = describe_exception(StatementError("boom hunter2"))
    assert "hunter2" not in text
    assert text.startswith("StatementError (ValueError)")


def test_sentry_before_send_scrubs_event() -> None:
    from sandloom.core.observability.observability import scrub_sentry_event

    event = {
        "request": {
            "headers": {"Authorization": "Bearer abc", "X-Ok": "1"},
            "cookies": "session=zzz",
        },
        "extra": {"refresh_token": "rt"},
        "exception": {
            "values": [
                {
                    "value": "postgresql://u:pw@h/db [parameters: ('a@b.c',)]",
                    "stacktrace": {"frames": [{"vars": {"password": "x"}}]},
                }
            ]
        },
        "breadcrumbs": {"values": [{"message": "token=abc123", "data": {}}]},
    }
    out = scrub_sentry_event(event, None)
    assert out["request"]["headers"]["Authorization"] == "***"
    assert out["request"]["headers"]["X-Ok"] == "1"
    assert out["request"]["cookies"] == "***"
    assert out["extra"]["refresh_token"] == "***"
    value = out["exception"]["values"][0]
    assert "pw@" not in value["value"] and "a@b.c" not in value["value"]
    assert "vars" not in value["stacktrace"]["frames"][0]
    assert "abc123" not in out["breadcrumbs"]["values"][0]["message"]


# --------------------------------------------------------------------------- #
# Lifecycle phases + claims provenance
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_module_task_named_infra_does_not_jump_ahead() -> None:
    from sandloom.core.lifecycle import run_startup

    order: list[str] = []
    core = CoreFacade(app=FastAPI())

    async def infra_sneaky() -> None:  # a MODULE task with an infra_ name
        order.append("module")

    async def kernel_task() -> None:
        order.append("infra")

    async def post_cleanup() -> None:
        order.append("post")

    with core.module_registration("x.mod") as api:
        api.add_startup_task(infra_sneaky)
    core.add_startup_task(kernel_task, phase="infra")
    core.add_startup_task(post_cleanup, phase="post")
    await run_startup(core)
    assert order == ["infra", "module", "post"]
    with pytest.raises(ValueError, match="phase"):
        core.add_startup_task(kernel_task, phase="bogus")


@pytest.mark.asyncio
async def test_claims_provenance_from_registering_module() -> None:
    core = CoreFacade(app=FastAPI())

    async def billing_claims(user_id: Any) -> dict[str, Any]:
        return {"subscription_status": "active"}

    async def broken(user_id: Any) -> dict[str, Any]:
        raise RuntimeError("down")

    async def empty(user_id: Any) -> dict[str, Any]:
        return {}

    # A downstream app module (not under sandloom.*) gets provenance too.
    with core.module_registration("myapp.modules.billing") as api:
        api.add_claims_contributor(billing_claims)
    with core.module_registration("myapp.modules.reports") as api:
        api.add_claims_contributor(broken)
    with core.module_registration("sandloom_identity") as api:
        api.add_claims_contributor(empty)

    # Contributors run through the contract runtime; a bare facade has no
    # database, so bind a stand-in session for them (the test seam).
    from sandloom.core.module_api import bind_contract_session

    with bind_contract_session(SimpleNamespace()):
        claims = await core.collect_claims("u")
    assert claims["subscription_status"] == "active"
    # identity ran (even with no claims) -> authoritative; reports failed.
    assert claims["claims_sources"] == ["billing", "identity"]
    assert claims["claims_sources_failed"] == ["reports"]


@pytest.mark.asyncio
async def test_claims_provenance_always_stamped() -> None:
    claims = await CoreFacade(app=FastAPI()).collect_claims("u")
    assert claims == {"claims_sources": [], "claims_sources_failed": []}
