"""The in-memory ``document_store`` driver (``memory``): the only
non-relational driver of Public API 1.0 (owner decision 12) — tests, demos
and single-process apps, never durable.

Besides the capability's CRUD it implements ``ConditionalWriteStore``
(``conditional_write``: version-guarded projection writes, tombstones) with
native tenancy (``tenant_native``), so a projection may target it
(``Projection(store=("document_store", name))``, spec §6.4). It passes
``sandloom.core.projections.stores.conditional_write_conformance``.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.extra_capabilities import DocumentStoreProvider
from sandloom.core.projections.stores import (
    CONDITIONAL_WRITE,
    TENANT_NATIVE,
    MemoryDocumentStore,
)

#: What the ``memory`` driver advertises.
MEMORY_DOCUMENT_STORE_FEATURES = frozenset({CONDITIONAL_WRITE, TENANT_NATIVE})


def _matches(document: Mapping[str, Any], query: Mapping[str, Any]) -> bool:
    return all(document.get(k) == v for k, v in query.items())


class MemoryDocumentStoreProvider(DocumentStoreProvider):
    """Process-local documents per collection, plus one conditional-write
    store per projection name (``collection``)."""

    def __init__(self, *, name: str, driver: str, options: Mapping[str, Any]) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._collections: dict[str, list[dict[str, Any]]] = {}
        self._projections: dict[str, MemoryDocumentStore] = {}
        self._ids = itertools.count(1)

    def features(self) -> frozenset[str]:
        return MEMORY_DOCUMENT_STORE_FEATURES

    # -- CRUD (DocumentStoreProvider) --------------------------------------
    async def insert(self, collection: str, document: Mapping[str, Any]) -> str:
        doc = dict(document)
        doc.setdefault("_id", str(next(self._ids)))
        self._collections.setdefault(collection, []).append(doc)
        return str(doc["_id"])

    async def find_one(
        self, collection: str, query: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        for doc in self._collections.get(collection, ()):
            if _matches(doc, query):
                return dict(doc)
        return None

    async def update(
        self, collection: str, query: Mapping[str, Any], changes: Mapping[str, Any]
    ) -> int:
        n = 0
        for doc in self._collections.get(collection, ()):
            if _matches(doc, query):
                doc.update(changes)
                n += 1
        return n

    async def delete(self, collection: str, query: Mapping[str, Any]) -> int:
        docs = self._collections.get(collection, [])
        keep = [d for d in docs if not _matches(d, query)]
        self._collections[collection] = keep
        return len(docs) - len(keep)

    # -- projections (ConditionalWriteStore per projection) ----------------
    def projection(
        self, name: str, deleted_column: str = "deleted_at"
    ) -> MemoryDocumentStore:
        """The conditional-write store of projection ``name``."""
        store = self._projections.get(name)
        if store is None:
            store = self._projections[name] = MemoryDocumentStore(deleted_column)
        return store

    async def put_if_newer(self, *, collection: str, **kwargs: Any) -> bool:
        return await self.projection(collection).put_if_newer(**kwargs)

    async def tombstone_if_newer(
        self,
        *,
        collection: str,
        tenant_id: UUID,
        key: Mapping[str, Any],
        version_column: str,
        version: int,
        deleted_at: datetime,
    ) -> bool:
        return await self.projection(collection).tombstone_if_newer(
            tenant_id=tenant_id,
            key=key,
            version_column=version_column,
            version=version,
            deleted_at=deleted_at,
        )


def create_memory_document_store(cfg: InstanceConfig) -> MemoryDocumentStoreProvider:
    """Driver factory for ``document_store`` / ``memory``."""
    return MemoryDocumentStoreProvider(
        name=cfg.name, driver=cfg.driver, options=dict(cfg.options)
    )


__all__ = [
    "MEMORY_DOCUMENT_STORE_FEATURES",
    "MemoryDocumentStoreProvider",
    "create_memory_document_store",
]
