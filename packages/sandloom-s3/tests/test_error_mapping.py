"""Every S3 failure reaches the caller as the contract's error (spec 9),
opening the client and presigning included, with no backend text."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import NoCredentialsError, NoRegionError
from sandloom_s3 import S3ObjectStore

from sandloom.driverkit import PermissionDenied, Unavailable


def _store() -> S3ObjectStore:
    return S3ObjectStore(name="default", driver="s3", options={"bucket": "b"})


def _no_client(self: S3ObjectStore) -> Any:
    raise NoRegionError()


@pytest.mark.asyncio
async def test_a_client_the_sdk_cannot_create_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(S3ObjectStore, "_connect", _no_client)
    store = _store()
    with pytest.raises(Unavailable) as refused:
        await store.aopen()
    assert "NoRegionError" in str(refused.value)
    with pytest.raises(Unavailable):
        await store.put("a/b.txt", b"x")
    with pytest.raises(Unavailable):
        await store.url("a/b.txt")
    health = await store.health()
    assert health.status == "fail"
    assert health.details == {"error": "NoRegionError"}


class _Unsigned:
    def generate_presigned_url(self, *_args: Any, **_kwargs: Any) -> str:
        raise NoCredentialsError()


@pytest.mark.asyncio
async def test_a_url_the_sdk_cannot_sign_is_permission_denied() -> None:
    store = _store()
    store._client = _Unsigned()
    with pytest.raises(PermissionDenied, match="NoCredentialsError"):
        await store.url("a/b.txt")
