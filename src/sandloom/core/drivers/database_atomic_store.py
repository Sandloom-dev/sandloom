"""The ``database`` atomic_store driver: keys in the table ``infra_atomic_store``.

An option for an application that runs on PostgreSQL alone (or SQLite, for one
host); never the default. The table is in the core migration chain
(``0015_infra_atomic_store``): ``key`` (primary key), ``value`` and
``expires_at`` (``NULL``: the key never expires). It is **logged** (an
``UNLOGGED`` table is emptied by a crash, so the driver refuses it at ``aopen``
and in ``health``: that is how it verifies ``never_evicts``).

Every statement compares ``expires_at`` with the database's own clock, so no
process clock is trusted, and treats a row with ``expires_at <= now()`` as
absent, also while the row is still there (the driver's own purge task deletes
it later: see below). No advisory lock is taken (they break under pgbouncer
transaction pooling):

- ``get``: ``SELECT`` of the live row;
- ``add``: ``INSERT ... ON CONFLICT (key) DO UPDATE ... WHERE expires_at <=
  now() RETURNING key``: a live key updates nothing and returns no row;
- ``add_or_get``: that insert, then a read of the live value when it returned no
  row; a key that expired or was deleted between the two repeats the insert
  (bounded: ``max_attempts``, then ``Unavailable``), so the value is never
  ``None``;
- ``compare_and_set`` / ``compare_and_delete``: one ``UPDATE`` / ``DELETE``
  whose ``WHERE`` holds the expected value and the liveness;
- ``set_subject_state``: one transaction. The row is read ``FOR UPDATE``
  (SQLite: a write that takes its single write lock first), ranked in Python
  with :func:`~sandloom.driverkit.atomic_store.subject_state_rank` (an exact
  :class:`~decimal.Decimal`, never a float or text), then written. An absent
  key is written by the insert of ``add``; when that insert loses the loop
  reads again (bounded).

The purge: ``aopen`` starts one asyncio task that, every ``purge_interval_seconds``,
deletes the expired rows of this table in bounded batches on the driver's own
engine (so a dedicated store URL is purged too, whichever process runs it). The
cutoff is computed by the database (``now()`` minus ``purge_grace_seconds``),
never by the process; a failed purge is logged at WARNING (the error's type name
only), counted in the operation-errors series with ``operation="purge"`` and
retried at the next interval. Concurrent purgers in several processes are
harmless (``FOR UPDATE SKIP LOCKED`` on PostgreSQL); ``aclose`` cancels the task.

A single statement runs in autocommit (one round trip); only
``set_subject_state`` opens a transaction. The statements are built with
SQLAlchemy Core and rendered through the engine's dialect (PostgreSQL ``now()``
and interval arithmetic, SQLite ``strftime`` with millisecond text); another
dialect is refused at ``aopen``.

The driver declares ``shared`` only for PostgreSQL (every process reaches that
server; a SQLite file belongs to one host) and ``never_evicts`` for both. The
connection pool does no ping on checkout (3 round trips with asyncpg): an
idempotent operation re-sends once on a connection the pool found dead, and
``pool_pre_ping`` turns the ping on. ``add``, ``add_or_get``, ``compare_and_set``
and ``compare_and_delete`` are never re-sent (run twice they answer differently).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Column,
    DateTime,
    Interval,
    MetaData,
    String,
    Table,
    Text,
    and_,
    bindparam,
    delete,
    func,
    null,
    or_,
    select,
    text,
    update,
)
from sqlalchemy import exc as sa_exc
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from sandloom.driverkit import (
    AtomicStoreProvider,
    HealthResult,
    InfraError,
    InvalidArgument,
    PermissionDenied,
    Timeout,
    Unavailable,
    observe,
)
from sandloom.driverkit.atomic_store import (
    DEFAULT_MAX_VALUE_BYTES,
    check_key,
    check_subject_state,
    check_ttl,
    check_value,
    subject_state_rank,
)

#: Failed operations of the store (connection, timeout, server error), by
#: instance and operation: the page-level alert on the security store reads it.
#: The same series as the Redis driver's (one declaration, one help text).
atomic_store_operation_errors_total = observe.counter(
    "sandloom_atomic_store_operation_errors_total",
    "Atomic store operations that raised (connection, timeout, server error)",
    ("instance", "operation"),
)

_logger = logging.getLogger(__name__)

_SQLITE_TIME = "%Y-%m-%d %H:%M:%f"

#: Most batches one purge pass runs before it yields to the next interval.
_PURGE_MAX_BATCHES = 20

#: The table as the driver sees it (the model and the migration own its DDL).
_TABLE = Table(
    "infra_atomic_store",
    MetaData(),
    Column("key", String(1024), primary_key=True),
    Column("value", Text, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=True),
)

#: The operations a single statement answers (one round trip): the only ones
#: the driver hints as such (the rest run a second statement or a transaction).
_PERFORMANCE_HINTS = {"get": "one_round_trip", "compare_and_set": "one_round_trip"}

#: What a database backend raises: SQLAlchemy's errors (the driver's own ones
#: wrapped), a socket error and a timeout. Anything else is a driver bug.
_BACKEND_ERRORS: tuple[type[BaseException], ...] = (
    sa_exc.SQLAlchemyError,
    OSError,
    TimeoutError,
)


class _Dialect:
    """The database's clock, upsert and transaction level in one dialect."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sqlite = name == "sqlite"
        #: The isolation level of ``set_subject_state``'s transaction.
        self.transaction_level = "SERIALIZABLE" if self.sqlite else "READ COMMITTED"

    def now(self) -> Any:
        if self.sqlite:
            return func.strftime(_SQLITE_TIME, "now")
        return func.now()

    def expiry(self, ttl_seconds: int | None) -> Any:
        """``now + ttl`` as SQL, or ``NULL`` for a key that never expires."""
        if not ttl_seconds:
            return null()
        if self.sqlite:
            return func.strftime(
                _SQLITE_TIME, "now", bindparam("ttl", f"+{ttl_seconds} seconds")
            )
        return func.now() + func.make_interval(
            0, 0, 0, 0, 0, 0, bindparam("ttl", float(ttl_seconds)), type_=Interval
        )

    def live(self) -> Any:
        """The row is not expired: ``expires_at IS NULL OR expires_at > now()``."""
        return or_(_TABLE.c.expires_at.is_(None), _TABLE.c.expires_at > self.now())

    def purge(self, grace_seconds: int, batch_size: int) -> Any:
        """``DELETE`` of at most ``batch_size`` rows expired more than
        ``grace_seconds`` ago, the cutoff being the database's clock."""
        cutoff: Any
        if self.sqlite:
            cutoff = func.strftime(
                _SQLITE_TIME, "now", bindparam("grace", f"-{grace_seconds} seconds")
            )
        else:
            cutoff = func.now() - func.make_interval(
                0,
                0,
                0,
                0,
                0,
                0,
                bindparam("grace", float(grace_seconds)),
                type_=Interval,
            )
        victims = (
            select(_TABLE.c.key)
            .where(_TABLE.c.expires_at < cutoff)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        return delete(_TABLE).where(_TABLE.c.key.in_(victims))

    def insert(self) -> Any:
        return sqlite.insert(_TABLE) if self.sqlite else postgresql.insert(_TABLE)

    def add(self, key: str, value: str, ttl_seconds: int | None) -> Any:
        """The insert of ``add``: a row comes back when this call created the key
        (new, or over an expired row)."""
        statement = self.insert().values(
            key=key, value=value, expires_at=self.expiry(ttl_seconds)
        )
        return statement.on_conflict_do_update(
            index_elements=[_TABLE.c.key],
            set_={
                "value": statement.excluded.value,
                "expires_at": statement.excluded.expires_at,
            },
            where=_TABLE.c.expires_at <= self.now(),
        ).returning(_TABLE.c.key)

    def put(self, key: str, value: str, ttl_seconds: int | None) -> Any:
        """The upsert of ``set``: the key holds ``value`` whatever it held."""
        statement = self.insert().values(
            key=key, value=value, expires_at=self.expiry(ttl_seconds)
        )
        return statement.on_conflict_do_update(
            index_elements=[_TABLE.c.key],
            set_={
                "value": statement.excluded.value,
                "expires_at": statement.excluded.expires_at,
            },
        )


class DatabaseAtomicStoreOptions(BaseModel):
    """Config-shape schema of the ``database`` atomic_store driver."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)

    #: The async SQLAlchemy URL of a database that carries the core chain.
    url: str | None = None
    pool_size: int = Field(default=8, ge=1)
    #: Seconds a caller waits for a pooled connection before ``Timeout``.
    pool_timeout_seconds: float = Field(default=5.0, gt=0)
    #: Seconds after which a pooled connection is replaced (a server-side idle
    #: timeout or a proxy would otherwise close it under the pool).
    pool_recycle_seconds: int = Field(default=1800, ge=-1)
    #: Ping on every checkout (3 round trips with asyncpg); off, a dead pooled
    #: connection fails one idempotent operation's first send, which re-sends once.
    pool_pre_ping: bool = False
    #: Seconds one statement (or the transaction) may take before ``Timeout``.
    statement_timeout_seconds: float = Field(default=5.0, gt=0)
    #: Bound on the retries of ``add_or_get`` and ``set_subject_state`` when a
    #: concurrent writer changes the key between their statements; past it the
    #: operation is ``Unavailable``.
    max_attempts: int = Field(default=8, ge=2, le=100)
    max_value_bytes: int = Field(default=DEFAULT_MAX_VALUE_BYTES, ge=1)
    #: Seconds between two purges of the expired rows (the driver's own task).
    purge_interval_seconds: float = Field(default=60.0, gt=0)
    #: Seconds past its expiry a row must be (by the database's clock) before
    #: the purge deletes it: a margin only, an expired row is absent for every
    #: statement already.
    purge_grace_seconds: int = Field(default=60, ge=0)
    #: Rows one purge ``DELETE`` removes at most.
    purge_batch_size: int = Field(default=1000, ge=1)


class DatabaseAtomicStoreProvider(AtomicStoreProvider):
    """The ``atomic_store`` in the database's ``infra_atomic_store`` table."""

    contract_version = 1
    supported_features: ClassVar[frozenset[str]] = frozenset({"shared", "never_evicts"})
    performance_hints = _PERFORMANCE_HINTS
    options_schema = DatabaseAtomicStoreOptions

    @classmethod
    def features_for(cls, options: Mapping[str, Any]) -> frozenset[str]:
        """``never_evicts`` always; ``shared`` only on PostgreSQL, a server every
        process reaches. A SQLite file belongs to one host."""
        url = options.get("url")
        try:
            backend = make_url(url).get_backend_name() if isinstance(url, str) else ""
        except sa_exc.ArgumentError:
            backend = ""
        if backend == "postgresql":
            return frozenset({"shared", "never_evicts"})
        return frozenset({"never_evicts"})

    def __init__(self, *, name: str, driver: str, options: Mapping[str, Any]) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._opts = DatabaseAtomicStoreOptions.model_validate(self.options)
        self._engine: AsyncEngine | None = None
        self._dialect: _Dialect | None = None
        #: Set by a check that refused the table: every operation then raises
        #: it, so a registry that only logs the refusal never serves from it.
        self._refusal: str | None = None
        self._purge_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------ plumbing
    def _failure(self, operation: str, exc: BaseException) -> InfraError:
        """The ``InfraError`` of a backend failure: its type name only."""
        atomic_store_operation_errors_total.labels(self.name, operation).inc()
        label = f"atomic_store {self.name!r} {operation}: {type(exc).__name__}"
        if isinstance(exc, TimeoutError | sa_exc.TimeoutError):
            return Timeout(label, capability=self.capability, instance=self.name)
        text_of = str(getattr(exc, "orig", exc)).lower()
        if isinstance(exc, sa_exc.DBAPIError) and (
            "password authentication" in text_of or "permission denied" in text_of
        ):
            return PermissionDenied(
                label, capability=self.capability, instance=self.name
            )
        return Unavailable(label, capability=self.capability, instance=self.name)

    def _engine_dialect(self) -> tuple[AsyncEngine, _Dialect]:
        if self._engine is not None and self._dialect is not None:
            return self._engine, self._dialect
        if not self._opts.url:
            raise InvalidArgument(
                f"atomic_store instance {self.name!r} has no 'url' option",
                capability=self.capability,
                instance=self.name,
            )
        try:
            backend = make_url(self._opts.url).get_backend_name()
            if backend not in ("postgresql", "sqlite"):
                raise InvalidArgument(
                    "the database atomic_store driver renders postgresql and "
                    f"sqlite, not {backend!r}",
                    capability=self.capability,
                    instance=self.name,
                )
            # A pooled server database; SQLite keeps its own (file) pool.
            pool: dict[str, Any] = (
                {
                    "pool_size": self._opts.pool_size,
                    "max_overflow": 0,
                    "pool_timeout": self._opts.pool_timeout_seconds,
                    "pool_recycle": self._opts.pool_recycle_seconds,
                    "pool_pre_ping": self._opts.pool_pre_ping,
                }
                if backend == "postgresql"
                else {}
            )
            engine = create_async_engine(
                self._opts.url, isolation_level="AUTOCOMMIT", **pool
            )
        except InfraError:
            raise
        except (sa_exc.SQLAlchemyError, ImportError, ValueError) as exc:
            raise self._failure("connect", exc) from None
        self._engine, self._dialect = engine, _Dialect(backend)
        return engine, self._dialect

    def _refused(self) -> None:
        """Raise the refusal of a check that failed: no operation runs after it."""
        if self._refusal is not None:
            raise InvalidArgument(
                self._refusal, capability=self.capability, instance=self.name
            )

    async def _exec(
        self, operation: str, statement: Any, *, idempotent: bool = False
    ) -> Any:
        """Run one statement in autocommit (one round trip): its rows, or the
        rowcount of a statement that returns none. An idempotent statement is
        sent again once on a connection the pool found dead."""
        self._refused()
        engine, _ = self._engine_dialect()
        for attempt in (1, 2):
            try:
                async with asyncio.timeout(self._opts.statement_timeout_seconds):
                    async with engine.connect() as conn:
                        result = await conn.execute(statement)
                        return result.all() if result.returns_rows else result.rowcount
            except InfraError:
                raise
            except sa_exc.DBAPIError as exc:
                if idempotent and attempt == 1 and exc.connection_invalidated:
                    continue
                raise self._failure(operation, exc) from None
            except _BACKEND_ERRORS as exc:
                raise self._failure(operation, exc) from None
        raise AssertionError("unreachable")  # pragma: no cover

    def _contended(self, operation: str) -> InfraError:
        atomic_store_operation_errors_total.labels(self.name, operation).inc()
        return Unavailable(
            f"atomic_store {self.name!r} {operation}: contention past "
            f"{self._opts.max_attempts} attempts",
            capability=self.capability,
            instance=self.name,
        )

    # ----------------------------------------------------------- lifecycle
    async def _persistence_problem(self) -> str | None:
        """Why the table cannot hold a revocation mark, or ``None``: it must exist
        and be logged (PostgreSQL empties an ``UNLOGGED`` table after a crash)."""
        _, dialect = self._engine_dialect()
        await self._exec("open", select(_TABLE.c.key).limit(1))
        if dialect.sqlite:
            return None
        rows = await self._exec(
            "open",
            text(
                "SELECT relpersistence::text FROM pg_class "
                "WHERE oid = to_regclass('infra_atomic_store')"
            ),
        )
        if rows and rows[0][0] != "p":
            return (
                f"atomic_store instance {self.name!r}: the table infra_atomic_store "
                "is not a logged table, so a crash would empty it and lose "
                "revocation marks and idempotency reservations "
                "(fix: ALTER TABLE infra_atomic_store SET LOGGED)"
            )
        return None

    async def aopen(self) -> None:
        self._refusal = None
        self._engine_dialect()
        problem = await self._persistence_problem()
        if problem is not None:
            self._refusal = problem
            raise InvalidArgument(
                problem, capability=self.capability, instance=self.name
            )
        if self._purge_task is None or self._purge_task.done():
            self._purge_task = asyncio.create_task(
                self._purge_loop(), name=f"atomic_store_purge:{self.name}"
            )

    async def purge_expired(self) -> int:
        """Delete the rows expired beyond the grace, by the database's clock, in
        bounded batches (at most ``_PURGE_MAX_BATCHES`` per call). Their number."""
        _, dialect = self._engine_dialect()
        batch = self._opts.purge_batch_size
        statement = dialect.purge(self._opts.purge_grace_seconds, batch)
        total = 0
        for _ in range(_PURGE_MAX_BATCHES):
            deleted = int(await self._exec("purge", statement, idempotent=True))
            total += deleted
            if deleted < batch:
                break
        return total

    async def _purge_loop(self) -> None:
        """Purge every interval until cancelled; a failure never ends the loop."""
        while True:
            await asyncio.sleep(self._opts.purge_interval_seconds)
            try:
                await self.purge_expired()
            except InfraError as exc:  # counted by ``_failure`` when a backend error
                _logger.warning("%s", exc)
            except _BACKEND_ERRORS as exc:  # not wrapped: count it here
                atomic_store_operation_errors_total.labels(self.name, "purge").inc()
                _logger.warning(
                    "atomic_store %r purge: %s", self.name, type(exc).__name__
                )

    async def aclose(self) -> None:
        task, self._purge_task = self._purge_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        engine, self._engine, self._dialect = self._engine, None, None
        if engine is not None:
            with contextlib.suppress(Exception):  # closing is best effort
                await engine.dispose()

    async def health(self) -> HealthResult:
        if self._refusal is not None:
            return HealthResult(status="fail", details={"reason": "table_not_logged"})
        started = time.monotonic()
        try:
            problem = await self._persistence_problem()
        except InfraError as exc:  # a failed probe, by its type only
            return HealthResult(status="fail", details={"error": type(exc).__name__})
        if problem is not None:
            self._refusal = problem  # fail closed: no operation serves from it now
            return HealthResult(status="fail", details={"reason": "table_not_logged"})
        latency = round((time.monotonic() - started) * 1000, 1)
        return HealthResult(status="ok", details={"latency_ms": latency})

    async def isolation_identity(self) -> str | None:
        """``database:<system identifier>/<database name>``: the server and the
        database the URL reaches (the same through DNS aliases), never its
        credentials. A SQLite file is ``database:sqlite-<hash of its path>/<file>``."""
        engine, dialect = self._engine_dialect()
        if dialect.sqlite:
            path = engine.url.database or ":memory:"
            digest = hashlib.sha256(path.encode()).hexdigest()[:16]
            return f"{self.driver}:sqlite-{digest}/{path.rsplit('/', 1)[-1]}"
        rows = await self._exec(
            "isolation_identity",
            text(
                "SELECT (SELECT system_identifier FROM pg_control_system())::text, "
                "current_database()"
            ),
            idempotent=True,
        )
        return f"{self.driver}:{rows[0][0]}/{rows[0][1]}"

    # ---------------------------------------------------------- operations
    async def get(self, key: str) -> str | None:
        check_key(key, instance=self.name)
        _, dialect = self._engine_dialect()
        rows = await self._exec(
            "get",
            select(_TABLE.c.value).where(and_(_TABLE.c.key == key, dialect.live())),
            idempotent=True,
        )
        return str(rows[0][0]) if rows else None

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        check_key(key, instance=self.name)
        check_value(value, limit=self._opts.max_value_bytes, instance=self.name)
        ttl = check_ttl(ttl_seconds, instance=self.name)
        _, dialect = self._engine_dialect()
        await self._exec("set", dialect.put(key, value, ttl or None), idempotent=True)

    async def delete(self, key: str) -> None:
        check_key(key, instance=self.name)
        await self._exec(
            "delete", delete(_TABLE).where(_TABLE.c.key == key), idempotent=True
        )

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        check_key(key, instance=self.name)
        check_value(value, limit=self._opts.max_value_bytes, instance=self.name)
        ttl = check_ttl(ttl_seconds, instance=self.name)
        _, dialect = self._engine_dialect()
        rows = await self._exec("add", dialect.add(key, value, ttl or None))
        return bool(rows)

    async def add_or_get(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> tuple[bool, str | None]:
        check_key(key, instance=self.name)
        check_value(value, limit=self._opts.max_value_bytes, instance=self.name)
        ttl = check_ttl(ttl_seconds, instance=self.name)
        _, dialect = self._engine_dialect()
        for _ in range(self._opts.max_attempts):
            if await self._exec("add_or_get", dialect.add(key, value, ttl or None)):
                return True, None
            held = await self._exec(
                "add_or_get",
                select(_TABLE.c.value).where(and_(_TABLE.c.key == key, dialect.live())),
            )
            if held:
                return False, str(held[0][0])
            # Expired or deleted between the two statements: create it now.
        raise self._contended("add_or_get")

    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        check_key(key, instance=self.name)
        check_value(expected, limit=self._opts.max_value_bytes, instance=self.name)
        check_value(value, limit=self._opts.max_value_bytes, instance=self.name)
        ttl = check_ttl(ttl_seconds, instance=self.name)
        _, dialect = self._engine_dialect()
        statement = (
            update(_TABLE)
            .where(
                and_(
                    _TABLE.c.key == key,
                    _TABLE.c.value == expected,
                    dialect.live(),
                )
            )
            .values(value=value, expires_at=dialect.expiry(ttl or None))
        )
        return bool(await self._exec("compare_and_set", statement))

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        check_key(key, instance=self.name)
        check_value(expected, limit=self._opts.max_value_bytes, instance=self.name)
        _, dialect = self._engine_dialect()
        statement = delete(_TABLE).where(
            and_(_TABLE.c.key == key, _TABLE.c.value == expected, dialect.live())
        )
        return bool(await self._exec("compare_and_delete", statement))

    async def set_subject_state(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str:
        check_key(key, instance=self.name)
        check_subject_state(value, instance=self.name)
        ttl = check_ttl(ttl_seconds, instance=self.name) or None
        self._refused()
        engine, dialect = self._engine_dialect()
        incoming = subject_state_rank(value)
        if incoming is None:  # unreachable: check_subject_state parsed it
            raise InvalidArgument(
                "a subject state is v1:after:<decimal> or v1:blocked:<decimal>",
                capability=self.capability,
                instance=self.name,
            )
        for attempt in range(self._opts.max_attempts):
            held: str | None = None
            try:
                async with asyncio.timeout(self._opts.statement_timeout_seconds):
                    async with engine.connect() as conn:
                        await conn.execution_options(
                            isolation_level=dialect.transaction_level
                        )
                        async with conn.begin():
                            held = await self._write_if_higher(
                                conn, dialect, key, value, incoming, ttl
                            )
            except InfraError:
                raise
            except sa_exc.DBAPIError as exc:
                if attempt == 0 and exc.connection_invalidated:
                    continue  # the same value written twice leaves one state
                raise self._failure("set_subject_state", exc) from None
            except _BACKEND_ERRORS as exc:
                raise self._failure("set_subject_state", exc) from None
            if held is not None:
                return held
            # The insert of an absent key lost to a concurrent writer: read again.
        raise self._contended("set_subject_state")

    @staticmethod
    async def _write_if_higher(
        conn: AsyncConnection,
        dialect: _Dialect,
        key: str,
        value: str,
        incoming: tuple[int, Any],
        ttl: int | None,
    ) -> str | None:
        """In the caller's transaction: lock the live row and write ``value``
        when it ranks above the stored one. The value held afterwards, or
        ``None`` when the insert of an absent key lost."""
        if dialect.sqlite:
            # SQLite has no row lock: a write takes its one write lock, so the
            # read below and the write after it are not interleaved.
            await conn.execute(
                update(_TABLE).where(_TABLE.c.key == key).values(key=_TABLE.c.key)
            )
        rows = (
            await conn.execute(
                select(_TABLE.c.value)
                .where(and_(_TABLE.c.key == key, dialect.live()))
                .with_for_update()
            )
        ).all()
        if not rows:
            created = await conn.execute(dialect.add(key, value, ttl))
            return value if created.all() else None
        stored = str(rows[0][0])
        ranked = subject_state_rank(stored)
        if ranked is not None and not ranked < incoming:
            return stored  # the stored value ranks as high or higher
        await conn.execute(
            update(_TABLE)
            .where(_TABLE.c.key == key)
            .values(value=value, expires_at=dialect.expiry(ttl))
        )
        return value


__all__: Sequence[str] = ["DatabaseAtomicStoreOptions", "DatabaseAtomicStoreProvider"]
