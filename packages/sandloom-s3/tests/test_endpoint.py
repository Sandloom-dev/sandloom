"""Run against a real S3 endpoint with a pre-created test bucket."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from botocore.exceptions import ClientError
from sandloom_s3 import S3ObjectStore

from sandloom.core.config.settings import settings
from sandloom.core.tests._root_support import create_root_app
from sandloom.driverkit import UploadTooLargeError
from sandloom.driverkit.testing import ObjectStoreConformance
from sandloom.sdk.infra import ObjectStore, objectstore


class TestS3Endpoint(ObjectStoreConformance):
    @pytest_asyncio.fixture
    async def store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> AsyncIterator[S3ObjectStore]:
        bucket = os.environ.get("SANDLOOM_S3_TEST_BUCKET")
        if not bucket:
            pytest.skip("set SANDLOOM_S3_TEST_BUCKET to run the S3 endpoint suite")
        options = {"driver": "s3", "bucket": bucket, "max_upload_size_bytes": 1024}
        if endpoint := os.environ.get("SANDLOOM_S3_TEST_ENDPOINT"):
            options.update(endpoint_url=endpoint, addressing_style="path")
        monkeypatch.setattr(settings, "infra", {"objectstore": {"default": options}})
        app = create_root_app()
        async with app.router.lifespan_context(app):
            store = objectstore()
            assert isinstance(store, S3ObjectStore)
            yield store

    async def test_bucket_health_and_signed_get(self, store: S3ObjectStore) -> None:
        assert (await store.health()).status == "ok"
        assert store.features() == {"metadata", "presigned_get"}
        key = f"conformance/{uuid4().hex}/with spaces.txt"
        assert (
            await store.put(
                key,
                b"hello",
                content_type="text/plain",
                metadata={"source": "conformance"},
            )
            == key
        )
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(await store.url(key))
                response.raise_for_status()
            assert response.content == b"hello"
            assert response.headers["content-type"] == "text/plain"
            assert response.headers["x-amz-meta-source"] == "conformance"
        finally:
            await store.delete(key)
        await store.delete(key)

    async def test_limit_does_not_replace_existing_object(
        self, store: ObjectStore
    ) -> None:
        key = f"conformance/{uuid4().hex}/bounded.bin"
        await store.put(key, b"original")
        try:
            with pytest.raises(UploadTooLargeError, match="Upload exceeds"):
                await store.put(key, b"x" * 1025)
            assert await store.get(key) == b"original"
        finally:
            await store.delete(key)

    @pytest.mark.skipif(
        not os.environ.get("SANDLOOM_S3_TEST_DENIED_KEY"),
        reason="requires a restricted identity and a seeded private object",
    )
    async def test_distinguishes_missing_object_from_denied_read(
        self, store: S3ObjectStore
    ) -> None:
        absent = f"conformance/{uuid4().hex}/absent.bin"
        denied = os.environ["SANDLOOM_S3_TEST_DENIED_KEY"]

        with pytest.raises(FileNotFoundError) as missing:
            await store.get(absent)
        backend_error = missing.value.__cause__
        assert isinstance(backend_error, ClientError)
        assert backend_error.response["ResponseMetadata"]["HTTPStatusCode"] == 404

        with pytest.raises(ClientError) as refused:
            await store.get(denied)
        assert refused.value.response["Error"]["Code"] == "AccessDenied"
        assert refused.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403

        async with httpx.AsyncClient() as client:
            response = await client.get(await store.url(denied))
        assert response.status_code == 403
