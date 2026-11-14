"""``ObjectStoreConformance``: what an ``objectstore`` driver must do (spec 9)."""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import ClassVar
from uuid import uuid4

import pytest

from sandloom.core.infra.errors import InvalidArgument, NotFound, TooLarge
from sandloom.core.infra.ports.objectstore import (
    OBJECTSTORE_CAPABILITY,
    ObjectStore,
    ObjectStoreProvider,
)
from sandloom.driverkit.kits._harness import Call, ProviderConformance, require


def _key(name: str) -> str:
    return f"conformance/{uuid4().hex}/{name}"


class _ObjectStream:
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = iter(chunks)

    async def read(self, size: int = -1) -> bytes:
        return next(self._chunks, b"")


class _FailingObjectStream:
    def __init__(self, store: ObjectStore, key: str) -> None:
        self._store = store
        self._key = key
        self._started = False

    async def read(self, size: int = -1) -> bytes:
        if self._started:
            require(
                await self._store.get(self._key) == b"original",
                "partial upload stays hidden",
            )
            raise OSError("source failed")
        self._started = True
        return b"partial replacement"


@pytest.mark.asyncio
class ObjectStoreConformance(ProviderConformance[ObjectStoreProvider]):
    """Portable object operations, after the harness's checks. Point
    ``make_provider`` at a test namespace. ``presigned_get`` is a behaviour
    feature: without it ``url`` raises ``Unsupported("presigned_get")``.
    """

    contract = ObjectStoreProvider
    protocol = ObjectStore
    spec = OBJECTSTORE_CAPABILITY

    #: The driver's upload limit in bytes, when the test instance has one:
    #: a larger ``put`` raises ``TooLarge`` and keeps the previous object.
    upload_limit_bytes: ClassVar[int | None] = None
    #: The driver's buffered read limit in bytes, when the test instance has
    #: one below its upload limit: ``get`` of a larger object raises ``TooLarge``.
    read_limit_bytes: ClassVar[int | None] = None

    def feature_calls(self, provider: ObjectStoreProvider) -> Mapping[str, Call]:
        async def link() -> None:
            # Without presigned_get this raises before the check below.
            found = await provider.url(_key("link.bin"))
            require(
                isinstance(found, str) and bool(found),
                f"url() must answer a retrieval link, got {found!r}",
            )

        return {"presigned_get": link}

    def operation_calls(self, provider: ObjectStoreProvider) -> Mapping[str, Call]:
        key = _key("failing.bin")
        return {
            "put": partial(provider.put, key, b"conformance"),
            "get": partial(provider.get, key),
            "delete": partial(provider.delete, key),
        }

    async def read_metadata(
        self, provider: ObjectStoreProvider, key: str
    ) -> tuple[str | None, Mapping[str, str]]:
        """Read back the content type and metadata stored with ``key`` from
        the backend. A driver that declares ``metadata`` must override it:
        the kit checks the round trip and fails without the override."""
        raise NotImplementedError

    async def test_object_bytes_and_overwrite(
        self, provider: ObjectStoreProvider
    ) -> None:
        key = _key("object.bin")
        await provider.put(key, b"first")
        try:
            require(await provider.get(key) == b"first", "read returns stored bytes")
            prefix, _, _ = key.rpartition("/")
            with pytest.raises(NotFound):
                await provider.get(prefix)
            await provider.delete(prefix)
            require(
                await provider.get(key) == b"first",
                "deleting a prefix preserves objects",
            )
            await provider.put(key, b"second")
            require(await provider.get(key) == b"second", "put replaces the content")
        finally:
            await provider.delete(key)
        await provider.delete(key)
        with pytest.raises(NotFound):
            await provider.get(key)

    async def test_object_absent_key_is_not_found(
        self, provider: ObjectStoreProvider
    ) -> None:
        with pytest.raises(NotFound):
            await provider.get(_key("never-written.bin"))
        await provider.delete(_key("never-written.bin"))

    async def test_object_failed_upload_preserves_previous_value(
        self, provider: ObjectStoreProvider
    ) -> None:
        key = _key("previous.bin")
        await provider.put(key, b"original")
        try:
            with pytest.raises(OSError, match="source failed"):
                await provider.put(key, _FailingObjectStream(provider, key))
            require(
                await provider.get(key) == b"original", "failed put preserves old data"
            )
        finally:
            await provider.delete(key)

    async def test_object_invalid_keys(self, provider: ObjectStoreProvider) -> None:
        for key in (
            "",
            "/absolute",
            "a//b",
            "a/./b",
            "a/../b",
            "a\\b",
            "a\x00b",
            "é" * 513,
        ):
            with pytest.raises(InvalidArgument):
                await provider.put(key, b"test")
            with pytest.raises(InvalidArgument):
                await provider.get(key)
            with pytest.raises(InvalidArgument):
                await provider.delete(key)
            with pytest.raises(InvalidArgument):
                await provider.url(key)

    async def test_object_async_stream(self, provider: ObjectStoreProvider) -> None:
        key = _key("stream.bin")
        await provider.put(key, _ObjectStream(b"first", b"second"))
        try:
            require(await provider.get(key) == b"firstsecond", "put reads all chunks")
        finally:
            await provider.delete(key)

    async def test_object_upload_over_the_limit_is_too_large(
        self, provider: ObjectStoreProvider
    ) -> None:
        limit = self.upload_limit_bytes
        if limit is None:
            pytest.skip("set upload_limit_bytes to the test instance's upload limit")
        key = _key("bounded.bin")
        await provider.put(key, b"original")
        try:
            with pytest.raises(TooLarge):
                await provider.put(key, b"x" * (limit + 1))
            require(
                await provider.get(key) == b"original",
                "a refused upload keeps the previous object",
            )
        finally:
            await provider.delete(key)

    async def test_object_read_over_the_limit_is_too_large(
        self, provider: ObjectStoreProvider
    ) -> None:
        limit = self.read_limit_bytes
        if limit is None:
            pytest.skip("set read_limit_bytes to the test instance's read limit")
        key = _key("large.bin")
        await provider.put(key, b"x" * (limit + 1))
        try:
            with pytest.raises(TooLarge):
                await provider.get(key)
        finally:
            await provider.delete(key)

    async def test_object_metadata_round_trip(
        self, provider: ObjectStoreProvider
    ) -> None:
        self.requires_feature(provider, "metadata")
        key = _key("typed.txt")
        stored = {"source": "conformance"}
        await provider.put(key, b"hello", content_type="text/plain", metadata=stored)
        try:
            try:
                content_type, metadata = await self.read_metadata(provider, key)
            except NotImplementedError:
                raise AssertionError(
                    "the driver declares metadata: override read_metadata to "
                    "read the content type and metadata back from the backend"
                ) from None
            require(
                content_type == "text/plain",
                f"content type read back as {content_type!r}, stored 'text/plain'",
            )
            require(
                dict(metadata) == stored,
                f"metadata read back as {dict(metadata)!r}, stored {stored!r}",
            )
        finally:
            await provider.delete(key)


__all__ = ["ObjectStoreConformance"]
