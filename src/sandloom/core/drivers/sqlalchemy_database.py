"""Built-in SQLAlchemy database driver.

Produces a :class:`~sandloom.core.infra.contracts.DatabaseProvider` backed by an
async SQLAlchemy engine. Supports any number of named instances, each with its
own engine/pool — this is what makes "different databases simultaneously"
work: ``database/default`` and ``database/analytics`` are independent engines
selected purely by configuration.

This driver is the SOLE constructor of SQLAlchemy engines, including the
process-wide ``default`` one (:func:`get_default_engine` /
:func:`get_default_sessionmaker`, built lazily so importing never opens a pool).
The raw request dependency ``sandloom.core._internal.db_provider.get_db`` wraps the
sessionmaker built here. The ``default`` instance
reuses that single engine/pool whenever its URL matches ``DATABASE_URL``; any
other instance (or a default whose URL was overridden via ``INFRA``) gets a
freshly built engine.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import event, exc, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.util.concurrency import await_

import sandloom.core.observability.metrics as _metrics
import sandloom.core.runtime as _runtime
import sandloom.core.sql_alchemy.driver_compat as _driver_compat
import sandloom.core.sql_alchemy.planner_adapter as _planner_adapter
from sandloom.core.config.settings import settings
from sandloom.core.contracts.uow import UnitOfWorkContract
from sandloom.core.descriptors import HealthResult
from sandloom.core.drivers.linger_pool import LingeringQueuePool
from sandloom.core.infra.config import DEFAULT_INSTANCE, InstanceConfig
from sandloom.core.infra.contracts import (
    AuditStore,
    CommandLogStore,
    DatabaseProvider,
    OutboxStore,
    RequestLogStore,
)
from sandloom.core.sql_alchemy.command_log import sql_alchemy_command_log_store
from sandloom.core.sql_alchemy.dead_connection import install_dead_connection_handling
from sandloom.core.sql_alchemy.outbox_store import (
    sql_alchemy_audit_store,
    sql_alchemy_outbox_store,
)
from sandloom.core.sql_alchemy.reconnect import install_reconnect_backoff
from sandloom.core.sql_alchemy.request_log import sql_alchemy_request_log_store
from sandloom.core.sql_alchemy.tenant_rls import (
    TenantRLSConfigError,
    install_tenant_rls,
    tenant_rls_coverage,
    verify_tenant_rls_roles,
)
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.sql_alchemy.write_admission import (
    KernelSession,
    install_write_admission,
)

_logger = logging.getLogger("sandloom.tenant_rls")

#: Features every SQLAlchemy database instance provides unconditionally.
SQLALCHEMY_DB_BASE_FEATURES = frozenset(
    {
        "transactions",
        "savepoints",
        "transactional_outbox",
        "audit",
        "command_idempotency",
        "http_idempotency",
        "write_admission",
    }
)

#: Per-instance opt-in features: advertised by a provider only when the
#: instance's option of the same name is on.
SQLALCHEMY_DB_OPTIONAL_FEATURES = frozenset({"tenant_rls"})

#: Driver-level declaration (what the driver CAN provide) used for the
#: build-free registry registration. A built provider's ``features()`` is the
#: instance-accurate subset — see :func:`sqlalchemy_db_features`.
SQLALCHEMY_DB_FEATURES = SQLALCHEMY_DB_BASE_FEATURES | SQLALCHEMY_DB_OPTIONAL_FEATURES


class SQLAlchemyDatabaseOptions(BaseModel):
    """Config-shape schema for the ``sqlalchemy`` database driver.

    Validates an instance's ``options`` (``INFRA["database"][<name>]``) so a
    misshapen / typo'd key fails fast at startup instead of silently falling
    back to ``DATABASE_URL``. ``url`` is optional (the driver falls back to the
    flat ``DATABASE_URL`` when omitted); ``extra="forbid"`` turns an unknown key
    (e.g. ``ur1`` for ``url``) into a :class:`ConfigSchemaError`.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str | None = None
    #: Opt-in PostgreSQL Row-Level Security defense in depth: keep the
    #: transaction-local ``sandloom.tenant_id`` setting in sync with the tenant
    #: context (see ``sandloom.core.sql_alchemy.tenant_rls``).
    tenant_rls: bool = False
    #: DSN of the SYSTEM channel (a separate least-privilege login role, e.g.
    #: ``sandloom_system``) used only by framework-internal cross-tenant work
    #: (``sandloom.core.drivers.system_db``). The ``default`` instance falls
    #: back to ``DATABASE_SYSTEM_URL``. Requires ``tenant_rls``.
    system_url: str | None = None
    #: Fail startup when no system channel is configured (deployments whose
    #: operations rely on cross-tenant system jobs).
    require_system_channel: bool = False

    @model_validator(mode="before")
    @classmethod
    def _removed_options(cls, value: Any) -> Any:
        # ``verify_roles`` (the 0.7 escape hatch that skipped the RLS role
        # check) was removed in 0.8: the check always runs on tenant_rls.
        if isinstance(value, Mapping) and "verify_roles" in value:
            raise ValueError(
                "the instance option verify_roles was removed in 0.8: the "
                "startup check that the application role cannot escape "
                "Row-Level Security always runs on a tenant_rls instance — "
                "drop the option and fix the role "
                "(docs/guides/upgrading-0.8.md)"
            )
        return value


def sqlalchemy_db_features(options: Mapping[str, object]) -> frozenset[str]:
    """The features an instance configured with ``options`` advertises.

    ``tenant_rls`` is included only when that option is on, so a module's
    ``CapabilityRequirement("database", features=frozenset({"tenant_rls"}))``
    is satisfied only by an instance that actually installs the RLS hooks.
    """
    tenant_rls = SQLAlchemyDatabaseOptions.model_validate(
        {"tenant_rls": options.get("tenant_rls", False)}
    ).tenant_rls
    if tenant_rls:
        return SQLALCHEMY_DB_BASE_FEATURES | {"tenant_rls"}
    return SQLALCHEMY_DB_BASE_FEATURES


def _pool_snapshot(pool: Any) -> dict[str, int] | None:
    """Occupancy of a QueuePool-like pool, or ``None`` without accounting."""
    try:
        size = int(pool.size())
        checked_out = int(pool.checkedout())
        idle = int(pool.checkedin())
        overflow = max(0, int(pool.overflow()))
        max_overflow = max(0, int(getattr(pool, "_max_overflow", 0)))
    except (AttributeError, TypeError, ValueError):
        return None
    # Checkouts queued for a fair handoff behind an exhausted pool.
    waiters = pool.checkout_waiters() if isinstance(pool, LingeringQueuePool) else 0
    return {
        "size": size,
        "max": size + max_overflow,
        "checked_out": max(0, checked_out),
        "idle": max(0, idle),
        "overflow": overflow,
        "waiters": max(0, waiters),
    }


class SQLAlchemyDatabaseProvider(DatabaseProvider):
    """A single SQLAlchemy-backed database instance."""

    def __init__(
        self,
        *,
        name: str,
        driver: str,
        options: dict[str, object],
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        owns_engine: bool,
        contract_engine: Callable[[], AsyncEngine | None] | None = None,
    ) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._rls_options = _validated_rls_options(name, options)
        self._engine = engine
        self._session_factory = session_factory
        # The shared process-wide engine is disposed by its owner, not here.
        self._owns_engine = owns_engine
        self._features = sqlalchemy_db_features(options)
        # Dedicated pool for contract calls' own UoWs (built lazily on first
        # use). ``None`` from the factory = no separate pool for this backend.
        self._contract_engine_factory = contract_engine
        self._contract_engine: AsyncEngine | None = None
        self._contract_session_factory: async_sessionmaker[AsyncSession] | None = None
        self._contract_resolved = False
        self._uow_factory: Callable[[], UnitOfWorkContract] | None = None
        self._contract_factory: Callable[[], UnitOfWorkContract] | None = None

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    def features(self) -> frozenset[str]:
        return self._features

    def outbox(self) -> OutboxStore:
        return sql_alchemy_outbox_store()

    def pool_stats(self) -> list[dict[str, Any]]:
        """Point-in-time pool occupancy (``sandloom_db_pool_*`` gauges; read-only).

        ``pool`` = ``main`` / ``contract`` (only once that pool was built).
        Pools without accounting (``NullPool``, SQLite's) are left out.
        """
        stats: list[dict[str, Any]] = []
        for label, engine in (
            ("main", self._engine),
            ("contract", self._contract_engine),
        ):
            if engine is None:
                continue
            row = _pool_snapshot(engine.pool)
            if row is not None:
                stats.append({"pool": label, **row})
        return stats

    def audit(self) -> AuditStore:
        return sql_alchemy_audit_store()

    def command_log(self) -> CommandLogStore:
        return sql_alchemy_command_log_store()

    def has_pending_changes(self, session: Any) -> bool:
        """Unflushed ORM changes in ``session`` (COMMIT would flush them)."""
        sync = getattr(session, "sync_session", session)
        try:
            return not sync._is_clean()
        except AttributeError:  # not an ORM session
            return False

    def request_log(self) -> RequestLogStore:
        return sql_alchemy_request_log_store()

    def execution_adapter(self) -> Any:
        """PROTOTYPE: the planner adapter of this instance (built once)."""
        adapter = getattr(self, "_execution_adapter", None)
        if adapter is None:
            adapter = self._execution_adapter = (
                _planner_adapter.SQLAlchemyExecutionAdapter(self._engine)
            )
        return adapter

    def contract_execution_adapter(self) -> Any:
        """The planner adapter on the DEDICATED contract pool (built once).

        Declared reads run by a contract call lease from the contract pool,
        exactly like the contract's own Unit of Work would, so a contract
        called by a request that holds a request-pool connection can never
        starve on that pool. Without a separate pool: the regular adapter.
        """
        self.contract_uow_factory()  # resolves the contract engine (lazily)
        engine = self._contract_engine
        if engine is None:
            return self.execution_adapter()
        adapter = getattr(self, "_contract_execution_adapter", None)
        if adapter is None:
            adapter = self._contract_execution_adapter = (
                _planner_adapter.SQLAlchemyExecutionAdapter(engine)
            )
        return adapter

    def uow_factory(self) -> Callable[[], UnitOfWorkContract]:
        # One stable factory per provider: the post-commit coalescer batches
        # mark-delivered UPDATEs per factory, so equal databases must compare
        # equal (a fresh closure per request would defeat the batching).
        factory = self._uow_factory
        if factory is None:
            session_factory = self._session_factory

            def _factory() -> UnitOfWorkContract:
                return SQLAlchemyUnitOfWork(session_factory=session_factory)

            factory = self._uow_factory = _factory
        return factory

    def contract_uow_factory(self) -> Callable[[], UnitOfWorkContract]:
        """UoW factory on the DEDICATED contract pool (same database).

        A local contract call opens its own Unit of Work while the request
        that made it still holds a connection from the request pool; serving it
        from a separate pool (``CONTRACT_DB_POOL_SIZE``) means it can never
        starve on that pool. Connections carry ``lock_timeout``
        (``CONTRACT_DB_LOCK_TIMEOUT_MS``) so a contract waiting on a row lock
        held by its own caller fails fast instead of hanging. Backends without
        a separate pool (SQLite) use the regular factory.
        """
        if not self._contract_resolved:
            self._contract_resolved = True
            engine = (
                self._contract_engine_factory()
                if self._contract_engine_factory is not None
                else None
            )
            if engine is not None:
                self._contract_engine = engine
                self._contract_session_factory = async_sessionmaker(
                    bind=engine,
                    class_=AsyncSession,
                    sync_session_class=KernelSession,
                    expire_on_commit=False,
                )
        session_factory = self._contract_session_factory
        if session_factory is None:
            return self.uow_factory()
        factory = self._contract_factory
        if factory is None:

            def _factory() -> UnitOfWorkContract:
                return SQLAlchemyUnitOfWork(session_factory=session_factory)

            factory = self._contract_factory = _factory
        return factory

    async def aopen(self) -> None:
        """Verify the RLS role setup at startup (``tenant_rls`` instances only).

        A violation raises :class:`TenantRLSConfigError`, which aborts startup
        in every environment (a security misconfiguration, not an outage).
        """
        await self.verify_tenant_rls()

    async def verify_tenant_rls(self) -> None:
        """Check the app (and system) role cannot escape RLS; no-op without it.

        Runs on throw-away ``NullPool`` engines, so it can be called from any
        entrypoint (API lifespan, Celery worker boot, CLIs) without leaving a
        pooled connection bound to that caller's event loop.
        """
        opts = self._rls_options
        if not opts.tenant_rls:
            return
        checks = [(self._engine.url, False)]
        if system_channel_configured(self.name):
            checks.append((get_system_engine(self.name).url, True))
        for url, system in checks:
            engine = create_async_engine(url, poolclass=NullPool)
            try:
                async with engine.connect() as conn:
                    await verify_tenant_rls_roles(conn, system=system)
            finally:
                await engine.dispose()

    async def tenant_rls_coverage(
        self, tables: list[str]
    ) -> tuple[list[str], list[str]]:
        """``(present, problems)``: tenant RLS on ``tables`` in this database.

        Every listed table that exists here must have RLS enabled + forced and
        the tenant-isolation policy; an instance without ``tenant_rls``
        reports each present table (the production profile requires it).
        Runs on a throw-away ``NullPool`` engine, like the role verification.
        """
        engine = create_async_engine(self._engine.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                present, problems = await tenant_rls_coverage(conn, tables)
        finally:
            await engine.dispose()
        if not self._rls_options.tenant_rls and present:
            problems = [
                f"tenant_rls is off but it hosts TenantScoped table(s) {present}",
                *problems,
            ]
        return present, problems

    async def health(self) -> HealthResult:
        # Readiness must not queue behind saturated application requests.
        # NullPool bounds this probe to one short-lived connection, separate
        # from both request and contract pool capacity.
        connect_args: dict[str, object] = {}
        if self._engine.url.drivername == "postgresql+asyncpg":
            connect_args["timeout"] = min(
                settings.db_connect_timeout_seconds,
                settings.health_check_timeout_seconds,
            )
        engine = create_async_engine(
            self._engine.url, poolclass=NullPool, connect_args=connect_args
        )
        try:
            async with asyncio.timeout(settings.health_check_timeout_seconds):
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
            return HealthResult(status="ok")
        except Exception as exc:  # noqa: BLE001 - any failure is a failed probe
            return HealthResult(
                status="fail", details={"error": str(exc) or type(exc).__name__}
            )
        finally:
            await engine.dispose()

    async def release_pools(self) -> None:
        """Return this provider's pooled connections, keeping its engines
        (test seam: pooled asyncpg connections belong to the loop that opened
        them, see :func:`release_default_pools`)."""
        for engine in (self._engine, self._contract_engine):
            if engine is not None:
                await engine.dispose()

    async def aclose(self) -> None:
        await _dispose_system_engine(self.name)
        if self._owns_engine:
            await self._engine.dispose()
            if self._contract_engine is not None:
                await self._contract_engine.dispose()
        self._contract_engine = None
        self._contract_session_factory = None
        self._contract_resolved = False
        self._contract_factory = None


logger = logging.getLogger(__name__)

#: ``ConnectionPoolEntry.info`` key: monotonic time the connection was last
#: returned to the pool (absent = never used yet).
_LAST_USED_KEY = "sandloom_last_used"


def _is_asyncpg(url: str) -> bool:
    return url.startswith("postgresql+asyncpg")


def _server_settings(
    url: str, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """PostgreSQL GUCs sent in the startup packet (zero extra round trips)."""
    if not _is_asyncpg(url):
        return {}
    values: dict[str, str] = {}
    if settings.db_statement_timeout_ms:
        values["statement_timeout"] = str(settings.db_statement_timeout_ms)
    if settings.db_idle_in_tx_timeout_ms:
        values["idle_in_transaction_session_timeout"] = str(
            settings.db_idle_in_tx_timeout_ms
        )
    values.update(extra or {})
    return values


def _ping(dbapi_connection: Any, dialect: Any) -> None:
    """One-round-trip liveness probe of a pooled connection (no transaction).

    asyncpg: ``SELECT 1`` over the simple-query protocol (no prepare, no
    transaction) — ONE round trip, where SQLAlchemy's own asyncpg ping wraps a
    ``;`` in BEGIN/ROLLBACK (three). Other drivers use the dialect's ping.
    """
    raw = getattr(dbapi_connection, "_connection", None)
    if raw is not None and hasattr(raw, "execute"):
        # (SQLAlchemy 2.1's adapter has no ``await_`` attribute: the module
        # function, as the tenant_rls hook uses — the old attribute lookup fell
        # back to the 3-round-trip dialect ping.)
        await_(raw.execute("SELECT 1"))
        return
    if not dialect.do_ping(dbapi_connection):
        raise exc.DisconnectionError("database ping failed")


def install_idle_ping(engine: AsyncEngine, idle_seconds: float) -> None:
    """Ping a pooled connection on checkout only if it sat idle >= ``idle_seconds``.

    Replaces ``pool_pre_ping`` (3 round trips on EVERY checkout with asyncpg):
    a connection that served a request moments ago is handed out as is; one
    that idled long enough for a NAT/firewall/server timeout to have killed it
    is probed first (one round trip). A failed probe raises
    :class:`~sqlalchemy.exc.InvalidatePoolError` (a ``DisconnectionError``) — the
    pool reconnects this checkout transparently and recycles every older
    connection (they most likely died with it: a server restart / failover),
    exactly as ``pool_pre_ping`` did. Negative ``idle_seconds`` disables it.

    Independently of the idle time, an asyncpg connection that already reports
    itself closed (its backend died while it sat in the pool and the close was
    processed) is replaced before it is handed out: an attribute read, no
    round trip (see :mod:`sandloom.core.sql_alchemy.dead_connection` for the one
    whose close is still in flight).
    """
    sync_engine = engine.sync_engine
    dialect = sync_engine.dialect
    ping = idle_seconds >= 0

    def _fresh(_dbapi_connection: Any, record: Any) -> None:
        # A just-opened connection is known good: its first checkout skips
        # the ping (as pool_pre_ping skipped "fresh" connections).
        record.info.pop(_LAST_USED_KEY, None)

    def _stamp(_dbapi_connection: Any, record: Any) -> None:
        record.info[_LAST_USED_KEY] = time.monotonic()

    def _checkout(dbapi_connection: Any, record: Any, _proxy: Any) -> None:
        raw = getattr(dbapi_connection, "_connection", None)
        if raw is not None and raw.is_closed():
            # Died while idle: the pool replaces only this connection.
            _metrics.db_dead_connection_replaced_total.labels(phase="checkout").inc()
            raise exc.DisconnectionError("pooled connection closed while idle")
        if not ping:
            return
        last = record.info.get(_LAST_USED_KEY)
        if last is None or time.monotonic() - last < idle_seconds:
            return
        try:
            _ping(dbapi_connection, dialect)
        except Exception as error:  # any failure = dead link
            logger.info("idle pooled connection failed its ping; reconnecting")
            raise exc.InvalidatePoolError(
                f"idle connection ping failed: {error!r}"
            ) from error

    if ping:
        event.listen(sync_engine, "connect", _fresh)
        event.listen(sync_engine, "checkin", _stamp)
    if ping or dialect.driver == "asyncpg":
        event.listen(sync_engine, "checkout", _checkout)


def _pooled_engine(
    url: str,
    *,
    pool_size: int,
    max_overflow: int,
    server_settings: Mapping[str, str] | None = None,
) -> AsyncEngine:
    """An application pool: LIFO + idle-aware ping + server-side timeouts.

    ``pool_use_lifo`` keeps the hot connections hot (and lets surplus ones idle
    out), so the idle ping rarely fires under steady load. Server databases use
    :class:`LingeringQueuePool`: overflow connections linger for
    ``DB_POOL_OVERFLOW_LINGER_SECONDS`` instead of churning connect/close while
    load hovers above ``pool_size``.
    """
    connect_args: dict[str, object] = {}
    if _is_asyncpg(url):
        connect_args["timeout"] = settings.db_connect_timeout_seconds
    guc = _server_settings(url, server_settings)
    if guc:
        connect_args["server_settings"] = guc
    pool_kw: dict[str, Any] = {}
    if not url.startswith("sqlite"):
        # SQLite keeps the dialect's own pool choice (StaticPool for :memory:).
        pool_kw["poolclass"] = LingeringQueuePool
        pool_kw["overflow_linger_seconds"] = settings.db_pool_overflow_linger_seconds
        pool_kw["fair_barge_ms"] = settings.db_pool_fair_barge_ms
    engine = create_async_engine(
        url,
        future=True,
        echo=False,
        pool_pre_ping=False,
        pool_use_lifo=True,
        pool_recycle=1800,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=settings.db_pool_timeout,
        connect_args=connect_args,
        **pool_kw,
    )
    install_idle_ping(engine, float(settings.db_pool_ping_idle_seconds))
    install_reconnect_backoff(
        engine,
        base=settings.db_reconnect_base_seconds,
        cap=settings.db_reconnect_max_seconds,
        wait_timeout=settings.db_pool_timeout,
    )
    if engine.dialect.driver == "asyncpg":
        # Verify (once per process) the private driver internals the round-trip
        # fast paths use; a mismatch warns + counts and they fall back.
        _driver_compat.asyncpg_fast_paths_ok()
        _driver_compat.session_connections_ok()
        _driver_compat.dead_connection_retry_ok()
    # BEFORE the other dialect wrappers (read-only policy here, tenant_rls by the
    # caller): they run outside it and it only ever retries the statement.
    install_dead_connection_handling(engine)
    # One writer per request (spec §4.4): the cursor-boundary admission hook.
    install_write_admission(engine)
    if settings.db_read_only_sessions:
        from sandloom.core.sql_alchemy.read_only_policy import install_read_executor  # noqa: PLC0415 - cold: loaded with the first read-only UoW factory

        install_read_executor(engine)
    return engine


def _build_engine(url: str) -> AsyncEngine:
    return _pooled_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )


def _supports_contract_pool(url: str) -> bool:
    # SQLite (esp. in-memory) cannot share data across separate engines/pools.
    return not url.startswith("sqlite")


def _build_contract_engine(url: str, *, tenant_rls: bool = False) -> AsyncEngine:
    """Engine for contract calls' own UoWs: same URL, its own (smaller) pool."""
    extra: dict[str, str] = {}
    lock_timeout = settings.contract_db_lock_timeout_ms
    if lock_timeout:
        extra["lock_timeout"] = str(lock_timeout)
    engine = _pooled_engine(
        url,
        pool_size=settings.contract_db_pool_size,
        max_overflow=settings.contract_db_max_overflow,
        server_settings=extra,
    )
    if tenant_rls:
        install_tenant_rls(engine)
    return engine


#: Process-wide ``default`` engine / sessionmaker, built lazily from settings.
_default_engine: AsyncEngine | None = None
_default_sessionmaker: async_sessionmaker[AsyncSession] | None = None
#: Process-wide contract-pool engine of the ``default`` database (lazy).
_default_contract_engine: AsyncEngine | None = None


def get_default_contract_engine(*, tenant_rls: bool = False) -> AsyncEngine | None:
    """The ``default`` database's dedicated contract-pool engine (built once)."""
    global _default_contract_engine
    if not _supports_contract_pool(settings.database_url):
        return None
    if _default_contract_engine is None:
        _default_contract_engine = _build_contract_engine(
            settings.database_url, tenant_rls=tenant_rls
        )
    elif tenant_rls:
        # Built once per process, possibly for a registry without tenant_rls:
        # a later one enabling it must not inherit an RLS-less contract pool
        # (idempotent; the main default engine is treated the same way).
        install_tenant_rls(_default_contract_engine)
    return _default_contract_engine


def get_default_engine() -> AsyncEngine:
    """Return the process-wide ``default`` async engine (built once)."""
    global _default_engine
    if _default_engine is None:
        _default_engine = _build_engine(settings.database_url)
    return _default_engine


def get_default_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the sessionmaker bound to the ``default`` engine (built once)."""
    global _default_sessionmaker
    if _default_sessionmaker is None:
        _default_sessionmaker = async_sessionmaker(
            bind=get_default_engine(),
            class_=AsyncSession,
            sync_session_class=KernelSession,
            expire_on_commit=False,
        )
    return _default_sessionmaker


async def close_default_engine() -> None:
    """Dispose the process-wide ``default`` engine (idempotent)."""
    global _default_engine, _default_sessionmaker, _default_contract_engine
    if _default_engine is not None:
        engine, _default_engine = _default_engine, None
        _default_sessionmaker = None
        await engine.dispose()
    if _default_contract_engine is not None:
        contract_engine, _default_contract_engine = _default_contract_engine, None
        await contract_engine.dispose()


async def release_default_pools() -> None:
    """Return the ``default`` engines' pooled connections (keep the engines).

    Test seam (``sandloom.core.testing``): pooled asyncpg connections belong to the
    event loop that opened them, and pytest-asyncio gives every test its own
    loop. Disposing the pools on the test's own loop closes them cleanly; the
    engine objects survive, so providers holding them reconnect on the next
    loop. Unlike :func:`close_default_engine` nothing is torn down for good.
    """
    for engine in (_default_engine, _default_contract_engine):
        if engine is not None:
            await engine.dispose()
    # The active application's database providers too: a provider rebuilt
    # after a reconfiguration (an override restored, a validation pass) owns
    # an engine of its own, which a later test would otherwise reuse with a
    # connection of a closed loop ("another operation is in progress",
    # "attached to a different loop").
    core = _runtime.get_active_core()
    if core is None:
        return
    try:
        providers = core.infra.all_built()
    except RuntimeError:  # no infrastructure registry wired
        return
    for provider in providers:
        release = getattr(provider, "release_pools", None)
        if release is not None:
            await release()


def build_disposable_engine(
    url: str, *, pool_size: int | None = None, max_overflow: int | None = None
) -> AsyncEngine:
    """A standalone, caller-owned engine (test harness schema/session work).

    Kept here so the driver stays the sole constructor of database engines;
    the caller disposes it. ``pool_size`` / ``max_overflow`` bound its pool
    (SQLAlchemy's defaults when omitted).
    """
    pool: dict[str, int] = {}
    if pool_size is not None:
        pool["pool_size"] = pool_size
    if max_overflow is not None:
        pool["max_overflow"] = max_overflow
    engine = create_async_engine(url, future=True, echo=False, **pool)
    install_write_admission(engine)
    return engine


#: System channel per database instance: its DSN, and the lazily built engine.
#: Module-private on purpose: the system engine is NOT a provider attribute and
#: no DI / facade / capability path returns it; only the kernel's
#: ``sandloom.core.drivers.system_db`` reads it (``sandloom.core.drivers`` is
#: off-limits to business modules — the ``infra-imports`` architecture rule).
_system_urls: dict[str, str] = {}
_system_engines: dict[str, AsyncEngine] = {}


class SystemChannelUnavailable(TenantRLSConfigError):
    """No system channel is configured for the requested database instance."""

    fatal_at_startup = True


def _validated_rls_options(
    name: str, options: Mapping[str, object]
) -> SQLAlchemyDatabaseOptions:
    opts = SQLAlchemyDatabaseOptions(**options)
    system_url = opts.system_url or (
        settings.database_system_url if name == DEFAULT_INSTANCE else None
    )
    if opts.system_url and not opts.tenant_rls:
        raise TenantRLSConfigError(
            f"database/{name}: system_url is set but tenant_rls is off; the "
            "system channel only exists for tenant_rls databases"
        )
    if opts.require_system_channel and not opts.tenant_rls:
        raise TenantRLSConfigError(
            f"database/{name}: require_system_channel needs tenant_rls: true"
        )
    if opts.require_system_channel and not system_url:
        raise SystemChannelUnavailable(
            f"database/{name}: require_system_channel is on but no system "
            "channel is configured; set "
            + (
                "DATABASE_SYSTEM_URL"
                if name == DEFAULT_INSTANCE
                else f'INFRA["database"]["{name}"]["system_url"]'
            )
            + " to the DSN of the dedicated system role (see "
            "docs/reference/security-model.md, 'System channel')"
        )
    return opts.model_copy(update={"system_url": system_url})


def _register_system_channel(name: str, opts: SQLAlchemyDatabaseOptions) -> None:
    if opts.tenant_rls and opts.system_url:
        if _system_urls.get(name) != opts.system_url:
            _system_urls[name] = opts.system_url
            _system_engines.pop(name, None)
    else:
        _system_urls.pop(name, None)
        _system_engines.pop(name, None)


def system_channel_configured(name: str = DEFAULT_INSTANCE) -> bool:
    """True if database instance ``name`` has a system channel."""
    return name in _system_urls


def get_system_engine(name: str = DEFAULT_INSTANCE) -> AsyncEngine:
    """The system-channel engine of database instance ``name`` (built once).

    Kernel-internal (``sandloom.core.drivers.system_db``). Logged in as the
    system role, its own small pool, the RLS hook installed (``sandloom.tenant_id``
    follows the context, so tables WITHOUT a ``system_access`` policy stay
    tenant-scoped).
    """
    url = _system_urls.get(name)
    if url is None:
        raise SystemChannelUnavailable(
            f"database/{name} has no system channel: enable tenant_rls and set "
            + (
                "DATABASE_SYSTEM_URL"
                if name == DEFAULT_INSTANCE
                else f'INFRA["database"]["{name}"]["system_url"]'
            )
        )
    engine = _system_engines.get(name)
    if engine is None:
        engine = create_async_engine(
            url,
            future=True,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=2,
            max_overflow=2,
            pool_timeout=settings.db_pool_timeout,
            connect_args={"timeout": settings.db_connect_timeout_seconds}
            if _is_asyncpg(url)
            else {},
        )
        install_reconnect_backoff(
            engine,
            base=settings.db_reconnect_base_seconds,
            cap=settings.db_reconnect_max_seconds,
            wait_timeout=settings.db_pool_timeout,
        )
        install_dead_connection_handling(engine)
        install_write_admission(engine)
        install_tenant_rls(engine, system=True)
        _system_engines[name] = engine
    return engine


async def _dispose_system_engine(name: str) -> None:
    engine = _system_engines.pop(name, None)
    if engine is not None:
        await engine.dispose()


def create_sqlalchemy_database(cfg: InstanceConfig) -> SQLAlchemyDatabaseProvider:
    """Driver factory for ``database`` capability / ``sqlalchemy`` driver."""
    url = str(cfg.options.get("url") or settings.database_url)
    rls_options = _validated_rls_options(cfg.name, cfg.options)
    tenant_rls = rls_options.tenant_rls
    _register_system_channel(cfg.name, rls_options)

    # Reuse the one process-wide engine for the canonical default DB so we keep
    # a single connection pool (and a single injection seam).
    if cfg.name == DEFAULT_INSTANCE and url == settings.database_url:
        if tenant_rls:
            install_tenant_rls(get_default_engine())
        return SQLAlchemyDatabaseProvider(
            name=cfg.name,
            driver=cfg.driver,
            options=dict(cfg.options),
            engine=get_default_engine(),
            session_factory=get_default_sessionmaker(),
            # The shared process-wide engine is disposed via close_default_engine
            # (main's lifecycle), not the registry, to avoid double-dispose.
            owns_engine=False,
            contract_engine=lambda: get_default_contract_engine(tenant_rls=tenant_rls),
        )

    engine = _build_engine(url)
    if tenant_rls:
        install_tenant_rls(engine)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        sync_session_class=KernelSession,
        expire_on_commit=False,
    )
    return SQLAlchemyDatabaseProvider(
        name=cfg.name,
        driver=cfg.driver,
        options=dict(cfg.options),
        engine=engine,
        session_factory=session_factory,
        owns_engine=True,
        contract_engine=lambda: (
            _build_contract_engine(url, tenant_rls=tenant_rls)
            if _supports_contract_pool(url)
            else None
        ),
    )


__all__ = [
    "SQLAlchemyDatabaseProvider",
    "SQLAlchemyDatabaseOptions",
    "create_sqlalchemy_database",
    "sqlalchemy_db_features",
    "get_default_engine",
    "get_default_sessionmaker",
    "get_default_contract_engine",
    "close_default_engine",
    "release_default_pools",
    "build_disposable_engine",
    "install_idle_ping",
    "SystemChannelUnavailable",
    "get_system_engine",
    "system_channel_configured",
]
