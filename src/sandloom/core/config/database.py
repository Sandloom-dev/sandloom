"""The default database instance, its pool and server-side guards, and the
query planner.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DatabaseConfig(BaseModel):
    """The default database instance, its pool and server-side guards, and the
    query planner.
    """

    # Database (the "default" database instance unless overridden via INFRA)
    database_url: str = Field(
        alias="DATABASE_URL",
        description=(
            "Connection URL of the default application database; production "
            "relational deployments use PostgreSQL."
        ),
    )
    # System channel of the default database (only meaningful with the
    # ``tenant_rls`` instance option): DSN of a SEPARATE login role (default
    # ``sandloom_system``) used by framework-internal privileged/cross-tenant work
    # (``sandloom.core.drivers.system_db``). It is never handed to business
    # modules and never used for DbSession. SECRETS_DIR-aware like every
    # setting (the DSN carries a password). Named instances set the
    # ``system_url`` INFRA option.
    database_system_url: str | None = Field(
        default=None,
        alias="DATABASE_SYSTEM_URL",
        description=(
            "Separate privileged database login used only by framework "
            "cross-tenant work on the default database; never supplied to "
            "business-module sessions."
        ),
    )
    # Connection pool sizing for the default engine (production tuning knobs).
    # Defaults are sized for the async model: one worker handles many concurrent
    # in-flight requests, each holding a connection for its whole request-scoped
    # transaction — so the pool must track request concurrency, not CPU count.
    # NOTE: total Postgres connections ≈ (db_pool_size + db_max_overflow) ×
    # WEB_CONCURRENCY; keep it under the server's max_connections.
    db_pool_size: int = Field(
        default=10,
        alias="DB_POOL_SIZE",
        description=(
            "Persistent connections per request/worker database pool; multiply "
            "the pool budget by the number of worker processes."
        ),
    )
    db_max_overflow: int = Field(
        default=20,
        alias="DB_MAX_OVERFLOW",
        description=("Additional temporary connections permitted above DB_POOL_SIZE."),
    )
    db_pool_timeout: int = Field(
        default=30,
        alias="DB_POOL_TIMEOUT",
        description=(
            "Maximum seconds a database checkout waits for an available pooled "
            "connection."
        ),
    )
    db_connect_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        alias="DB_CONNECT_TIMEOUT_SECONDS",
        description=("Maximum seconds to establish a database connection."),
    )
    db_reconnect_base_seconds: float = Field(
        default=0.05,
        gt=0,
        alias="DB_RECONNECT_BASE_SECONDS",
        description=(
            "Initial pool-wide backoff delay in seconds for new connections after "
            "transport failures; never retries statements."
        ),
    )
    db_reconnect_max_seconds: float = Field(
        default=2.0,
        gt=0,
        alias="DB_RECONNECT_MAX_SECONDS",
        description=("Maximum database reconnect backoff delay in seconds."),
    )
    # Overflow hysteresis: while a checkout found no idle connection within
    # the last N seconds, a returned overflow connection stays idle in the pool
    # instead of being closed (stock QueuePool closes it at once, so load
    # hovering above db_pool_size churns connect/close continuously — each a
    # TCP + TLS + SCRAM handshake on the event loop). After N quiet seconds
    # the pool shrinks back to db_pool_size. The total bound
    # (db_pool_size + db_max_overflow) is unchanged. 0 = stock behaviour.
    db_pool_overflow_linger_seconds: float = Field(
        default=30.0,
        ge=0,
        alias="DB_POOL_OVERFLOW_LINGER_SECONDS",
        description=(
            "Seconds to retain returned overflow connections after recent pool "
            "pressure; zero restores immediate overflow eviction."
        ),
    )
    # Fair checkout with bounded barging: while the oldest waiting checkout
    # has waited less than N ms, a returned connection goes back to the idle
    # queue (whoever runs first takes it — no connection idles reserved for a
    # task the loop has not resumed yet); past N ms it is handed straight to
    # that waiter, so no checkout waits more than ~N ms behind newcomers.
    # 0 = strict FIFO handoff. docs/design/pool-checkout-fairness-2026-10-22.md
    db_pool_fair_barge_ms: float = Field(
        default=25.0,
        ge=0,
        alias="DB_POOL_FAIR_BARGE_MS",
        description=(
            "Maximum waiter age in milliseconds before connection handoff takes "
            "priority over new arrivals; zero requests strict FIFO handoff."
        ),
    )
    # Liveness ping on checkout, ONLY for a connection that sat idle in the
    # pool for at least this many seconds (one ``;`` round trip, no
    # transaction). A recently used connection is handed out without a ping
    # (SQLAlchemy's pool_pre_ping cost 3 round trips — BEGIN/;/ROLLBACK — on
    # EVERY checkout). A failed ping invalidates the pool and reconnects
    # transparently. 0 = ping every checkout; negative = never ping.
    db_pool_ping_idle_seconds: float = Field(
        default=5.0,
        alias="DB_POOL_PING_IDLE_SECONDS",
        description=(
            "Ping only connections idle for this many seconds; zero pings every "
            "checkout and a negative value disables idle pings."
        ),
    )
    # Default ON (kill switch: DB_DEAD_CONNECTION_RETRY=false): when the FIRST
    # message of a fresh transaction finds its pooled connection already dead
    # and asyncpg refused it BEFORE writing a byte (the backend died while the
    # connection idled: ERROR_CONSUME / closed), the connection is replaced in
    # place and the message sent once on the new one. Nothing else is ever
    # retried: a statement / COMMIT that reached a dying connection is a 503
    # (DatabaseUnavailable). See sandloom.core.sql_alchemy.dead_connection.
    db_dead_connection_retry: bool = Field(
        default=True,
        alias="DB_DEAD_CONNECTION_RETRY",
        description=(
            "Replace and retry a connection only when the first transaction "
            "message was refused before sending any bytes; never retries an "
            "ambiguous statement or commit."
        ),
    )
    # PROTOTYPE (default off): endpoints that declare ``ReadOnlyDbSession`` get
    # a server-enforced READ ONLY transaction plus early statement refusals.
    # Off: ReadOnlyDbSession == DbSession.
    db_read_only_sessions: bool = Field(
        default=False,
        alias="DB_READ_ONLY_SESSIONS",
        description=(
            "Enable server-enforced read-only transactions and early write "
            "refusals for read-only session dependencies; disabled uses normal "
            "session semantics."
        ),
    )
    # Default ON (kill switch: DB_RLS_MERGED_BEGIN=false): on a ``tenant_rls``
    # engine (asyncpg) the first statement of a transaction sends ``BEGIN …;
    # SELECT set_config(tenant)`` as ONE simple-query message (one round trip
    # fewer per RLS transaction). Only applies to asyncpg + tenant_rls; see
    # sandloom.core.sql_alchemy.tenant_rls (merged BEGIN) for the semantics. A
    # startup probe of the private driver internals it uses (driver_compat)
    # falls back to the two-message form, with a warning + metric, when an
    # asyncpg / SQLAlchemy upgrade changed them (docs/operations/operator-reference.md).
    db_rls_merged_begin: bool = Field(
        default=True,
        alias="DB_RLS_MERGED_BEGIN",
        description=(
            "Combine transaction BEGIN and tenant binding in one asyncpg message "
            "on tenant-RLS engines; incompatible driver internals fall back to "
            "separate messages."
        ),
    )
    # Best-effort footgun detection (Public API 1.0 §4.3 item 3, SANDLOOM-R013): a
    # kernel session (or its connection) used from a task that does not own
    # its unit. Off by default — the §10.4 performance gate moved it off the
    # hot path; the test environment turns it on, where it catches the
    # mistake in the inner loop. db() from a non-owner task is always refused.
    db_session_owner_checks: bool = Field(
        default=False,
        alias="DB_SESSION_OWNER_CHECKS",
        description=(
            "Extra session/connection ownership checks for accidental use from "
            "another task; db() always enforces its unit owner independently."
        ),
    )
    # DECLARED read operations (sandloom.core.planner) that
    # the database adapter can compile to one generated SELECT run on the
    # OPTIMIZED executor (driver-level, BEGIN READ ONLY + tenant GUC in one
    # message) instead of the general ORM executor. Same guarantees layer.
    planner_optimized_executor: bool = Field(
        default=True,
        alias="PLANNER_OPTIMIZED_EXECUTOR",
        description=(
            "Allow eligible declared read operations to use the optimized "
            "database executor with the same tenant and transaction guarantees."
        ),
    )
    # Framework cap of one page of a declared ``ListOperation`` (a declaration
    # may set a lower ``max_limit``; a request above the cap is a 400).
    planner_list_max_limit: int = Field(
        default=500,
        ge=1,
        le=10_000,
        alias="PLANNER_LIST_MAX_LIMIT",
        description=(
            "Framework maximum page size for declared list operations; a "
            "declaration may impose a smaller limit."
        ),
    )
    # Server-side guards sent in the connection startup packet (zero extra
    # round trips) on the request/worker/contract pools — migrations use their
    # own connections and are unaffected. 0 = server default (off). Set both
    # to 0 behind a PgBouncer that rejects unknown startup parameters.
    db_statement_timeout_ms: int = Field(
        default=30_000,
        ge=0,
        alias="DB_STATEMENT_TIMEOUT_MS",
        description=(
            "PostgreSQL statement timeout in milliseconds on runtime pools; zero "
            "uses the server default. Does not configure migration connections."
        ),
    )
    db_idle_in_tx_timeout_ms: int = Field(
        default=60_000,
        ge=0,
        alias="DB_IDLE_IN_TX_TIMEOUT_MS",
        description=(
            "PostgreSQL idle-in-transaction timeout in milliseconds on runtime "
            "pools; zero uses the server default."
        ),
    )
