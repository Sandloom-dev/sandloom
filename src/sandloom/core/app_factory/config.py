"""Configuration checks ``create_app`` and every entry point run at boot:
refusals (``configuration_problems``, ``enforce_configuration``) and startup
warnings."""

import logging
import os
from typing import Any

import sandloom.core.sql_alchemy.dbapi as _dbapi
from sandloom.core import rpc_binding, rpc_identity, rpc_pair_mac
from sandloom.core.auth.tokens import jwt_key_material_problem
from sandloom.core.config import accepted_risks as _risks
from sandloom.core.config import tiers
from sandloom.core.config.common import INVALID_CONFIGURATION, strict_profile_active
from sandloom.core.config.password_params import meets_owasp_minimum
from sandloom.core.config.settings import load_settings, settings
from sandloom.core.guarantee_lattice import (
    Mechanism,
    nested_unit_label,
    tenant_isolation_label,
)
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    InfraConfigError,
    database_instance_url,
    load_infra_config,
)
from sandloom.core.infra.registry import InfraRegistry
from sandloom.core.observability import metrics
from sandloom.core.production_profile import config_problems, tenant_scoped_tables
from sandloom.core.sql_alchemy.portable import (
    development_only,
    dialect_of_url,
    row_security_of_url,
    serial_writers,
    sql_dialect,
    unrendered_dialect,
    unrendered_partial_indexes,
)
from sandloom.core.sql_alchemy.read_only_policy import read_only_sessions_problem
from sandloom.core.sql_alchemy.tenant_rls import row_security_problem


class ConfigurationError(SystemExit):
    """The configuration fails a check that runs at boot.

    A ``SystemExit`` like a ``Settings`` load failure: the entry point
    (ASGI import, worker, ``sandloom-migrate``, the ``sandloom`` CLI) stops with the
    message and exit status 1 before serving or touching anything.
    """


def configured_database_urls(cfg: Any) -> list[tuple[str, str]]:
    """``(label, url)`` for every database URL of ``cfg``: each instance's
    URL, its ``system_url`` option, and ``DATABASE_SYSTEM_URL`` for the
    default instance. Empty when ``INFRA`` is invalid (refused where the
    infrastructure is built)."""
    try:
        databases = load_infra_config(cfg).get("database", {})
    except InfraConfigError:
        return []
    found: list[tuple[str, str]] = []
    for name, instance in sorted(databases.items()):
        url = database_instance_url(instance.options, cfg)
        if url:
            found.append((name, url))
        system = instance.options.get("system_url") or (
            getattr(cfg, "database_system_url", None)
            if name == DEFAULT_INSTANCE
            else None
        )
        if system:
            found.append((f"{name} system channel", str(system)))
    return found


def sql_dialect_problem(cfg: Any) -> str | None:
    """A configured SQL database whose dialect has no SQL dialect renderer.

    Modules write the portable forms of ``sandloom.sdk.data`` and the database's
    renderer turns them into SQL (SANDLOOM-B024); without one the first table
    or statement that uses a form would fail, so the process does not start.
    """
    missing: list[str] = []
    for name, url in configured_database_urls(cfg):
        dialect = unrendered_dialect(url)
        if dialect is not None:
            missing.append(f"{name} ({dialect})")
    if not missing:
        return None
    return (
        "no SQL dialect renderer for the database(s) "
        + ", ".join(missing)
        + " [SANDLOOM-B024]: install the driver package that provides one"
    )


def row_security_config_problem(cfg: Any) -> str | None:
    """A database instance that asks for row security its database lacks.

    ``tenant_rls``, ``system_url`` and ``require_system_channel`` need a
    renderer with row security (SANDLOOM-B026). Build-free: the options and
    the URL's renderer only, so every entry point refuses before it starts.
    """
    try:
        databases = load_infra_config(cfg).get("database", {})
    except InfraConfigError:
        return None  # refused where the infrastructure is built
    problems: list[str] = []
    for name, instance in sorted(databases.items()):
        url = database_instance_url(instance.options, cfg)
        if not url:
            continue
        system_url = (
            getattr(cfg, "database_system_url", None)
            if name == DEFAULT_INSTANCE
            else None
        )
        problem = row_security_problem(
            name, str(url), instance.options, system_url=system_url
        )
        if problem is not None:
            problems.append(problem)
    return "; ".join(problems) or None


def dbapi_config_problem(cfg: Any) -> str | None:
    """A configured database its DBAPI adapter cannot open, as the adapter
    states it (SQLite: in memory, or aiosqlite not installed, SANDLOOM-B032).
    Build-free: the URLs and the installed packages only."""
    problems: list[str] = []
    for name, url in configured_database_urls(cfg):
        problem = _dbapi.dbapi_config_problem(url)
        if problem is not None:
            problems.append(f"database/{name}: {problem}")
    return "; ".join(problems) or None


#: The accepted risk that admits a development database in production.
DEVELOPMENT_DATABASE_RISK = "development_database_in_prod"


def development_database_config_problem(cfg: Any) -> str | None:
    """A production process on a database whose renderer serves development
    and tests only (SQLite, SANDLOOM-B029): refused unless the deployment
    accepts the risk ``development_database_in_prod``, and always under
    ``PRODUCTION_PROFILE=multi_tenant_strict``. Build-free."""
    if not getattr(cfg, "is_prod", False):
        return None
    try:
        databases = load_infra_config(cfg).get("database", {})
    except InfraConfigError:
        return None  # refused where the infrastructure is built
    strict = strict_profile_active(cfg)
    if not strict and _risks.accepts(cfg, DEVELOPMENT_DATABASE_RISK):
        return None
    found = []
    for name, instance in sorted(databases.items()):
        url = database_instance_url(instance.options, cfg)
        if url and development_only(dialect_of_url(url)):
            found.append(f"database/{name} ({dialect_of_url(url)})")
    if not found:
        return None
    fix = (
        "PRODUCTION_PROFILE=multi_tenant_strict refuses it even when accepted: "
        "use a production database"
        if strict
        else "use a production database, or accept the risk with "
        f'SANDLOOM_ACCEPTED_RISKS=["{DEVELOPMENT_DATABASE_RISK}"]'
    )
    return (
        f"{', '.join(found)} serve(s) development and tests only, and "
        f"ENVIRONMENT=prod [SANDLOOM-B029]: {fix}"
    )


def read_only_sessions_config_problem(cfg: Any) -> str | None:
    """``DB_READ_ONLY_SESSIONS`` on a default database with nothing to refuse
    a write: no read-only transactions in its renderer and no client
    read-only policy in its DBAPI adapter (SANDLOOM-B031). Build-free."""
    if not getattr(cfg, "db_read_only_sessions", False):
        return None
    try:
        databases = load_infra_config(cfg).get("database", {})
    except InfraConfigError:
        return None  # refused where the infrastructure is built
    instance = databases.get(DEFAULT_INSTANCE)
    url = database_instance_url(instance.options if instance else None, cfg)
    if not url:
        return None
    return read_only_sessions_problem(DEFAULT_INSTANCE, url)


def application_enforced_isolation(
    infra: InfraRegistry, metadata: Any
) -> list[tuple[str, str]]:
    """``(instance, label)`` for each SQL database whose renderer has no row
    security, when ``metadata`` maps a ``TenantScoped`` table: their
    isolation there is the ORM guard only. The label is the guarantee
    lattice's, computed from the instance's features.

    Which instance hosts which table is not known before a database is
    reached, so any mapped ``TenantScoped`` table counts for every such
    instance. A PostgreSQL instance without ``tenant_rls`` is
    application-enforced too, by the operator's choice: it is not reported.
    """
    found: list[tuple[str, str]] = []
    mapped = set(getattr(metadata, "tables", {}))
    if not mapped.intersection(tenant_scoped_tables()):
        return found
    for name in infra.instances("database"):
        served = infra.sql_dialects("database", name)
        config = infra.instance_config("database", name)
        url = database_instance_url(config.options if config else None, settings)
        if served is None or not served[1] or not url:
            continue
        if sql_dialect(dialect_of_url(str(url))) is None:
            continue  # SANDLOOM-B024's refusal
        if row_security_of_url(str(url)) is not None:
            continue
        features = infra.advertised_features("database", name) or frozenset()
        label = tenant_isolation_label(Mechanism.from_features(features))
        if label is not None:
            found.append((name, label))
    return found


def warn_application_enforced_isolation(
    infra: InfraRegistry, metadata: Any, log: logging.Logger
) -> None:
    """One startup warning per :func:`application_enforced_isolation`
    instance: said aloud, never weakened silently (spec infra-agnostic,
    decision 3)."""
    for name, label in application_enforced_isolation(infra, metadata):
        log.warning(
            "database/%s has no row security: %s (the ORM guard only; raw SQL "
            "is not confined to the bound tenant). The strict production "
            "profile refuses it.",
            name,
            label,
            extra={"event": "tenant_isolation.application_enforced"},
        )


def nested_units_join_caller(infra: InfraRegistry) -> list[tuple[str, str]]:
    """``(instance, label)`` for each SQL database whose writers are serial
    (its renderer's ``serial_writers``: SQLite): a nested write unit of the
    same task there (a local contract call, a saga step's unit, a child
    ``open_uow()``) is a SAVEPOINT of its caller and commits with it. The
    label is the guarantee lattice's."""
    found: list[tuple[str, str]] = []
    for name in infra.instances("database"):
        served = infra.sql_dialects("database", name)
        config = infra.instance_config("database", name)
        url = database_instance_url(config.options if config else None, settings)
        if served is None or not served[1] or not url:
            continue
        label = nested_unit_label(
            Mechanism(serial_writers=serial_writers(dialect_of_url(str(url))))
        )
        if label is not None:
            found.append((name, label))
    return found


def warn_nested_units_join_caller(infra: InfraRegistry, log: logging.Logger) -> None:
    """One startup warning per :func:`nested_units_join_caller` instance:
    the weaker contract atomicity is said aloud, never silently."""
    for name, label in nested_units_join_caller(infra):
        log.warning(
            "database/%s has one writer at a time: a nested write unit of the "
            "same task (a local contract call, a saga step, a child unit) runs "
            "as a SAVEPOINT of its caller and %s (an outer rollback undoes its "
            "rows, outbox events and command record). Development and tests "
            "only.",
            name,
            label,
            extra={"event": "unit.nested_savepoint"},
        )


def sql_storage_problems(infra: InfraRegistry, metadata: Any) -> list[str]:
    """The SQL databases a process cannot use as configured.

    Each database instance's URL must name a dialect its driver declares it
    serves (``sql_dialects=`` at driver registration, SANDLOOM-B024), and every
    :func:`~sandloom.sdk.data.partial_index` must have a condition on each of
    those dialects (SANDLOOM-R015): otherwise it would index every row.
    """
    problems: list[str] = []
    dialects: set[str] = set()
    for name in infra.instances("database"):
        served = infra.sql_dialects("database", name)
        config = infra.instance_config("database", name)
        url = database_instance_url(config.options if config else None, settings)
        if served is None or not served[1] or not url:
            continue
        driver, names = served
        dialect = dialect_of_url(str(url))
        dialects.add(dialect)
        if dialect not in names:
            problems.append(
                f"database {name!r}: driver {driver!r} serves {sorted(names)}, "
                f"not {dialect!r} [SANDLOOM-B024]"
            )
    unrestricted = unrendered_partial_indexes(metadata, dialects)
    if unrestricted:
        problems.append(
            "partial indexes with no condition on their database: "
            + ", ".join(unrestricted)
            + " [SANDLOOM-R015]"
        )
    return problems


def configuration_problems(cfg: Any, *, include_profile: bool = True) -> list[str]:
    """Every startup configuration problem of ``cfg`` (empty = usable).

    ``Settings`` validates what the configuration layer can judge alone;
    these checks need kernel code above it and therefore run when a process
    boots (:func:`enforce_configuration`: ``create_app`` — the API, the worker
    and the ops CLIs boot through it — and the ``sandloom-migrate`` / ``sandloom``
    command lines):

    * the asymmetric JWT key material
      (:func:`sandloom.core.auth.tokens.jwt_key_material_problem`);
    * the internal-RPC keys, bindings and allowlist
      (``rpc_binding`` / ``rpc_pair_mac`` / ``rpc_identity``
      ``configuration_problem``);
    * a SQL dialect renderer for every configured database
      (:func:`sql_dialect_problem`), with row security where an instance
      asks for it (:func:`row_security_config_problem`, SANDLOOM-B026), a
      database its DBAPI adapter can open (:func:`dbapi_config_problem`,
      SANDLOOM-B032 on SQLite), no development-only database in production
      (:func:`development_database_config_problem`, SANDLOOM-B029), and a
      guard for ``DB_READ_ONLY_SESSIONS``
      (:func:`read_only_sessions_config_problem`, SANDLOOM-B031);
    * ``PRODUCTION_PROFILE=multi_tenant_strict`` in prod
      (:func:`sandloom.core.production_profile.config_problems`), unless
      ``include_profile=False`` (``sandloom doctor`` reports the profile itself).
    """
    problems: list[str] = []
    for check in (
        jwt_key_material_problem,
        rpc_binding.configuration_problem,
        rpc_pair_mac.configuration_problem,
        rpc_identity.configuration_problem,
        sql_dialect_problem,
        row_security_config_problem,
        dbapi_config_problem,
        development_database_config_problem,
        read_only_sessions_config_problem,
    ):
        problem = check(cfg)
        if problem:
            problems.append(problem)
    if include_profile and strict_profile_active(cfg):
        errors, _warnings = config_problems(cfg)
        if errors:
            problems.append(
                "PRODUCTION_PROFILE=multi_tenant_strict is not met: "
                + "; ".join(errors)
            )
    return problems


def enforce_configuration(cfg: Any = None) -> None:
    """Refuse to boot on any :func:`configuration_problems` finding.

    ``cfg`` defaults to the process settings, which this forces (settings
    are lazy, spec §2.4: an invalid environment fails here, at startup).
    """
    problems = configuration_problems(load_settings() if cfg is None else cfg)
    if problems:
        raise ConfigurationError(INVALID_CONFIGURATION + "; ".join(problems))


def warn_pair_mac_rpc(cfg: Any, log: logging.Logger) -> list[str]:
    """Startup warnings of pair-MAC internal RPC (ADR-0004); returns them."""

    if not (cfg.service_bindings or cfg.rpc_dh_private_key):
        return []  # this process neither calls nor can serve internal RPC
    out: list[str] = []
    stale = rpc_pair_mac.stale_extra_kids(cfg)
    if stale:
        out.append(
            f"RPC_DH_EXTRA_PRIVATE_KEYS {stale} are no longer published in "
            "RPC_SERVICE_DH_KEYS: a retired provider key still derives every "
            "caller's pair key towards this service. Drop it (the rotation "
            "overlap is bounded: docs/adr/0004, 'Key rotation')."
        )
    if not cfg.is_prod and cfg.rpc_replay_guard != "local":
        out.append(
            "RPC nonce guard: the shared (security cache) layer FAILS OPEN "
            "outside prod — a cache outage accepts a replay on another worker "
            "or replica (prod fails closed)."
        )
    if not rpc_pair_mac.commands_require_signature(cfg):
        out.append(
            "RPC_COMMANDS_REQUIRE_SIGNATURE is off: a leaked provider X25519 key "
            "can forge COMMAND calls from any caller to that provider (on in "
            "prod by default)."
        )
    for line in out:
        log.warning(line)
    return out


def warn_configuration_overrides(cfg: Any, log: logging.Logger) -> list[tiers.Override]:
    """Log the profile overrides of ``cfg`` once at startup; returns them.

    A semantic setting moved off its profile value, or an accepted risk
    (:mod:`sandloom.core.config.tiers`): an operator-facing log line plus
    ``sandloom_config_override_active{setting,kind}``.
    """
    found = tiers.log_overrides(cfg, log)
    for item in found:
        metrics.config_override_active.labels(item.setting, item.kind).set(1)
        if item.kind == "accepted_risk":
            metrics.accepted_risk_active.labels(str(item.value)).set(1)
    return found


def outbox_relay_problem(cfg: Any) -> str | None:
    """Why this prod app process must not start without an outbox relay.

    ``OUTBOX_RELAY_ENABLED=off`` leaves committed events that the post-commit
    dispatch failed to publish in the outbox for good: delivery degrades from
    at-least-once to a single attempt. Refused in prod unless the deployment
    accepts the risk ``relay_disabled``. ``delegated`` (set by the gunicorn
    config in every worker but the relay leader) is fine inside a gunicorn
    worker (``SANDLOOM_WORKER_COUNT`` set by its ``post_fork``).
    """
    if not cfg.is_prod:
        return None
    mode = cfg.outbox_relay_enabled
    if mode == "delegated" and os.environ.get("SANDLOOM_WORKER_COUNT"):
        return None
    if mode not in ("off", "delegated") or cfg.accepts_risk("relay_disabled"):
        return None
    return (
        f"OUTBOX_RELAY_ENABLED={mode} in a production app process: no process "
        "retries the outbox, so an event whose post-commit dispatch failed is "
        "never delivered. Fix: leave it auto (the gunicorn config keeps exactly "
        "one relay per process group), or accept the risk explicitly with "
        'SANDLOOM_ACCEPTED_RISKS=["relay_disabled"] when no process should relay'
    )


def warn_weak_password_hashing(cfg: Any, log: logging.Logger) -> bool:
    """Log argon2id parameters below the OWASP minimum on every start.

    In prod they only get this far with the accepted risk ``weak_password_hash`` (the
    settings validator refuses them otherwise), so the line is loud; outside
    prod weak parameters are a normal fast-test setting and get a warning.
    """
    params = cfg.password_hash_params
    if meets_owasp_minimum(params):
        return False
    if cfg.is_prod:
        log.warning(
            "SECURITY: password hashing runs with argon2id %s, BELOW the OWASP "
            "minimum (m=19456 KiB, t=2, p=1), because the risk weak_password_hash "
            "is accepted. Stolen password hashes are cheaper to crack; switch "
            "PASSWORD_HASH_PROFILE to owasp_min or strong.",
            params.describe(),
        )
    else:
        log.warning(
            "password hashing uses argon2id %s, below the OWASP minimum "
            "(allowed outside prod; refused in prod)",
            params.describe(),
        )
    return True
