"""Contract-driven execution planner (see docs/reference/execution-planner.md).

A module DECLARES an operation with known semantics instead of writing the
query by hand::

    get_item = ReadOperation(entity=Item, result=ItemOut, by=("id",),
                             name="catalog.item.get")

and runs it from a route that declared itself read-only::

    @router.get("/items/{item_id}", response_model=ItemOut)
    async def read(item_id: int, reads: PlannedReads, tenant: Tenant) -> ItemOut:
        item = await reads.one(get_item, id=item_id)
        ...

The kernel prepares a :class:`Plan` per (operation, database) once and picks an
executor for it:

* the GENERAL executor — the ORM on a Session — runs ANY declared operation;
* the OPTIMIZED executor — driver level, one generated statement — runs only
  operations the database adapter can prove simple (equality key, every DTO
  field a plain column of one table), and only with
  ``PLANNER_OPTIMIZED_EXECUTOR=true`` (the default). Unsupported semantics
  use the general executor; ``false`` is the deployment rollback switch.

Both executors sit under ONE guarantees layer, implemented here and in the
adapter's connection lease, never per executor:

* the tenant is read from the context AT EXECUTION (never cached in a plan); a
  tenant-scoped operation with no tenant bound fails closed before any I/O;
* a tenant-scoped plan ALWAYS carries the tenant predicate, and the adapter
  sets the RLS GUC for the operation's transaction;
* the route runs inside :func:`sandloom.core.unit_of_work.scope.read_only_scope`:
  ``publish_event`` / ``record_audit`` / ``on_commit`` raise instead of
  bypassing the outbox or skipping the audit row; the database transaction is
  ``READ ONLY`` so a write cannot happen even through a bug in the executor;
* the result is always the DECLARED DTO, built by the same mapper for both
  executors (an ORM entity is never handed out — modules that need the
  identity map or lazy relationships keep using ``DbSession``).

This module is paradigm-agnostic: the relational specifics (ORM inspection,
SQL generation, connection lease) live in the database provider's
:class:`ExecutionAdapter` (``sandloom.core.sql_alchemy.planner_adapter``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import uuid
import weakref
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Generic, Literal, NamedTuple, Protocol, TypeVar

from sandloom.core.config.settings import settings
from sandloom.core.tenant_context import (
    MissingTenantError,
    _current_tenant,
    current_tenant_id,
)
from sandloom.core.unit_of_work.scope import ReadOnlyScopeError, read_only_scope

if TYPE_CHECKING:  # pragma: no cover
    from sandloom.core.facade import CoreRuntime

R = TypeVar("R")

ExecutorKind = Literal["general", "optimized"]
#: Isolation of a read-only scope (part of the UoW / read-unit contract):
#:
#: * ``"read_committed"`` (default) — PostgreSQL's default. Every statement
#:   sees the data committed before IT started: two reads of one scope (even
#:   in the same read unit) MAY see different snapshots;
#: * ``"repeatable_read"`` — ONE snapshot for the whole scope: every read of
#:   the scope runs in the scope's read unit (``REPEATABLE READ READ ONLY``),
#:   whichever executor its plan names.
#:
#: Anything else is refused (``PlanningError``) — never silently downgraded.
Isolation = Literal["read_committed", "repeatable_read"]
ISOLATION_LEVELS: dict[str, str] = {
    "read_committed": "READ COMMITTED",
    "repeatable_read": "REPEATABLE READ",
}


class PlanningError(RuntimeError):
    """A declared operation is inconsistent with its entity / database."""


#: Every operation declared in this process (weak: test-local ones go away).
_DECLARED: weakref.WeakSet[Any] = weakref.WeakSet()


def declared_operations() -> list[ReadOperation[Any]]:
    """Every live declared operation (equivalence tests, diagnostics)."""
    return sorted(_DECLARED, key=lambda op: op.name)


@dataclass(frozen=True, eq=False)
class ReadOperation(Generic[R]):
    """A declared single-row read: ``entity`` WHERE ``by`` columns = arguments.

    ``result`` is part of the contract: the caller always gets an instance of
    it (or ``None``), whichever executor ran. ``tenant_scoped`` is inferred from
    the entity; declaring it explicitly is checked against the entity (an
    operation can never opt OUT of tenant scoping of a tenant-owned entity).
    Identity-hashed: one object = one declared operation.
    """

    entity: type
    result: type[R]
    by: tuple[str, ...] = ("id",)
    tenant_scoped: bool | None = None
    database: str = "default"
    name: str = ""
    #: Locking reads (``FOR UPDATE`` / ``FOR SHARE``, ``NOWAIT``,
    #: ``SKIP LOCKED``) are NEVER planned: they belong to a read-write Unit of
    #: Work (``DbSession`` + ``select(...).with_for_update(...)``). Any value
    #: other than ``None`` is refused at declaration (``PlanningError``).
    lock: str | None = None
    #: Every keyword argument of a call, in order (``by`` for a plain read).
    keys: tuple[str, ...] = field(init=False, repr=False, default=())
    #: ``keys`` as a set (the per-call argument check is one comparison).
    keyset: frozenset[str] = field(init=False, repr=False, default=frozenset())

    def __post_init__(self) -> None:
        if not self.by and not isinstance(self, ListOperation | JoinRead):
            raise PlanningError("ReadOperation.by must name at least one column")
        if self.lock is not None:
            raise PlanningError(
                f"locking read ({self.lock!r}) cannot be a declared operation: "
                "a lock needs a read-write Unit of Work — keep it on DbSession "
                "with select(...).with_for_update(...)"
            )
        _DECLARED.add(self)
        object.__setattr__(self, "by", tuple(self.by))
        object.__setattr__(self, "keys", self.by)
        object.__setattr__(self, "keyset", frozenset(self.by))
        if not self.name:
            object.__setattr__(
                self, "name", f"{self.entity.__name__}.by_{'_'.join(self.by)}"
            )


@dataclass(frozen=True, eq=False)
class JoinRead(ReadOperation[R]):
    """A declared single-row read of ``entity`` joined to ``join`` on a FK.

    ``entity`` JOIN ``join`` ON ``entity.<via>`` = ``join``'s primary key, with

    * ``by`` — equality key columns of ``entity`` (call arguments); they must
      cover ``entity``'s primary key or a unique constraint, so the read
      returns at most one row on EVERY executor. ``via`` counts towards that
      key when the join side is pinned to at most one row (``join_by`` +
      ``join_where`` cover a unique key of ``join``): a read keyed only on the
      joined row (``by=()``) is allowed when ``via`` is itself unique on
      ``entity`` (e.g. a one-to-one detail row of a customer found by its
      unique tenant id);
    * ``join_by`` — ``{argument: column of join}``: more equality arguments;
    * ``join_where`` — ``{column of join: constant}``: fixed equality filters
      (e.g. ``{"is_active": True}``), sent as parameters;
    * ``joined_fields`` — ``{DTO field: attribute of join}``; every other DTO
      field is read from ``entity``.

    ``via`` must be a column of ``entity`` with a declared ``ForeignKey`` to
    ``join`` (no guessed join conditions). Tenant scoping is inferred PER
    SIDE: every ``TenantScoped`` table of the join gets the tenant predicate
    (and RLS). Both entities must belong to the declaring module (the import
    boundary rules already forbid reaching another module's models).
    """

    join: type | None = None
    via: str = ""
    join_by: Mapping[str, str] = field(default_factory=dict)
    join_where: Mapping[str, Any] = field(default_factory=dict)
    joined_fields: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.join is None or not self.via:
            raise PlanningError("JoinRead needs join=<entity> and via=<FK attribute>")
        if not self.name:
            object.__setattr__(
                self,
                "name",
                f"{self.entity.__name__}+{self.join.__name__}.by_{'_'.join(self.by)}",
            )
        super().__post_init__()
        for attr in ("join_by", "join_where", "joined_fields"):
            object.__setattr__(self, attr, MappingProxyType(dict(getattr(self, attr))))
        clash = set(self.by) & set(self.join_by)
        if clash:
            raise PlanningError(f"{self.name}: argument names used twice: {clash}")
        object.__setattr__(self, "keys", self.by + tuple(self.join_by))
        object.__setattr__(self, "keyset", frozenset(self.keys))
        if not self.keys:
            raise PlanningError(
                f"{self.name}: JoinRead needs at least one key argument "
                "(by= or join_by=)"
            )


#: Optional + prefix filters of one ListOperation (2**n statements at most).
MAX_OPTIONAL_FILTERS = 4


def like_prefix(value: str) -> str:
    """``value`` as a LITERAL ``LIKE`` prefix pattern (backslash escapes)."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


@dataclass(frozen=True, eq=False)
class ListOperation(ReadOperation[R]):
    """A declared list read: ``entity`` rows, one keyset page per call.

    * ``by`` — equality filter columns of ``entity`` (call arguments; may be
      empty for a tenant-scoped "all rows of my tenant" list);
    * ``where`` — ``{column of entity: constant}`` fixed filters;
    * ``order_by`` — columns of ``entity``, each part of an index of its table;
      the primary key is appended as the tie-breaker (a total order, so keyset
      pages never skip or repeat a row); ``descending`` flips the whole order;
    * ``max_limit`` — the declaration's page cap (never above
      ``PLANNER_LIST_MAX_LIMIT``); every call passes ``limit`` (mandatory);
    * optional ``join`` on a DECLARED foreign key, in either direction:
      ``via`` on ``entity`` -> ``join``'s primary key (many-to-one), or ``via``
      on ``join`` -> ``entity``'s primary key (one-to-many: ``via`` +
      ``join_by`` + ``join_where`` must cover a unique key of ``join``, so the
      join never multiplies rows); ``join_by`` / ``join_where`` /
      ``joined_fields`` as for :class:`JoinRead`;
    * ``optional`` — ``{argument: column of entity}`` equality filters applied
      only when the call's argument is not ``None`` (``None`` = no filter,
      never ``IS NULL``);
    * ``prefix`` — ``{argument: String column of entity}``: ``column LIKE
      <argument> || '%'`` with the argument matched as a LITERAL prefix (its
      ``%`` / ``_`` / ``\\`` are escaped), applied only when the argument is
      not ``None``; ``prefix_ignore_case=True`` matches case-insensitively
      (``ILIKE``).

    Optional and prefix filters are residual predicates: the covering-index
    rule is about ``by`` + ``where`` (+ tenant) and the order only — a page
    walks that index in order and skips non-matching rows, it never sorts.
    Each combination of present filters is its own statement (at most
    :data:`MAX_OPTIONAL_FILTERS` optional + prefix filters per operation).

    Pages are addressed by an opaque, tamper-evident cursor (HMAC-SHA256 over
    the operation, the filter arguments, the bound tenant and the position):
    the client cannot move it to another filter, tenant or position. The
    tenant is never read from it. Tenant scoping is inferred per side.
    """

    by: tuple[str, ...] = ()
    where: Mapping[str, Any] = field(default_factory=dict)
    order_by: tuple[str, ...] = ("id",)
    descending: bool = False
    max_limit: int | None = None
    #: Planning REFUSES a list without a btree index whose leading columns are
    #: the equality filter arguments (any order) followed by the declared
    #: order columns (a page must be an index range scan, never a sort of the
    #: filtered rows). ``False`` downgrades the refusal to a logged warning
    #: (``Plan.reason`` says so) — for small, bounded tables only.
    require_index: bool = True
    join: type | None = None
    via: str = ""
    join_by: Mapping[str, str] = field(default_factory=dict)
    join_where: Mapping[str, Any] = field(default_factory=dict)
    joined_fields: Mapping[str, str] = field(default_factory=dict)
    optional: Mapping[str, str] = field(default_factory=dict)
    prefix: Mapping[str, str] = field(default_factory=dict)
    prefix_ignore_case: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(
                self,
                "name",
                f"{self.entity.__name__}.list_by_{'_'.join(self.by) or 'tenant'}",
            )
        super().__post_init__()
        if not self.order_by:
            raise PlanningError(f"{self.name}: order_by must name a column")
        object.__setattr__(self, "order_by", tuple(self.order_by))
        if (self.join is None) != (not self.via):
            raise PlanningError(f"{self.name}: join= and via= go together")
        if self.max_limit is not None and self.max_limit < 1:
            raise PlanningError(f"{self.name}: max_limit must be >= 1")
        for attr in (
            "where",
            "join_by",
            "join_where",
            "joined_fields",
            "optional",
            "prefix",
        ):
            object.__setattr__(self, attr, MappingProxyType(dict(getattr(self, attr))))
        keys = (*self.by, *self.join_by, *self.optional, *self.prefix)
        if len(set(keys)) != len(keys):
            clash = sorted({k for k in keys if keys.count(k) > 1})
            raise PlanningError(f"{self.name}: argument names used twice: {clash}")
        if len(self.optional) + len(self.prefix) > MAX_OPTIONAL_FILTERS:
            raise PlanningError(
                f"{self.name}: at most {MAX_OPTIONAL_FILTERS} optional / prefix "
                "filters (each combination is its own statement)"
            )
        object.__setattr__(self, "keys", keys)
        object.__setattr__(self, "keyset", frozenset(self.keys))

    @property
    def n_required(self) -> int:
        """The number of leading ``keys`` that are always applied."""
        return len(self.by) + len(self.join_by)

    def page_cap(self) -> int:
        """The effective page cap: the declaration's, never above the framework's."""
        cap = int(settings.planner_list_max_limit)
        return cap if self.max_limit is None else min(cap, self.max_limit)


@dataclass(frozen=True)
class Page(Generic[R]):
    """One page of a :class:`ListOperation`: the DTOs and the next cursor."""

    items: list[R]
    #: ``None`` on the last page.
    next_cursor: str | None


class InvalidCursor(ValueError):
    """A pagination cursor that is malformed, tampered with or foreign (400)."""

    http_status = 400

    def __init__(self, detail: str = "invalid pagination cursor") -> None:
        super().__init__(detail)
        self.detail = detail


class PageLimitError(ValueError):
    """``limit`` missing, < 1 or above the operation's page cap (400)."""

    http_status = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


_CURSOR_LABEL = b"sandloom.planner.cursor.v1"


def _cursor_key() -> bytes:
    """The cursor MAC key, derived from the deployment-wide shared secret.

    ``INTERNAL_API_TOKEN`` when set, else ``JWT_SECRET`` (the same in every
    instance of a service, so any instance verifies any cursor). A verify-only
    service of an asymmetric JWT deployment holds no ``JWT_SECRET``: it needs
    ``INTERNAL_API_TOKEN`` (or ``PLANNER_CURSOR_SECRET``) for cursors.
    """
    secret = (
        settings.planner_cursor_secret
        or settings.internal_api_token
        or settings.jwt_secret
    )
    if not secret:
        raise RuntimeError(
            "keyset cursors need a secret: set PLANNER_CURSOR_SECRET or "
            "INTERNAL_API_TOKEN (JWT_SECRET is absent on a verify-only service)"
        )
    return hmac.new(secret.encode(), _CURSOR_LABEL, hashlib.sha256).digest()


def _tag(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, uuid.UUID):
        return {"u": str(value)}
    if isinstance(value, datetime):
        return {"t": value.isoformat()}
    if isinstance(value, date):
        return {"d": value.isoformat()}
    if isinstance(value, Decimal):
        return {"n": str(value)}
    if isinstance(value, float):
        return {"f": repr(value)}
    raise PlanningError(f"cannot put a {type(value).__name__} into a cursor")


def _untag(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    ((kind, raw),) = value.items()
    if kind == "u":
        return uuid.UUID(raw)
    if kind == "t":
        return datetime.fromisoformat(raw)
    if kind == "d":
        return date.fromisoformat(raw)
    if kind == "n":
        return Decimal(raw)
    if kind == "f":
        return float(raw)
    raise ValueError(kind)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _cursor_mac(
    op: ListOperation[Any], args: Sequence[Any], tenant: Any, payload: bytes
) -> bytes:
    bound = json.dumps(
        [op.name, [_tag(a) for a in args], str(tenant) if tenant else "-"],
        separators=(",", ":"),
    ).encode()
    return hmac.new(_cursor_key(), bound + b"\0" + payload, hashlib.sha256).digest()


def encode_cursor(
    op: ListOperation[Any], args: Sequence[Any], tenant: Any, position: Sequence[Any]
) -> str:
    """The opaque cursor of ``position`` (the order values of the last row)."""
    payload = json.dumps([_tag(v) for v in position], separators=(",", ":")).encode()
    mac = _cursor_mac(op, args, tenant, payload)[:16]
    return f"{_b64(payload)}.{_b64(mac)}"


def decode_cursor(
    op: ListOperation[Any], args: Sequence[Any], tenant: Any, cursor: str, width: int
) -> tuple[Any, ...]:
    """Verify ``cursor`` for THIS operation, arguments and tenant; its position.

    Any mismatch — malformed, tampered, minted for other filters / another
    tenant / another operation — is :class:`InvalidCursor`; nothing about the
    reason is revealed to the client.
    """
    try:
        head, _, tail = cursor.partition(".")
        payload, mac = _unb64(head), _unb64(tail)
    except (ValueError, TypeError):
        raise InvalidCursor() from None
    if len(mac) != 16 or not hmac.compare_digest(
        mac, _cursor_mac(op, args, tenant, payload)[:16]
    ):
        raise InvalidCursor()
    try:
        values = tuple(_untag(v) for v in json.loads(payload))
    except (ValueError, TypeError, KeyError):
        raise InvalidCursor() from None
    if len(values) != width:
        raise InvalidCursor()
    return values


def make_cursor(op: ListOperation[Any], position: Sequence[Any], /, **key: Any) -> str:
    """A cursor starting after ``position`` (the operation's order values, in
    ``order_by`` + primary-key order) for these filter arguments and the bound
    tenant (e.g. to resume a list at a known row)."""
    if key.keys() != op.keyset:
        raise TypeError(f"{op.name}: expected key arguments {sorted(op.keys)}")
    return encode_cursor(
        op, [key[k] for k in op.keys], _current_tenant.get(), tuple(position)
    )


@dataclass(frozen=True)
class Plan(Generic[R]):
    """The prepared, immutable plan of one operation on one database.

    Holds NO per-execution value (tenant, key): those are bound by the
    guarantees layer on every call.
    """

    operation: ReadOperation[R]
    executor: ExecutorKind
    reason: str
    tenant_scoped: bool
    fields: tuple[str, ...]
    #: Adapter-private compiled form (SQL text, ORM statement, ...).
    compiled: Any = field(repr=False, default=None)
    _validate: Any = field(init=False, repr=False, compare=False, default=None)

    #: ListOperation: the number of order values appended to each row after
    #: the DTO's ``fields`` — only the order columns the DTO does not already
    #: project (a projected one is read once, from its DTO position).
    order_width: int = 0
    #: ListOperation: the row position of every order value (``order_by`` +
    #: primary key, in order) — the cursor position is these values of the
    #: page's last row. Default: the ``order_width`` trailing values.
    order_index: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.order_width and not self.order_index:
            n = len(self.fields)
            object.__setattr__(
                self, "order_index", tuple(range(n, n + self.order_width))
            )

    def to_result(self, values: Sequence[Any] | None) -> R | None:
        """THE mapper both executors share: values in ``fields`` order -> DTO."""
        if values is None:
            return None
        if self.order_width:
            values = values[: len(values) - self.order_width]
        # the model's own validator (= ``model_validate`` minus its Python
        # wrapper): types are still validated, both executors share it
        validate = self._validate
        if validate is None:
            model: Any = self.operation.result
            validator = getattr(model, "__pydantic_validator__", None)
            validate = getattr(validator, "validate_python", model.model_validate)
            object.__setattr__(self, "_validate", validate)
        return validate(dict(zip(self.fields, values, strict=True)))  # type: ignore[no-any-return]


class ExecutionContext(NamedTuple):
    """Per-execution guarantees, built fresh for EVERY call (never cached).

    Immutable (a tuple: cheaper to build per call than a frozen dataclass).
    """

    database: str
    tenant: uuid.UUID | None
    tenant_scoped: bool
    read_only: bool = True


def execution_context(plan: Plan[Any]) -> ExecutionContext:
    """Bind the guarantees of one execution (fail closed on a missing tenant)."""
    tenant = _current_tenant.get()
    if tenant is None and plan.tenant_scoped:
        current_tenant_id()  # raises MissingTenantError: no tenant -> no query
    return ExecutionContext(plan.operation.database, tenant, plan.tenant_scoped)


class ExecutionAdapter(Protocol):
    """What a database driver offers the planner (one per database instance)."""

    #: Prepared plans, keyed by (id(operation), optimized mode).
    plans: dict[tuple[int, bool], Plan[Any]]

    def compile(self, op: ReadOperation[Any], *, optimized: bool) -> Plan[Any]:
        """Validate ``op`` against the schema and return its plan."""

    async def fetch_one(
        self, plan: Plan[Any], ctx: ExecutionContext, key: Sequence[Any]
    ) -> Sequence[Any] | None:
        """Run ``plan`` under ``ctx`` in its own read-only transaction."""

    async def fetch_in_unit(
        self, plan: Plan[Any], ctx: ExecutionContext, key: Sequence[Any], unit: Any
    ) -> Sequence[Any] | None:
        """Run ``plan``'s GENERAL form on ``unit`` (an open Unit of Work)."""

    def read_unit(self, isolation: str = "READ COMMITTED") -> ReadUnit:
        """A scope-wide read-only unit (one lease + transaction, opened lazily)."""

    async def fetch_page(
        self,
        plan: Plan[Any],
        ctx: ExecutionContext,
        key: Sequence[Any],
        limit: int,
        after: Sequence[Any] | None,
        unit: Any = None,
    ) -> list[Sequence[Any]]:
        """A ListOperation page (``limit`` rows after ``after``): own read-only
        transaction, or the GENERAL form on ``unit`` when given."""


class ReadUnit(Protocol):
    """ONE read-only transaction shared by the general-executor reads of a scope.

    Opened lazily by the first read that needs it, closed by the scope
    (:data:`sandloom.core.di.PlannedReads` teardown, :func:`read_scope` exit).
    """

    async def fetch(
        self, plan: Plan[Any], ctx: ExecutionContext, key: Sequence[Any]
    ) -> Sequence[Any] | None:
        """Run ``plan``'s GENERAL form in the unit's read-only transaction."""

    async def fetch_page(
        self,
        plan: Plan[Any],
        ctx: ExecutionContext,
        key: Sequence[Any],
        limit: int,
        after: Sequence[Any] | None,
    ) -> list[Sequence[Any]]:
        """A ListOperation page in the unit's read-only transaction."""

    async def close(self) -> None:
        """End the unit (ROLLBACK of the read-only transaction, lease returned)."""


def _adapter(core: CoreRuntime, database: str) -> ExecutionAdapter:
    provider = core.database(database)
    get = getattr(provider, "execution_adapter", None)
    adapter = get() if callable(get) else None
    if adapter is None:
        raise PlanningError(
            f"database {database!r} (driver {getattr(provider, 'driver', '?')!r}) "
            "offers no execution adapter for declared operations"
        )
    return adapter  # type: ignore[no-any-return]


def _contract_adapter(core: CoreRuntime, database: str) -> ExecutionAdapter:
    """The adapter on the database's dedicated CONTRACT pool (if it has one)."""
    provider = core.database(database)
    get = getattr(provider, "contract_execution_adapter", None)
    adapter = get() if callable(get) else None
    return adapter if adapter is not None else _adapter(core, database)


def _plan(adapter: ExecutionAdapter, op: ReadOperation[R]) -> Plan[R]:
    optimized = bool(settings.planner_optimized_executor)
    cache = adapter.plans
    plan = cache.get((id(op), optimized))
    if plan is None or plan.operation is not op:
        plan = adapter.compile(op, optimized=optimized)
        cache[(id(op), optimized)] = plan
    guard = getattr(adapter, "guard_plan", None)
    if guard is not None:
        guarded = guard(plan)
        if guarded is not plan:
            cache[(id(op), optimized)] = plan = guarded
    return plan


def plan_for(core: CoreRuntime, op: ReadOperation[R]) -> Plan[R]:
    """The plan of ``op`` on its database (prepared once per adapter + mode)."""
    return _plan(_adapter(core, op.database), op)


def prepare(
    core: CoreRuntime, operations: Sequence[ReadOperation[Any]]
) -> list[Plan[Any]]:
    """Plan every operation now (a startup task): schema errors fail the boot."""
    return [plan_for(core, op) for op in operations]


_log = logging.getLogger("sandloom.core.planner")
#: Attribute of the CoreFacade holding the registered operations.
DECLARED_OPERATIONS_ATTR = "_sandloom_declared_operations"


def describe(plan: Plan[Any]) -> dict[str, Any]:
    """A plan as diagnostics data (never a per-execution value)."""
    op = plan.operation
    return {
        "name": op.name,
        "kind": type(op).__name__,
        "entity": getattr(op.entity, "__name__", repr(op.entity)),
        "database": op.database,
        "tenant_scoped": plan.tenant_scoped,
        "executor": plan.executor,
        "reason": plan.reason,
    }


def register_prepared(
    facade: Any, module: str, operations: Sequence[ReadOperation[Any]]
) -> None:
    """Plan a module's declared operations at startup, log and record them.

    Called by ``ModuleAPI.register_operations``' startup task. Every operation
    is planned now (``PlanningError`` fails the boot); the executor the kernel
    picked — and, when it is the general executor, WHY — is logged and kept
    for ``/diagnostics/core``.
    """
    plans = prepare(facade.runtime_view(), operations)
    registry = facade.__dict__.setdefault(DECLARED_OPERATIONS_ATTR, {})
    for plan in plans:
        registry[plan.operation.name] = (module, plan.operation)
        _log.info(
            "declared operation %s (%s): %s executor — %s",
            plan.operation.name,
            module,
            plan.executor,
            plan.reason,
        )


def diagnostics(facade: Any) -> list[dict[str, Any]]:
    """Every registered operation with its CURRENT plan (executor + reason)."""
    registry = facade.__dict__.get(DECLARED_OPERATIONS_ATTR) or {}
    core = facade.runtime_view()
    out = []
    for name in sorted(registry):
        module, op = registry[name]
        try:
            entry = describe(plan_for(core, op))
        except Exception as exc:  # noqa: BLE001 - diagnostics never fail
            entry = {"name": name, "error": f"{type(exc).__name__}: {exc}"}
        entry["module"] = module
        out.append(entry)
    return out


async def execute_one(
    core: CoreRuntime, op: ReadOperation[R], key: Mapping[str, Any]
) -> R | None:
    """Run a declared read under the shared guarantees (no scope management)."""
    return await Reads(core).one(op, **key)


class Reads:
    """Handle that runs declared operations.

    Two modes, fixed when the handle is made:

    * **read-only scope** (``unit is None``; :data:`sandloom.core.di.PlannedReads`,
      :func:`read_scope`): consecutive OPTIMIZED reads share a lazy read-only
      transaction per database. A GENERAL read releases that transaction and
      opens an ORM read unit; subsequent reads use that unit. Stronger isolation
      always uses one ORM transaction. A scope never holds two connections of
      one pool. A handle made without a scope
      (``shared=False``, :func:`execute_one`) runs every operation in its own
      transaction;
    * **inside a Unit of Work** (``unit`` = the UoW's session;
      :data:`sandloom.core.di.UowReads`, ``sandloom.core.orm.session_reads``): every
      operation runs ON that session — the UoW's connection, transaction and
      tenant GUC — with the GENERAL executor (ORM: autoflush + identity map, so
      the read sees the unit's own writes, flushed or not). The optimized
      executor is never used there (see the design doc, "mixed operations").
    """

    __slots__ = (
        "_core",
        "_unit",
        "_fixed",
        "_shared",
        "_units",
        "_last",
        "_lookup",
        "_isolation",
    )

    def __init__(
        self,
        core: CoreRuntime | None,
        unit: Any = None,
        *,
        adapter: ExecutionAdapter | None = None,
        shared: bool = False,
        contract: bool = False,
        isolation: Isolation = "read_committed",
    ) -> None:
        if core is None and adapter is None:
            raise PlanningError("Reads needs a core or an execution adapter")
        if isolation not in ISOLATION_LEVELS:
            raise PlanningError(
                f"unsupported read isolation {isolation!r}; one of "
                f"{sorted(ISOLATION_LEVELS)}"
            )
        if isolation != "read_committed" and (unit is not None or not shared):
            raise PlanningError(
                "repeatable_read needs a read-only SCOPE (SnapshotReads / "
                "read_scope(..., isolation=...)); inside a Unit of Work the "
                "isolation is the unit's"
            )
        self._isolation = isolation
        self._core = core
        self._unit = unit
        self._fixed = adapter
        self._shared = shared and unit is None
        #: open read units of this scope: [(adapter, unit)], usually one
        self._units: list[tuple[ExecutionAdapter, ReadUnit]] | None = None
        #: (database, adapter) of the previous call: one registry lookup per handle
        self._last: tuple[str, ExecutionAdapter] | None = None
        #: contract=True: the database's CONTRACT pool (a contract call never
        #: waits for a connection its calling request holds)
        self._lookup = _contract_adapter if contract else _adapter

    @property
    def in_unit_of_work(self) -> bool:
        return self._unit is not None

    async def one(self, op: ReadOperation[R], /, **key: Any) -> R | None:
        """Run ``op`` for ``key`` (the hot path: one adapter lookup, no wrappers)."""
        if isinstance(op, ListOperation):
            raise TypeError(f"{op.name} is a ListOperation: use reads.page(...)")
        if key.keys() != op.keyset:
            raise TypeError(
                f"{op.name}: expected key arguments {sorted(op.keys)}, "
                f"got {sorted(key)}"
            )
        by = op.keys
        adapter = self._fixed
        if adapter is None:
            last = self._last
            if last is not None and last[0] == op.database:
                adapter = last[1]
            else:
                assert self._core is not None  # noqa: S101 - type narrowing of an invariant
                adapter = self._lookup(self._core, op.database)
                self._last = (op.database, adapter)
        plan = _plan(adapter, op)
        ctx = execution_context(plan)  # before ANY I/O: no tenant -> no query
        args = [key[name] for name in by]
        unit = self._unit
        if unit is not None:
            values = await adapter.fetch_in_unit(plan, ctx, args, unit)
        elif self._shared:
            ru = self._read_unit(
                adapter,
                open_new=True,
            )
            if ru is None:
                values = await adapter.fetch_one(plan, ctx, args)
            else:
                values = await ru.fetch(plan, ctx, args)
        else:
            values = await adapter.fetch_one(plan, ctx, args)
        return plan.to_result(values)

    async def page(
        self,
        op: ListOperation[R],
        /,
        *,
        limit: int,
        cursor: str | None = None,
        **key: Any,
    ) -> Page[R]:
        """One keyset page of ``op`` for the filter arguments ``key``.

        ``limit`` is mandatory and capped (:meth:`ListOperation.page_cap`,
        :class:`PageLimitError`); ``cursor`` is the previous page's
        ``next_cursor`` and is verified BEFORE any I/O (:class:`InvalidCursor`).
        """
        if not isinstance(op, ListOperation):
            raise TypeError(f"{op.name} is not a ListOperation: use reads.one(...)")
        if key.keys() != op.keyset:
            raise TypeError(
                f"{op.name}: expected key arguments {sorted(op.keys)}, "
                f"got {sorted(key)}"
            )
        cap = op.page_cap()
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= cap
        ):
            raise PageLimitError(f"limit must be an integer between 1 and {cap}")
        adapter = self._fixed
        if adapter is None:
            last = self._last
            if last is not None and last[0] == op.database:
                adapter = last[1]
            else:
                assert self._core is not None  # noqa: S101 - type narrowing of an invariant
                adapter = self._lookup(self._core, op.database)
                self._last = (op.database, adapter)
        plan = _plan(adapter, op)
        ctx = execution_context(plan)  # before ANY I/O: no tenant -> no query
        args = [key[name] for name in op.keys]
        after = (
            None
            if cursor is None
            else decode_cursor(op, args, ctx.tenant, cursor, len(plan.order_index))
        )
        unit = self._unit
        if unit is not None:
            rows = await adapter.fetch_page(plan, ctx, args, limit + 1, after, unit)
        elif self._shared:
            ru = self._read_unit(
                adapter,
                open_new=True,
            )
            if ru is None:
                rows = await adapter.fetch_page(plan, ctx, args, limit + 1, after)
            else:
                rows = await ru.fetch_page(plan, ctx, args, limit + 1, after)
        else:
            rows = await adapter.fetch_page(plan, ctx, args, limit + 1, after)
        to_result = plan.to_result
        items: list[R] = [to_result(r) for r in rows[:limit]]  # type: ignore[misc]
        next_cursor = None
        if len(rows) > limit:
            last_row = rows[limit - 1]
            next_cursor = encode_cursor(
                op, args, ctx.tenant, tuple([last_row[i] for i in plan.order_index])
            )
        return Page(items, next_cursor)

    def _read_unit(
        self, adapter: ExecutionAdapter, *, open_new: bool
    ) -> ReadUnit | None:
        units = self._units
        if units is not None:
            for a, ru in units:
                if a is adapter:
                    return ru
        if not open_new:
            return None
        ru = adapter.read_unit(ISOLATION_LEVELS[self._isolation])
        if units is None:
            self._units = [(adapter, ru)]
        else:
            units.append((adapter, ru))
        return ru

    async def aclose(self) -> None:
        """End the scope's read units (the scope owner calls it, once)."""
        units, self._units = self._units, None
        if not units:
            return
        error: BaseException | None = None
        for _, ru in units:
            try:
                await ru.close()
            except BaseException as exc:  # noqa: BLE001 - close every unit, then re-raise
                error = error or exc
        if error is not None:
            raise error


@asynccontextmanager
async def read_scope(
    core: CoreRuntime, owner: str, *, isolation: Isolation = "read_committed"
) -> AsyncIterator[Reads]:
    """Open a read-only scope (workers, scripts) and yield its handle.

    ``isolation="repeatable_read"``: one snapshot for every read of the scope.
    """
    with read_only_scope(owner):
        reads = Reads(core, shared=True, isolation=isolation)
        try:
            yield reads
        finally:
            await reads.aclose()


__all__ = [
    "MissingTenantError",
    "ExecutionAdapter",
    "ExecutionContext",
    "ISOLATION_LEVELS",
    "InvalidCursor",
    "Isolation",
    "JoinRead",
    "ListOperation",
    "MAX_OPTIONAL_FILTERS",
    "Page",
    "PageLimitError",
    "Plan",
    "PlanningError",
    "ReadOnlyScopeError",
    "ReadOperation",
    "ReadUnit",
    "Reads",
    "declared_operations",
    "describe",
    "diagnostics",
    "decode_cursor",
    "encode_cursor",
    "execute_one",
    "make_cursor",
    "execution_context",
    "like_prefix",
    "plan_for",
    "prepare",
    "read_scope",
    "register_prepared",
]
