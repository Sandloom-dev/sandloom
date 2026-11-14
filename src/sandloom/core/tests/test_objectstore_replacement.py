"""Exercise object storage through the kit, app composition and the public SDK."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.drivers.local_objectstore import (
    LocalObjectStoreProvider,
    create_local_objectstore,
)
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.tests._root_support import create_root_app
from sandloom.driverkit import (
    NotFound,
    ObjectContent,
    ObjectStoreProvider,
    iter_object_chunks,
    validate_object_key,
)
from sandloom.driverkit.testing import ObjectStoreConformance
from sandloom.sdk.infra import objectstore

#: The probe's objects, per namespace option: two providers built from the
#: same options share one store, as the harness's isolation check requires.
_STORES: dict[str, dict[str, bytes]] = {}


class MemoryObjectStore(ObjectStoreProvider):
    contract_version = 2
    supported_features = frozenset({"presigned_get"})

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.namespace = str(self.options.get("namespace", "default"))
        self.objects = _STORES.setdefault(self.namespace, {})

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
            raise NotFound(key, capability="objectstore") from exc

    async def delete(self, key: str) -> None:
        validate_object_key(key)
        self.objects.pop(key, None)

    async def url(self, key: str) -> str:
        validate_object_key(key)
        return f"memory://{key}"

    async def isolation_identity(self) -> str | None:
        return f"{self.driver}:{self.namespace}"


def _install_test_plugin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dist = tmp_path / "objectstore_probe-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Name: objectstore-probe\nVersion: 1.0\n")
    (dist / "entry_points.txt").write_text(
        f"[sandloom.drivers]\nobjectstore.probe = {__name__}:MemoryObjectStore\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))


class TestLocalObjectStore(ObjectStoreConformance):
    upload_limit_bytes = 1024

    @pytest.fixture
    def make_provider(self, tmp_path: Path) -> Callable[[], ObjectStoreProvider]:
        options = {"path": str(tmp_path / "objects"), "max_upload_size_bytes": 1024}
        return lambda: create_local_objectstore(
            InstanceConfig("objectstore", "uploads", "local", dict(options))
        )


class TestProbeObjectStore(ObjectStoreConformance):
    @pytest.fixture
    def make_provider(self) -> Callable[[], ObjectStoreProvider]:
        return lambda: MemoryObjectStore(
            name="default", driver="probe", options={"namespace": "kit"}
        )


@pytest.mark.parametrize(
    ("driver", "expected"),
    [("local", LocalObjectStoreProvider), ("probe", MemoryObjectStore)],
)
async def test_the_app_resolves_the_selected_driver(
    driver: str,
    expected: type[ObjectStoreProvider],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_test_plugin(monkeypatch, tmp_path)
    options = {"driver": driver}
    if driver == "local":
        options["path"] = str(tmp_path / "objects")
    monkeypatch.setattr(settings, "infra", {"objectstore": {"default": options}})
    app = create_root_app()
    async with app.router.lifespan_context(app):
        store = objectstore()
        assert isinstance(store, expected)
        key = "conformance/composed.bin"
        await store.put(key, b"composed")
        assert await store.get(key) == b"composed"
        await store.delete(key)
