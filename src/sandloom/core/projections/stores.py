"""Projection targets beyond a relational model (spec §6.4, §6.3 "consumer
effect in another store"): the ``ConditionalWriteStore`` contract, the
in-memory document driver (the only non-relational driver in 1.0) and the
conformance kit every driver of the ``conditional_write`` feature passes.

A projection into another store is convergent only because every write is
conditional on the row version (``put_if_newer`` / ``tombstone_if_newer``):
an older or duplicate event is a no-op, so any interleaving with duplicates
ends in the max-version state per source group (§6.4 item 3). A tombstone
is cleared only by a newer write of the group that owns deletion (its
``fields`` carry the deleted column as ``None``), never by another group's
upsert, as on a relational model. Tenancy is the
store's (``tenant_native``): every call names its tenant, a row of another
tenant is never read, overwritten or tombstoned.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

#: The store features a non-relational projection target needs.
CONDITIONAL_WRITE = "conditional_write"
TENANT_NATIVE = "tenant_native"


@runtime_checkable
class ConditionalWriteStore(Protocol):
    """Driver feature ``conditional_write`` (spec §2.3)."""

    async def put_if_newer(
        self,
        *,
        tenant_id: uuid.UUID,
        key: Mapping[str, Any],
        version_column: str,
        version: int,
        fields: Mapping[str, Any],
        insert_only: Mapping[str, Any] | None = None,
    ) -> bool: ...

    async def tombstone_if_newer(
        self,
        *,
        tenant_id: uuid.UUID,
        key: Mapping[str, Any],
        version_column: str,
        version: int,
        deleted_at: datetime,
    ) -> bool: ...


def _key(key: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(k), str(v)) for k, v in key.items()))


class MemoryDocumentStore:
    """The in-memory document driver (tests, single-process demos): rows per
    ``(tenant, key)``, version-guarded per source group's version column."""

    features = frozenset({CONDITIONAL_WRITE, TENANT_NATIVE})

    def __init__(self, deleted_column: str = "deleted_at") -> None:
        self.deleted_column = deleted_column
        self._rows: dict[
            tuple[uuid.UUID, tuple[tuple[str, str], ...]], dict[str, Any]
        ] = {}

    @staticmethod
    def _newer(
        row: Mapping[str, Any] | None, version_column: str, version: int
    ) -> bool:
        stored = None if row is None else row.get(version_column)
        return stored is None or stored < version

    async def put_if_newer(
        self,
        *,
        tenant_id: uuid.UUID,
        key: Mapping[str, Any],
        version_column: str,
        version: int,
        fields: Mapping[str, Any],
        insert_only: Mapping[str, Any] | None = None,
    ) -> bool:
        slot = (tenant_id, _key(key))
        row = self._rows.get(slot)
        if not self._newer(row, version_column, version):
            return False
        if row is None:
            row = {**dict(key), **dict(insert_only or {}), "tenant_id": tenant_id}
            self._rows[slot] = row
        # The tombstone is a field like any other: only the group that owns
        # deletion clears it (its fields carry ``deleted_column: None``), so
        # an independent group's upsert never resurrects a deleted row.
        row.update(fields)
        row[version_column] = version
        return True

    async def tombstone_if_newer(
        self,
        *,
        tenant_id: uuid.UUID,
        key: Mapping[str, Any],
        version_column: str,
        version: int,
        deleted_at: datetime,
    ) -> bool:
        slot = (tenant_id, _key(key))
        row = self._rows.get(slot)
        if not self._newer(row, version_column, version):
            return False
        if row is None:
            row = {**dict(key), "tenant_id": tenant_id}
            self._rows[slot] = row
        row[version_column] = version
        row[self.deleted_column] = deleted_at
        return True

    # -- reads (ProjectionView over this store) ---------------------------
    async def get(
        self, *, tenant_id: uuid.UUID, key: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        row = self._rows.get((tenant_id, _key(key)))
        if row is None or row.get(self.deleted_column) is not None:
            return None
        return dict(row)

    async def version_of(
        self, *, tenant_id: uuid.UUID, key: Mapping[str, Any], version_column: str
    ) -> int | None:
        """The stored version (tombstones included): what a consistency token
        waits for."""
        row = self._rows.get((tenant_id, _key(key)))
        return None if row is None else row.get(version_column)

    async def query(
        self,
        *,
        tenant_id: uuid.UUID,
        filter: Mapping[str, Any] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        out = []
        for (tenant, _k), row in sorted(self._rows.items(), key=lambda kv: kv[0][1]):
            if tenant != tenant_id or row.get(self.deleted_column) is not None:
                continue
            if all(row.get(c) == v for c, v in (filter or {}).items()):
                out.append(dict(row))
                if len(out) >= limit:
                    break
        return out

    async def purge_tombstones(self, before: datetime) -> int:
        doomed = [
            slot
            for slot, row in self._rows.items()
            if (deleted := row.get(self.deleted_column)) is not None
            and deleted < before
        ]
        for slot in doomed:
            del self._rows[slot]
        return len(doomed)


def _require(condition: object, rule: str) -> None:
    if not condition:
        raise AssertionError(f"conditional_write conformance: {rule}")


async def conditional_write_conformance(
    store: ConditionalWriteStore, *, deleted_at: datetime
) -> None:
    """The conformance kit of the ``conditional_write`` + ``tenant_native``
    features: raises ``AssertionError`` on the first violated rule. A driver
    whose store passes may be a projection target (SANDLOOM-B012 otherwise)."""
    a, b = uuid.uuid4(), uuid.uuid4()
    key = {"tenant_id": str(a), "id": "x"}
    vc = "v"
    _require(
        await store.put_if_newer(
            tenant_id=a, key=key, version_column=vc, version=2, fields={"f": "two"}
        ),
        "the first write of a key applies",
    )
    _require(
        not await store.put_if_newer(
            tenant_id=a, key=key, version_column=vc, version=1, fields={"f": "one"}
        ),
        "an older version never applies",
    )
    _require(
        not await store.put_if_newer(
            tenant_id=a, key=key, version_column=vc, version=2, fields={"f": "dup"}
        ),
        "a duplicate (same version) never applies",
    )
    _require(
        await store.put_if_newer(
            tenant_id=b, key=key, version_column=vc, version=1, fields={"f": "b"}
        ),
        "the same key of another tenant is another row",
    )
    _require(
        not await store.tombstone_if_newer(
            tenant_id=a, key=key, version_column=vc, version=1, deleted_at=deleted_at
        ),
        "an older tombstone never applies",
    )
    _require(
        await store.tombstone_if_newer(
            tenant_id=a, key=key, version_column=vc, version=3, deleted_at=deleted_at
        ),
        "a newer tombstone applies",
    )
    _require(
        not await store.put_if_newer(
            tenant_id=a, key=key, version_column=vc, version=3, fields={"f": "zombie"}
        ),
        "a write at the tombstone's version never resurrects the row",
    )
    _require(
        await store.put_if_newer(
            tenant_id=a, key=key, version_column="w", version=1, fields={"g": 1}
        ),
        "another group's version is guarded on its own",
    )
    reader = getattr(store, "get", None)
    if reader is not None:
        _require(
            await reader(tenant_id=a, key=key) is None,
            "another group's upsert never clears a tombstone",
        )
        other = await reader(tenant_id=b, key=key)
        _require(other is not None and other.get("f") == "b", "tenants stay isolated")
        deleted = getattr(store, "deleted_column", "deleted_at")
        _require(
            await store.put_if_newer(
                tenant_id=a,
                key=key,
                version_column=vc,
                version=4,
                fields={"f": "back", deleted: None},
            ),
            "a newer write of the deleting group applies",
        )
        back = await reader(tenant_id=a, key=key)
        _require(
            back is not None and back.get("f") == "back",
            "a newer write of the deleting group clears its tombstone",
        )


__all__ = [
    "CONDITIONAL_WRITE",
    "TENANT_NATIVE",
    "ConditionalWriteStore",
    "MemoryDocumentStore",
    "conditional_write_conformance",
]
