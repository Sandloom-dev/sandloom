"""Internal RPC authentication and authorization (ADR-0004)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import NoDecode


class RpcConfig(BaseModel):
    """Internal RPC authentication and authorization (ADR-0004)."""

    # Internal RPC authentication (ADR-0004, sandloom.core.rpc_pair_mac): each
    # request is authenticated by an HMAC under a per-pair key derived from
    # the caller's and the provider's X25519 keys (RPC_DH_PRIVATE_KEY /
    # RPC_SERVICE_DH_KEYS) — audience-bound, nonce-guarded — and authorized
    # against RPC_ALLOWED_CALLS (deny by default); commands also carry the
    # caller's Ed25519 signature (RPC_PRIVATE_KEY / RPC_SERVICE_KEYS).
    # Max |now - iat| of a request (seconds; both directions) and how long a
    # provider remembers a nonce (<= 2x this + 2).
    rpc_auth_max_skew_seconds: int = Field(
        default=60,
        ge=5,
        le=300,
        alias="RPC_AUTH_MAX_SKEW_SECONDS",
        description=(
            "Maximum accepted timestamp difference in seconds for internal RPC "
            "authentication; also bounds nonce lifetime."
        ),
    )
    # Nonce replay guard. Always: a per-process table
    # (RPC_REPLAY_CACHE_MAX_ENTRIES, fail closed when full of live nonces).
    # ``commands`` (default) also SET NX's command calls in the security
    # cache (shared by every replica / worker), ``all`` every call, ``local``
    # none. A cache outage fails closed in prod (503), open elsewhere.
    rpc_replay_guard: Literal["local", "commands", "all"] = Field(
        default="commands",
        alias="RPC_REPLAY_GUARD",
        description=(
            "Shared RPC replay protection: local keeps only per-process "
            "protection, commands also guards commands in the security cache, and "
            "all guards every call there."
        ),
    )
    rpc_replay_cache_max_entries: int = Field(
        default=200_000,
        ge=1_000,
        alias="RPC_REPLAY_CACHE_MAX_ENTRIES",
        description=(
            "Maximum live nonces in each process replay table; a full table fails "
            "closed."
        ),
    )
    # ...of which ONE caller may hold at most this many live nonces (an
    # allowed peer flooding the table only ever refuses itself). Sizing: a
    # nonce lives ~skew + 2 s (worst case 2 * skew + 2), so a caller needs
    # >= its peak calls/s per process x (2 * skew + 2); the default (half the
    # table) is ~830 calls/s per caller per process at skew 60. Alert on
    # sandloom_rpc_auth_rejected_total{reason="caller_quota"}.
    rpc_replay_cache_per_caller_max_entries: int = Field(
        default=100_000,
        ge=100,
        alias="RPC_REPLAY_CACHE_PER_CALLER_MAX_ENTRIES",
        description=(
            "Maximum live nonces one caller may occupy in the process replay table."
        ),
    )
    # COMMAND calls also carry and need the caller's Ed25519
    # signature (RPC_PRIVATE_KEY / RPC_SERVICE_KEYS), so a leaked PROVIDER
    # X25519 key — which can derive every caller's pair key (key-compromise
    # impersonation, ADR-0004) — can at worst forge reads, never effects.
    # ``auto`` = on in prod; ``off`` is refused under multi_tenant_strict;
    # ``all`` = every call (reads too: KCI-resistant reads, Ed25519 cost).
    rpc_commands_require_signature: Literal["auto", "on", "all", "off"] = Field(
        default="auto",
        alias="RPC_COMMANDS_REQUIRE_SIGNATURE",
        description=(
            "Ed25519 RPC signature policy: auto enables commands in production, "
            "on signs commands, all also signs reads, and off disables the extra "
            "signature requirement."
        ),
    )
    rpc_service_id: str | None = Field(
        default=None,
        alias="RPC_SERVICE_ID",
        description=(
            "This process service identity used for internal RPC authentication "
            "and authorization."
        ),
    )
    # This service's Ed25519 signing key (PEM; SECRETS_DIR file
    # ``rpc_private_key``) and its kid in RPC_SERVICE_KEYS[RPC_SERVICE_ID].
    rpc_private_key: str | None = Field(
        default=None,
        alias="RPC_PRIVATE_KEY",
        description=(
            "This service's Ed25519 PEM private key for signing internal RPC calls; "
            "separate from its X25519 key-agreement key."
        ),
    )
    rpc_key_id: str | None = Field(
        default=None,
        alias="RPC_KEY_ID",
        description=(
            "Key ID of RPC_PRIVATE_KEY in this service published Ed25519 keys."
        ),
    )
    # Public keys of every calling service (identical everywhere):
    # {"<service>": {"<kid>": "<Ed25519 PEM>"}} — several kids per service
    # while THAT service rotates (independently of the others).
    rpc_service_keys: Annotated[dict[str, dict[str, str]], NoDecode] = Field(
        default_factory=dict,
        alias="RPC_SERVICE_KEYS",
        description=(
            "JSON map of calling service IDs to key-ID/Ed25519-public-key maps; "
            "retain old keys during independent service rotations."
        ),
    )
    # This service's DEDICATED X25519 key-agreement key (request MAC and
    # response binding; PEM; SECRETS_DIR file ``rpc_dh_private_key``) — never
    # the Ed25519 signing key — and its kid in
    # RPC_SERVICE_DH_KEYS[RPC_SERVICE_ID]. Every service that serves or calls
    # RPC holds one (`sandloom keys generate --purpose rpc` emits both pairs).
    rpc_dh_private_key: str | None = Field(
        default=None,
        alias="RPC_DH_PRIVATE_KEY",
        description=(
            "This service's dedicated X25519 PEM private key for pair-MAC "
            "authentication and response binding."
        ),
    )
    rpc_dh_key_id: str | None = Field(
        default=None,
        alias="RPC_DH_KEY_ID",
        description=(
            "Key ID of RPC_DH_PRIVATE_KEY in this service published X25519 keys."
        ),
    )
    # Rotation: further X25519 private keys this provider
    # still / already accepts requests for, {"<kid>": "<PEM>"} (SECRETS_DIR
    # file ``rpc_dh_extra_private_keys``). Callers address a service's LAST
    # kid in RPC_SERVICE_DH_KEYS (docs/adr/0004, "Key rotation").
    rpc_dh_extra_private_keys: Annotated[dict[str, str], NoDecode] = Field(
        default_factory=dict,
        alias="RPC_DH_EXTRA_PRIVATE_KEYS",
        description=(
            "JSON map of additional X25519 private-key IDs to PEM keys accepted "
            "during this service key rotation."
        ),
    )
    # X25519 public keys of every service (identical everywhere), same shape
    # and rotation as RPC_SERVICE_KEYS: {"<service>": {"<kid>": "<PEM>"}}.
    rpc_service_dh_keys: Annotated[dict[str, dict[str, str]], NoDecode] = Field(
        default_factory=dict,
        alias="RPC_SERVICE_DH_KEYS",
        description=(
            "JSON map of service IDs to key-ID/X25519-public-key maps, shared "
            "consistently across services."
        ),
    )
    rpc_service_dh_current_kids: Annotated[dict[str, str], NoDecode] = Field(
        default_factory=dict,
        alias="RPC_SERVICE_DH_CURRENT_KIDS",
        description=(
            "JSON map selecting the current published X25519 key ID addressed for "
            "each service; absent entries use the greatest published key ID."
        ),
    )
    # Per-caller allowlist (identical everywhere; deny by default):
    # {"<service>": ["<contract>", ...]} or
    # {"<service>": {"<contract>": "*" | ["<method>", ...]}}. A caller's entry
    # must cover every contract its SERVICE_BINDINGS bind (startup check);
    # `sandloom keys allowlist` derives it from the services' SERVICE_BINDINGS.
    rpc_allowed_calls: Annotated[dict[str, Any], NoDecode] = Field(
        default_factory=dict,
        alias="RPC_ALLOWED_CALLS",
        description=(
            "Per-caller JSON allowlist of contract IDs and optional methods; "
            "omitted calls are denied and bindings must be covered."
        ),
    )
