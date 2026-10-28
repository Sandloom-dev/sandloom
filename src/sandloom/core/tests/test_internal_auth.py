"""Internal/ops endpoint guard tests."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from sandloom.core.config.settings import settings
from sandloom.core.internal_auth import require_internal_access


@pytest.mark.asyncio
async def test_closed_in_dev_without_token_by_default(
    monkeypatch: pytest.MonkeyPatch,
):
    # ENVIRONMENT defaults to dev: that alone must NOT open internal RPC /
    # diagnostics to anyone who can reach the port.
    monkeypatch.setattr(settings, "internal_api_token", None)
    monkeypatch.setattr(settings, "internal_endpoints_open_in_dev", False)
    monkeypatch.setattr(type(settings), "is_prod", property(lambda self: False))
    try:
        with pytest.raises(HTTPException) as exc:
            await require_internal_access(x_internal_token=None)
        assert exc.value.status_code == 404
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_open_in_dev_only_with_explicit_opt_in(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "internal_api_token", None)
    monkeypatch.setattr(settings, "internal_endpoints_open_in_dev", True)
    monkeypatch.setattr(type(settings), "is_prod", property(lambda self: False))
    try:
        await require_internal_access(x_internal_token=None)  # no raise
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_dev_opt_in_ignored_in_prod(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "internal_api_token", None)
    monkeypatch.setattr(settings, "internal_endpoints_open_in_dev", True)
    monkeypatch.setattr(type(settings), "is_prod", property(lambda self: True))
    try:
        with pytest.raises(HTTPException):
            await require_internal_access(x_internal_token=None)
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_metrics_token_unlocks_only_metrics(monkeypatch: pytest.MonkeyPatch):
    from sandloom.core.internal_auth import has_metrics_access, require_metrics_access

    monkeypatch.setattr(settings, "internal_api_token", "i" * 40)
    monkeypatch.setattr(settings, "metrics_token", "m" * 40)
    await require_metrics_access(x_internal_token="m" * 40)  # no raise
    await require_metrics_access(x_internal_token="i" * 40)  # internal also ok
    assert not has_metrics_access("wrong")
    with pytest.raises(HTTPException):
        await require_internal_access(x_internal_token="m" * 40)


@pytest.mark.asyncio
async def test_disabled_in_prod_without_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "internal_api_token", None)
    monkeypatch.setattr(type(settings), "is_prod", property(lambda self: True))
    try:
        with pytest.raises(HTTPException) as exc:
            await require_internal_access(x_internal_token=None)
        assert exc.value.status_code == 404
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_token_required_when_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "internal_api_token", "s3cr3t-token")

    with pytest.raises(HTTPException) as exc:
        await require_internal_access(x_internal_token=None)
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException):
        await require_internal_access(x_internal_token="wrong")

    await require_internal_access(x_internal_token="s3cr3t-token")  # no raise


@pytest.mark.asyncio
@pytest.mark.parametrize("presented", ["\xff" * 40, "токен", "i" * 39 + "\xe9"])
async def test_non_ascii_token_is_hidden_404_not_typeerror(
    monkeypatch: pytest.MonkeyPatch, presented: str
):
    # Starlette decodes header bytes as latin-1, so a raw 0xFF byte arrives as
    # "\xff": compare_digest(str, str) on it raises TypeError (-> 500) instead
    # of the hidden 404.
    from sandloom.core.internal_auth import has_metrics_access, require_metrics_access

    monkeypatch.setattr(settings, "internal_api_token", "i" * 40)
    monkeypatch.setattr(settings, "metrics_token", "m" * 40)
    for guard in (require_internal_access, require_metrics_access):
        with pytest.raises(HTTPException) as exc:
            await guard(x_internal_token=presented)
        assert exc.value.status_code == 404
    assert not has_metrics_access(presented)
