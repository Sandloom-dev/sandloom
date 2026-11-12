"""Exercise object storage through app composition and the public SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandloom.core.config.settings import settings
from sandloom.core.drivers.local_objectstore import LocalObjectStoreProvider
from sandloom.core.tests._root_support import create_root_app
from sandloom.driverkit import (
    ObjectContent,
    ObjectStoreProvider,
    iter_object_chunks,
    validate_object_key,
)
from sandloom.driverkit.testing import ObjectStoreConformance
from sandloom.sdk.infra import ObjectStore, objectstore


class MemoryObjectStore(ObjectStoreProvider):
    contract_version = "v1"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.objects: dict[str, bytes] = {}

    async def put(
        self,
        key: str,
        data: ObjectContent,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        validate_object_key(key)
        chunks = [chunk async for chunk in iter_object_chunks(data, 1024)]
        self.objects[key] = b"".join(chunks)
        return key

    async def get(self, key: str) -> bytes:
        validate_object_key(key)
        try:
            return self.objects[key]
        except KeyError as exc:
            raise FileNotFoundError(key) from exc

    async def delete(self, key: str) -> None:
        validate_object_key(key)
        self.objects.pop(key, None)

    async def url(self, key: str) -> str:
        validate_object_key(key)
        return f"memory://{key}"


def _install_test_plugin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dist = tmp_path / "objectstore_probe-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Name: objectstore-probe\nVersion: 1.0\n")
    (dist / "entry_points.txt").write_text(
        f"[sandloom.drivers]\nobjectstore.probe = {__name__}:MemoryObjectStore\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))


class TestObjectStoreReplacement(ObjectStoreConformance):
    @pytest_asyncio.fixture(params=["local", "probe"])
    async def store(
        self,
        request: pytest.FixtureRequest,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> AsyncIterator[ObjectStore]:
        _install_test_plugin(monkeypatch, tmp_path)
        options = {"driver": request.param}
        if request.param == "local":
            options["path"] = str(tmp_path / "objects")
        monkeypatch.setattr(settings, "infra", {"objectstore": {"default": options}})
        app = create_root_app()
        async with app.router.lifespan_context(app):
            store = objectstore()
            expected = (
                LocalObjectStoreProvider
                if request.param == "local"
                else MemoryObjectStore
            )
            assert isinstance(store, expected)
            yield store
