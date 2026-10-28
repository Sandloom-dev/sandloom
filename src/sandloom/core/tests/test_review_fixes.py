"""Regression tests for the kernel code-review fixes.

Covers: channel version parsing (no crash), HTTP method label clamping,
CORS wildcard disabling credentials, substring-based redaction, and idempotency
caller-identity scoping (token rotation, anonymous fallback).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from sandloom.core.auth.tokens import create_access_token
from sandloom.core.config.settings import settings
from sandloom.core.observability.guardrails import redact
from sandloom.core.schema.registry import _parse_version

# --- channel version parsing ------------------------------------------------


def test_parse_version_defensive() -> None:
    assert _parse_version("billing.credits.spent.v2") == 2
    assert _parse_version("billing.credits.spent.v1") == 1
    # A malformed/unversioned tail must NOT crash (int("pent") used to raise).
    assert _parse_version("billing.credits.spent") == 1
    assert _parse_version("single") == 1


# --- HTTP method label clamping ---------------------------------------------


def test_method_label_clamps_unknown_verbs() -> None:
    from types import SimpleNamespace

    from sandloom.core.http.middlewares import _method_label

    assert _method_label(SimpleNamespace(method="GET")) == "GET"  # type: ignore[arg-type]
    assert _method_label(SimpleNamespace(method="post")) == "POST"  # type: ignore[arg-type]
    # Arbitrary client-supplied verbs collapse to OTHER (bounded cardinality).
    assert _method_label(SimpleNamespace(method="FOOBAR")) == "OTHER"  # type: ignore[arg-type]
    assert _method_label(SimpleNamespace(method="\x00evil")) == "OTHER"  # type: ignore[arg-type]


# --- CORS wildcard + credentials --------------------------------------------


def _cors_kwargs(app: FastAPI) -> dict[str, object]:
    for mw in app.user_middleware:
        if mw.cls is CORSMiddleware:
            return dict(getattr(mw, "kwargs", None) or getattr(mw, "options", {}))
    raise AssertionError("CORSMiddleware not installed")


def test_cors_wildcard_disables_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.http.middlewares import register_middlewares

    monkeypatch.setattr(settings, "cors_origins", ["*"])
    app = FastAPI()
    register_middlewares(app)
    kwargs = _cors_kwargs(app)
    assert kwargs["allow_credentials"] is False


def test_cors_explicit_origin_keeps_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.http.middlewares import register_middlewares

    monkeypatch.setattr(settings, "cors_origins", ["https://app.example.com"])
    app = FastAPI()
    register_middlewares(app)
    kwargs = _cors_kwargs(app)
    assert kwargs["allow_credentials"] is True


# --- redaction --------------------------------------------------------------


def test_redact_matches_compound_secret_keys() -> None:
    out = redact(
        {
            "db_password": "hunter2",
            "x-api-key": "k-123",
            "client_secret": "s-456",
            "stripe_secret_key": "sk_live_x",
            "set-cookie": "session=abc",
            "user_id": "keep-me",
        }
    )
    assert out["db_password"] == "***"
    assert out["x-api-key"] == "***"
    assert out["client_secret"] == "***"
    assert out["stripe_secret_key"] == "***"
    assert out["set-cookie"] == "***"
    assert out["user_id"] == "keep-me"


# --- idempotency caller identity --------------------------------------------


def _req_with_auth(auth: str | None):
    from types import SimpleNamespace

    headers = {"authorization": auth} if auth is not None else {}
    return SimpleNamespace(headers=headers)


def test_caller_identity_survives_token_rotation() -> None:
    from sandloom.core.idempotency.fingerprint import _caller_identity

    uid = str(uuid.uuid4())
    # Two DIFFERENT tokens for the same subject (a refresh) must map to the same
    # identity, so a retry after rotation replays instead of 422-ing.
    t1 = create_access_token(data={"sub": uid, "token_type": "access"})
    t2 = create_access_token(
        data={"sub": uid, "token_type": "access"}, expires_delta=99
    )
    assert t1 != t2
    id1 = _caller_identity(_req_with_auth(f"Bearer {t1}"))  # type: ignore[arg-type]
    id2 = _caller_identity(_req_with_auth(f"Bearer {t2}"))  # type: ignore[arg-type]
    assert id1 == id2 == f"u:{uid}"


def test_caller_identity_distinguishes_users_and_anon() -> None:
    from sandloom.core.idempotency.fingerprint import _caller_identity

    a = create_access_token(data={"sub": "user-a", "token_type": "access"})
    b = create_access_token(data={"sub": "user-b", "token_type": "access"})
    assert _caller_identity(_req_with_auth(f"Bearer {a}")) != _caller_identity(  # type: ignore[arg-type]
        _req_with_auth(f"Bearer {b}")  # type: ignore[arg-type]
    )
    # No auth -> identity is scoped by source IP; this fake request has no
    # client peer, so it falls back to "anon:unknown".
    assert _caller_identity(_req_with_auth(None)) == "anon:unknown"  # type: ignore[arg-type]
