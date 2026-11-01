"""Declarative projections (L1, spec §2.3, §6.4).

A projection is a read model fed by source groups of one aggregate each, with
per-aggregate versions, one owning group per column, full-state events,
delta events only under the durable inbox and versioned tombstones::

    TASKS = Projection(
        "board.tasks",
        key=("tenant_id", "task_id"),
        model=BoardTask,
        groups=[SourceGroup(
            events={TaskSaved: full_state(lambda e: {"title": e.title}),
                    TaskDeleted: tombstone(lambda e: {})},
            aggregate_key="task_id", version_field="version",
            version_column="task_version", owns=("title",),
        )],
    )

The L2 equivalent (``test_l1_equals_l2_registration``): a
``QueryProjectionDescriptor``, one ``ProjectionConsumerDescriptor`` per source
channel whose handler applies ``monotonic_upsert`` / ``monotonic_delete``,
and a ``ProjectionRebuilder`` when ``rebuild_from`` is given. The
descriptor carries the tombstone purge (``purge_tombstones()``) and the
consistency-token entries of a source event.

A projection targets a relational ``model`` or a store with the
``conditional_write`` feature: ``store=("document_store", "<instance>")``
(version-guarded ``put_if_newer`` / ``tombstone_if_newer``; delta mappers
are refused there, spec §6.4 item 4).
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Generic, Literal, Protocol, TypeVar, cast
from uuid import UUID

from fastapi import Request, Response, params
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sandloom.sdk._decl._owner import OWNER_ATTR, caller_module
from sandloom.sdk._decl.errors import ProjectionDefinitionError
from sandloom.sdk._decl.events import Event, from_wire, is_concrete, wire_name
from sandloom.sdk._prim.contracts import service
from sandloom.sdk._prim.data import Page, TenantScoped, open_uow
from sandloom.sdk._prim.events import EventEnvelope
from sandloom.sdk._prim.infra import CapabilityRef, capability
from sandloom.sdk._prim.projections import (
    ConditionalWriteStore,
    ConsistencyToken,
    ProjectionConsumerDescriptor,
    ProjectionRebuildResult,
    QueryProjectionDescriptor,
    monotonic_delete,
    monotonic_upsert,
    read_token,
)
from sandloom.sdk._prim.tenant import bind_tenant, current_tenant_id_optional, unscoped
from sandloom.sdk._prim.web import DbSession
from sandloom.sdk._stub import not_yet

#: Request header of a consistency token; response header of a stale read.
CONSISTENCY_HEADER = "SANDLOOM-Consistency-Token"
STALE_HEADER = "SANDLOOM-Projection-Stale"

E = TypeVar("E", bound=Event)
E_contra = TypeVar("E_contra", bound=Event, contravariant=True)
R = TypeVar("R")

#: A provider's bound contract read method a projection rebuilds from.
BoundContractMethod = Callable[..., Awaitable[Any]]
MapperKind = Literal["full_state", "delta", "tombstone"]


class Mapper(Protocol[E_contra]):
    """Maps an event to the projection fields its group owns."""

    kind: ClassVar[Literal["full_state", "delta", "tombstone"]]

    def __call__(self, event: E_contra) -> Mapping[str, Any]: ...


class _Mapper(Generic[E]):
    kind: ClassVar[MapperKind]

    def __init__(self, fn: Callable[[E], Mapping[str, Any]]) -> None:
        if not callable(fn):
            raise ProjectionDefinitionError(f"a mapper wraps a function, not {fn!r}")
        self.fn = fn

    def __call__(self, event: E) -> Mapping[str, Any]:
        return self.fn(event)

    def __repr__(self) -> str:
        return f"{self.kind}({getattr(self.fn, '__qualname__', self.fn)})"


class _FullState(_Mapper[E]):
    kind: ClassVar[MapperKind] = "full_state"


class _Delta(_Mapper[E]):
    kind: ClassVar[MapperKind] = "delta"


class _Tombstone(_Mapper[E]):
    kind: ClassVar[MapperKind] = "tombstone"


def full_state(fn: Callable[[E], Mapping[str, Any]]) -> Mapper[E]:
    """A mapper of full-state events: every owned field, replaced iff the
    stored version is NULL or older (convergent under any interleaving)."""
    return _FullState(fn)


def delta(fn: Callable[[E], Mapping[str, Any]]) -> Mapper[E]:
    """A mapper of delta events (column increments); allowed only for a
    relational projection applied under the durable inbox (§6.4 item 4)."""
    return _Delta(fn)


def tombstone(fn: Callable[[E], Mapping[str, Any]]) -> Mapper[E]:
    """A mapper of delete events: a versioned tombstone in the owning group
    (§6.4 item 5); reads filter it."""
    return _Tombstone(fn)


@dataclass(frozen=True)
class SourceGroup:
    """The channels of ONE aggregate feeding a projection, with its version."""

    events: Mapping[type[Event], Mapper[Any]]
    aggregate_key: str  # event field holding the aggregate id
    version_field: str  # event field holding the aggregate version
    version_column: str  # projection column that stores it
    owns: tuple[str, ...]  # projection columns this group writes
    #: Event field holding the row's tenant: ``"organization_id"`` for a
    #: source that names its organization so (``tenancy.member_*.v1``). The
    #: consumer binds that tenant and the row's ``tenant_id`` key is read
    #: from it unless the mapper sets it.
    tenant_from: str = "tenant_id"


def _columns(model: type[Any]) -> set[str]:
    table = getattr(model, "__table__", None)
    return {c.name for c in table.columns} if table is not None else set()


def _uuid_field(event: type[Event], field: str) -> bool:
    info = event.model_fields.get(field)
    return info is not None and info.annotation is UUID


class Projection:
    """A declared read model (spec §6.4), registered by ``define_module``
    from the module's ``projections`` submodule.

    ``model=`` (a relational ``TenantScoped`` table) is the supported target
    in 1.0. ``store=`` (a ``document_store`` instance) is experimental in 1.0:
    tested against the in-memory driver only, without production drivers."""

    def __init__(
        self,
        name: str,
        *,
        key: tuple[str, ...],
        groups: Sequence[SourceGroup],
        model: type[TenantScoped] | None = None,
        store: tuple[str, str] | None = None,
        insert_only: Mapping[str, Any] | None = None,
        deleted_column: str = "deleted_at",
        rebuild_from: BoundContractMethod | tuple[str, str] | None = None,
        lag_slo_s: float = 5.0,
        at_least_once: bool = False,
    ) -> None:
        self.name = name
        self.key = tuple(key)
        self.groups = tuple(groups)
        self.model = model
        self.store = store
        self.insert_only = dict(insert_only or {})
        self.deleted_column = deleted_column
        self.rebuild_from = rebuild_from
        self.lag_slo_s = float(lag_slo_s)
        self.at_least_once = bool(at_least_once)
        setattr(self, OWNER_ATTR, caller_module(1))
        self._validate()

    def __repr__(self) -> str:
        return f"Projection({self.name!r})"

    # -- validation (SANDLOOM-B012) -------------------------------------------
    def _fail(self, problem: str, fix: str) -> None:
        raise ProjectionDefinitionError(f"projection {self.name!r}: {problem}", fix=fix)

    def _validate(self) -> None:
        if not self.name:
            self._fail("needs a name", 'Projection("<module>.<name>", ...)')
        if not self.key or self.key[0] != "tenant_id":
            self._fail(
                f"key {self.key!r} must start with 'tenant_id'",
                'key=("tenant_id", "<aggregate id>")',
            )
        self._validate_target()
        if not self.groups:
            self._fail("needs at least one SourceGroup", "groups=[SourceGroup(...)]")
        columns = _columns(self.model) if self.model is not None else None
        owner: dict[str, int] = {}
        versions: set[str] = set()
        for index, group in enumerate(self.groups):
            self._validate_group(index, group, owner, versions)
            for event, mapper in group.events.items():
                self._validate_event(group, event, mapper, columns)
        if columns is None:
            return  # a store target is schemaless
        needed = set(self.key) | set(owner) | versions | set(self.insert_only)
        missing = sorted(needed - columns)
        if missing:
            model = getattr(self.model, "__name__", self.model)
            self._fail(
                f"{model} has no column(s) {missing}",
                "add the columns, or fix key / owns / version_column / insert_only",
            )

    def _validate_target(self) -> None:
        if self.model is None and self.store is None:
            self._fail(
                "needs a target",
                'model=<a TenantScoped model>, or store=("document_store", "<name>")',
            )
        if self.model is not None and self.store is not None:
            self._fail("has two targets", "pass model= OR store=, not both")
        if self.store is not None and not (
            isinstance(self.store, tuple)
            and len(self.store) == 2
            and all(isinstance(part, str) and part for part in self.store)
        ):
            self._fail(
                f"store={self.store!r} is not (capability, instance)",
                'store=("document_store", "default")',
            )

    def _validate_group(
        self, index: int, group: SourceGroup, owner: dict[str, int], versions: set[str]
    ) -> None:
        if group.version_column in versions:
            self._fail(
                f"version column {group.version_column!r} is used by two groups",
                "give each source group (aggregate) its own version column",
            )
        versions.add(group.version_column)
        if not group.events:
            self._fail(f"group {index} has no events", "events={Event: mapper}")
        for column in group.owns:
            if column in owner:
                self._fail(
                    f"column {column!r} is owned by groups {owner[column]} and {index}",
                    "every column has exactly one owning source group (§6.4)",
                )
            if column in self.insert_only:
                self._fail(
                    f"column {column!r} is both owned and insert_only",
                    "a column is owned by a group or seeded insert_only",
                )
            owner[column] = index

    def _validate_event(
        self, group: SourceGroup, event: Any, mapper: Any, columns: set[str] | None
    ) -> None:
        if not (isinstance(event, type) and issubclass(event, Event)) or not (
            is_concrete(event)
        ):
            self._fail(f"{event!r} is not an Event class with a channel", "")
        kind = getattr(mapper, "kind", None)
        if kind not in ("full_state", "delta", "tombstone"):
            self._fail(
                f"the mapper of {event.__name__} is not full_state, delta or tombstone",
                "wrap it: full_state(lambda e: {...})",
            )
        if kind == "delta" and self.store is not None:
            self._fail(
                f"delta mapper for {event.__name__} on a store target",
                "deltas are allowed only for a relational model= projection "
                "under the durable inbox; send full-state events (§6.4 item 4)",
            )
        if kind == "delta" and self.at_least_once:
            self._fail(
                f"delta mapper for {event.__name__} without the durable inbox",
                "deltas are exactly once only under the inbox: drop "
                "at_least_once=True or send full-state events",
            )
        if (
            kind == "tombstone"
            and columns is not None
            and self.deleted_column not in columns
        ):
            self._fail(
                f"tombstones need the column {self.deleted_column!r} on "
                f"{getattr(self.model, '__name__', self.model)}",
                "add a nullable timestamp column, or name it deleted_column=",
            )
        for field in (group.aggregate_key, group.version_field):
            if field not in event.model_fields:
                self._fail(
                    f"{event.__name__} has no field {field!r}",
                    "aggregate_key / version_field name fields of every event "
                    "in the group",
                )
        if not _uuid_field(event, group.tenant_from):
            self._fail(
                f"{event.__name__} has no UUID field {group.tenant_from!r} "
                "naming the row's tenant",
                "name the event's tenant field: SourceGroup(..., "
                'tenant_from="organization_id")',
            )
        version = event.model_fields[group.version_field]
        if not version.is_required():
            self._fail(
                f"{event.__name__}.{group.version_field} is optional, so an event "
                "without it cannot be ordered against the stored row",
                "consume this source with a hand-written @on consumer that "
                "re-reads the owner's state when the version is absent (for "
                "tenancy.member_*.v1: member_event_action from "
                "sandloom.shared.membership), or project an event whose "
                "version is required (docs: modules/between/projections.md)",
            )

    # -- the L2 form ------------------------------------------------------------
    def channels(self) -> tuple[str, ...]:
        """The source channels, in declaration order."""
        seen: dict[str, None] = {}
        for group in self.groups:
            for event in group.events:
                seen.setdefault(event.channel, None)
        return tuple(seen)

    def descriptor(self, owner_module: str) -> QueryProjectionDescriptor:
        return QueryProjectionDescriptor(
            name=self.name,
            owner_module=owner_module,
            source_events=self.channels(),
            rebuildable=self.rebuild_from is not None,
            purge=self._purger(),
            token_entries=self.token_entries,
        )

    def _has_tombstones(self) -> bool:
        return any(
            mapper.kind == "tombstone"
            for group in self.groups
            for mapper in group.events.values()
        )

    def _purger(self) -> Callable[[Any, datetime], Awaitable[int]] | None:
        """Deletes the tombstones older than a cutoff (``purge_tombstones``).

        Relational: one DELETE over every tenant's rows (``unscoped()``; with
        ``tenant_rls`` only the system channel reaches other tenants' rows,
        so the purge may leave some: never unsafe, the watermark is raised
        first). A store: its ``purge_tombstones(cutoff)``."""
        if not self._has_tombstones():
            return None
        if self.store is not None:

            async def purge_store(_session: Any, cutoff: datetime) -> int:
                purge = getattr(self.target(), "purge_tombstones", None)
                if purge is None:
                    raise ProjectionDefinitionError(
                        f"projection {self.name!r}: store {self.store!r} cannot "
                        "purge tombstones"
                    )
                return int(await purge(cutoff))

            return purge_store
        table = cast(Any, self.model).__table__
        column = table.c[self.deleted_column]
        aware = bool(getattr(column.type, "timezone", False))

        async def purge_rows(session: Any, cutoff: datetime) -> int:
            if aware and cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=UTC)  # the kernel's cutoff is UTC
            elif not aware and cutoff.tzinfo is not None:
                cutoff = cutoff.astimezone(UTC).replace(tzinfo=None)
            with unscoped():
                result = await session.execute(
                    delete(table).where(column.is_not(None), column < cutoff)
                )
            return int(getattr(result, "rowcount", 0) or 0)

        return purge_rows

    def token_entries(
        self, channel: str, payload: Mapping[str, Any]
    ) -> Iterable[tuple[Mapping[str, Any], str, Any, int]]:
        """The ``(row key, group, aggregate id, version)`` an event of
        ``channel`` will produce (``group``: its source group's version
        column): a write's consistency token (spec §6.4 item 7)."""
        out: list[tuple[Mapping[str, Any], str, Any, int]] = []
        for group in self.groups:
            for event_cls, mapper in group.events.items():
                if event_cls.channel != channel:
                    continue
                event = from_wire(event_cls, payload)
                key, _values, version = self._row(group, event, mapper)
                aggregate = getattr(event, group.aggregate_key)
                out.append((key, group.version_column, aggregate, version))
        return out

    def target(self) -> ConditionalWriteStore:
        """The store of a ``store=`` projection (``conditional_write``)."""
        kind, name = cast(tuple[str, str], self.store)
        provider: Any = capability(CapabilityRef[Any](kind), name)
        per_projection = getattr(provider, "projection", None)
        store = (
            per_projection(self.name, self.deleted_column)
            if callable(per_projection)
            else provider
        )
        if not isinstance(store, ConditionalWriteStore):
            raise ProjectionDefinitionError(
                f"projection {self.name!r}: {kind} {name!r} has no conditional "
                "writes (driver feature conditional_write)",
                fix="target a store whose driver advertises conditional_write",
            )
        return store

    def consumers(self) -> list[ProjectionConsumerDescriptor]:
        out: list[ProjectionConsumerDescriptor] = []
        for group in self.groups:
            for event, mapper in group.events.items():
                out.append(
                    ProjectionConsumerDescriptor(
                        name=f"{self.name}@{event.channel}",
                        source_channel=event.channel,
                        projection=self.name,
                        handler=self._handler(group, event, mapper),
                        durable=not self.at_least_once,
                        bind_tenant_from=(
                            wire_name(event, group.tenant_from)
                            if _uuid_field(event, group.tenant_from)
                            else None
                        ),
                    )
                )
        return out

    def rebuilder(self) -> ProjectionRebuilderFromContract | None:
        if self.rebuild_from is None:
            return None
        return ProjectionRebuilderFromContract(self)

    # -- apply ------------------------------------------------------------------
    def _row(
        self, group: SourceGroup, event: Event, mapper: Mapper[Any]
    ) -> tuple[dict[str, Any], dict[str, Any], int]:
        fields = dict(mapper(event))
        key: dict[str, Any] = {}
        for column in self.key:
            if column in fields:
                key[column] = fields.pop(column)
            elif column == "tenant_id" and group.tenant_from != "tenant_id":
                key[column] = getattr(event, group.tenant_from)
            elif hasattr(event, column):
                key[column] = getattr(event, column)
            else:
                raise ProjectionDefinitionError(
                    f"projection {self.name!r}: key column {column!r} is neither "
                    f"mapped nor a field of {type(event).__name__}"
                )
        foreign = sorted(set(fields) - set(group.owns))
        if foreign:
            raise ProjectionDefinitionError(
                f"projection {self.name!r}: the mapper of {type(event).__name__} "
                f"writes {foreign}, which its group does not own"
            )
        version = int(getattr(event, group.version_field))
        return key, fields, version

    def _seed(self, event: Event) -> dict[str, Any]:
        return {
            c: (v(event) if callable(v) else v) for c, v in self.insert_only.items()
        }

    async def apply(
        self, session: Any, group: SourceGroup, event: Event, mapper: Mapper[Any]
    ) -> bool:
        """Apply one event to the projection in ``session`` (the consumer's unit)."""
        key, values, version = self._row(group, event, mapper)
        if self.store is not None:
            return await self._apply_store(key, values, group, version, event, mapper)
        model = self.model
        tombstones = any(m.kind == "tombstone" for m in group.events.values())
        if mapper.kind == "full_state":
            if tombstones:
                values[self.deleted_column] = None
            return await monotonic_upsert(
                session,
                model,
                key=key,
                values=values,
                version_column=group.version_column,
                incoming_version=version,
                insert_only=self._seed(event) or None,
            )
        if mapper.kind == "tombstone":
            return await monotonic_delete(
                session,
                model,
                key=key,
                version_column=group.version_column,
                incoming_version=version,
                values=values or None,
                insert_only=self._seed(event) or None,
                deleted_column=self.deleted_column,
            )
        return await self._apply_delta(session, key, values, group, version, event)

    async def _apply_store(
        self,
        key: dict[str, Any],
        values: dict[str, Any],
        group: SourceGroup,
        version: int,
        event: Event,
        mapper: Mapper[Any],
    ) -> bool:
        tenant = key.get("tenant_id")
        bound = current_tenant_id_optional()
        if not isinstance(tenant, UUID) or (bound is not None and tenant != bound):
            raise ProjectionDefinitionError(
                f"projection {self.name!r}: the row's tenant_id is missing or "
                "does not match the bound tenant"
            )
        store = self.target()
        if mapper.kind == "full_state" and any(
            m.kind == "tombstone" for m in group.events.values()
        ):
            # Only the group owning deletion clears the tombstone (as relational).
            values[self.deleted_column] = None
        if mapper.kind == "tombstone":
            return await store.tombstone_if_newer(
                tenant_id=tenant,
                key=key,
                version_column=group.version_column,
                version=version,
                deleted_at=datetime.now(UTC).replace(tzinfo=None),
            )
        return await store.put_if_newer(
            tenant_id=tenant,
            key=key,
            version_column=group.version_column,
            version=version,
            fields=values,
            insert_only=self._seed(event) or None,
        )

    async def _apply_delta(
        self,
        session: Any,
        key: dict[str, Any],
        values: dict[str, Any],
        group: SourceGroup,
        version: int,
        event: Event,
    ) -> bool:
        table = cast(Any, self.model).__table__
        row_tenant = key.get("tenant_id")
        bound = current_tenant_id_optional()
        if row_tenant is None or (bound is not None and row_tenant != bound):
            raise ProjectionDefinitionError(
                f"projection {self.name!r}: the row's tenant_id is missing or "
                "does not match the bound tenant"
            )
        row = {**key, **self._seed(event), **values, group.version_column: version}
        stmt = pg_insert(table).values(**row)
        update = {c: table.c[c] + stmt.excluded[c] for c in values}
        update[group.version_column] = func.greatest(
            func.coalesce(table.c[group.version_column], version),
            stmt.excluded[group.version_column],
        )
        upsert = stmt.on_conflict_do_update(
            index_elements=list(self.key),
            set_=update,
            where=table.c["tenant_id"] == row_tenant,  # never another tenant's row
        )
        # The same trusted path as monotonic_upsert: the tenant was checked
        # above; Core SQL takes no ORM loader criteria; RLS stays underneath.
        with unscoped():
            await session.execute(upsert)
        return True

    def _handler(
        self, group: SourceGroup, event_cls: type[Event], mapper: Mapper[Any]
    ) -> Callable[[Any, EventEnvelope], Awaitable[None]]:
        async def apply_event(session: Any, envelope: EventEnvelope) -> None:
            event = from_wire(event_cls, envelope.get("payload") or {})
            await self.apply(session, group, event, mapper)

        apply_event.__qualname__ = f"Projection({self.name}).{event_cls.__name__}"
        apply_event.__module__ = str(getattr(self, OWNER_ATTR))
        return apply_event


class ProjectionRebuilderFromContract:
    """The ``ProjectionRebuilder`` of a projection with ``rebuild_from``:
    reads source state through the read contract and applies it through the
    same version-guarded path the consumers use (never infers a delete).

    ``rebuild_from(tenant_id)`` returns the aggregate states as source events
    (instances, or mappings of the group's single full-state event).
    ``rebuild_all`` rebuilds every tenant the projection holds (a relational
    target; ``rebuild(name, tenant_id=...)`` for the others), and
    ``rebuild_entity("<tenant id>:<aggregate id>")`` one aggregate.
    """

    def __init__(self, projection: Projection) -> None:
        self.projection = projection

    def _call(self) -> BoundContractMethod:
        source = self.projection.rebuild_from
        if callable(source):
            return source
        if isinstance(source, tuple) and len(source) == 2:
            contract_name, method = source
            return cast(BoundContractMethod, getattr(service(contract_name), method))
        raise ProjectionDefinitionError(
            f"projection {self.projection.name!r}: rebuild_from={source!r} is "
            "neither a contract method nor (contract name, method name)"
        )

    def _decode(self, item: Any) -> tuple[SourceGroup, Event, Mapper[Any]]:
        for group in self.projection.groups:
            for event_cls, mapper in group.events.items():
                if isinstance(item, event_cls):
                    return group, item, mapper
        if isinstance(item, Mapping):
            candidates = [
                (group, event_cls, mapper)
                for group in self.projection.groups
                for event_cls, mapper in group.events.items()
                if mapper.kind == "full_state"
            ]
            if len(candidates) == 1:
                group, event_cls, mapper = candidates[0]
                return group, event_cls.model_validate(dict(item)), mapper
        raise ProjectionDefinitionError(
            f"projection {self.projection.name!r}: cannot apply rebuild item {item!r}"
        )

    async def rebuild_tenant(
        self, tenant_id: UUID, *, aggregate: str | None = None
    ) -> ProjectionRebuildResult:
        call = self._call()
        rows = 0
        with bind_tenant(tenant_id):
            items = await call(tenant_id)
            async with open_uow() as session:
                for item in items:
                    group, event, mapper = self._decode(item)
                    if aggregate is not None and (
                        str(getattr(event, group.aggregate_key)) != aggregate
                    ):
                        continue
                    rows += int(
                        await self.projection.apply(session, group, event, mapper)
                    )
        return ProjectionRebuildResult(projection=self.projection.name, rows=rows)

    async def _tenants(self) -> list[UUID]:
        model = self.projection.model
        if model is None:
            raise ProjectionDefinitionError(
                f"projection {self.projection.name!r}: a store target cannot "
                "list its tenants; rebuild(name, tenant_id=...) per tenant"
            )
        column = cast(Any, model).__table__.c["tenant_id"]
        async with open_uow() as session:
            with unscoped():
                found = await session.execute(select(column).distinct())
            return [row[0] for row in found]

    async def rebuild_all(self) -> ProjectionRebuildResult:
        """Every tenant the projection holds, one ``rebuild_tenant`` each (a
        tenant with no row yet is rebuilt by ``rebuild(name, tenant_id=)``)."""
        rows, tenants = 0, 0
        for tenant in await self._tenants():
            rows += (await self.rebuild_tenant(tenant)).rows
            tenants += 1
        return ProjectionRebuildResult(
            projection=self.projection.name, rows=rows, details={"tenants": tenants}
        )

    async def rebuild_entity(self, ref: str) -> ProjectionRebuildResult:
        """One aggregate, ``ref`` = ``"<tenant id>:<aggregate id>"``."""
        tenant, sep, aggregate = ref.partition(":")
        try:
            tenant_id = UUID(tenant)
        except ValueError:
            tenant_id = None
        if not sep or not aggregate or tenant_id is None:
            raise ProjectionDefinitionError(
                f"projection {self.projection.name!r}: entity ref {ref!r} is not "
                "'<tenant id>:<aggregate id>'"
            )
        return await self.rebuild_tenant(tenant_id, aggregate=aggregate)

    async def source_row(self, tenant_id: UUID, digest: str) -> dict[str, Any] | None:
        """The row of key ``digest`` read from the source (``on_stale=
        "source"``): the aggregate's state through ``rebuild_from``."""
        with bind_tenant(tenant_id):
            items = await self._call()(tenant_id)
        for item in items:
            group, event, mapper = self._decode(item)
            if mapper.kind != "full_state":
                continue
            key, values, version = self.projection._row(group, event, mapper)
            if ConsistencyToken.digest(key) == digest:
                return {**key, **values, group.version_column: version}
        return None


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

_PROJECTIONS: dict[str, Projection] = {}


def register_view_source(projection: Projection) -> None:
    """Make ``projection`` readable through ``View(name, ...)`` (discovery)."""
    _PROJECTIONS[projection.name] = projection


class ProjectionView(Generic[R]):
    """Reads one projection (``Annotated[ProjectionView[R], View(...)]``).

    Rows whose tombstone is set are never returned. With a consistency token
    (``SANDLOOM-Consistency-Token``) a read waits up to ``wait_ms`` for the rows
    the caller's write produces; then it serves anyway with ``stale = True``
    and ``SANDLOOM-Projection-Stale: true`` (``on_stale="serve"``), or reads the
    entity from its source through ``rebuild_from`` (``on_stale="source"``,
    ``get`` only). Never an error status.
    """

    stale: bool

    def __init__(
        self,
        projection: Projection,
        row: type[R],
        session: Any,
        *,
        on_stale: Literal["serve", "source"] = "serve",
        wait_ms: int | None = None,
        token: ConsistencyToken | None = None,
        response: Response | None = None,
    ) -> None:
        self.projection = projection
        self.row = row
        self.session = session
        self.on_stale = on_stale
        self.wait_ms = wait_ms
        self.token = token
        self.response = response
        self.stale = False

    # -- consistency tokens ---------------------------------------------------
    # A token entry names its source group (version column): a wait compares
    # that group's own column, never another group's (independent sources).
    @staticmethod
    def _group_version(row: Any, column: str) -> int | None:
        if isinstance(row, Mapping):
            return cast("int | None", row.get(column))
        return cast("int | None", getattr(row, column, None))

    def _group_column(self, group: str) -> str | None:
        """The version column of the token's ``group`` (``None``: not a
        group of this projection, so never reached)."""
        for g in self.projection.groups:
            if g.version_column == group:
                return g.version_column
        return None

    def _full_key(self, key: Mapping[str, Any]) -> dict[str, Any] | None:
        """The key scoped to the bound tenant (G-MSTORE-10). A key naming
        another tenant is ``None`` (fail closed: nothing to read); a store
        read needs a bound tenant, as a store query does."""
        full = dict(key)
        bound = current_tenant_id_optional()
        if bound is None:
            if self.projection.store is not None:
                raise LookupError(
                    f"projection {self.projection.name!r}: a store read needs a "
                    "bound tenant"
                )
            return full
        named = full.get("tenant_id")
        if named is not None and str(named) != str(bound):
            return None
        full["tenant_id"] = bound
        return full

    async def _stored_version(self, key: Mapping[str, Any], group: str) -> int | None:
        """The row's version in source ``group`` (its version column),
        tombstones included: what a token entry of that group waits for."""
        column = self._group_column(group)
        if column is None:
            return None
        if self.projection.store is not None:
            store = cast(Any, self.projection.target())
            version = await store.version_of(
                tenant_id=key.get("tenant_id"), key=key, version_column=column
            )
            return cast("int | None", version)
        model = cast(Any, self.projection.model)
        stmt = select(getattr(model, column))
        for name, value in key.items():
            stmt = stmt.where(getattr(model, name) == value)
        return cast("int | None", (await self.session.execute(stmt.limit(1))).scalar())

    def _mark_stale(self) -> None:
        self.stale = True
        if self.response is not None:
            self.response.headers[STALE_HEADER] = "true"

    async def _await_key(self, key: dict[str, Any]) -> bool:
        token = self.token
        if token is None:
            return True
        digest = ConsistencyToken.digest(key)
        if digest not in token.keys(self.projection.name):
            return True

        async def stored(entry_digest: str, group: str) -> int | None:
            if entry_digest != digest:
                return None  # another row of the token: never reached here
            return await self._stored_version(key, group)

        return await token.wait_for(self.projection.name, stored, wait_ms=self.wait_ms)

    def _select(self) -> Any:
        model = cast(Any, self.projection.model)
        # populate_existing: a row the session already holds (its identity
        # map) is RELOADED from the result, never served as the cached
        # object, so a token wait's reread sees the newer version another
        # unit committed meanwhile (G-MSTORE-07)
        stmt = select(model).execution_options(populate_existing=True)
        deleted = self.projection.deleted_column
        if deleted in _columns(model):
            stmt = stmt.where(getattr(model, deleted).is_(None))
        return stmt

    def _convert(self, obj: Any) -> R:
        validate = getattr(self.row, "model_validate", None)
        if callable(validate):
            return cast(R, validate(obj, from_attributes=True))
        return cast(R, obj)

    async def get(self, key: Mapping[str, Any]) -> R | None:
        full = self._full_key(key)
        if full is None:
            return None  # a key naming another tenant: never its row
        if not await self._await_key(full):
            source = self.projection.rebuilder()
            if self.on_stale == "source" and source is not None:
                tenant = full.get("tenant_id")
                if isinstance(tenant, UUID):
                    row = await source.source_row(tenant, ConsistencyToken.digest(full))
                    if (
                        row is not None
                        and row.get(self.projection.deleted_column) is None
                    ):
                        return self._convert(row)
            self._mark_stale()
        if self.projection.store is not None:
            store = cast(Any, self.projection.target())
            found_row = await store.get(tenant_id=full.get("tenant_id"), key=full)
            return None if found_row is None else self._convert(found_row)
        model = cast(Any, self.projection.model)
        stmt = self._select()
        for column, value in key.items():
            stmt = stmt.where(getattr(model, column) == value)
        found = (await self.session.execute(stmt.limit(1))).scalars().first()
        return None if found is None else self._convert(found)

    async def query(
        self,
        *,
        filter: Mapping[str, Any] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[R]:
        offset = _decode_cursor(cursor)
        if self.projection.store is not None:
            return await self._query_store(filter, limit, offset)
        model = cast(Any, self.projection.model)
        stmt = self._select()
        for column, value in (filter or {}).items():
            stmt = stmt.where(getattr(model, column) == value)
        for column in self.projection.key:
            stmt = stmt.order_by(getattr(model, column))
        stmt = stmt.offset(offset).limit(limit + 1)

        async def read() -> list[Any]:
            return list((await self.session.execute(stmt)).scalars())

        rows = await self._read_with_token(read)
        return self._page(rows, limit, offset)

    async def _query_store(
        self, filter: Mapping[str, Any] | None, limit: int, offset: int
    ) -> Page[R]:
        store = cast(Any, self.projection.target())
        tenant = current_tenant_id_optional()
        if tenant is None:
            raise LookupError(
                f"projection {self.projection.name!r}: a store read needs a "
                "bound tenant"
            )
        named = (filter or {}).get("tenant_id")
        if named is not None and str(named) != str(tenant):
            return self._page([], limit, offset)  # another tenant: fail closed

        async def read() -> list[Any]:
            found = await store.query(
                tenant_id=tenant, filter=filter, limit=offset + limit + 1
            )
            return list(found)[offset:]

        rows = await self._read_with_token(read)
        return self._page(rows, limit, offset)

    async def _read_with_token(
        self, read: Callable[[], Awaitable[list[Any]]]
    ) -> list[Any]:
        """A page read that waits for the token's rows it can see. A token
        row missing from the page counts as not reached (a filter that
        excludes it makes the read wait its budget, then serve stale)."""
        rows = await read()
        token = self.token
        if token is None or not token.versions(self.projection.name):
            return rows
        latest: dict[str, list[Any]] = {"rows": rows}

        async def stored(digest: str, group: str) -> int | None:
            column = self._group_column(group)
            if column is None:
                return None
            for row in latest["rows"]:
                if ConsistencyToken.digest(self._row_key(row)) == digest:
                    return self._group_version(row, column)
            return None

        async def reread() -> None:
            # Every poll round after the first rereads the page: a cached row
            # behind the token (not only a missing one) catches up (G-MSTORE-07).
            latest["rows"] = await read()

        if not await token.wait_for(
            self.projection.name, stored, wait_ms=self.wait_ms, refresh=reread
        ):
            self._mark_stale()
        return latest["rows"]

    def _row_key(self, row: Any) -> dict[str, Any]:
        if isinstance(row, Mapping):
            return {c: row.get(c) for c in self.projection.key}
        return {c: getattr(row, c, None) for c in self.projection.key}

    def _page(self, rows: list[Any], limit: int, offset: int) -> Page[R]:
        more = len(rows) > limit
        items = [self._convert(r) for r in rows[:limit]]
        return Page(
            items=items, next_cursor=_encode_cursor(offset + limit) if more else None
        )

    async def search(self, q: str, *, limit: int = 50) -> list[R]:
        not_yet("ProjectionView.search", "§2.3, §6.4", "H4 (WS-A)")


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        offset = int(json.loads(base64.urlsafe_b64decode(cursor.encode()))["o"])
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise ValueError("invalid pagination cursor") from exc
    if offset < 0:
        raise ValueError("invalid pagination cursor")
    return offset


def View(
    name: str,
    row: type[R],
    *,
    on_stale: Literal["serve", "source"] = "serve",
    wait_ms: int | None = None,
) -> params.Depends:
    """The dependency resolving a ``ProjectionView`` of projection ``name``::

    tasks: Annotated[ProjectionView[TaskRow], View("board.tasks", TaskRow)]
    """

    async def projection_view(
        session: DbSession, request: Request, response: Response
    ) -> ProjectionView[R]:
        projection = _PROJECTIONS.get(name)
        if projection is None:
            raise LookupError(f"no projection {name!r} is registered")
        return ProjectionView(
            projection,
            row,
            session,
            on_stale=on_stale,
            wait_ms=wait_ms,
            token=read_token(request.headers.get(CONSISTENCY_HEADER)),
            response=response,
        )

    return params.Depends(projection_view)


def projection_of(value: Any) -> Projection | None:
    return value if isinstance(value, Projection) else None


__all__ = [
    "BoundContractMethod",
    "Mapper",
    "Projection",
    "ProjectionRebuilderFromContract",
    "ProjectionView",
    "SourceGroup",
    "View",
    "delta",
    "full_state",
    "projection_of",
    "register_view_source",
    "tombstone",
]
