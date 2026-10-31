"""Built-in object-store drivers.

``local`` is backed by :class:`~sandloom.core.drivers.filesystem_storage.Storage`
(filesystem), constructed only here. The ``default`` instance is built once and
reused so the health check and callers refer to the same object; named instances
get their own root path.

``s3``/``gcs``/``yc`` are registered as known drivers that surface a clear
"not implemented" error rather than silently mis-routing — the contract and
config plumbing are ready for a real driver to be dropped in.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from sandloom.core.config.settings import settings
from sandloom.core.descriptors import HealthResult
from sandloom.core.drivers.filesystem_storage import Storage
from sandloom.core.infra.config import DEFAULT_INSTANCE, InstanceConfig
from sandloom.core.infra.contracts import ObjectStoreProvider
from sandloom.core.infra.registry import FeatureUnsupportedError


class _BytesReader:
    """Adapts an in-memory ``bytes`` blob to the async-readable Storage expects."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._sent = False

    async def read(self, size: int = -1) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return self._data


#: Features the local (and any plug-compatible) object store supports.
OBJECTSTORE_FEATURES: frozenset[str] = frozenset()


class LocalObjectStoreOptions(BaseModel):
    """Config-shape schema for the ``local`` (filesystem) object-store driver.

    The driver reads ``path`` (root directory) and ``max_upload_size_bytes``;
    both are optional (default to the flat ``STORAGE_*`` settings).
    ``extra="forbid"`` rejects a typo'd key at startup. Cloud drivers
    (``s3``/``gcs``/``yc``) take different options and carry their own schema
    when implemented.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    path: str | None = None
    max_upload_size_bytes: int | None = None


class LocalObjectStoreProvider(ObjectStoreProvider):
    """Filesystem-backed object store."""

    def __init__(
        self,
        *,
        name: str,
        driver: str,
        options: dict[str, object],
        storage: Storage,
    ) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._storage = storage

    @property
    def storage(self) -> Storage:
        return self._storage

    def features(self) -> frozenset[str]:
        return OBJECTSTORE_FEATURES

    async def put(
        self,
        key: str,
        data: bytes | Any,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        # content_type/metadata have no place on a plain filesystem; cloud
        # drivers (S3/GCS) carry them. The key is sanitized by Storage (no
        # traversal, no symlink escape) before it touches the host path.
        if isinstance(data, (bytes, bytearray)):
            reader: Any = _BytesReader(bytes(data))
        else:
            reader = data
        await self._storage.save(reader, key)
        return key

    async def get(self, key: str) -> bytes:
        return await self._storage.read(key)

    async def delete(self, key: str) -> None:
        await self._storage.delete(key)

    async def url(self, key: str) -> str:
        self._storage._resolve(key)
        raise FeatureUnsupportedError(
            "The local object store has no HTTP retrieval route",
            fix="serve objects through an authenticated application endpoint "
            "using objectstore.get(), or install a driver providing retrieval URLs",
        )

    async def health(self) -> HealthResult:
        # Existence of the configured local root is sufficient liveness.
        if self._storage.type == "local" and not os.path.isdir(self._storage.path):  # noqa: ASYNC240 - one local stat, negligible
            return HealthResult(
                status="fail", details={"error": f"missing path {self._storage.path}"}
            )
        return HealthResult(status="ok")


_default_storage: Storage | None = None


def get_default_storage() -> Storage:
    """Return the process-wide ``default`` filesystem storage (built once)."""
    global _default_storage
    if _default_storage is None:
        _default_storage = Storage()
    return _default_storage


def create_local_objectstore(cfg: InstanceConfig) -> LocalObjectStoreProvider:
    """Driver factory for ``objectstore`` capability / ``local`` driver."""

    if settings.is_prod and not settings.accepts_risk("local_storage_in_prod"):
        raise ValueError(
            "Local object storage in production requires the accepted risk "
            'SANDLOOM_ACCEPTED_RISKS=["local_storage_in_prod"] and a persistent '
            "filesystem shared by every application replica; otherwise install "
            "a shared object store driver"
        )
    if cfg.name == DEFAULT_INSTANCE and not cfg.options.get("path"):
        return LocalObjectStoreProvider(
            name=cfg.name,
            driver=cfg.driver,
            options=dict(cfg.options),
            storage=get_default_storage(),
        )
    path = cfg.options.get("path")
    storage = Storage(
        storage_type="local",
        path=str(path) if path else None,
        max_upload_size_bytes=_as_int(cfg.options.get("max_upload_size_bytes")),
    )
    return LocalObjectStoreProvider(
        name=cfg.name,
        driver=cfg.driver,
        options=dict(cfg.options),
        storage=storage,
    )


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):  # avoid treating bools as ints
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


__all__ = [
    "LocalObjectStoreProvider",
    "LocalObjectStoreOptions",
    "create_local_objectstore",
    "get_default_storage",
]
