"""Sync service contracts: remote bindings, the RPC client, compatibility
checks and the provider-side contract pool / dedupe.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from sandloom.core.config.replay_limits import REPLAY_RECORD_TTL_SECONDS


class ContractsConfig(BaseModel):
    """Sync service contracts: remote bindings, the RPC client, compatibility
    checks and the provider-side contract pool / dedupe.
    """

    # Service-registry remote bindings. When a requested service is not
    # registered in-process, CoreFacade returns a generated HTTP proxy for a
    # matching binding instead of forcing business code to know deployment
    # topology. Shape:
    #   {"tenancy.membership.v1": {"base_url": "http://tenancy:2222"}}
    # ``service_id`` (required) names the provider's RPC service identity:
    # every call's pair key derives from its X25519 key (published in
    # RPC_SERVICE_DH_KEYS) and its responses must be keyed by it. Optional
    # per-binding keys: timeout_seconds, critical (claims contributors) and
    # required — ``"required": true`` makes /ready fail while that peer is
    # unreachable (default false: an unreachable peer degrades the calls that
    # need it, never the caller's readiness/liveness).
    # Validated strictly at startup (unknown keys / bad URLs are errors).
    service_bindings: dict[str, dict[str, object]] = Field(
        default_factory=dict,
        alias="SERVICE_BINDINGS",
        description=(
            "JSON map of remote contract IDs to provider bindings, including "
            "base_url and service_id. Optional required bindings affect "
            "readiness; binding keys are startup-validated."
        ),
    )
    service_rpc_timeout_seconds: float = Field(
        default=1.0,
        alias="SERVICE_RPC_TIMEOUT_SECONDS",
        description=(
            "Default timeout in seconds for a remote contract call unless its "
            "binding overrides it."
        ),
    )
    # Client of internal RPC calls (sandloom.core.rpc_transport). ``aiohttp``
    # (default): aiohttp's pooled keep-alive client (~7x less caller CPU per
    # call than httpx, docs/SERVICE_RPC.md "Transport & performance").
    # ``httpx``: the previous httpx.AsyncClient (e.g. behind an egress proxy —
    # the aiohttp client ignores HTTP(S)_PROXY).
    rpc_transport: Literal["aiohttp", "httpx"] = Field(
        default="aiohttp",
        alias="RPC_TRANSPORT",
        description=(
            "Internal RPC HTTP client implementation. aiohttp is the default; "
            "httpx is available when its transport behavior is needed."
        ),
    )
    # aiohttp transport: max connections per peer (callers beyond it wait within
    # their timeout), idle keep-alive eviction (below uvicorn's default 5 s
    # keep-alive, so an idle connection is retired before the peer closes it)
    # and the largest accepted response body (a bigger one -> 502).
    rpc_pool_max_connections: int = Field(
        default=100,
        ge=1,
        alias="RPC_POOL_MAX_CONNECTIONS",
        description=(
            "Maximum aiohttp RPC connections per peer; additional callers wait "
            "within their call timeout."
        ),
    )
    rpc_pool_idle_seconds: float = Field(
        default=4.0,
        gt=0,
        alias="RPC_POOL_IDLE_SECONDS",
        description=(
            "Seconds an idle aiohttp RPC connection remains in the keep-alive "
            "pool before eviction."
        ),
    )
    rpc_max_response_bytes: int = Field(
        default=32 * 1024 * 1024,
        ge=1024,
        alias="RPC_MAX_RESPONSE_BYTES",
        description=(
            "Largest accepted internal RPC response body in bytes; larger "
            "responses are rejected."
        ),
    )
    rpc_max_request_bytes: int = Field(
        default=4 * 1024 * 1024,
        gt=0,
        alias="RPC_MAX_REQUEST_BYTES",
        description=("Largest accepted internal RPC request body in bytes."),
    )
    # Upper bound (seconds) for one readiness probe of a required remote
    # binding; probes run concurrently, so /ready never waits much longer.
    service_probe_timeout_seconds: float = Field(
        default=1.0,
        gt=0,
        alias="SERVICE_PROBE_TIMEOUT_SECONDS",
        description=(
            "Maximum seconds for each concurrent readiness probe of a required "
            "remote service binding."
        ),
    )

    # Packages whose pydantic models / Enums a sync contract may use in its
    # signature, besides ``sandloom.shared`` (always allowed). A contract DTO must
    # be importable by every caller process — a class inside a business module
    # package is not (that module is not loaded there), so over RPC it would
    # decode as a plain dict while a local call returns the model. Checked at
    # registration (ContractTypeError) and by ``sandloom check`` (contract-dtos).
    contract_dto_packages: list[str] = Field(
        default_factory=list,
        alias="CONTRACT_DTO_PACKAGES",
        description=(
            "Additional packages allowed to provide contract wire DTOs, beyond "
            "sandloom.shared; the same packages must be importable by every "
            "caller."
        ),
    )
    # Rolling-upgrade guard for remote contracts (sandloom.core.contract_compat):
    # the caller compares each bound provider's described contract with the
    # snapshot it was built against (the provider module's
    # ``contracts.snapshot.json``). ``ready`` (default): an incompatible
    # provider fails /ready with a precise diagnostic and is logged at
    # startup; ``strict``: an incompatible provider reachable at startup also
    # aborts startup; ``off``: no check.
    contract_compat_check: Literal["off", "ready", "strict"] = Field(
        default="ready",
        alias="CONTRACT_COMPAT_CHECK",
        description=(
            "Remote contract compatibility policy: ready degrades readiness, "
            "strict also aborts startup for reachable incompatible providers, and "
            "off skips the check."
        ),
    )
    # Extra contract snapshot files / directories consulted for the expected
    # shape of remote contracts (they win over the snapshots shipped inside
    # the provider modules' packages).
    contract_snapshot_paths: list[str] = Field(
        default_factory=list,
        alias="CONTRACT_SNAPSHOT_PATHS",
        description=(
            "Additional contract snapshot files or directories; these override "
            "expected snapshots shipped in provider packages."
        ),
    )

    # Local (in-process) contract calls open the provider's own Unit of Work on
    # a DEDICATED pool per database instance (same URL), so a request holding
    # its own connection never starves its nested contract calls / claims
    # contributors. Budget: + (size + overflow) × WEB_CONCURRENCY connections.
    contract_db_pool_size: int = Field(
        default=5,
        ge=1,
        alias="CONTRACT_DB_POOL_SIZE",
        description=(
            "Persistent connections per database in the dedicated local-contract "
            "pool, separate from the request pool."
        ),
    )
    contract_db_max_overflow: int = Field(
        default=5,
        ge=0,
        alias="CONTRACT_DB_MAX_OVERFLOW",
        description=(
            "Additional temporary connections allowed above CONTRACT_DB_POOL_SIZE."
        ),
    )
    # PostgreSQL lock_timeout of contract connections (0 = server default): a
    # contract that would wait on a row lock held by its caller fails fast.
    contract_db_lock_timeout_ms: int = Field(
        default=5000,
        ge=0,
        alias="CONTRACT_DB_LOCK_TIMEOUT_MS",
        description=(
            "PostgreSQL lock timeout in milliseconds on contract-pool "
            "connections; zero uses the server default."
        ),
    )
    # Request-scoped read scope for LOCAL read contracts: within one HTTP
    # request, read-contract calls on the same database reuse ONE contract-pool
    # connection/transaction (READ COMMITTED, always rolled back — never the
    # caller's transaction) and identical calls are memoised. The connection
    # goes back to the pool as soon as the request awaits anything else after
    # a read call (CONTRACT_READ_SCOPE_IDLE_MS=0, default), or that many ms
    # after the last read call.
    contract_read_scope_enabled: bool = Field(
        default=True,
        alias="CONTRACT_READ_SCOPE_ENABLED",
        description=(
            "Reuse one separate local read-contract transaction and memoize "
            "identical local read calls within a request; never joins the caller "
            "transaction."
        ),
    )
    contract_read_scope_idle_ms: int = Field(
        default=0,
        ge=0,
        alias="CONTRACT_READ_SCOPE_IDLE_MS",
        description=(
            "Delay in milliseconds before releasing an idle local-contract read "
            "scope; zero releases it when the request awaits unrelated work."
        ),
    )
    # SHADOW measurement of a potential caller-side cache of contract reads
    # (ADR-0003, sandloom.core.contract_cache_shadow). OFF by default ({}): nothing
    # is ever served from it — it only counts what a TTL cache WOULD have done
    # (sandloom_contract_cache_shadow_total, plus per-request share, time avoided
    # and stale decisions by kind). JSON {contract: {"ttls": [1, 5],
    # "invalidated_by": {channel: [payload fields]}, "rank": {answer: int}}}
    # ("rank" optional: tells a downgrade from an upgrade); set the same value
    # on every process (the provider announces invalidations, callers apply
    # them).
    contract_cache_shadow: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        alias="CONTRACT_CACHE_SHADOW",
        description=(
            "JSON specification of hypothetical contract-read caches and "
            "invalidations. Records measurements only; cached results are never "
            "served."
        ),
    )
    contract_cache_shadow_max_entries: int = Field(
        default=100_000,
        ge=1,
        alias="CONTRACT_CACHE_SHADOW_MAX_ENTRIES",
        description=("Maximum entries retained by contract-cache shadow measurement."),
    )
    # Directory for a JSON snapshot of the shadow counters at process exit
    # (benchmark runs); unset = no file.
    contract_cache_shadow_report_dir: str | None = Field(
        default=None,
        alias="CONTRACT_CACHE_SHADOW_REPORT_DIR",
        description=(
            "Directory for contract-cache shadow counter snapshots at process "
            "exit; unset writes no report file."
        ),
    )
    # How long a completed COMMAND contract's result is replayable for its
    # idempotency key (local and RPC-served calls share it). Bounded: 7 days.
    contract_command_dedupe_ttl_seconds: int = Field(
        default=REPLAY_RECORD_TTL_SECONDS,
        ge=1,
        le=7 * 24 * 60 * 60,
        alias="CONTRACT_COMMAND_DEDUPE_TTL_SECONDS",
        description=(
            "Seconds a completed command-contract result remains replayable for "
            "its idempotency key; must cover configured saga and retry horizons."
        ),
    )
