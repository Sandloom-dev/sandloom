"""Event bus transport, Streams retention, the outbox relay and event
guardrails.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class EventingConfig(BaseModel):
    """Event bus transport, Streams retention, the outbox relay and event
    guardrails.
    """

    # Event bus transport: auto = durable "streams" in prod, in-memory otherwise
    # (the volatile memory/redis drivers are refused in prod; see infra.config).
    event_bus_backend: Literal["auto", "memory", "redis", "streams"] = Field(
        default="auto", alias="EVENT_BUS_BACKEND"
    )
    # Logical service name of this process (e.g. "billing-api"). Informational:
    # it is stamped into Streams consumer ids, the group-ownership fingerprint
    # and the subscription inventory so operators can tell which deployment a
    # consumer belongs to. It never changes a consumer-group name (a module
    # keeps its group when it moves between services).
    sandloom_service_name: str | None = Field(
        default=None, alias="SANDLOOM_SERVICE_NAME"
    )
    # Streams retention. Every XADD trims the channel stream so it cannot grow
    # forever: entries older than EVENT_STREAM_RETENTION_SECONDS (MINID ~,
    # default 7 days) and, when EVENT_STREAM_MAXLEN > 0, beyond that many
    # entries (MAXLEN ~). 0 disables the respective bound. A consumer offline
    # longer than the retention window misses the trimmed events (the outbox
    # still holds them for a requeue).
    event_stream_retention_seconds: int = Field(
        default=7 * 24 * 60 * 60, ge=0, alias="EVENT_STREAM_RETENTION_SECONDS"
    )
    event_stream_maxlen: int = Field(default=0, ge=0, alias="EVENT_STREAM_MAXLEN")
    event_handler_timeout_seconds: float = Field(
        default=30.0, gt=0, alias="EVENT_HANDLER_TIMEOUT_SECONDS"
    )
    event_streams_reconnect_base_seconds: float = Field(
        default=0.5, gt=0, alias="EVENT_STREAMS_RECONNECT_BASE_SECONDS"
    )
    event_streams_reconnect_max_seconds: float = Field(
        default=30.0, gt=0, alias="EVENT_STREAMS_RECONNECT_MAX_SECONDS"
    )
    # Optional namespace for every Streams key (streams, DLQs, group-ownership
    # and inventory keys) so several environments can share one Redis, e.g.
    # "staging:". Changing it on a live deployment starts from empty streams.
    event_stream_prefix: str = Field(default="", alias="EVENT_STREAM_PREFIX")
    # Entries a Streams consumer reads per XREADGROUP. The batch's inbox
    # dedupe is one MGET, its marks one pipeline and its acks one XACK.
    event_stream_read_count: int = Field(
        default=64, ge=1, le=1000, alias="EVENT_STREAM_READ_COUNT"
    )

    # Transactional outbox relay: auto = run in prod processes only. "off" in
    # a prod app process is refused at startup unless SANDLOOM_ACCEPTED_RISKS has
    # "relay_disabled" (delivery would lose its retry). "delegated" is set by
    # the gunicorn config in every worker but the elected relay leader (the
    # process group still runs exactly one relay); never set it by hand.
    outbox_relay_enabled: Literal["auto", "on", "off", "delegated"] = Field(
        default="auto", alias="OUTBOX_RELAY_ENABLED"
    )

    # A row is dead-lettered (dead_at set) after this many failed publishes.
    outbox_relay_max_attempts: int = Field(
        default=10, ge=1, alias="OUTBOX_RELAY_MAX_ATTEMPTS"
    )
    # Per-row retry backoff: base * 2**(n-1) seconds (equal jitter), capped.
    outbox_relay_backoff_base_seconds: float = Field(
        default=2.0, ge=0, alias="OUTBOX_RELAY_BACKOFF_BASE_SECONDS"
    )
    outbox_relay_stall_seconds: float = Field(
        default=60.0, gt=0, alias="OUTBOX_RELAY_STALL_SECONDS"
    )
    outbox_relay_backoff_max_seconds: float = Field(
        default=300.0, ge=0, alias="OUTBOX_RELAY_BACKOFF_MAX_SECONDS"
    )
    # Broker-down circuit: exponentially growing relay cool-down, capped. An
    # outage does not consume rows' attempts.
    outbox_relay_circuit_base_seconds: float = Field(
        default=1.0, ge=0, alias="OUTBOX_RELAY_CIRCUIT_BASE_SECONDS"
    )
    outbox_relay_circuit_max_seconds: float = Field(
        default=30.0, ge=0, alias="OUTBOX_RELAY_CIRCUIT_MAX_SECONDS"
    )
    # Retention of DELIVERED outbox rows (history only once delivered): the
    # relay deletes them in bounded batches once older than this; dead-lettered
    # and pending rows are never purged. 0 = keep forever. `sandloom outbox purge`
    # runs the same purge on demand.
    outbox_retention_hours: float = Field(
        default=168.0, ge=0, alias="OUTBOX_RETENTION_HOURS"
    )

    # Guardrails (Phase K)
    event_callback_time_budget_seconds: float = Field(
        default=0.0,
        alias="EVENT_CALLBACK_TIME_BUDGET_SECONDS",
        description=(
            "If > 0, each event bus callback execution time is measured and a"
            " warning is logged when exceeded (non-fatal)."
        ),
    )
    event_payload_max_bytes: int = Field(
        default=0,
        alias="EVENT_PAYLOAD_MAX_BYTES",
        description=(
            "If > 0, serialized payload size is checked before publish;"
            " ValueError raised if exceeded."
        ),
    )
    event_shutdown_drain_seconds: float = Field(
        default=5.0,
        alias="EVENT_SHUTDOWN_DRAIN_SECONDS",
        description=(
            "Max seconds to wait for in-flight event callbacks to finish on"
            " bus shutdown before cancelling the stragglers. Bounds shutdown so"
            " a hung callback cannot hang the process. <= 0 waits unbounded."
        ),
    )

    # Event bus concurrency limit
    EVENT_BUS_REDIS_SEMAPHORE_LIMIT: int = Field(
        default=100,
        alias="EVENT_BUS_REDIS_SEMAPHORE_LIMIT",
        description=(
            "Max concurrent callbacks for RedisEventBus to prevent overload. "
            "Adjust based on CPU/network capacity."
        ),
    )

    # JSON Schema engine of event / contract payload validation
    # (sandloom.core.contracts.payload_validation): ``auto`` uses jsonschema-rs when
    # installed (sandloom[fast]), ``python`` forces the pure-Python
    # jsonschema package. Read when a schema is compiled, never at import.
    jsonschema_backend: Literal["auto", "python"] = Field(
        default="auto", alias="SANDLOOM_JSONSCHEMA_BACKEND"
    )

    @field_validator("jsonschema_backend", mode="before")
    @classmethod
    def _jsonschema_backend(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower() or "auto"
        return value

    # Projection consistency tokens (spec §6.4): HMAC-SHA256 keys by kid
    # (``{"<kid>": "<secret>"}``; a secret setting, rotated by adding a kid —
    # the newest kid signs, every listed kid verifies). Empty: no token is
    # issued and every presented token is ignored (reads stay eventual).
    projection_token_keys: dict[str, str] = Field(
        default_factory=dict, alias="PROJECTION_TOKEN_KEYS"
    )
    # How long a read presenting a token waits for the projection to catch up
    # before serving with SANDLOOM-Projection-Stale: true (on_stale="serve").
    projection_ryw_wait_ms: int = Field(
        default=300, ge=0, le=10_000, alias="PROJECTION_RYW_WAIT_MS"
    )

    # Schema registry persistence (Phase J extension)
    schema_registry_path: str = Field(
        default="./data/schema_registry.json",
        alias="SCHEMA_REGISTRY_PATH",
        description=(
            "Path to JSON file used to persist in-memory event schema registry. "
            "If empty, persistence is disabled. File will be created atomically "
            "on updates."
        ),
    )
