"""FastAPI dependency adapters that route through CoreFacade.

Modules should import dependencies from here instead of core internals. The
request-scoped session dependencies (``get_db`` / ``Database`` / ``DbSession``)
live here; the UoW write-path engine they drive — ``db_session``,
``publish_event``, ``record_audit``, ``durable_inbox`` and the context-scoped
session bookkeeping — lives in :mod:`sandloom.core.unit_of_work` and is re-exported
here so modules keep importing it from ``sandloom.core.di`` (as is
``publish_event_versions``, the v1 → v2 dual-publish helper of
:mod:`sandloom.core.unit_of_work.versioning`).
"""

from __future__ import annotations

import inspect
import math
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from functools import cache as _dependency_cache
from typing import Annotated, Any, Literal, TypeVar, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import sandloom.core.auth.bearer as _bearer
import sandloom.core.auth.hashing as _hashing
import sandloom.core.auth.revocation as _revocation
import sandloom.core.auth.tokens as _tokens
import sandloom.core.auth.verification as _verification
import sandloom.core.consumers.callbacks as _callbacks
import sandloom.core.idempotency as _idempotency
import sandloom.core.ingress.rate_limit as _rate_limit
import sandloom.core.planner as _planner
import sandloom.core.route_policy as _route_policy
import sandloom.core.sql_alchemy.uow as _uow2
import sandloom.core.sql_alchemy.write_admission as _write_admission

# The UoW write-path engine. ``di`` drives the request-scoped session itself, so
# it imports the private scope/dispatch helpers it needs under a module alias —
# private UoW state is NOT re-exported from this public module (tests and
# tooling that need it import ``sandloom.core.unit_of_work`` directly).
from sandloom.core import unit_of_work as _uow
from sandloom.core.async_runner import run_async
from sandloom.core.config.settings import settings as _settings
from sandloom.core.consumers.callbacks import EventEnvelope
from sandloom.core.effects import RECORD_TTL_SECONDS
from sandloom.core.facade import ClaimsUnavailable, CoreFacade, CoreRuntime
from sandloom.core.guard_order import authenticates, authorizes
from sandloom.core.http.tenant_scope import TenantScope
from sandloom.core.idempotency import durable as _idempotency_durable
from sandloom.core.infra.access import (
    cache,
    capability,
    notifier,
    objectstore,
    taskqueue,
)
from sandloom.core.infra.contracts import Notification
from sandloom.core.module_api import bind_contract_session, contract_session
from sandloom.core.module_api._service_registry import (
    ServiceNotFound,
    ServiceUnavailableInDeployment,
)
from sandloom.core.rpc import RemoteServiceUnavailable
from sandloom.core.runtime import get_active_core
from sandloom.core.sql_alchemy.durable_inbox import durable_inbox
from sandloom.core.tasks.enqueue import (
    TaskEnqueueConflict,
    TaskEnqueueUnavailable,
    TaskEnqueueUncertain,
)
from sandloom.core.tenant import (
    MissingTenantError,
    TenantMismatchError,
    UnsafeTenantOperationError,
    bind_tenant,
    current_tenant_id,
    current_tenant_id_optional,
    scoped,
    tenant_filter,
    unscoped,
)
from sandloom.core.tenant_context import configured_tenant_scope
from sandloom.core.unit_of_work import db_session
from sandloom.core.unit_of_work import hooks as _uow_hooks
from sandloom.core.unit_of_work import request as _uow_request
from sandloom.core.unit_of_work import scope as _uow_scope
from sandloom.core.unit_of_work.effects import (
    AuditUnavailableError,
    UnitOfWorkRequiredError,
    publish_event,
    record_audit,
)
from sandloom.core.unit_of_work.hooks import before_commit, on_commit
from sandloom.core.unit_of_work.versioning import publish_event_versions

_T = TypeVar("_T")


def _get_facade(request: Request) -> CoreFacade:
    """The full CoreFacade on app.state (framework-internal wiring only)."""
    core: CoreFacade | None = getattr(request.app.state, "core", None)
    if core is None:  # pragma: no cover - startup misconfiguration
        raise RuntimeError("CoreFacade not found on app.state.core")
    return core


async def get_core(request: Request) -> CoreRuntime:
    """Return the read-only :class:`CoreRuntime` view of the app's facade.

    Module code gets contract/capability resolution, settings, logging,
    tracing and counters — never the event bus, the infra registry, the raw
    app or the ``register_*`` mutators (those belong to ``register(core)``).

    ``async`` on purpose: it does no I/O, and FastAPI runs a SYNC dependency in
    the anyio thread pool on every request (a thread hop for an attribute
    read). Use it as ``Depends(get_core)``; a direct call is
    ``core = await get_core(request)``.
    """
    return _get_facade(request).runtime_view()


def get_service(name: str, *, optional: bool = False) -> Any:
    """The sync service contract ``name`` from the ACTIVE core (the one
    ``create_app`` bound, or a ``use_active_core`` scope): local provider
    first, then the RPC binding, exactly as ``core.get_service``.

    ``optional=True`` returns ``None`` only when the provider is not deployed
    in this process and no binding names it (``ServiceUnavailableInDeployment``);
    a deployed provider that is down still raises (spec §5.3, G-DECL-04).
    """
    core = get_active_core()
    if core is None:
        raise RuntimeError(
            f"get_service({name!r}): no active core; call it inside a running "
            "application (create_app binds one) or a use_active_core() scope"
        )
    if not optional:
        return core.get_service(name)
    try:
        return core.get_service(name)
    except ServiceUnavailableInDeployment:
        return None


async def _yield_from_provider(
    dep_callable: Callable[[], Any],
) -> AsyncGenerator[AsyncSession, None]:
    """Yield a session from a provider/override of any supported shape.

    Properly manages underlying async generator lifecycles so FastAPI's
    AsyncExitStack can inject exceptions and close resources cleanly.
    """
    agen = None
    session: AsyncSession | None = None
    try:
        res = dep_callable()
        if inspect.isawaitable(res):
            session = await res
        elif inspect.isasyncgen(res):
            agen = res
            session = cast("AsyncSession | None", await agen.__anext__())
        else:
            session = res

        if session is None:  # pragma: no cover - defensive
            raise RuntimeError("DB dependency did not return a session")

        yield session
    finally:
        # Ensure proper cleanup of underlying generator or session
        if agen is not None:
            try:
                await agen.aclose()
            except Exception:  # noqa: BLE001, S110 - best-effort session teardown
                pass
        elif session is not None:
            close = getattr(session, "close", None)
            if callable(close):
                try:
                    maybe_coro = close()
                    if inspect.isawaitable(maybe_coro):
                        await maybe_coro
                except Exception:  # noqa: BLE001, S110 - best-effort session teardown
                    pass


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Request-scoped DB session governed by a UnitOfWork (the canonical path).

    The whole request runs in one transaction: commit when the request
    succeeds, rollback on any exception (including HTTPException). Services
    must use `flush()` — never `commit()` — so the UoW alone owns the
    transaction boundary. Events published via `publish_event` are written to
    the transactional outbox in the same transaction and dispatched to the bus
    only after a successful commit.

    Test overrides registered for the raw provider
    (`sandloom.core._internal.db_provider.get_db`) bypass the UoW entirely: the test
    owns the session and its transaction.
    """
    core = _get_facade(request)
    provider = core.get_db
    overrides = getattr(request.app, "dependency_overrides", {})

    if provider is not None and provider in overrides:
        # Test override path — the override manages transactions itself. The
        # test owns the transaction, so contract calls made during this
        # request run on the same session (bind_contract_session) instead of
        # opening their own Unit of Work against a database the test never
        # wrote to.
        with configured_tenant_scope(getattr(core, "settings", None)):
            async for session in _yield_from_provider(overrides[provider]):
                _uow_request.join_request_scope(session)
                _uow_request.stash_test_pending_check(core, session)
                with bind_contract_session(session):
                    yield session
                # Reached only when the request succeeded (an exception is
                # thrown in at the yield): the test treats it as committed, so
                # post-commit effects queued via on_commit run now — exactly
                # as after a real UoW commit, and never after a failure.
                # The request's before-commit checks run here too (a failing
                # one propagates, and on_commit never runs).
                # The commit pipeline without its outbox stage (the test's
                # outbox rows never commit): COMMIT_PIPELINE in unit_of_work.
                await _uow_hooks._run_before_commit_callbacks(session)
                await _uow._dispatch_outbox_after_commit(session, dispatch=False)
        return

    async with _uow.open_uow(core, factory=core.get_uow_factory()) as session:
        yield session


def _read_only_uow_factory(core: CoreFacade) -> Callable[[], Any]:
    """UoW factory on the default engine with the ``sandloom_read_only`` option.

    Built once per provider (cached on it): an option-engine shares the pool
    and dialect; the read executor sees the option on every statement.
    """
    provider = core.database("default")
    factory = getattr(provider, "_sandloom_read_only_uow_factory", None)
    if factory is None:
        from sandloom.core.sql_alchemy.read_only_policy import (  # noqa: PLC0415 - cold: loaded with the first read-only UoW factory
            READ_ONLY_OPTION,
            install_read_executor,
        )

        engine = provider.engine
        install_read_executor(engine)
        maker = async_sessionmaker(
            bind=engine.execution_options(
                **{READ_ONLY_OPTION: True, "postgresql_readonly": True}
            ),
            class_=AsyncSession,
            sync_session_class=_write_admission.KernelSession,
            expire_on_commit=False,
        )

        def factory() -> Any:
            return _uow2.SQLAlchemyUnitOfWork(session_factory=maker)

        provider._sandloom_read_only_uow_factory = factory
    return cast(Callable[[], Any], factory)


async def get_read_only_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """PROTOTYPE: request session for a route DECLARED read-only.

    With ``DB_READ_ONLY_SESSIONS=true`` statements run in a server-enforced
    READ ONLY transaction. The client policy also refuses writes, flushes,
    locking reads and raw statements before sending them. PostgreSQL remains
    the backstop for SELECT functions with side effects. Off (default):
    exactly ``get_db``.
    """
    core = _get_facade(request)
    overrides = getattr(request.app, "dependency_overrides", {})
    if not _settings.db_read_only_sessions or core.get_db in overrides:
        agen = get_db(request)
        session = await agen.__anext__()
        try:
            yield session
        except BaseException as exc:
            await agen.athrow(exc)
            raise
        else:
            async for _ in agen:  # pragma: no cover - get_db yields once
                pass
        return
    async with _uow.open_uow(core, factory=_read_only_uow_factory(core)) as session:
        yield session


async def get_planned_reads(request: Request) -> AsyncGenerator[Any, None]:
    """The handle of a route DECLARED read-only (execution planner).

    The whole handler runs inside a read-only scope: ``publish_event`` /
    ``record_audit`` / ``on_commit`` raise. Declared operations run through
    ``reads.one(op, **key)`` on the planner's general or optimized executor.
    """
    reads_cls = _planner_reads()
    core = _get_facade(request)
    route = request.scope.get("route")
    if route is not None and not route.__dict__.get("_sandloom_read_only_route"):
        _check_read_only_route(route)
    owner = f"PlannedReads({getattr(route, 'path', None) or request.scope['path']})"
    # the read_only_scope() contextmanager, inlined (no generator frame)
    var = _uow_scope._read_only_scope
    provider = core.get_db
    overrides = getattr(request.app, "dependency_overrides", {})
    if provider is not None and provider in overrides:
        # Test override path (the harness's ``client``): the test owns an
        # uncommitted transaction, so the declared reads run ON its session
        # (general executor) — still inside the read-only scope, exactly as
        # contract calls run on it (bind_contract_session).
        token = var.set(var.get() or owner)
        try:
            with configured_tenant_scope(getattr(core, "settings", None)):
                async for session in _yield_from_provider(overrides[provider]):
                    with bind_contract_session(session):
                        yield reads_cls(core.runtime_view(), session)
        finally:
            var.reset(token)
        return
    token = var.set(var.get() or owner)
    # shared=True: the route's general-executor reads share ONE read-only
    # transaction, ended here (after the handler, before the response)
    reads = reads_cls(core.runtime_view(), shared=True)
    try:
        # TENANCY_MODE=single binds the canonical tenant exactly as the
        # request UoW does (multi-tenant mode: nothing to bind, no wrapper)
        cfg = getattr(core, "settings", None)
        if getattr(cfg, "tenancy_mode", "multi") != "single":
            yield reads
        else:
            with configured_tenant_scope(cfg):
                yield reads
    except BaseException:
        # the handler failed (often a plain 404 after a successful read): end
        # the unit, but never mask the handler's exception with a close error
        with suppress(Exception):
            await reads.aclose()
        raise
    else:
        await reads.aclose()
    finally:
        var.reset(token)


async def get_snapshot_reads(request: Request) -> AsyncGenerator[Any, None]:
    """``PlannedReads`` with ONE snapshot: ``REPEATABLE READ READ ONLY``.

    Every declared read of the route runs in the route's read unit, in one
    ``REPEATABLE READ`` read-only transaction (the default ``PlannedReads`` is
    READ COMMITTED: two reads may see different snapshots). Use it when the
    route's reads must be mutually consistent.
    """
    reads_cls = _planner_reads()
    core = _get_facade(request)
    route = request.scope.get("route")
    if route is not None and not route.__dict__.get("_sandloom_read_only_route"):
        _check_read_only_route(route)
    owner = f"SnapshotReads({getattr(route, 'path', None) or request.scope['path']})"
    var = _uow_scope._read_only_scope
    token = var.set(var.get() or owner)
    reads = reads_cls(core.runtime_view(), shared=True, isolation="repeatable_read")
    try:
        cfg = getattr(core, "settings", None)
        if getattr(cfg, "tenancy_mode", "multi") != "single":
            yield reads
        else:
            with configured_tenant_scope(cfg):
                yield reads
    except BaseException:
        with suppress(Exception):
            await reads.aclose()
        raise
    else:
        await reads.aclose()
    finally:
        var.reset(token)


def core_from_request(request: Request) -> CoreRuntime:
    """SYNC accessor of the request's :class:`CoreRuntime` (direct calls).

    For code that holds a ``Request`` and cannot ``await``: the same value as
    ``await get_core(request)``. As a dependency use ``Depends(get_core)``
    (async: no thread-pool hop), never ``Depends(core_from_request)``.
    """
    return _get_facade(request).runtime_view()


_READS_CLS: Any = None


def _check_read_only_route(route: Any) -> None:
    """A PlannedReads route must not ALSO open a Unit of Work (checked once).

    ``PlannedReads`` declares the route read-only; a ``DbSession`` /
    ``Database(...)`` / ``UowReads`` in the same route would open a writable
    UoW inside that declaration. Mixed routes use ``UowReads`` (planned reads
    inside the UoW) instead. Fails closed (500) on the first request.
    """

    writable = {get_db, get_read_only_db, get_uow_reads}
    calls = {d.call for d in _flat_dependencies(route)}
    if get_planned_reads in calls and get_snapshot_reads in calls:
        raise _planner.PlanningError(
            f"route {getattr(route, 'path', route)!r} declares PlannedReads AND "
            "SnapshotReads: two read scopes of different isolation"
        )
    stack = list(getattr(getattr(route, "dependant", None), "dependencies", ()))
    while stack:
        dep = stack.pop()
        call = dep.call
        name = getattr(call, "__qualname__", "")
        if call in writable or call in _NAMED_DB_DEPENDENCIES:
            raise _planner.PlanningError(
                f"route {getattr(route, 'path', route)!r} declares PlannedReads "
                f"(read-only) AND a Unit of Work ({name}); use UowReads for "
                "planned reads inside a read-write route"
            )
        stack.extend(dep.dependencies)
    route.__dict__["_sandloom_read_only_route"] = True


def _flat_dependencies(route: Any) -> list[Any]:
    out: list[Any] = []
    stack = list(getattr(getattr(route, "dependant", None), "dependencies", ()))
    while stack:
        dep = stack.pop()
        out.append(dep)
        stack.extend(dep.dependencies)
    return out


def _planner_reads() -> Any:
    """``sandloom.core.planner.Reads``, imported once (only by planned routes)."""
    global _READS_CLS
    if _READS_CLS is None:
        _READS_CLS = _planner.Reads
    return _READS_CLS


_NAMED_DB_DEPENDENCIES: set[Any] = set()
for _dep in (get_read_only_db, get_planned_reads, get_snapshot_reads):
    _route_policy.register_request_session(_dep)
_route_policy.register_request_session(get_db, writable_database="default")


@_dependency_cache
def make_db_dependency(
    name: str,
) -> Callable[[Request], AsyncGenerator[AsyncSession, None]]:
    """Build a request dependency yielding a session for a NAMED database.

    Use this when a module's endpoints must read/write a non-default database
    (e.g. ``"analytics"``). The module references only the logical name; the
    concrete engine/driver is resolved by the infrastructure registry. Events
    published while this session is the active UoW go to that database's
    transactional outbox.

    ``DbSession`` remains the canonical dependency for the default database.
    """

    async def _named_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
        async with _uow.open_uow(_get_facade(request), name) as session:
            yield session

    _NAMED_DB_DEPENDENCIES.add(_named_db)
    _route_policy.register_request_session(_named_db, writable_database=name)
    return _named_db


def Database(name: str) -> Any:  # dependency factory, used as a type
    """Annotated dependency for a named database session.

    Usage in an endpoint signature::
        async def handler(db: Annotated[AsyncSession, Database("analytics")]):
            ...
    """
    return Depends(make_db_dependency(name), scope=_UOW_DEPENDENCY_SCOPE)


def Capability(  # dependency factory, reads like a type
    capability: str,
    contract: type | None = None,
    name: str = "default",
    *,
    features: set[str] | None = None,
) -> Any:
    """Annotated dependency resolving any capability instance.

    The universal alternative to a bespoke ``Database()``/``GraphStore()`` per
    capability — resolves through the request app's core, enforcing the contract
    and any required features::
        async def handler(
            graph: Annotated[
                GraphStoreProvider,
                Capability("graph_store", GraphStoreProvider, "recommendations"),
            ],
        ):
            ...
    """
    feats = set(features) if features else None

    async def _dep(request: Request) -> Any:  # async: no thread-pool hop
        return (
            _get_facade(request)
            .runtime_view()
            .require_capability(capability, name, features=feats, contract=contract)
        )

    return Depends(_dep)


def rate_limiter(*, requests: int, period: int, fail_closed: bool = False) -> Any:
    """Expose rate limiter dependency routed through sandloom.core.ingress.rate_limit.

    Import lazily so test patches against
    `sandloom.core.ingress.rate_limit.rate_limiter`
    are respected. Pass ``fail_closed=True`` on admission-control endpoints
    (login, password reset, payment) so a cache outage returns 503 rather than
    silently lifting the limit.
    """

    return _rate_limit.rate_limiter(
        requests=requests, period=period, fail_closed=fail_closed
    )


def decode_token(token: str) -> dict[str, Any] | None:
    """Decode JWT using sandloom.core.auth.tokens (stable adapter).

    Import at call-time so tests patching `sandloom.core.auth.tokens.decode_token`
    take effect.
    """

    return _tokens.decode_token(token)


def decode_token_cached(request: Any, token: str) -> dict[str, Any] | None:
    """Per-request-cached JWT decode via sandloom.core.auth (stable adapter).

    Import at call-time so tests patching `sandloom.core.auth.tokens.decode_token`
    take effect (the cache wrapper calls it through the module global).
    """

    return _bearer.decode_token_cached(request, token)


def create_access_token(
    *, data: dict[str, Any], expires_delta: int | None = None
) -> str:
    """Create JWT access token via sandloom.core.auth (stable adapter).

    Import at call-time so test patches against security are honored.
    """

    return _tokens.create_access_token(data=data, expires_delta=expires_delta)


async def run_password_hashing(fn: Callable[..., _T], *args: Any) -> _T:
    """Run an argon2 hash/verify ``fn(*args)`` on the BOUNDED password pool.

    See :func:`sandloom.core.auth.hashing.run_password_hashing`
    (``PASSWORD_HASH_CONCURRENCY``): never the loop's default executor.
    """

    return await _hashing.run_password_hashing(fn, *args)


def password_hasher() -> Any:
    """A fresh argon2id ``PasswordHasher`` with the CONFIGURED parameters
    (``PASSWORD_HASH_PROFILE`` / ``_TIME_COST`` / ``_MEMORY_KIB`` /
    ``_PARALLELISM``; fixed 16-byte salt, 32-byte digest).

    Every component that hashes passwords builds its hasher here, so
    registration, password change/reset and CLIs share one work factor.
    """

    return _hashing.build_password_hasher()


async def rehash_password_if_needed(
    plain_password: str, stored_hash: str, *, hasher: Any = None
) -> str | None:
    """New hash when ``stored_hash`` was made with other argon2 parameters
    than the configured ones (stronger or weaker), else ``None``.

    Call only after a SUCCESSFUL verify and store the result in the same Unit
    of Work. Never raises (a failure is logged + counted in
    ``sandloom_password_rehash_total{outcome="failed"}``) — see
    :func:`sandloom.core.auth.hashing.rehash_password_if_needed`.
    """

    return await _hashing.rehash_password_if_needed(
        plain_password, stored_hash, hasher=hasher
    )


def idempotency_guard(
    *,
    scope: str,
    ttl_seconds: int = RECORD_TTL_SECONDS,
    required: bool = True,
    fail_closed: bool = False,
    store_response: bool = True,
    durable: bool = False,
) -> Any:
    """Public re-export of the Idempotency-Key dependency factory.

    See :mod:`sandloom.core.idempotency`. Use as an endpoint dependency to make a
    mutating operation safe against duplicate client retries. Pass
    ``fail_closed=True`` for money / irreversible side effects so a cache
    outage returns 503 instead of allowing a possible duplicate.
    ``store_response=False``: in-flight dedupe only — nothing is stored or
    replayed (the endpoint owns durable idempotency; its response must not be
    replayed without re-authorization).
    ``durable=True``: the record is claimed in the request's ``DbSession``
    Unit of Work and committed with the endpoint's effect (exactly-once
    effect on that database, replay of the stored response; ADR-0005) —
    the form a money route that stores responses needs (SANDLOOM-B019).
    """

    return _idempotency.idempotency_guard(
        scope=scope,
        ttl_seconds=ttl_seconds,
        required=required,
        fail_closed=fail_closed,
        store_response=store_response,
        durable=durable,
    )


def register_event_consumer(
    core: CoreFacade,
    channel: str,
    handler: Any,
    *,
    name: str,
    durable: bool = True,
    database: str = "default",
    required: bool = True,
    group: str | None = None,
    start_from: str = "earliest",
    bind_tenant_from: str | None = None,
    dead_letter: bool = True,
) -> None:
    """Register a durable event consumer directly on a facade (non-draft path).

    Modules normally call ``ModuleAPI.register_event_consumer`` inside
    ``register(core)``; this helper is for scripts/tests wiring a consumer onto a
    bare facade. Same keywords and meaning as the ``ModuleAPI`` method (one
    signature, pinned by a test). The handler is
    ``async (session, envelope) -> None``.
    """

    core.register_event_consumer(
        _callbacks.EventConsumerDescriptor(
            name=name,
            channel=channel,
            handler=handler,
            durable=durable,
            database=database,
            required=required,
            group=group,
            start_from=start_from,
            bind_tenant_from=bind_tenant_from,
            dead_letter=dead_letter,
        )
    )


# Annotated dependency type: lets API layers declare a DB session parameter
# without importing sqlalchemy (keeps endpoint files persistence-free).
#: FastAPI scope of every Unit-of-Work session dependency. ``"function"``: the
#: UoW commits (and its on-commit hooks, e.g. token revocation, run) BEFORE the
#: response is sent. FastAPI >= 0.118 otherwise exits a yield dependency only
#: AFTER the response went out, so a client could act on a 200 before the
#: write was committed (read-your-writes broken across requests). Every
#: ``Depends(get_db ...)`` must use it: FastAPI caches dependencies per scope,
#: so mixing scopes would open two UoWs in one request.
_UOW_DEPENDENCY_SCOPE: Literal["function"] = "function"

DbSession = Annotated[AsyncSession, Depends(get_db, scope=_UOW_DEPENDENCY_SCOPE)]
# ``idempotency_guard(durable=True)`` claims in the endpoint's own DbSession
# UoW: hand it this exact dependency (injected: the kernel never imports di).
_idempotency_durable.bind_request_session(get_db, scope=_UOW_DEPENDENCY_SCOPE)
#: PROTOTYPE: a DECLARED read-only request session (see ``get_read_only_db``).
ReadOnlyDbSession = Annotated[
    AsyncSession, Depends(get_read_only_db, scope=_UOW_DEPENDENCY_SCOPE)
]

#: A route DECLARED read-only that runs declared operations through
#: the execution planner (see ``sandloom.core.planner``).
PlannedReads = Annotated[Any, Depends(get_planned_reads, scope=_UOW_DEPENDENCY_SCOPE)]


async def get_uow_reads(request: Request, db: DbSession) -> Any:
    """Planned reads INSIDE the request Unit of Work (mixed routes).

    Depends on ``DbSession`` (the SAME session the route gets: FastAPI caches
    the dependency per request), so declared operations run on the UoW's
    connection and transaction with the general executor: one connection,
    one tenant GUC, read-your-own-writes, and the UoW's outbox / audit /
    on_commit semantics unchanged (no read-only scope is opened).
    """
    return _planner_reads()(_get_facade(request).runtime_view(), db)


_route_policy.register_request_session(get_uow_reads)


#: ``PlannedReads`` with one ``REPEATABLE READ`` snapshot for all its reads.
SnapshotReads = Annotated[Any, Depends(get_snapshot_reads, scope=_UOW_DEPENDENCY_SCOPE)]

#: Declared operations inside the route's read-write Unit of Work.
UowReads = Annotated[Any, Depends(get_uow_reads, scope=_UOW_DEPENDENCY_SCOPE)]

# Expose settings via DI for modules that need read-only config
settings = _settings

__all__ = [
    "TaskEnqueueConflict",
    "TaskEnqueueUnavailable",
    "TaskEnqueueUncertain",
    "run_async",
    "DbSession",
    "ReadOnlyDbSession",
    "PlannedReads",
    "SnapshotReads",
    "UowReads",
    "core_from_request",
    "get_snapshot_reads",
    "get_planned_reads",
    "get_uow_reads",
    "get_read_only_db",
    "Database",
    "Capability",
    "make_db_dependency",
    "get_core",
    "get_db",
    "rate_limiter",
    "decode_token",
    "decode_token_cached",
    "authenticates",
    "authorizes",
    "create_access_token",
    "run_password_hashing",
    "password_hasher",
    "rehash_password_if_needed",
    "publish_event",
    "publish_event_versions",
    "on_commit",
    "before_commit",
    "at_commit",
    "db",
    "external_call",
    "mark_reauthenticated",
    "SERVICE_PRINCIPAL_ID",
    "has_internal_access",
    "request_operation_key",
    "sensitive_effect",
    "release_check",
    "ResolvedAccount",
    "subject_commit_check",
    "cache",
    "capability",
    "get_service",
    "notifier",
    "objectstore",
    "taskqueue",
    "Notification",
    "idempotency_guard",
    "record_audit",
    "AuditUnavailableError",
    "UnitOfWorkRequiredError",
    "durable_inbox",
    "register_event_consumer",
    "EventEnvelope",
    "db_session",
    "settings",
    "TenantScope",
    "current_tenant_id",
    "current_tenant_id_optional",
    "scoped",
    "tenant_filter",
    "bind_tenant",
    "unscoped",
    "MissingTenantError",
    "TenantMismatchError",
    "UnsafeTenantOperationError",
    "CoreRuntime",
    "ServiceNotFound",
    "ServiceUnavailableInDeployment",
    "RemoteServiceUnavailable",
    "ClaimsUnavailable",
    "contract_session",
    "bind_contract_session",
    "revoke_user_tokens",
    "revoke_user_tokens_on_commit",
    "apply_pending_revocation",
    "ensure_token_not_revoked",
    "public",
    "no_idempotency",
    "replay_safe",
    "sensitive",
    "operation_intent",
    "writes",
    "allow_multi_db_write",
    "block_user",
    "unblock_user",
    "is_user_blocked",
    "ensure_user_not_blocked",
    "BlockUnavailableError",
    "get_client_ip",
    "PoisonEventError",
    # Sagas (ADR-0008): start / read an instance, derive operation keys
    "start_saga",
    "saga_status",
    "SagaHandle",
    "SagaInputConflict",
    "operation_key",
]

# --- auth: token revocation + client IP (public module surface) -------------
# The emergency user block (sandloom.core.auth.blocks): a value of the subject-state
# record every authenticated request reads live (ADR-0009).
from sandloom.core.auth.blocks import (  # noqa: E402
    BlockUnavailableError as BlockUnavailableError,
)
from sandloom.core.auth.blocks import (  # noqa: E402
    block_user as block_user,
)
from sandloom.core.auth.blocks import (  # noqa: E402
    ensure_user_not_blocked as ensure_user_not_blocked,
)
from sandloom.core.auth.blocks import (  # noqa: E402
    is_user_blocked as is_user_blocked,
)
from sandloom.core.auth.blocks import (  # noqa: E402
    unblock_user as unblock_user,
)

# Sagas (ADR-0008): start_saga runs in the caller's Unit of Work (instance row
# + first nudge through the outbox); operation_key is the key-derivation rule.
from sandloom.core.descriptors import operation_key as operation_key  # noqa: E402
from sandloom.core.eventing.delivery import (  # noqa: E402
    PoisonEventError as PoisonEventError,
)

# Units (spec §4.3): db() is the session of the running unit; at_commit()
# re-checks the session before every unit's COMMIT.
from sandloom.core.http.route_runtime import at_commit as at_commit  # noqa: E402
from sandloom.core.ingress.client_ip import get_client_ip as get_client_ip  # noqa: E402

# The service principal of internal calls (spec §2.3); sandloom.shared.guards
# builds current_service on them.
from sandloom.core.internal_auth import (  # noqa: E402
    SERVICE_PRINCIPAL_ID as SERVICE_PRINCIPAL_ID,
)
from sandloom.core.internal_auth import (  # noqa: E402
    has_internal_access as has_internal_access,
)

# Model E route declarations (ADR-0009; spec §4.2, §5.1): no-op markers read
# off the route by create_app and the route snapshot.
from sandloom.core.route_policy import (  # noqa: E402
    allow_multi_db_write as allow_multi_db_write,
)
from sandloom.core.route_policy import no_idempotency as no_idempotency  # noqa: E402
from sandloom.core.route_policy import (  # noqa: E402
    operation_intent as operation_intent,
)
from sandloom.core.route_policy import public as public  # noqa: E402
from sandloom.core.route_policy import replay_safe as replay_safe  # noqa: E402
from sandloom.core.route_policy import sensitive as sensitive  # noqa: E402
from sandloom.core.route_policy import writes as writes  # noqa: E402
from sandloom.core.sagas import SagaHandle as SagaHandle  # noqa: E402
from sandloom.core.sagas import SagaInputConflict as SagaInputConflict  # noqa: E402
from sandloom.core.sagas import saga_status as saga_status  # noqa: E402
from sandloom.core.sagas import start_saga as start_saga  # noqa: E402
from sandloom.core.unit_of_work.intent import (  # noqa: E402
    ResolvedAccount as ResolvedAccount,
)
from sandloom.core.unit_of_work.intent import external_call as external_call  # noqa: E402
from sandloom.core.unit_of_work.intent import release_check as release_check  # noqa: E402
from sandloom.core.unit_of_work.request import db as db  # noqa: E402
from sandloom.core.unit_of_work.risk import (  # noqa: E402
    mark_reauthenticated as mark_reauthenticated,
)
from sandloom.core.unit_of_work.risk import (  # noqa: E402
    request_operation_key as request_operation_key,
)
from sandloom.core.unit_of_work.risk import (  # noqa: E402
    sensitive_effect as sensitive_effect,
)


async def revoke_user_tokens(user_id: Any, *, valid_after: int | None = None) -> bool:
    """Invalidate every access token issued to ``user_id`` up to now.

    Framework-wide: ``current_context`` / ``require(...)`` /
    ``get_current_user_id`` / ``TenantScope`` reject an older token with 401.
    See :func:`sandloom.core.auth.revocation.revoke_user_tokens`.
    """

    return await _revocation.revoke_user_tokens(user_id, valid_after=valid_after)


async def revoke_user_tokens_on_commit(
    user_id: Any,
    *,
    reason: str,
    valid_after: int | None = None,
    database: str | None = None,
    require_immediate: bool = False,
) -> int:
    """Revoke ``user_id``'s access tokens AFTER the current UoW commits.

    The single framework path for "this DB change invalidates the user's
    sessions" (identity and tenancy both use it): skipped on rollback, retried
    after commit, persisted durably in the UoW's outbox
    (``core.auth.tokens_revoked.v1``) for the session owner's applier; returns
    the fixed ``valid_after`` mark in whole seconds, rounded UP (the precise
    mark has microsecond precision: a token issued at or before it is revoked).
    ``require_immediate=True`` also writes the mark BEFORE commit and refuses
    the change (``RevocationUnavailableError``, 503) when it cannot — for
    changes whose revocation must hold the moment they commit.
    See :func:`sandloom.core.auth.revocation.revoke_user_tokens_on_commit`.
    """

    mark = await _revocation.revoke_user_tokens_on_commit(
        user_id,
        reason=reason,
        valid_after=valid_after,
        database=database,
        require_immediate=require_immediate,
    )
    return math.ceil(mark)


def subject_commit_check(request: Request) -> Callable[[], Awaitable[None]]:
    """A ``before_commit`` check: is the request's bearer STILL valid now?

    Returns a no-argument coroutine function that re-reads the caller's
    revocation and block marks LIVE (no request memo, no staleness budget) and
    raises ``401`` (revoked / blocked) or ``503`` (fail-closed cache outage).
    A request without a bearer gets a no-op (its guards decide).

    Guarantee (ADR-0003 "Guarantees, precisely"): every change that
    withdraws authority — a tenancy role change / removal, an identity role
    revocation, ``block_user``, a password change — writes the mark BEFORE it
    commits (``revoke_user_tokens_on_commit(require_immediate=True)`` / the
    block mark) and reads nothing our action writes. A mark seen rolls our
    transaction back; no mark seen means the history is SERIALIZABLE with our
    action first. It is NOT strictly serializable / linearizable: a withdrawal
    that writes its mark and commits between this read and our COMMIT is not
    seen — the window is narrowed to read → COMMIT, not closed (a fence making
    the withdrawal wait for in-flight actions would close it; not
    implemented). On a revocation-store outage ``AUTH_REVOCATION_FAIL_CLOSED``
    decides: closed (the prod default) → 503; open (the default outside prod)
    → the check ACCEPTS, like the request-start check. Register it with
    ``await before_commit(subject_commit_check(request))`` inside a UoW you
    open yourself; request routes are re-checked automatically (writes, a
    GET whose unit wrote) or with ``at_commit()``.
    """

    claims = _bearer.bearer_claims(request)

    async def _check() -> None:
        if claims is None:
            return

        await _verification.assert_token_valid_now(claims)

    return _check


async def apply_pending_revocation(payload: dict[str, Any]) -> None:
    """Apply a ``core.auth.tokens_revoked.v1`` payload (durable applier body).

    Idempotent; raises when the mark cannot be written so the delivery is
    retried. See :func:`sandloom.core.auth.revocation.apply_pending_revocation`.
    """

    await _revocation.apply_pending_revocation(payload)


async def ensure_token_not_revoked(request: Any, claims: dict[str, Any]) -> None:
    """Raise 401 when ``claims`` predate a revocation (503 on fail-closed outage)."""

    await _verification.ensure_token_not_revoked(request, claims)
