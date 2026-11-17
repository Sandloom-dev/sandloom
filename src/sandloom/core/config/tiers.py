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
    opinion on (a module's own policy knobs).

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
it). The one reach above it is the override report's read of the resolved
``INFRA`` options (:func:`_resolved_options`), by name and at call time.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Mapping, Sequence
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
    any other value fails startup — that behaviour is gone. ``inert=None``
    means there is no inert value: whatever the setting is set to (an empty
    string, ``0`` or ``false`` included, which are real values of a url or a
    bound) fails startup.
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
    # ---- transport
    SemanticSetting(
        "notification.use_tls",
        "STARTTLS / TLS to the SMTP server (credentials and recovery links in "
        "clear without it); the smtp driver defaults to on.",
        profile=_all(True),
        strength=_BOOL_WEAK_FALSE,
        refused=frozenset({"prod_strict"}),
        infra_option=True,
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
        "the beat singleton lease is always on (it is held in the `lease` "
        "instance INFRA.lease.beat)",
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
        "BEAT_LEASE_CACHE_INSTANCE",
        "1.0",
        "the beat lease and the saga sweep are held in the `lease` instance "
        'INFRA.lease.beat (a driver: "redis", "database"; e.g. '
        'INFRA={"lease": {"beat": {"driver": "redis", "url": '
        '"redis://..."}}}), no longer in a cache instance; absent, the '
        "composition's own lease instance applies and needs its own url",
    ),
    RemovedSetting(
        "SECURITY_CACHE_INSTANCE",
        "1.0",
        "revocation marks, blocks and RPC nonces are held in the `atomic_store` "
        'instance INFRA.atomic_store.security (a driver: "redis"; e.g. '
        'INFRA={"atomic_store": {"security": {"driver": "redis", "url": '
        '"redis://..."}}}), no longer in a cache instance; every production '
        "deployment, with any PRODUCTION_PROFILE, is refused at startup without "
        "it. Upgrade: delete SECURITY_CACHE_INSTANCE and move the INFRA cache "
        'entry it named to INFRA["atomic_store"]["security"] (the same url, on a '
        "Redis that never evicts)",
    ),
    RemovedSetting(
        "IDEMPOTENCY_CACHE_INSTANCE",
        "1.0",
        "idempotency reservations and enqueue dedupe are held in the "
        "`atomic_store` instance INFRA.atomic_store.idempotency, the `security` "
        "instance when it is absent. Upgrade: delete IDEMPOTENCY_CACHE_INSTANCE "
        "and move the INFRA cache entry it named to "
        'INFRA["atomic_store"]["idempotency"]',
    ),
    *(
        RemovedSetting(
            name,
            "1.0",
            f"the smtp notifier reads its `{option}` from the driver options of "
            "the `notification` instance, no longer from a flat setting. Upgrade: "
            f"delete {name} and write `{option}` into INFRA, e.g. "
            'INFRA={"notification": {"default": {"driver": "smtp", '
            f'"{option}": ...}}}}}} (keys: host, port, username, password, '
            "from_addr, use_tls; use_tls stays on unless set false)",
            # SMTP_USE_TLS defaulted to true: only an explicit false was a choice.
            inert=name == "SMTP_USE_TLS",
        )
        for name, option in (
            ("SMTP_HOST", "host"),
            ("SMTP_PORT", "port"),
            ("SMTP_USERNAME", "username"),
            ("SMTP_PASSWORD", "password"),
            ("SMTP_FROM", "from_addr"),
            ("SMTP_USE_TLS", "use_tls"),
        )
    ),
    *(
        RemovedSetting(
            name,
            "1.0",
            f"the s3 object store reads its `{option}` from the driver options of "
            "the `objectstore` instance, no longer from a flat setting. Upgrade: "
            f"delete {name} and write `{option}` into INFRA, e.g. "
            'INFRA={"objectstore": {"default": {"driver": "s3", '
            f'"{option}": ...}}}}}} (keys: bucket, region, access_key_id, '
            "secret_access_key, endpoint_url); STORAGE_TYPE=s3 still selects "
            "the driver",
        )
        for name, option in (
            ("S3_BUCKET", "bucket"),
            ("S3_REGION", "region"),
            ("S3_ACCESS_KEY_ID", "access_key_id"),
            ("S3_SECRET_ACCESS_KEY", "secret_access_key"),
        )
    ),
    *(
        RemovedSetting(
            name,
            "1.0",
            f"the `streams` event bus reads its `{option}` from the driver "
            "options of the `eventbus` instance, no longer from a flat "
            f"setting. Upgrade: delete {name} and write `{option}` into INFRA, "
            'e.g. INFRA={"eventbus": {"default": {"driver": "streams", '
            f'"{option}": ...}}}}}}',
        )
        for name, option in (
            ("EVENT_STREAM_RETENTION_SECONDS", "retention_seconds"),
            ("EVENT_STREAM_MAXLEN", "max_length"),
            ("EVENT_STREAM_PREFIX", "prefix"),
        )
    ),
    RemovedSetting(
        "CACHE_URL",
        "1.0",
        "no instance takes its url from a flat setting or from another "
        "instance any more: every Redis instance states its own url in INFRA "
        "(cache.default, eventbus.default for the streams bus, lease.beat, "
        "atomic_store.security, broadcast.default), e.g. "
        'INFRA={"cache": {"default": {"driver": "redis", "url": "redis://..."}}, '
        '"eventbus": {"default": {"driver": "streams", "url": "redis://..."}}, '
        '"lease": {"beat": {"driver": "redis", "url": "redis://..."}}, '
        '"atomic_store": {"security": {"driver": "redis", "url": '
        '"redis://..."}}, "broadcast": {"default": {"driver": "redis", "url": '
        '"redis://..."}}} (list only the instances the '
        "app selects). The old value filled all of them: a staging-like "
        "environment whose INFRA cache.default url differs from CACHE_URL "
        "moves its implicit security store with this change, so state "
        "atomic_store.security.url on purpose",
        inert=None,
    ),
    RemovedSetting(
        "BROKER_URL",
        "1.0",
        "the Celery broker is the option `broker` of the `taskqueue` instance, "
        'e.g. INFRA={"taskqueue": {"default": {"driver": "celery", "broker": '
        '"redis://..."}}}; the '
        "result backend (`backend`, formerly CACHE_URL) is off unless INFRA "
        "states it",
        inert=None,
    ),
    *(
        RemovedSetting(
            name,
            "1.0",
            f"the Redis client bound is the driver option `{option}` of each "
            "instance (cache, atomic_store, broadcast, eventbus), no longer a "
            "flat setting, e.g. "
            f'INFRA={{"cache": {{"default": {{"{option}": ...}}}}}}; the '
            "driver defaults are the old defaults",
            inert=None,
        )
        for name, option in (
            ("CACHE_SOCKET_TIMEOUT_SECONDS", "socket_timeout_seconds"),
            (
                "CACHE_SOCKET_CONNECT_TIMEOUT_SECONDS",
                "socket_connect_timeout_seconds",
            ),
            (
                "CACHE_HEALTH_CHECK_INTERVAL_SECONDS",
                "health_check_interval_seconds",
            ),
            ("CACHE_MAX_CONNECTIONS", "max_connections"),
            ("CACHE_POOL_TIMEOUT_SECONDS", "pool_timeout_seconds"),
            ("CACHE_RETRY_ATTEMPTS", "retry_attempts"),
        )
    ),
    RemovedSetting(
        "MODULE_FS_SCAN",
        "0.7",
        "modules are no longer packages under sandloom.*: list them in "
        "SANDLOOM_MODULES (import path or `sandloom.modules` entry-point name) "
        "or set MODULE_LOAD_ENTRYPOINTS=true to load every installed module",
    ),
    RemovedSetting(
        "EVENT_BUS_REDIS_SEMAPHORE_LIMIT",
        "0.7.0a15",
        "the limit of concurrent callbacks of the volatile `broadcast` event "
        "bus is named after that bus, not after Redis: set "
        "EVENT_BUS_BROADCAST_SEMAPHORE_LIMIT (same meaning, same default of 100)",
        inert=None,
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
    # The value is echoed only when it is a flag spelling (true / false / on /
    # off ...): never free text, which may be a credential (SMTP_PASSWORD,
    # S3_SECRET_ACCESS_KEY) that would land in startup logs and crash reports.
    echoed = f"{removed.name}={str(value).lower()}" if isinstance(value, bool) else None
    lead = f"{removed.name} was removed in {removed.removed_in}" + (
        f" and {echoed or f'the value set for {removed.name}'} is no longer supported"
        if fatal
        else ""
    )
    return (
        f"{lead}: {removed.hint}; drop it from the environment",
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
        "PROJECTION_TOKEN_KEYS",
        "PROJECTION_RYW_WAIT_MS",
        "EVENT_HANDLER_TIMEOUT_SECONDS",
        "EVENT_STREAMS_RECONNECT_BASE_SECONDS",
        "EVENT_STREAMS_RECONNECT_MAX_SECONDS",
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
        "DB_RECREATE_ON_MODEL_CHANGE",
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
        "STORAGE_TYPE",
        "STORAGE_PATH",
        "MAX_UPLOAD_SIZE_BYTES",
        "MAX_REQUEST_BODY_BYTES",
        "NOTIFIER_BACKEND",
        "NOTIFIER_LOG_LINKS",
        "JWT_SECRET",
        "JWT_ALGORITHM",
        "JWT_PRIVATE_KEY",
        "JWT_PUBLIC_KEYS",
        "ACCESS_TOKEN_EXPIRE_MINUTES",
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
        "EVENT_BUS_BROADCAST_SEMAPHORE_LIMIT",
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
    #: the declaration of an accepted module risk (a kernel risk is looked up
    #: in :data:`accepted_risks.RISKS`).
    risk: accepted_risks.Risk | None = None

    def message(self) -> str:
        if self.kind == "accepted_risk":
            item = self.risk or accepted_risks.risk(str(self.value))
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


def _resolved_options(cfg: Any, capability: str) -> dict[str, Mapping[str, Any]]:
    """The options of each instance of ``capability`` as the application
    resolves them: the composition's selection under ``INFRA``, not ``INFRA``
    alone (a ``DEFAULT_INFRA`` pin that sets ``use_tls`` false is a choice too).

    Reached by name: ``infra`` sits above this leaf (ADR-0007). Accepted as it
    is. The other shapes: a resolver ``infra`` registers into this leaf, a
    hidden global that fails open when nothing registered; or a resolver each
    caller passes in, which moves the same import into every caller of the
    check, for one read at startup. The import is lazy, so the leaf imports
    nothing above it. ``importlib.import_module`` is invisible to import-linter,
    so no contract guards this edge: the kernel-edges register
    (``tests/architecture/kernel_edges.yml``) flags a dynamic import it cannot
    resolve to a literal, and this one is a literal. A malformed
    ``INFRA`` falls back to what the setting itself declares: loading the
    configuration refuses it, with its own message, at startup."""
    infra_config = importlib.import_module("sandloom.core.infra.config")
    try:
        resolved = infra_config.resolved_instances(cfg, capability)
    except infra_config.InfraConfigError:
        infra = getattr(cfg, "infra", None) or {}
        declared = infra.get(capability) if isinstance(infra, Mapping) else None
        if not isinstance(declared, Mapping):
            return {}
        return {
            str(name): options
            for name, options in declared.items()
            if isinstance(options, Mapping)
        }
    return {name: inst.options for name, inst in resolved.items()}


def _declared_in_infra(cfg: Any, capability: str, name: str, option: str) -> bool:
    """Whether the ``INFRA`` setting itself states ``option`` of the instance
    (not only the composition's ``DEFAULT_INFRA`` selection)."""
    infra = getattr(cfg, "infra", None)
    declared = infra.get(capability) if isinstance(infra, Mapping) else None
    instance = declared.get(name) if isinstance(declared, Mapping) else None
    return isinstance(instance, Mapping) and option in instance


def _infra_options(cfg: Any, capability: str, option: str) -> list[tuple[str, object]]:
    """(label, value) of every resolved instance. The label names where the
    value was written: ``INFRA.<cap>.<instance>.<option>`` when ``INFRA`` states
    it, ``<cap>.<instance>.<option> (composition DEFAULT_INFRA)`` otherwise (the
    composition's selection, or the driver's own default)."""
    instances: dict[str, object] = {"default": {}}
    instances.update(_resolved_options(cfg, capability))
    out = []
    for name, options in sorted(instances.items()):
        opts = options if isinstance(options, Mapping) else {}
        label = (
            f"INFRA.{capability}.{name}.{option}"
            if _declared_in_infra(cfg, capability, name, option)
            else f"{capability}.{name}.{option} (composition DEFAULT_INFRA)"
        )
        out.append((label, opts.get(option)))
    return out


#: What an instance that leaves an ``infra_option`` out runs with (the driver's
#: own default), where it is not ``False``: the smtp driver defaults to TLS on,
#: so an unset ``use_tls`` is no override. RLS is opt-in (unset = off).
_INFRA_OPTION_UNSET: Mapping[str, bool] = {"notification.use_tls": True}


def _infra_option_unset(name: str) -> bool:
    return _INFRA_OPTION_UNSET.get(name, False)


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
        unset = _infra_option_unset(spec.name)
        pairs = [
            (s, unset if v is None else bool(v))
            for s, v in _infra_options(cfg, cap, option)
        ]
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


#: Weaker values ``sandloom new`` itself writes into the dev ``.env``: an
#: override nobody chose by hand is not news on every command (DEBUG).
GENERATED_DEV_DEFAULTS: dict[str, object] = {"INTERNAL_ENDPOINTS_OPEN_IN_DEV": True}


def log_overrides(
    cfg: Any, log: logging.Logger, overrides: list[Override] | None = None
) -> list[Override]:
    """Log every active override once at startup; returns them.

    WARNING in prod, and for accepted risks everywhere; INFO for a deliberate
    weaker choice outside prod; DEBUG for a generated dev default
    (:data:`GENERATED_DEV_DEFAULTS`).
    """
    found = active_overrides(cfg) if overrides is None else overrides
    for item in found:
        loud = _is_prod(cfg) or item.kind != "weaker"
        level = logging.WARNING if loud else logging.INFO
        if (
            not loud
            and item.setting in GENERATED_DEV_DEFAULTS
            and item.value == GENERATED_DEV_DEFAULTS[item.setting]
        ):
            level = logging.DEBUG
        log.log(level, item.message())
    return found


def diagnostics(
    cfg: Any,
    module_risks: accepted_risks.ModuleRiskReport | None = None,
    module_overrides: Sequence[Override] = (),
    module_tiers: Mapping[str, Mapping[str, int]] | None = None,
) -> dict[str, object]:
    """``/diagnostics/core`` block: profile, tier counts, overrides, accepted risks.

    What the loaded modules declare (ADR-0015) comes from the loader, which
    read it: ``module_risks`` the module-namespaced accepted risks as it
    classified them (``accepted_risks.diagnostics``), ``module_overrides`` the
    modules' semantic settings off their profile value and their active risks,
    ``module_tiers`` each module's declared tier counts."""
    block: dict[str, object] = {
        "profile": profile_of(cfg),
        "tiers": tier_counts(),
        "overrides": [o.to_dict() for o in (*active_overrides(cfg), *module_overrides)],
        "accepted_risks": accepted_risks.diagnostics(cfg, module_risks),
    }
    if module_tiers:
        block["module_tiers"] = {m: dict(c) for m, c in sorted(module_tiers.items())}
    return block


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
        '`SANDLOOM_ACCEPTED_RISKS=["relay_disabled"]` (JSON list or comma-'
        "separated). An unknown id fails startup. The ids below are the "
        "kernel's. A module declares its own, named `<module>.<name>` "
        "(for example `inventory.oversell_in_prod`): the id of a loaded module "
        "is checked against what that module declares, the id of a module "
        "installed or listed in `SANDLOOM_MODULES` but not loaded in this "
        "process is inert, listed as not applicable here, and any other "
        "module name fails startup, so a typo never passes silently. A module "
        "risk has no flat spelling and no alias. Each accepted risk is logged "
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
