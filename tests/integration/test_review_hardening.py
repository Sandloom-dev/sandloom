"""Regression tests for the external core-review findings.

Covers: token_type enforcement in shared guards, UserContext sub-alias,
additionalProperties tightening = breaking, upload size limit, strict
service naming for unversioned names, atomic rate-limit TTL.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from sandloom.core.auth.tokens import create_access_token
from sandloom.core.contracts.validators import classify_schema_change
from sandloom.core.module_api._service_registry import (
    ServiceRegistry,
    ServiceRegistryError,
)
from sandloom.shared.authz import current_context
from sandloom.shared.subject import UserContext

# --- JWT guards -------------------------------------------------------------


def _req() -> Any:
    """Minimal request stub exposing a writable .state for context caching."""
    return SimpleNamespace(state=SimpleNamespace())


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.mark.asyncio
async def test_guard_rejects_token_without_access_type() -> None:
    token = create_access_token(data={"sub": str(uuid.uuid4())})  # no token_type
    with pytest.raises(HTTPException) as exc:
        await current_context(_req(), _creds(token))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_guard_accepts_access_token_and_maps_sub() -> None:
    uid = uuid.uuid4()
    token = create_access_token(data={"sub": str(uid), "token_type": "access"})
    ctx = await current_context(_req(), _creds(token))
    assert ctx.user_id == uid


def test_decode_token_requires_exp() -> None:
    # A token signed with the shared secret but WITHOUT exp must not verify
    # forever — its absence is a hard failure (options require=["exp"]), not the
    # JWT default "skip the check if the claim is missing".
    import jwt as _jwt

    from sandloom.core.auth.tokens import decode_token
    from sandloom.core.config.settings import settings

    no_exp = _jwt.encode(
        {"sub": str(uuid.uuid4()), "token_type": "access"},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    assert decode_token(no_exp) is None
    good = create_access_token(data={"sub": str(uuid.uuid4()), "token_type": "access"})
    assert decode_token(good) is not None


def test_decode_token_cached_verifies_once_per_request() -> None:
    # Several guards on one request read the same bearer token; the per-request
    # cache must verify it exactly once and reuse the parsed payload.
    from types import SimpleNamespace
    from unittest.mock import patch

    from sandloom.core.auth import bearer, tokens

    token = create_access_token(data={"sub": str(uuid.uuid4()), "token_type": "access"})
    request = SimpleNamespace(state=SimpleNamespace())
    with patch.object(tokens, "decode_token", wraps=tokens.decode_token) as spy:
        first = bearer.decode_token_cached(request, token)
        second = bearer.decode_token_cached(request, token)
    assert first is second is not None
    assert spy.call_count == 1  # second call served from request.state cache


def test_user_context_accepts_sub_and_user_id() -> None:
    uid = uuid.uuid4()
    assert UserContext(**{"sub": str(uid)}).user_id == uid
    assert UserContext(user_id=uid).user_id == uid


@pytest.mark.asyncio
async def test_guard_malformed_claims_is_401_not_500() -> None:
    token = create_access_token(data={"sub": "not-a-uuid", "token_type": "access"})
    with pytest.raises(HTTPException) as exc:
        await current_context(_req(), _creds(token))
    assert exc.value.status_code == 401


# --- Schema compatibility ----------------------------------------------------


def test_tightening_additional_properties_is_breaking() -> None:
    old = {"type": "object", "properties": {"a": {"type": "string"}}}
    new = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": False,
    }
    report = classify_schema_change(old, new, "x.y.v1", 1)
    assert report.breaking is True
    assert report.other_changes is True


def test_changing_array_items_is_detected() -> None:
    old = {
        "type": "object",
        "properties": {
            "xs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"n": {"type": "integer"}},
                    "required": ["n"],
                },
            }
        },
    }
    new = {
        "type": "object",
        "properties": {
            "xs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"n": {"type": "integer"}},
                    "required": [],
                },
            }
        },
    }
    report = classify_schema_change(old, new, "x.y.v1", 1)
    assert "xs[].n" in report.removed_required
    assert report.breaking is True


def test_unmodeled_keyword_change_sets_other_changes() -> None:
    old = {"type": "object", "properties": {"a": {"type": "string"}}}
    new = {
        "type": "object",
        "properties": {"a": {"type": "string", "pattern": "^x"}},
    }
    report = classify_schema_change(old, new, "x.y.v1", 1)
    assert report.other_changes is True


def _status_schema(values: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"status": {"type": "string", "enum": values}},
        "required": ["status"],
    }


def test_adding_an_enum_value_is_breaking() -> None:
    """A consumer in ANOTHER service with the old schema rejects the new value."""
    report = classify_schema_change(
        _status_schema(["active"]), _status_schema(["active", "paused"]), "x.y.v1", 1
    )
    assert report.added_enum_values == {"status": ["paused"]}
    assert report.breaking is True


def test_adding_a_nested_enum_value_is_breaking() -> None:
    def schema(values: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"items": {"type": "array", "items": _status_schema(values)}},
        }

    report = classify_schema_change(schema(["a"]), schema(["a", "b"]), "x.y.v1", 1)
    assert report.added_enum_values == {"items[].status": ["b"]}
    assert report.breaking is True


def test_enum_values_compare_as_json_values() -> None:
    """(G-DECL-01, round 9) enum members are JSON values: a list or an
    object member (unhashable) and mixed types are compared -- on a property
    and on a ``$defs`` enum -- without crashing, ``true`` is not ``1``, and
    ``1.0`` is ``1``."""

    def schema(values: list[Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"v": {"enum": values}, "d": {"$ref": "#/$defs/D"}},
            "$defs": {"D": {"enum": values}},
        }

    old = [[1, 2], {"k": "v"}, "a", 1, None]
    report = classify_schema_change(schema(old), schema([*old, True]), "x.y.v1", 1)
    assert report.added_enum_values == {"v": [True], "$defs.D": [True]}
    assert report.removed_enum_values == {}
    assert report.breaking is True
    same = classify_schema_change(
        schema(old), schema([None, 1.0, "a", {"k": "v"}, [1, 2]]), "x.y.v1", 1
    )
    assert (same.added_enum_values, same.removed_enum_values) == ({}, {})
    gone = classify_schema_change(schema(old), schema(old[1:]), "x.y.v1", 1)
    assert gone.removed_enum_values == {"v": [[1, 2]], "$defs.D": [[1, 2]]}


def test_adding_an_optional_property_stays_non_breaking() -> None:
    old = _status_schema(["active"])
    new = {**old, "properties": {**old["properties"], "note": {"type": "string"}}}
    report = classify_schema_change(old, new, "x.y.v1", 1)
    assert report.added_optional == ["note"]
    assert report.breaking is False


def test_the_registry_accepts_a_field_added_with_a_default_factory() -> None:
    """(G-DECL-01, round 7) the live registry reads the wire schema's
    ``DEFAULTED`` mark: a field added with a default factory -- listed as
    required, no ``default`` keyword -- is an optional addition; the same
    field without a default is breaking."""
    from pydantic import BaseModel, Field

    from sandloom.core.schema.registry import InMemorySchemaRegistry
    from sandloom.sdk import Event

    class _Part(BaseModel):
        sku: str

    class Old(Event, channel="x.factory.v1"):
        x: int

    class New(Event, channel="x.factory.v1"):
        x: int
        tags: list[str] = Field(default_factory=list)
        part: _Part = Field(default_factory=lambda: _Part(sku="none"))

    class Bare(Event, channel="x.factory.v1"):
        x: int
        tags: list[str]

    report = classify_schema_change(
        Old.wire_schema(), New.wire_schema(), "x.factory.v1", 1
    )
    assert (report.added_required, report.added_optional) == ([], ["part", "tags"])
    assert report.breaking is False
    bare = classify_schema_change(
        Old.wire_schema(), Bare.wire_schema(), "x.factory.v1", 1
    )
    assert (bare.added_required, bare.breaking) == (["tags"], True)

    registry = InMemorySchemaRegistry()
    registry.register("x.factory.v1", "x.factory.v1", Old.wire_schema())
    registry.register("x.factory.v1", "x.factory.v1", New.wire_schema())


def test_registry_rejects_an_in_place_enum_addition() -> None:
    from sandloom.core.contracts.validators import ContractValidationError
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    registry = InMemorySchemaRegistry()
    registry.register("x.status.v1", "x.status.v1", _status_schema(["active"]))
    with pytest.raises(ContractValidationError, match="added_enum_values"):
        registry.register(
            "x.status.v1", "x.status.v1", _status_schema(["active", "paused"])
        )
    registry.register(
        "x.status.v2", "x.status.v2", _status_schema(["active", "paused"])
    )


# --- Service registry strict naming ------------------------------------------


def test_unversioned_service_name_rejected() -> None:
    registry = ServiceRegistry()
    with pytest.raises(ServiceRegistryError):
        registry.register("billing.entitlements", object())  # unversioned: rejected
    registry.register("billing.entitlements.v1", object())  # versioned: OK


# --- Storage upload limit -----------------------------------------------------


class _SizedAsyncFile:
    """Mimics UploadFile.read(size) over an in-memory buffer."""

    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    async def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)


@pytest.mark.asyncio
async def test_upload_over_limit_rejected_and_cleaned_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.config.settings import settings
    from sandloom.core.drivers.filesystem_storage import Storage, UploadTooLargeError

    monkeypatch.setattr(settings, "storage_path", str(tmp_path))
    monkeypatch.setattr(settings, "max_upload_size_bytes", 10)
    storage = Storage()
    with pytest.raises(UploadTooLargeError):
        await storage.save(_SizedAsyncFile(b"x" * 64), "big.bin")
    assert not (tmp_path / "big.bin").exists()  # partial file removed

    monkeypatch.setattr(settings, "max_upload_size_bytes", 1024)
    saved = await storage.save(_SizedAsyncFile(b"small"), "ok.bin")
    assert Path(saved).read_bytes() == b"small"


# --- Rate limiter atomic TTL ---------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_key_gets_ttl_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Lua path must be preferred: ONE script call sets count AND TTL and
    reads the previous window (``incr_window`` — a single round trip).

    The atomic primitive now lives in the CacheProvider, so the limiter
    delegates through di.cache() -> the shared Redis provider -> the client
    built by redis_cache.get_default_client.
    """
    from sandloom.core.config.settings import settings
    from sandloom.core.ingress import rate_limit as rl

    calls: list[tuple[Any, ...]] = []

    class _LuaRedis:
        async def evalsha(self, sha: str, numkeys: int, *keys_args: Any) -> Any:
            calls.append(("evalsha", numkeys, *keys_args))
            return [1, None]  # (count, previous window's counter)

        async def get(self, key: str) -> str | None:
            calls.append(("get", key))
            return None

    async def fake_get_cache() -> Any:
        return _LuaRedis()

    monkeypatch.setattr(
        "sandloom.core.drivers.redis_cache.get_default_client", fake_get_cache
    )
    monkeypatch.setattr(settings, "rate_limit_enabled", True)

    dep = rl.rate_limiter(requests=5, period=60)

    class _Req:
        headers: dict[str, str] = {}

        class url:
            path = "/x"

        class client:
            host = "1.2.3.4"

    await dep(_Req())  # type: ignore[arg-type]
    # Counters live two periods so the previous window stays readable.
    assert len(calls) == 1, calls
    op, numkeys, key, previous_key, ttl = calls[0]
    assert (op, numkeys, ttl) == ("evalsha", 2, 120)
    window = int(key.rsplit(":", 1)[1])
    assert previous_key == f"{key.rsplit(':', 1)[0]}:{window - 1}"
