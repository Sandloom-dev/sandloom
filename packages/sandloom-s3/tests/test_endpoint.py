"""Run against a real S3 endpoint with a pre-created test bucket."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

import httpx
import pytest
from botocore.exceptions import ClientError
from sandloom_s3 import S3ObjectStore

from sandloom.core.config.settings import settings
from sandloom.core.tests._root_support import create_root_app
from sandloom.driverkit import NotFound, ObjectStoreProvider, PermissionDenied
from sandloom.driverkit.testing import ObjectStoreConformance
from sandloom.sdk.infra import objectstore


def _options() -> dict[str, Any]:
    bucket = os.environ.get("SANDLOOM_S3_TEST_BUCKET")
    if not bucket:
        pytest.skip("set SANDLOOM_S3_TEST_BUCKET to run the S3 endpoint suite")
    options: dict[str, Any] = {"bucket": bucket, "max_upload_size_bytes": 1024}
    if endpoint := os.environ.get("SANDLOOM_S3_TEST_ENDPOINT"):
        options.update(endpoint_url=endpoint, addressing_style="path")
    return options


class TestS3Endpoint(ObjectStoreConformance):
    upload_limit_bytes = 1024

    @pytest.fixture
    def make_provider(self) -> Callable[[], ObjectStoreProvider]:
        options = _options()
        return lambda: S3ObjectStore(name="default", driver="s3", options=options)

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], ObjectStoreProvider]:
        # A closed port (no live service) and the kit's secret as the secret
        # key: no error message or health detail may show it.
        options = {
            "bucket": "kit-failing",
            "region": "us-east-1",
            "endpoint_url": "http://127.0.0.1:1",
            "addressing_style": "path",
            "access_key_id": "AKIAKITFAILING",
            "secret_access_key": self.leak_marker,
        }
        return lambda: S3ObjectStore(name="default", driver="s3", options=options)

    async def read_metadata(
        self, provider: ObjectStoreProvider, key: str
    ) -> tuple[str | None, Mapping[str, str]]:
        async with httpx.AsyncClient() as client:
            response = await client.get(await provider.url(key))
            response.raise_for_status()
        prefix = "x-amz-meta-"
        metadata = {
            name.removeprefix(prefix): value
            for name, value in response.headers.items()
            if name.startswith(prefix)
        }
        return response.headers.get("content-type"), metadata

    async def test_the_app_resolves_the_s3_driver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        options = {"driver": "s3", **_options()}
        monkeypatch.setattr(settings, "infra", {"objectstore": {"default": options}})
        app = create_root_app()
        async with app.router.lifespan_context(app):
            store = objectstore()
            assert isinstance(store, S3ObjectStore)
            assert (await store.health()).status == "ok"

    async def test_bucket_health_and_signed_get(
        self, provider: ObjectStoreProvider
    ) -> None:
        assert (await provider.health()).status == "ok"
        assert provider.features() == {"metadata", "presigned_get"}
        key = f"conformance/{uuid4().hex}/with spaces.txt"
        assert await provider.put(key, b"hello", content_type="text/plain") == key
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(await provider.url(key))
                response.raise_for_status()
            assert response.content == b"hello"
        finally:
            await provider.delete(key)
        await provider.delete(key)

    @pytest.mark.skipif(
        not os.environ.get("SANDLOOM_S3_TEST_DENIED_KEY"),
        reason="requires a restricted identity and a seeded private object",
    )
    async def test_distinguishes_missing_object_from_denied_read(
        self, provider: ObjectStoreProvider
    ) -> None:
        absent = f"conformance/{uuid4().hex}/absent.bin"
        denied = os.environ["SANDLOOM_S3_TEST_DENIED_KEY"]

        with pytest.raises(NotFound) as missing:
            await provider.get(absent)
        backend_error = missing.value.__cause__
        assert isinstance(backend_error, ClientError)
        assert backend_error.response["ResponseMetadata"]["HTTPStatusCode"] == 404

        with pytest.raises(PermissionDenied) as refused:
            await provider.get(denied)
        denied_error = refused.value.__cause__
        assert isinstance(denied_error, ClientError)
        assert denied_error.response["Error"]["Code"] == "AccessDenied"
        assert denied_error.response["ResponseMetadata"]["HTTPStatusCode"] == 403

        async with httpx.AsyncClient() as client:
            response = await client.get(await provider.url(denied))
        assert response.status_code == 403


async def test_two_instances_of_one_bucket_share_an_identity() -> None:
    options = {"bucket": "b", "endpoint_url": "http://minio:9000/", "region": "x"}
    identities = {
        await S3ObjectStore(
            name=name, driver="s3", options=options
        ).isolation_identity()
        for name in ("default", "uploads")
    }
    assert identities == {"s3:http://minio:9000/b"}
