"""The ``database`` lease driver: fenced leases over the table ``infra_lease``.

The table is in the core migration chain (``0014_infra_lease``): one row per
name, kept after release and expiry so the fence survives. Every operation is
one statement that compares ``expires_at`` with the database's own clock, so
no process clock is trusted and no advisory lock is taken (they break under
pgbouncer transaction pooling):

- **acquire**: ``INSERT ... ON CONFLICT (name) DO UPDATE SET owner, token,
  expires_at = now() + ttl, fence = infra_lease.fence + 1 WHERE
  infra_lease.expires_at <= now() RETURNING fence``; a held name updates
  nothing and returns no row;
- **renew** / **release**: ``UPDATE ... WHERE name AND token AND expires_at >
  now()`` (an expired lease is not held); release sets ``expires_at = now()``.

The statements are built with SQLAlchemy Core and rendered through the
engine's dialect: PostgreSQL ``now()`` and interval arithmetic, SQLite
``strftime`` with millisecond text (SQLite serialises writers, which is what
makes its single statements atomic). Another dialect is refused at ``aopen``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import uuid
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Interval,
    MetaData,
    String,
    Table,
    bindparam,
    func,
    select,
    update,
)
from sqlalchemy import exc as sa_exc
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sandloom.driverkit import (
    HealthResult,
    InfraError,
    InvalidArgument,
    Lease,
    LeaseHolder,
    LeaseProvider,
    PermissionDenied,
    Timeout,
    Unavailable,
)

_MIN_TTL_SECONDS = 3
_MAX_NAME_LENGTH = 255
_SQLITE_TIME = "%Y-%m-%d %H:%M:%f"

#: The table as the driver sees it (the model and the migration own its DDL).
_TABLE = Table(
    "infra_lease",
    MetaData(),
    Column("name", String(255), primary_key=True),
    Column("owner", String(255), nullable=False),
    Column("token", String(64), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("fence", BigInteger, nullable=False),
)


class _Clock:
    """The database's clock in one dialect: ``now`` and ``now + ttl`` as SQL."""

    def __init__(self, dialect: str) -> None:
        self.dialect = dialect

    def now(self) -> Any:
        if self.dialect == "sqlite":
            return func.strftime(_SQLITE_TIME, "now")
        return func.now()

    def after(self, ttl: Any) -> Any:
        """``now + ttl`` seconds; ``ttl`` is a bound parameter."""
        if self.dialect == "sqlite":
            return func.strftime(_SQLITE_TIME, "now", ttl)
        return func.now() + func.make_interval(0, 0, 0, 0, 0, 0, ttl, type_=Interval)

    def ttl_value(self, seconds: int) -> Any:
        return f"+{seconds} seconds" if self.dialect == "sqlite" else float(seconds)


class DatabaseLeaseOptions(BaseModel):
    """Config-shape schema of the ``database`` lease driver."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)

    #: The async SQLAlchemy URL of a database that carries the core chain.
    url: str | None = None
    pool_size: int = Field(default=2, ge=1)
    #: Seconds one statement may take before it fails as ``Timeout``.
    statement_timeout_seconds: float = Field(default=5.0, gt=0)


class DatabaseLeaseProvider(LeaseProvider):
    """Fenced leases in the database's ``infra_lease`` table."""

    contract_version = 1
    supported_features: ClassVar[frozenset[str]] = frozenset({"shared"})
    options_schema = DatabaseLeaseOptions

    def __init__(self, *, name: str, driver: str, options: dict[str, Any]) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._opts = DatabaseLeaseOptions(**options)
        self._engine: AsyncEngine | None = None
        self._clock: _Clock | None = None

    # ------------------------------------------------------------ plumbing
    def _fail(self, exc: BaseException, what: str) -> InfraError:
        """The ``InfraError`` of a backend failure: its type name only."""
        if isinstance(exc, InfraError):
            return exc
        label = f"database lease {what} failed: {type(exc).__name__}"
        if isinstance(exc, TimeoutError):
            return Timeout(label, capability=self.capability, instance=self.name)
        text = str(getattr(exc, "orig", exc)).lower()
        if isinstance(exc, sa_exc.DBAPIError) and (
            "password authentication" in text or "permission denied" in text
        ):
            return PermissionDenied(
                label, capability=self.capability, instance=self.name
            )
        return Unavailable(label, capability=self.capability, instance=self.name)

    def _engine_clock(self) -> tuple[AsyncEngine, _Clock]:
        if self._engine is not None and self._clock is not None:
            return self._engine, self._clock
        if not self._opts.url:
            raise InvalidArgument(
                "the database lease driver needs the option 'url'",
                capability=self.capability,
                instance=self.name,
            )
        try:
            dialect = make_url(self._opts.url).get_backend_name()
            if dialect not in ("postgresql", "sqlite"):
                raise InvalidArgument(
                    "the database lease driver renders postgresql and sqlite, "
                    f"not {dialect!r}",
                    capability=self.capability,
                    instance=self.name,
                )
            # A pooled server database; SQLite keeps its own (file or memory) pool.
            pool: dict[str, Any] = (
                {
                    "pool_size": self._opts.pool_size,
                    "max_overflow": 0,
                    "pool_pre_ping": True,
                }
                if dialect == "postgresql"
                else {}
            )
            engine = create_async_engine(self._opts.url, **pool)
        except InfraError:
            raise
        except Exception as exc:
            raise self._fail(exc, "connect") from exc
        self._engine, self._clock = engine, _Clock(dialect)
        return engine, self._clock

    async def _run(self, what: str, statement: Any) -> Any:
        """Run one statement in its own transaction; its rows, or the rowcount
        of a statement that returns none."""
        engine, _ = self._engine_clock()
        try:
            async with asyncio.timeout(self._opts.statement_timeout_seconds):
                async with engine.begin() as conn:
                    result = await conn.execute(statement)
                    return result.all() if result.returns_rows else result.rowcount
        except InfraError:
            raise
        except Exception as exc:
            raise self._fail(exc, what) from exc

    def _check_name(self, name: str) -> None:
        if not isinstance(name, str) or not name or len(name) > _MAX_NAME_LENGTH:
            raise InvalidArgument(
                f"a lease name is 1..{_MAX_NAME_LENGTH} characters",
                capability=self.capability,
                instance=self.name,
            )

    # ----------------------------------------------------------- lifecycle
    async def aopen(self) -> None:
        self._engine_clock()
        await self._run("open", select(_TABLE.c.name).limit(1))

    async def aclose(self) -> None:
        engine, self._engine, self._clock = self._engine, None, None
        if engine is not None:
            with contextlib.suppress(Exception):  # closing is best effort
                await engine.dispose()

    async def health(self) -> HealthResult:
        started = time.monotonic()
        try:
            await self._run("health", select(_TABLE.c.name).limit(1))
        except InfraError as exc:  # a failed probe, by its type only
            return HealthResult(status="fail", details={"error": type(exc).__name__})
        latency = round((time.monotonic() - started) * 1000, 1)
        return HealthResult(status="ok", details={"latency_ms": latency})

    async def isolation_identity(self) -> str | None:
        """``database:<sha256 of host, port and database, 16 hex>``: the store
        the URL names, never its credentials."""
        engine, _ = self._engine_clock()
        url = engine.url
        backend = url.get_backend_name()
        material = f"{backend}|{url.host}|{url.port}|{url.database}"
        return f"{self.driver}:{hashlib.sha256(material.encode()).hexdigest()[:16]}"

    # ---------------------------------------------------------- operations
    async def acquire(self, name: str, *, ttl_seconds: int, owner: str) -> Lease | None:
        self._check_name(name)
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or ttl_seconds < _MIN_TTL_SECONDS
        ):
            raise InvalidArgument(
                f"a lease TTL is a whole number of seconds >= {_MIN_TTL_SECONDS}",
                capability=self.capability,
                instance=self.name,
            )
        if not isinstance(owner, str) or len(owner) > _MAX_NAME_LENGTH:
            raise InvalidArgument(
                f"a lease owner is a label of up to {_MAX_NAME_LENGTH} characters",
                capability=self.capability,
                instance=self.name,
            )
        _, clock = self._engine_clock()
        token = uuid.uuid4().hex
        ttl: Any = bindparam("ttl", clock.ttl_value(ttl_seconds))
        expires = clock.after(ttl)
        if clock.dialect == "sqlite":
            upsert: Any = sqlite.insert(_TABLE)
        else:
            upsert = postgresql.insert(_TABLE)
        statement = (
            upsert.values(
                name=name, owner=owner, token=token, expires_at=expires, fence=1
            )
            .on_conflict_do_update(
                index_elements=[_TABLE.c.name],
                set_={
                    "owner": owner,
                    "token": token,
                    "expires_at": expires,
                    "fence": _TABLE.c.fence + 1,
                },
                where=_TABLE.c.expires_at <= clock.now(),
            )
            .returning(_TABLE.c.fence)
        )
        rows = await self._run("acquire", statement)
        if not rows:
            return None
        return Lease(
            name=name,
            owner=owner,
            token=token,
            fence=int(rows[0][0]),
            ttl_seconds=ttl_seconds,
        )

    async def renew(self, lease: Lease) -> bool:
        self._check_name(lease.name)
        _, clock = self._engine_clock()
        ttl: Any = bindparam("ttl", clock.ttl_value(lease.ttl_seconds))
        held = (
            (_TABLE.c.name == lease.name)
            & (_TABLE.c.token == lease.token)
            & (_TABLE.c.expires_at > clock.now())
        )
        statement = update(_TABLE).where(held).values(expires_at=clock.after(ttl))
        return bool(await self._run("renew", statement))

    async def release(self, lease: Lease) -> bool:
        self._check_name(lease.name)
        _, clock = self._engine_clock()
        held = (
            (_TABLE.c.name == lease.name)
            & (_TABLE.c.token == lease.token)
            & (_TABLE.c.expires_at > clock.now())
        )
        statement = update(_TABLE).where(held).values(expires_at=clock.now())
        return bool(await self._run("release", statement))

    async def holder(self, name: str) -> LeaseHolder | None:
        self._check_name(name)
        _, clock = self._engine_clock()
        statement = select(_TABLE.c.owner, _TABLE.c.fence).where(
            (_TABLE.c.name == name) & (_TABLE.c.expires_at > clock.now())
        )
        rows = await self._run("holder", statement)
        if not rows:
            return None
        return LeaseHolder(owner=str(rows[0][0]), fence=int(rows[0][1]))


__all__ = ["DatabaseLeaseOptions", "DatabaseLeaseProvider"]
