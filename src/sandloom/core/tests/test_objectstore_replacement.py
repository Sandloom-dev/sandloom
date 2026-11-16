"""Exercise object storage through the kit, app composition and the public SDK."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from functools import partial
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
    HealthResult,
    NotFound,
    ObjectContent,
    ObjectStoreProvider,
    PermissionDenied,
    TooLarge,
    Unavailable,
    iter_object_chunks,
    validate_object_key,
)
from sandloom.driverkit.testing import ObjectStoreConformance
from sandloom.sdk.infra import objectstore

#: The probe's objects, per namespace option: two providers built from the
#: same options share one store, as the harness's isolation check requires.
_STORES: dict[str, dict[str, bytes]] = {}


class MemoryObjectStore(ObjectStoreProvider):
    """A memory store with an upload and a read limit; ``down`` makes its
    backend fail."""

    contract_version = 2
    supported_features = frozenset({"presigned_get"})

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.namespace = str(self.options.get("namespace", "default"))
        self.objects = _STORES.setdefault(self.namespace, {})
        self.upload_limit = int(self.options.get("upload_limit", 0))
        self.read_limit = int(self.options.get("read_limit", 0))
        self.down = bool(self.options.get("down", False))

    def _reachable(self) -> None:
        if self.down:
            raise Unavailable("memory store down", capability="objectstore")

    async def put(
        self,
        key: str,
        data: ObjectContent,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        validate_object_key(key)
        self._reachable()
        chunks = [chunk async for chunk in iter_object_chunks(data, 1024)]
        body = b"".join(chunks)
        if self.upload_limit and len(body) > self.upload_limit:
            raise TooLarge("over the upload limit", capability="objectstore")
        self.objects[key] = body
        return key

    async def get(self, key: str) -> bytes:
        validate_object_key(key)
        self._reachable()
        try:
            body = self.objects[key]
        except KeyError as exc:
            raise NotFound(key, capability="objectstore") from exc
        if self.read_limit and len(body) > self.read_limit:
            raise TooLarge("over the read limit", capability="objectstore")
        return body

    async def delete(self, key: str) -> None:
        validate_object_key(key)
        self._reachable()
        self.objects.pop(key, None)

    async def url(self, key: str) -> str:
        validate_object_key(key)
        return f"memory://{key}"

    async def health(self) -> HealthResult:
        if self.down:
            return HealthResult(status="fail", details={"error": "Unavailable"})
        return HealthResult(status="ok")

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


def _local(path: Path, name: str = "uploads") -> LocalObjectStoreProvider:
    options = {"path": str(path), "max_upload_size_bytes": 1024}
    return create_local_objectstore(
        InstanceConfig("objectstore", name, "local", options)
    )


class TestLocalObjectStore(ObjectStoreConformance):
    upload_limit_bytes = 1024
    # One limit bounds both: the read check places its object out of band.
    read_limit_bytes = 1024

    @pytest.fixture
    def make_provider(self, tmp_path: Path) -> Callable[[], ObjectStoreProvider]:
        return lambda: _local(tmp_path / "objects")

    @pytest.fixture
    def make_failing_provider(
        self, tmp_path: Path
    ) -> Iterator[Callable[[], ObjectStoreProvider]]:
        if os.geteuid() == 0:
            pytest.skip("root reads and writes an unreadable directory")
        # An unreadable, unwritable root whose path carries the kit's secret:
        # an error that shows the host path shows the secret.
        root = tmp_path / self.leak_marker / "objects"
        built = _local(root)
        root.chmod(0)
        try:
            yield lambda: built
        finally:
            root.chmod(0o700)

    async def place_oversized_object(
        self, provider: ObjectStoreProvider, key: str, data: bytes
    ) -> None:
        assert isinstance(provider, LocalObjectStoreProvider)
        target = Path(provider.storage.path, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


class TestProbeObjectStore(ObjectStoreConformance):
    upload_limit_bytes = 64
    read_limit_bytes = 32

    @pytest.fixture
    def make_provider(self) -> Callable[[], ObjectStoreProvider]:
        options = {"namespace": "kit", "upload_limit": 64, "read_limit": 32}
        return lambda: MemoryObjectStore(
            name="default", driver="probe", options=options
        )

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], ObjectStoreProvider]:
        options = {"namespace": "kit-down", "down": True}
        return lambda: MemoryObjectStore(
            name="default", driver="probe", options=options
        )


async def test_the_local_identity_names_the_resolved_root_and_no_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "objects"
    root.mkdir()
    (tmp_path / "link").symlink_to(root, target_is_directory=True)
    direct = await _local(root).isolation_identity()
    linked = await _local(tmp_path / "link", name="exports").isolation_identity()
    assert direct == linked
    assert direct is not None
    assert direct.startswith("local:")
    assert str(tmp_path) not in direct
    assert "objects" not in direct


async def test_a_local_filesystem_failure_names_only_its_type(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads and writes an unreadable directory")
    root = tmp_path / "private-root" / "objects"
    store = _local(root)
    await store.put("a.bin", b"x")
    root.chmod(0)
    try:
        for call in (
            partial(store.put, "b/c.bin", b"y"),
            partial(store.get, "a.bin"),
            partial(store.delete, "a.bin"),
        ):
            with pytest.raises(PermissionDenied) as caught:
                await call()
            assert "private-root" not in str(caught.value)
            assert "PermissionError" in str(caught.value)
    finally:
        root.chmod(0o700)


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
    # the process INFRA keeps the urls of the Redis instances the app opens
    monkeypatch.setattr(
        settings, "infra", {**settings.infra, "objectstore": {"default": options}}
    )
    app = create_root_app()
    async with app.router.lifespan_context(app):
        store = objectstore()
        assert isinstance(store, expected)
        key = "conformance/composed.bin"
        await store.put(key, b"composed")
        assert await store.get(key) == b"composed"
        await store.delete(key)
