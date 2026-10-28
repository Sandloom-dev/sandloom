"""Configuration tiers: which settings change a guarantee, and who decides.

Every field of :class:`sandloom.core.config.settings.Settings` (and every driver-read
``INFRA`` instance option that changes a guarantee) belongs to exactly ONE
tier, recorded here and pinned by ``src/sandloom/core/tests/test_config_tiers.py``:

``semantic``
    The value changes a guarantee or a security property (fail closed vs
    open, which RPC caller authentication runs, whether a weaker backend is
    admitted in prod). The effective value of each one is defined PER PROFILE
    in :data:`SEMANTIC` — ``dev`` / ``test`` / ``prod`` / ``prod_strict``
    (``ENVIRONMENT=prod`` + ``PRODUCTION_PROFILE=multi_tenant_strict``) — so
    the trade-off is chosen by the framework, not exported to every operator.
    Moving one AWAY from its profile value (towards the weaker end of its
    ``strength`` order) is an *override*: logged as a WARNING at every
    production start, counted in ``sandloom_config_override_active``, listed by
    ``/diagnostics/core`` and ``sandloom doctor``. Where the profile already
    refuses a value (``refused``) the settings validator / startup check
    still refuses it — this module only describes and reports.
``ops``
    Capacity and operations: pools, timeouts, TTLs, sizes, intervals,
    log / metrics knobs, endpoints, credentials, backend selection (the
    escape hatch that admits a weaker backend in prod is the semantic
    ``SANDLOOM_ACCEPTED_RISKS`` list), and product policy the framework has no
    opinion on (``BILLING_DENY_ON_PAST_DUE``).

There is no migration tier and no deprecated value: the framework carries no
transitional flags (it is not released; a change of behaviour removes the old
one). Every weakening a production deployment ACCEPTS goes through ONE
semantic setting, ``SANDLOOM_ACCEPTED_RISKS``
(:mod:`sandloom.core.config.accepted_risks`):
each accepted risk is reported like an override (``kind="accepted_risk"``).
Settings that were REMOVED (:data:`REMOVED`) fail startup when still set to a
non-inert value and warn when set to the inert one.

The number of semantic settings is ratcheted: it may only go down, and a new
one needs an ``adr=`` reference (``test_config_tiers.py``). The generated
reference ``docs/reference/configuration-tiers.md`` (``render_markdown``)
shows the profile table.

Pure data + pure functions of a settings object: no I/O, no import of
``sandloom.core.config.settings`` (the settings validator and the docs build both use
it).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from sandloom.core.config import accepted_risks

SEMANTIC_TIER = "semantic"
OPS_TIER = "ops"
Tier = Literal["semantic", "ops"]

#: Profiles a semantic value is defined for, in table order.
PROFILES = ("dev", "test", "prod", "prod_strict")

#: Profile value meaning "no framework opinion" (a deployment / product choice).
NO_OPINION: object = object()

OverrideKind = Literal["weaker", "accepted_risk"]


def profile_of(cfg: Any) -> str:
    """The configuration profile ``cfg`` runs under."""
    env = getattr(getattr(cfg, "environment", None), "value", None) or str(
        getattr(cfg, "environment", "dev")
    )
    if env == "prod":
        strict = getattr(cfg, "production_profile", "standard") == "multi_tenant_strict"
        return "prod_strict" if strict else "prod"
    return "test" if env == "test" else "dev"


# ------------------------------------------------------------- predicates ----


def _is_prod(cfg: Any) -> bool:
    return bool(getattr(cfg, "is_prod", False))


def _rpc_calls(cfg: Any) -> bool:
    """This process calls remote contracts (it has SERVICE_BINDINGS)."""
    return bool(getattr(cfg, "service_bindings", None))


def _rpc_in_use(cfg: Any) -> bool:
    """This process calls internal RPC or holds the key to serve it."""
    return _rpc_calls(cfg) or bool(getattr(cfg, "rpc_dh_private_key", None))


# ------------------------------------------------------- effective values ----


def _attr(name: str) -> Callable[[Any], object]:
    def get(cfg: Any) -> object:
        value = getattr(cfg, name, None)
        return getattr(value, "value", value)

    return get


def _outside_prod(name: str) -> Callable[[Any], object]:
    """A dev-only opt-in: never honoured in prod, so prod's value is False."""

    def get(cfg: Any) -> object:
        return bool(getattr(cfg, name, False)) and not _is_prod(cfg)

    return get


def _revocation(cfg: Any) -> object:
    explicit = getattr(cfg, "auth_revocation_fail_closed", None)
    return explicit if explicit is not None else _is_prod(cfg)


def _docs(cfg: Any) -> object:
    explicit = getattr(cfg, "docs_enabled", None)
    return explicit if explicit is not None else not _is_prod(cfg)


def _command_signatures(cfg: Any) -> object:
    value = getattr(cfg, "rpc_commands_require_signature", "auto")
    if value == "auto":
        return "on" if _is_prod(cfg) else "off"
    return value


def _relay(cfg: Any) -> object:
    mode = getattr(cfg, "outbox_relay_enabled", "auto")
    if mode == "delegated":  # another gunicorn worker of the group runs it
        return "on"
    if mode == "auto":
        return "on" if _is_prod(cfg) else "off"
    return mode


def _risk_list(cfg: Any) -> object:
    return tuple(sorted(accepted_risks.effective(cfg)))


def _security_cache(cfg: Any) -> object:
    name = getattr(cfg, "security_cache_instance", "default") or "default"
    return "shared" if name == "default" else "separate"


# ------------------------------------------------------------------ specs ----


@dataclass(frozen=True, slots=True)
class SemanticSetting:
    """A setting whose value changes a guarantee, defined per profile."""

    name: str
    why: str
    #: profile -> effective value the framework chooses (``NO_OPINION`` = a
    #: deployment choice). Exactly the CURRENT defaults: a test builds each
    #: profile's default settings and expects no override.
    profile: Mapping[str, object]
    #: weakest -> strongest; only a move towards the weaker end is an
    #: override (empty: any difference is one).
    strength: tuple[object, ...] = ()
    #: profiles where a value weaker than the profile's is refused at startup
    #: (by the settings validator / a startup check — not by this module).
    refused: frozenset[str] = frozenset()
    #: effective value of ``cfg`` (default: the field itself).
    effective: Callable[[Any], object] | None = None
    #: the setting matters for ``cfg`` at all (default: always).
    applies: Callable[[Any], bool] | None = None
    #: ADR admitting a semantic setting added above the ratchet baseline.
    adr: str | None = None
    #: a profile SELECTOR (ENVIRONMENT / PRODUCTION_PROFILE): never an override.
    selector: bool = False
    #: an ``INFRA`` instance option ("<capability>.<option>"), not a field.
    infra_option: bool = False
    #: the accepted-risk id that admits a weaker value in prod: while it is
    #: accepted the weaker value is reported as that risk, not as an override.
    risk: str | None = None
    #: the ``SANDLOOM_ACCEPTED_RISKS`` list itself (reported one risk at a time).
    risk_list: bool = False

    def value(self, cfg: Any) -> object:
        if self.effective is not None:
            return self.effective(cfg)
        return _attr(self.name.lower())(cfg)

    def weaker(self, value: object, than: object) -> bool:
        if value == than:
            return False
        if value in self.strength and than in self.strength:
            return self.strength.index(value) < self.strength.index(than)
        return not self.strength


@dataclass(frozen=True, slots=True)
class RemovedSetting:
    """A setting that no longer exists; still configuring it is reported.

    Set to its ``inert`` value (today's only behaviour) it logs a warning;
    any other value fails startup — that behaviour is gone.
    """

    name: str
    removed_in: str
    hint: str
    inert: object = False


_BOOL_WEAK_TRUE = (True, False)  # True is the weaker value
_BOOL_WEAK_FALSE = (False, True)  # False is the weaker value


def _all(value: object, **overrides: object) -> dict[str, object]:
    table = dict.fromkeys(PROFILES, value)
    table.update(overrides)
    return table


SEMANTIC: tuple[SemanticSetting, ...] = (
    # ---- profile selectors
    SemanticSetting(
        "ENVIRONMENT",
        "Selects the profile: prod flips every fail-open default to closed.",
        profile={"dev": "dev", "test": "test", "prod": "prod", "prod_strict": "prod"},
        selector=True,
    ),
    SemanticSetting(
        "PRODUCTION_PROFILE",
        "multi_tenant_strict turns the multi-tenant trust model into startup checks.",
        profile=_all("standard", prod_strict="multi_tenant_strict"),
        selector=True,
    ),
    # ---- the one place prod accepts a weaker guarantee
    SemanticSetting(
        "SANDLOOM_ACCEPTED_RISKS",
        "Stable ids of the weaker guarantees this deployment accepts "
        "(sandloom.core.config.accepted_risks); each is reported at every start.",
        profile=_all(()),
        effective=_risk_list,
        risk_list=True,
        adr="docs/adr/0006-accepted-risks.md",
    ),
    # ---- tenancy
    SemanticSetting(
        "TENANCY_MODE",
        "single binds one canonical tenant; the strict profile isolates many.",
        profile=_all(NO_OPINION, prod_strict="multi"),
        strength=("single", "multi"),
        refused=frozenset({"prod_strict"}),
    ),
    SemanticSetting(
        "database.tenant_rls",
        "PostgreSQL Row-Level Security under the ORM tenant guard.",
        profile=_all(NO_OPINION, prod_strict=True),
        strength=_BOOL_WEAK_FALSE,
        refused=frozenset({"prod_strict"}),
        infra_option=True,
    ),
    SemanticSetting(
        "SECURITY_CACHE_INSTANCE",
        "Revocation marks / blocks / idempotency on a separate non-evicting Redis.",
        profile=_all("shared", prod_strict="separate"),
        strength=("shared", "separate"),
        refused=frozenset({"prod_strict"}),
        effective=_security_cache,
    ),
    # ---- authentication / tokens
    SemanticSetting(
        "AUTH_REVOCATION_FAIL_CLOSED",
        "A revocation-store outage rejects tokens (503) instead of accepting them.",
        profile={"dev": False, "test": False, "prod": True, "prod_strict": True},
        strength=_BOOL_WEAK_FALSE,
        refused=frozenset({"prod_strict"}),
        effective=_revocation,
    ),
    # ---- internal RPC (always pair-MAC authenticated, ADR-0004)
    SemanticSetting(
        "RPC_COMMANDS_REQUIRE_SIGNATURE",
        "Pair-MAC COMMAND calls also carry the caller's Ed25519 signature (KCI).",
        profile={"dev": "off", "test": "off", "prod": "on", "prod_strict": "on"},
        strength=("off", "on", "all"),
        refused=frozenset({"prod_strict"}),
        effective=_command_signatures,
        applies=_rpc_in_use,
    ),
    SemanticSetting(
        "RPC_REPLAY_GUARD",
        "Pair-MAC nonce replay guard shared across replicas (commands / all).",
        profile=_all("commands"),
        strength=("local", "commands", "all"),
        applies=_rpc_in_use,
    ),
    SemanticSetting(
        "CONTRACT_COMPAT_CHECK",
        "Rolling-upgrade check of bound providers' contracts against snapshots.",
        profile=_all("ready"),
        strength=("off", "ready", "strict"),
        refused=frozenset({"prod_strict"}),
        applies=_rpc_calls,
    ),
    # ---- exposure
    SemanticSetting(
        "INTERNAL_ENDPOINTS_OPEN_IN_DEV",
        "Opens /diagnostics, /_sandloom/rpc, /metrics without a token (never in prod).",
        profile=_all(False),
        strength=_BOOL_WEAK_TRUE,
        effective=_outside_prod("internal_endpoints_open_in_dev"),
    ),
    SemanticSetting(
        "DOCS_ENABLED",
        "Serves /docs, /redoc and /openapi.json.",
        profile={"dev": True, "test": True, "prod": False, "prod_strict": False},
        strength=_BOOL_WEAK_TRUE,
        effective=_docs,
    ),
    # ---- events / contracts
    SemanticSetting(
        "OUTBOX_RELAY_ENABLED",
        "The outbox relay retries undelivered events (at-least-once); off in "
        "a prod app process needs the accepted risk relay_disabled.",
        profile={"dev": "off", "test": "off", "prod": "on", "prod_strict": "on"},
        strength=("off", "on"),
        refused=frozenset({"prod", "prod_strict"}),
        effective=_relay,
        risk="relay_disabled",
        adr="docs/adr/0006-accepted-risks.md",
    ),
    # ---- billing policy
    SemanticSetting(
        "BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS",
        "A subscription without an operation intent grants entitlements.",
        profile=_all(False),
        strength=_BOOL_WEAK_TRUE,
    ),
    # ---- transport
    SemanticSetting(
        "SMTP_USE_TLS",
        "STARTTLS / TLS to the SMTP server (credentials in clear without it).",
        profile=_all(True),
        strength=_BOOL_WEAK_FALSE,
    ),
)


#: Settings removed from the framework (no longer ``Settings`` fields).
REMOVED: tuple[RemovedSetting, ...] = (
    RemovedSetting(
        "SCHEMA_REQUIRE_ALL_VERSIONED",
        "0.8",
        "every publish to a versioned channel needs a registered schema in "
        "every profile (dev = prod; SANDLOOM-R003)",
        inert=True,
    ),
    RemovedSetting(
        "LEGACY_UNSCOPED_EFFECTS",
        "0.8",
        "publish_event / record_audit outside a Unit of Work always raise "
        "unless the call passes required=False",
    ),
    RemovedSetting(
        "DB_LAZY_BEGIN",
        "0.8",
        "SELECTs always run in the UoW transaction; declare ReadOnlyDbSession "
        "(DB_READ_ONLY_SESSIONS) or a PlannedRead for optimised reads",
    ),
    RemovedSetting(
        "SCHEMA_VALIDATION_SOFT",
        "0.8",
        "an off-schema event is always refused before the outbox (dev = prod)",
    ),
    RemovedSetting(
        "MODULE_REQUIRE_LISTED",
        "0.8",
        "an explicitly listed module is required unless MODULE_REQUIRED names "
        "the modules that must load",
        inert=True,
    ),
    RemovedSetting(
        "BEAT_LEASE_ENABLED",
        "0.8",
        "the beat singleton lease is always on (BEAT_LEASE_CACHE_INSTANCE must "
        "be a cache with the 'cas' feature)",
        inert=True,
    ),
    RemovedSetting(
        "CONTRACT_READ_SCOPE_MEMO",
        "0.8",
        "read contracts are memoised per request; opt out per scope with "
        "contract_read_scope(memo=False)",
        inert=True,
    ),
    RemovedSetting(
        "BILLING_FAKE_ALLOW_UNSIGNED_WEBHOOKS",
        "0.8",
        "the fake PSP always rejects unsigned webhooks; sign test payloads with "
        "sandloom_billing.psp.sign_stripe_payload",
    ),
    RemovedSetting(
        "BILLING_ALLOW_FAKE_PSP_IN_PROD",
        "0.8",
        'accept the risk explicitly: SANDLOOM_ACCEPTED_RISKS=["fake_psp_in_prod"]',
    ),
    RemovedSetting(
        "NOTIFIER_ALLOW_LOG_IN_PROD",
        "0.8",
        'accept the risk explicitly: SANDLOOM_ACCEPTED_RISKS=["log_notifier_in_prod"]',
    ),
    RemovedSetting(
        "STORAGE_ALLOW_LOCAL_IN_PROD",
        "0.8",
        'accept the risk explicitly: SANDLOOM_ACCEPTED_RISKS=["local_storage_in_prod"]',
    ),
    RemovedSetting(
        "SANDLOOM_ALLOW_VOLATILE_CONSUMERS",
        "0.8",
        'accept the risk explicitly: SANDLOOM_ACCEPTED_RISKS=["volatile_event_bus"]',
    ),
    RemovedSetting(
        "SANDLOOM_ALLOW_UNVERSIONED_DDL_REPAIR",
        "0.8",
        (
            "accept the risk explicitly: "
            'SANDLOOM_ACCEPTED_RISKS=["unversioned_ddl_repair"]'
        ),
    ),
    RemovedSetting(
        "PASSWORD_HASH_ALLOW_WEAK",
        "0.8",
        'accept the risk explicitly: SANDLOOM_ACCEPTED_RISKS=["weak_password_hash"]',
    ),
    RemovedSetting(
        "JWT_ALLOW_SHARED_SECRET_SPLIT",
        "0.8",
        (
            "accept the risk explicitly: "
            'SANDLOOM_ACCEPTED_RISKS=["shared_hs_secret_split"]'
        ),
    ),
    RemovedSetting(
        "RPC_AUTH_MODE",
        "0.8",
        "internal RPC is always pair-MAC authenticated (ADR-0004): give every "
        "service RPC_DH_PRIVATE_KEY / RPC_SERVICE_DH_KEYS (and RPC_PRIVATE_KEY "
        "to sign commands); INTERNAL_API_TOKEN only guards the ops endpoints",
        inert="service_mac",
    ),
    RemovedSetting(
        "RPC_RESPONSE_BINDING",
        "0.8",
        "every RPC response is keyed to its request and pinned provider "
        "(the former strict mode)",
        inert="strict",
    ),
    RemovedSetting(
        "AUTH_REVOCATION_STRICT_PATH_PREFIXES",
        "1.0",
        "every authenticated request reads the subject-state record live "
        "(ADR-0009): there is no staleness budget left to override",
        inert=[],
    ),
    RemovedSetting(
        "MODULE_FS_SCAN",
        "0.7",
        "modules are no longer packages under sandloom.*: list them in "
        "SANDLOOM_MODULES (import path or `sandloom.modules` entry-point name) "
        "or set MODULE_LOAD_ENTRYPOINTS=true to load every installed module",
    ),
)

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})


def removed_setting_problem(removed: RemovedSetting, raw: object) -> tuple[str, bool]:
    """(message, fatal) for a REMOVED setting configured with ``raw``."""
    text = str(raw).strip().lower()
    value: object = raw
    if isinstance(removed.inert, list):
        value = [] if text in ("", "[]") or raw == [] else raw
    elif text in _TRUE:
        value = True
    elif text in _FALSE:
        value = False
    fatal = value != removed.inert
    lead = f"{removed.name} was removed in {removed.removed_in}" + (
        f" and {removed.name}={raw} is no longer supported" if fatal else ""
    )
    return (
        f"{lead}: {removed.hint}; drop it from the environment "
        "(docs/guides/upgrading-0.8.md)",
        fatal,
    )


#: Every other ``Settings`` field: capacity / operations.
OPS: frozenset[str] = frozenset(
    [
        "SANDLOOM_MODULES",
        "MODULE_WHITELIST",
        "MODULE_BLACKLIST",
        "MODULE_FAIL_FAST",
        "MODULE_LOAD_ENTRYPOINTS",
        "MODULE_REQUIRED",
        "STARTUP_TASK_TIMEOUT_SECONDS",
        "SHUTDOWN_TASK_TIMEOUT_SECONDS",
        "SHUTDOWN_BUDGET_SECONDS",
        "MIGRATION_LOCK_TIMEOUT_SECONDS",
        "BEAT_LEASE_TTL_SECONDS",
        "BEAT_LEASE_NAME",
        "BEAT_LEASE_CACHE_INSTANCE",
        "GC_FREEZE_AFTER_STARTUP",
        "GC_THRESHOLDS",
        "GC_METRICS_ENABLED",
        "HEALTH_CHECK_TIMEOUT_SECONDS",
        "HEALTH_CACHE_TTL_SECONDS",
        "CONSUMER_STARTUP_TIMEOUT_SECONDS",
        "CONSUMER_RETRY_BASE_SECONDS",
        "CONSUMER_RETRY_MAX_SECONDS",
        "EVENT_BUS_BACKEND",
        "SANDLOOM_SERVICE_NAME",
        "EVENT_STREAM_RETENTION_SECONDS",
        "PROJECTION_TOKEN_KEYS",
        "PROJECTION_RYW_WAIT_MS",
        "EVENT_STREAM_MAXLEN",
        "EVENT_HANDLER_TIMEOUT_SECONDS",
        "EVENT_STREAMS_RECONNECT_BASE_SECONDS",
        "EVENT_STREAMS_RECONNECT_MAX_SECONDS",
        "EVENT_STREAM_PREFIX",
        "EVENT_STREAM_READ_COUNT",
        "SANDLOOM_COMPOSITION_ROOT",
        "OUTBOX_RELAY_MAX_ATTEMPTS",
        "OUTBOX_RELAY_BACKOFF_BASE_SECONDS",
        "OUTBOX_RELAY_STALL_SECONDS",
        "OUTBOX_RELAY_BACKOFF_MAX_SECONDS",
        "OUTBOX_RELAY_CIRCUIT_BASE_SECONDS",
        "OUTBOX_RELAY_CIRCUIT_MAX_SECONDS",
        "OUTBOX_RETENTION_HOURS",
        "SAGA_SWEEP_INTERVAL_SECONDS",
        "SAGA_SWEEP_BATCH",
        "SAGA_COMPENSATION_DEADLINE_SECONDS",
        "SAGA_RETRY_BASE_SECONDS",
        "SAGA_RETRY_MAX_SECONDS",
        "SAGA_RETENTION_DAYS",
        "INFRA",
        "SERVICE_BINDINGS",
        "SERVICE_RPC_TIMEOUT_SECONDS",
        "RPC_TRANSPORT",
        "RPC_POOL_MAX_CONNECTIONS",
        "RPC_POOL_IDLE_SECONDS",
        "RPC_MAX_RESPONSE_BYTES",
        "RPC_MAX_REQUEST_BYTES",
        "SERVICE_PROBE_TIMEOUT_SECONDS",
        "CONTRACT_DTO_PACKAGES",
        "CONTRACT_SNAPSHOT_PATHS",
        "ACCESS",
        "IDEMPOTENCY_CACHE_INSTANCE",
        "SINGLE_TENANT_ID",
        "DATABASE_URL",
        "DATABASE_SYSTEM_URL",
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "DB_POOL_TIMEOUT",
        "DB_CONNECT_TIMEOUT_SECONDS",
        "DB_RECONNECT_BASE_SECONDS",
        "DB_RECONNECT_MAX_SECONDS",
        "DB_POOL_OVERFLOW_LINGER_SECONDS",
        "DB_POOL_FAIR_BARGE_MS",
        "DB_POOL_PING_IDLE_SECONDS",
        "DB_DEAD_CONNECTION_RETRY",
        "DB_READ_ONLY_SESSIONS",
        "DB_RLS_MERGED_BEGIN",
        "DB_SESSION_OWNER_CHECKS",
        "PLANNER_OPTIMIZED_EXECUTOR",
        "PLANNER_LIST_MAX_LIMIT",
        "DB_STATEMENT_TIMEOUT_MS",
        "DB_IDLE_IN_TX_TIMEOUT_MS",
        "CONTRACT_DB_POOL_SIZE",
        "CONTRACT_DB_MAX_OVERFLOW",
        "CONTRACT_DB_LOCK_TIMEOUT_MS",
        "CONTRACT_READ_SCOPE_ENABLED",
        "CONTRACT_READ_SCOPE_IDLE_MS",
        "CONTRACT_CACHE_SHADOW",
        "CONTRACT_CACHE_SHADOW_MAX_ENTRIES",
        "CONTRACT_CACHE_SHADOW_REPORT_DIR",
        "CONTRACT_COMMAND_DEDUPE_TTL_SECONDS",
        "CACHE_URL",
        "BROKER_URL",
        "CACHE_SOCKET_TIMEOUT_SECONDS",
        "CACHE_SOCKET_CONNECT_TIMEOUT_SECONDS",
        "CACHE_HEALTH_CHECK_INTERVAL_SECONDS",
        "CACHE_MAX_CONNECTIONS",
        "CACHE_POOL_TIMEOUT_SECONDS",
        "CACHE_RETRY_ATTEMPTS",
        "STORAGE_TYPE",
        "STORAGE_PATH",
        "MAX_UPLOAD_SIZE_BYTES",
        "MAX_REQUEST_BODY_BYTES",
        "NOTIFIER_BACKEND",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_FROM",
        "S3_BUCKET",
        "S3_REGION",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "JWT_SECRET",
        "JWT_ALGORITHM",
        "JWT_PRIVATE_KEY",
        "JWT_PUBLIC_KEYS",
        "ACCESS_TOKEN_EXPIRE_MINUTES",
        "REFRESH_TOKEN_EXPIRE_DAYS",
        "MAX_SESSION_DAYS",
        "TENANT_CLAIMS_MAX_ORGANIZATIONS",
        "AUTH_REVOCATION_MARK_TTL_SECONDS",
        "JWT_ISSUER",
        "JWT_AUDIENCE",
        "JWT_LEEWAY_SECONDS",
        "JWT_PREVIOUS_SECRETS",
        "JWT_KID",
        "AUTH_REVOCATION_CLOCK_SKEW_SECONDS",
        "AUTH_REVOCATION_WRITE_ATTEMPTS",
        "AUTH_REVOCATION_RETRY_BASE_SECONDS",
        "PASSWORD_HASH_CONCURRENCY",
        "PASSWORD_HASH_PROFILE",
        "PASSWORD_HASH_TIME_COST",
        "PASSWORD_HASH_MEMORY_KIB",
        "PASSWORD_HASH_PARALLELISM",
        "FRONTEND_URL",
        "CORS_ORIGINS",
        "INTERNAL_API_TOKEN",
        "RPC_AUTH_MAX_SKEW_SECONDS",
        "RPC_REPLAY_CACHE_MAX_ENTRIES",
        "RPC_REPLAY_CACHE_PER_CALLER_MAX_ENTRIES",
        "RPC_SERVICE_ID",
        "RPC_PRIVATE_KEY",
        "RPC_KEY_ID",
        "RPC_SERVICE_KEYS",
        "RPC_DH_PRIVATE_KEY",
        "RPC_DH_KEY_ID",
        "RPC_DH_EXTRA_PRIVATE_KEYS",
        "RPC_SERVICE_DH_KEYS",
        "RPC_SERVICE_DH_CURRENT_KIDS",
        "RPC_ALLOWED_CALLS",
        "PLANNER_CURSOR_SECRET",
        "METRICS_TOKEN",
        "TRUST_PROXY_HEADERS",
        "TRUSTED_PROXY_IPS",
        "RATE_LIMIT_ENABLED",
        "RATE_LIMIT_GLOBAL_REQUESTS",
        "RATE_LIMIT_GLOBAL_PERIOD_SECONDS",
        "RATE_LIMIT_GLOBAL_EXEMPT_PATH_PREFIXES",
        "TWOFA_ENCRYPTION_KEY",
        "BILLING_PROVIDER",
        "BILLING_DENY_ON_PAST_DUE",
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "BILLING_INTENT_RECONCILE_AFTER_MINUTES",
        "BILLING_INTENT_RECONCILE_MAX_ATTEMPTS",
        "BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
        "MICROSOFT_CLIENT_ID",
        "MICROSOFT_CLIENT_SECRET",
        "LINKEDIN_CLIENT_ID",
        "LINKEDIN_CLIENT_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "SSO_ALLOWED_REDIRECT_URIS",
        "SENTRY_DSN",
        "SENTRY_TRACES_SAMPLE_RATE",
        "TRACING_URL",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_SERVICE_NAME",
        "LOG_LEVEL",
        "LOG_FORMAT",
        "EVENT_CALLBACK_TIME_BUDGET_SECONDS",
        "EVENT_PAYLOAD_MAX_BYTES",
        "EVENT_SHUTDOWN_DRAIN_SECONDS",
        "EVENT_BUS_REDIS_SEMAPHORE_LIMIT",
        "SCHEMA_REGISTRY_PATH",
        "SANDLOOM_JSONSCHEMA_BACKEND",
    ]
)

#: Process environment read outside ``Settings`` (entrypoints, tooling): ops.
RAW_ENV_OPS: tuple[str, ...] = (
    "SECRETS_DIR",
    "SANDLOOM_WORKER_TASKQUEUE",
    "SANDLOOM_WORKER_COUNT",
    "WEB_CONCURRENCY",
    "GUNICORN_BIND",
    "GUNICORN_ACCESSLOG",
    "GUNICORN_TIMEOUT",
    "GUNICORN_GRACEFUL_TIMEOUT",
    "GUNICORN_KEEPALIVE",
    "PROMETHEUS_MULTIPROC_DIR",
)


def tier_of(name: str) -> Tier | None:
    """Tier of a ``Settings`` alias / ``<capability>.<option>`` (None: unknown)."""
    if any(s.name == name for s in SEMANTIC):
        return "semantic"
    if name in OPS:
        return "ops"
    return None


def tier_counts() -> dict[str, int]:
    return {
        SEMANTIC_TIER: len(SEMANTIC),
        OPS_TIER: len(OPS),
    }


# -------------------------------------------------------------- overrides ----


@dataclass(frozen=True, slots=True)
class Override:
    """A semantic setting off its profile value, or an accepted risk."""

    setting: str
    kind: OverrideKind
    value: object
    profile: str
    profile_value: object = None
    explicit: bool = True

    def message(self) -> str:
        if self.kind == "accepted_risk":
            item = accepted_risks.risk(str(self.value))
            return (
                f"ACCEPTED RISK: {item.id} — {item.weakens} "
                f"Consulted by: {item.consulted_by}."
            )
        return (
            f"CONFIG OVERRIDE: {self.setting}={_fmt(self.value)} weakens the "
            f"{self.profile} profile value {_fmt(self.profile_value)}"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "setting": self.setting,
            "kind": self.kind,
            "value": _fmt(self.value),
            "profile": self.profile,
            "profile_value": _fmt(self.profile_value),
            "explicit": self.explicit,
            "message": self.message(),
        }


def _fmt(value: object) -> str:
    if value is NO_OPINION:
        return "—"
    if isinstance(value, tuple):
        return "[" + ", ".join(str(v) for v in value) + "]"
    if isinstance(value, bool):
        return str(value).lower()
    return "unset" if value is None else str(value)


def _infra_options(cfg: Any, capability: str, option: str) -> list[tuple[str, object]]:
    """(``INFRA.<cap>.<instance>.<option>``, value) of every declared instance."""
    infra = getattr(cfg, "infra", None) or {}
    declared = infra.get(capability) if isinstance(infra, Mapping) else None
    instances: dict[str, object] = {"default": {}}
    if isinstance(declared, Mapping):
        instances.update({str(k): v for k, v in declared.items()})
    out = []
    for name, options in sorted(instances.items()):
        opts = options if isinstance(options, Mapping) else {}
        out.append((f"INFRA.{capability}.{name}.{option}", opts.get(option)))
    return out


def _explicit(cfg: Any, name: str) -> bool:
    fields_set = getattr(cfg, "model_fields_set", None)
    return fields_set is None or name.lower() in fields_set


def _semantic_overrides(spec: SemanticSetting, cfg: Any, prof: str) -> list[Override]:
    if spec.selector or spec.risk_list:
        return []
    if spec.applies is not None and not spec.applies(cfg):
        return []
    if spec.risk is not None and accepted_risks.accepts(cfg, spec.risk):
        return []  # reported as the accepted risk
    pairs: list[tuple[str, object]]
    if spec.infra_option:
        cap, option = spec.name.split(".", 1)
        pairs = [(s, bool(v)) for s, v in _infra_options(cfg, cap, option)]
        explicit = True
    else:
        pairs = [(spec.name, spec.value(cfg))]
        explicit = _explicit(cfg, spec.name)
    expected = spec.profile.get(prof, NO_OPINION)
    out: list[Override] = []
    for subject, value in pairs:
        if expected is not NO_OPINION and spec.weaker(value, expected):
            out.append(Override(subject, "weaker", value, prof, expected, explicit))
    return out


def _risk_overrides(cfg: Any, prof: str) -> list[Override]:
    return [
        Override(
            f"SANDLOOM_ACCEPTED_RISKS[{risk_id}]",
            "accepted_risk",
            risk_id,
            prof,
            (),
        )
        for risk_id in sorted(accepted_risks.effective(cfg))
        if accepted_risks.in_effect(cfg, risk_id)
    ]


def active_overrides(cfg: Any) -> list[Override]:
    """Semantic overrides and accepted risks of ``cfg``."""
    prof = profile_of(cfg)
    out: list[Override] = []
    for spec in SEMANTIC:
        out += _semantic_overrides(spec, cfg, prof)
    return out + _risk_overrides(cfg, prof)


def log_overrides(
    cfg: Any, log: logging.Logger, overrides: list[Override] | None = None
) -> list[Override]:
    """Log every active override once at startup; returns them.

    WARNING in prod, and for accepted risks everywhere; INFO for a deliberate
    weaker choice outside prod.
    """
    found = active_overrides(cfg) if overrides is None else overrides
    for item in found:
        loud = _is_prod(cfg) or item.kind != "weaker"
        log.log(logging.WARNING if loud else logging.INFO, item.message())
    return found


def diagnostics(cfg: Any) -> dict[str, object]:
    """``/diagnostics/core`` block: profile, tier counts, overrides, accepted risks."""
    return {
        "profile": profile_of(cfg),
        "tiers": tier_counts(),
        "overrides": [o.to_dict() for o in active_overrides(cfg)],
        "accepted_risks": accepted_risks.diagnostics(cfg),
    }


# ------------------------------------------------------------------- docs ----


def _cell(spec: SemanticSetting, prof: str) -> str:
    value = spec.profile.get(prof, NO_OPINION)
    text = f"`{_fmt(value)}`" if value is not NO_OPINION else "—"
    if prof in spec.refused and value is not NO_OPINION:
        text += " (refused)"
    return text


def render_markdown() -> str:
    """The generated ``docs/reference/configuration-tiers.md`` page."""
    counts = tier_counts()
    lines = [
        "---",
        "title: Configuration Tiers",
        "---",
        "",
        "# Configuration tiers",
        "",
        "Generated from `sandloom.core.config.tiers`. Every setting belongs to one "
        "tier: **semantic** (changes a guarantee or security property; its "
        "value is chosen per profile below), **ops** (capacity and operations "
        "— pools, timeouts, TTLs, sizes, endpoints, credentials, backend "
        "selection). There is no migration tier: the framework carries no "
        "transitional flags.",
        "",
        f"Counts: **{counts['semantic']} semantic** (ratcheted: may only go "
        f"down; a new one needs an ADR), **{counts['ops']} ops**.",
        "",
        "## Profile table (semantic tier)",
        "",
        "`prod_strict` = `ENVIRONMENT=prod` + "
        "`PRODUCTION_PROFILE=multi_tenant_strict`. A value is the EFFECTIVE "
        "one when the setting is left unset. Setting one towards the weaker "
        "end is an *override*: logged as a WARNING at every production start, "
        "counted in `sandloom_config_override_active{setting,kind}`, listed by "
        "`/diagnostics/core` (`configuration.overrides`) and `sandloom doctor`. "
        "*(refused)*: a weaker value aborts startup in that profile. "
        "— : no framework opinion (a deployment or product choice).",
        "",
        "| Setting | dev | test | prod | prod_strict | Meaning |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for spec in SEMANTIC:
        name = (
            f"`INFRA.{spec.name.replace('.', '.<instance>.')}`"
            if (spec.infra_option)
            else f"`{spec.name}`"
        )
        cells = " | ".join(_cell(spec, p) for p in PROFILES)
        lines.append(f"| {name} | {cells} | {spec.why} |")
    lines += [
        "",
        "## Accepted risks (`SANDLOOM_ACCEPTED_RISKS`)",
        "",
        "The one list of weaker guarantees a deployment accepts, e.g. "
        '`SANDLOOM_ACCEPTED_RISKS=["fake_psp_in_prod"]` (JSON list or comma-'
        "separated). An unknown id fails startup. Each accepted risk is logged "
        "as a WARNING at every start, exported as "
        "`sandloom_accepted_risk_active{risk}`, and listed by `/diagnostics/core` "
        "(`configuration.accepted_risks`) and `sandloom doctor`.",
        "",
        "| Risk id | Weakens | Consulted by |",
        "| --- | --- | --- |",
    ]
    for item in accepted_risks.RISKS:
        refused = (
            f" Refused in {', '.join(sorted(item.refused))} even when accepted."
            if item.refused
            else ""
        )
        lines.append(f"| `{item.id}` | {item.weakens}{refused} | {item.consulted_by} |")
    lines += [
        "",
        "## Removed settings",
        "",
        "No longer settings. Still configured with the inert value they log a "
        "warning; with any other value startup fails.",
        "",
        "| Setting | Removed in | Inert value | Instead |",
        "| --- | --- | --- | --- |",
    ]
    for removed in REMOVED:
        lines.append(
            f"| `{removed.name}` | {removed.removed_in} | `{_fmt(removed.inert)}` "
            f"| {removed.hint} |"
        )
    lines += [
        "",
        "## Ops tier",
        "",
        "Every other setting in the [configuration reference](configuration.md), "
        "every other `INFRA` instance option, and the process environment read "
        "by entrypoints: " + ", ".join(f"`{n}`" for n in RAW_ENV_OPS) + ".",
        "",
        ", ".join(f"`{n}`" for n in sorted(OPS)),
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "NO_OPINION",
    "OPS",
    "PROFILES",
    "RAW_ENV_OPS",
    "REMOVED",
    "SEMANTIC",
    "Override",
    "RemovedSetting",
    "SemanticSetting",
    "active_overrides",
    "diagnostics",
    "log_overrides",
    "profile_of",
    "removed_setting_problem",
    "render_markdown",
    "tier_counts",
    "tier_of",
]
