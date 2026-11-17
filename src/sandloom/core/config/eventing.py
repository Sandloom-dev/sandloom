"""Event bus transport, Streams retention, the outbox relay and event
guardrails.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from sandloom.core.config.removed_drivers import (
    REMOVED_DRIVERS,
    removed_driver_problem,
)

#: Default age bound of an event stream, in seconds (7 days): the one value of
#: the ``streams`` driver's ``retention_seconds`` option and the transport's own
#: default.
DEFAULT_EVENT_STREAM_RETENTION_SECONDS = 7 * 24 * 60 * 60


class EventingConfig(BaseModel):
    """Event bus transport, Streams retention, the outbox relay and event
    guardrails.
    """

    # Event bus transport: unset (None) = the composition's selection (no kernel
    # default, never memory); the volatile memory/broadcast drivers are refused
    # in prod (see infra.config). A driver name that was removed is refused by
    # name in the validator below (config.removed_drivers), not as a bare
    # "not a permitted value".
    event_bus_backend: Literal["memory", "broadcast", "streams"] | None = Field(
        default=None,
        alias="EVENT_BUS_BACKEND",
        description=(
            "Event transport selector: unset leaves the bus to the composition's "
            "selection (startup refuses when none names one); volatile "
            "production transports require accepted risk."
        ),
    )

    @field_validator("event_bus_backend", mode="before")
    @classmethod
    def _event_bus_backend(cls, value: object) -> object:
        # Compose passes ``${VAR:-}`` as an empty string: that means "unset".
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, str) and value.strip().lower() == "auto":
            raise ValueError(
                "EVENT_BUS_BACKEND=auto is no longer a value: unset "
                "EVENT_BUS_BACKEND to leave the bus to the composition's "
                "selection, or name a driver (memory, broadcast, streams)"
            )
        if isinstance(value, str):
            name = value.strip()
            if removed_driver_problem("eventbus", "default", name) is not None:
                new, release = REMOVED_DRIVERS[("eventbus", name)]
                raise ValueError(
                    f"EVENT_BUS_BACKEND={name}: the {name!r} event bus driver was "
                    f"removed in {release} and is now named {new!r}; set "
                    f"EVENT_BUS_BACKEND={new}, or in INFRA write "
                    f'{{"eventbus": {{"default": {{"driver": "{new}"}}}}}}'
                )
        return value

    # Logical service name of this process (e.g. "billing-api"). It names the
    # consumer group of the kernel's revocation applier,
    # sandloom:auth@tokens_revoked@<name> (ADR-0012 decision 5): every
    # replica of one service shares that group, and two services with one
    # name share it too and split revocations between them, so the name is
    # distinct per service and required on a shared event bus (the process
    # refuses to start without it). It is also stamped into Streams consumer
    # ids, the group-ownership fingerprint and the subscription inventory. It
    # never renames a module's consumer group (a module keeps its group when
    # it moves between services).
    sandloom_service_name: str | None = Field(
        default=None,
        alias="SANDLOOM_SERVICE_NAME",
        description=(
            "This service's name: required on a shared event bus and distinct "
            "per service, the same for all its replicas. Names the kernel "
            "revocation applier's consumer group "
            "(sandloom:auth@tokens_revoked@<name>) and labels the Streams "
            "consumer inventory and ownership diagnostics; never renames a "
            "module consumer group."
        ),
    )
    # Streams retention, the key namespace and the other bounds of a stream are
    # options of the ``streams`` driver (INFRA.eventbus.<name>: retention_seconds,
    # max_length, prefix): no setting of their own.
    event_handler_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        alias="EVENT_HANDLER_TIMEOUT_SECONDS",
        description=("Timeout in seconds for each Redis Streams event callback."),
    )
    event_streams_reconnect_base_seconds: float = Field(
        default=0.5,
        gt=0,
        alias="EVENT_STREAMS_RECONNECT_BASE_SECONDS",
        description=(
            "Initial delay in seconds between Redis Streams reconnect attempts."
        ),
    )
    event_streams_reconnect_max_seconds: float = Field(
        default=30.0,
        gt=0,
        alias="EVENT_STREAMS_RECONNECT_MAX_SECONDS",
        description=("Maximum Redis Streams reconnect backoff delay in seconds."),
    )
    # Entries a Streams consumer reads per XREADGROUP. The batch's inbox
    # dedupe is one MGET, its marks one pipeline and its acks one XACK.
    event_stream_read_count: int = Field(
        default=64,
        ge=1,
        le=1000,
        alias="EVENT_STREAM_READ_COUNT",
        description=("Maximum entries read in one Streams XREADGROUP batch."),
    )

    # Transactional outbox relay: auto = run in prod processes only. "off" in
    # a prod app process is refused at startup unless SANDLOOM_ACCEPTED_RISKS has
    # "relay_disabled" (delivery would lose its retry). "delegated" is set by
    # the gunicorn config in every worker but the elected relay leader (the
    # process group still runs exactly one relay); never set it by hand.
    outbox_relay_enabled: Literal["auto", "on", "off", "delegated"] = Field(
        default="auto",
        alias="OUTBOX_RELAY_ENABLED",
        description=(
            "Outbox relay policy: auto enables production relaying, on forces it, "
            "off disables it, and delegated is reserved for framework-elected "
            "worker leadership."
        ),
    )

    # A row is dead-lettered (dead_at set) after this many failed publishes.
    outbox_relay_max_attempts: int = Field(
        default=10,
        ge=1,
        alias="OUTBOX_RELAY_MAX_ATTEMPTS",
        description=(
            "Failed publish attempts before an outbox row is dead-lettered; "
            "broker-outage circuit pauses do not consume attempts."
        ),
    )
    # Per-row retry backoff: base * 2**(n-1) seconds (equal jitter), capped.
    outbox_relay_backoff_base_seconds: float = Field(
        default=2.0,
        ge=0,
        alias="OUTBOX_RELAY_BACKOFF_BASE_SECONDS",
        description=(
            "Base per-row outbox retry delay in seconds, increased exponentially "
            "with jitter."
        ),
    )
    outbox_relay_stall_seconds: float = Field(
        default=60.0,
        gt=0,
        alias="OUTBOX_RELAY_STALL_SECONDS",
        description=(
            "Seconds without local relay progress before relay health reports a stall; "
            "idle polling is counted as progress."
        ),
    )
    outbox_relay_backoff_max_seconds: float = Field(
        default=300.0,
        ge=0,
        alias="OUTBOX_RELAY_BACKOFF_MAX_SECONDS",
        description=("Maximum per-row outbox retry backoff in seconds."),
    )
    # Broker-down circuit: exponentially growing relay cool-down, capped. An
    # outage does not consume rows' attempts.
    outbox_relay_circuit_base_seconds: float = Field(
        default=1.0,
        ge=0,
        alias="OUTBOX_RELAY_CIRCUIT_BASE_SECONDS",
        description=(
            "Initial relay cooldown in seconds when the broker is unavailable."
        ),
    )
    outbox_relay_circuit_max_seconds: float = Field(
        default=30.0,
        ge=0,
        alias="OUTBOX_RELAY_CIRCUIT_MAX_SECONDS",
        description=("Maximum broker-outage relay cooldown in seconds."),
    )
    # Retention of DELIVERED outbox rows (history only once delivered): the
    # relay deletes them in bounded batches once older than this; dead-lettered
    # and pending rows are never purged. 0 = keep forever. `sandloom outbox purge`
    # runs the same purge on demand.
    outbox_retention_hours: float = Field(
        default=168.0,
        ge=0,
        alias="OUTBOX_RETENTION_HOURS",
        description=(
            "Retention of delivered outbox history in hours; zero keeps it "
            "forever. Pending and dead-lettered rows are not purged."
        ),
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
    EVENT_BUS_BROADCAST_SEMAPHORE_LIMIT: int = Field(
        default=100,
        ge=1,
        alias="EVENT_BUS_BROADCAST_SEMAPHORE_LIMIT",
        description=(
            "Max concurrent callbacks for BroadcastEventBus to prevent overload. "
            "Adjust based on CPU/network capacity. At least 1."
        ),
    )

    # JSON Schema engine of event / contract payload validation
    # (sandloom.core.contracts.payload_validation): ``auto`` uses jsonschema-rs when
    # installed (sandloom[fast]), ``python`` forces the pure-Python
    # jsonschema package. Read when a schema is compiled, never at import.
    jsonschema_backend: Literal["auto", "python"] = Field(
        default="auto",
        alias="SANDLOOM_JSONSCHEMA_BACKEND",
        description=(
            "Payload schema validator: auto uses jsonschema-rs when installed, "
            "otherwise Python jsonschema; python forces the Python "
            "implementation."
        ),
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
        default_factory=dict,
        alias="PROJECTION_TOKEN_KEYS",
        description=(
            "JSON map of key IDs to HMAC secrets for projection consistency "
            "tokens; empty disables issuing and honoring these tokens."
        ),
    )
    # How long a read presenting a token waits for the projection to catch up
    # before serving with SANDLOOM-Projection-Stale: true (on_stale="serve").
    projection_ryw_wait_ms: int = Field(
        default=300,
        ge=0,
        le=10_000,
        alias="PROJECTION_RYW_WAIT_MS",
        description=(
            "Maximum milliseconds a token-bearing projection read waits to catch "
            "up before returning its configured stale result behavior."
        ),
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
