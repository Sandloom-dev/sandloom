"""Production profiles: the multi-tenant trust model as an enforced contract.

``PRODUCTION_PROFILE`` names the guarantees a production deployment promises
its tenants. ``standard`` (default) keeps every existing knob as configured.
``multi_tenant_strict`` turns the documented trust model
(``docs/advanced/security-model.md`` — "Production profile") into startup
checks, so an operator cannot run a shared multi-tenant SaaS with one of its
layers silently off. In ``ENVIRONMENT=prod`` it requires:

1. ``TENANCY_MODE=multi`` (the profile is about isolating tenants);
2. PostgreSQL Row-Level Security on EVERY database instance
   (``INFRA.database.<name>.tenant_rls: true``; its role verification always
   runs), and at startup that every table of a
   ``TenantScoped`` model carries the forced tenant-isolation policy
   (:func:`verify_tenant_rls_coverage`) — RLS on "some" tables is not a
   guarantee;
3. ``AUTH_REVOCATION_FAIL_CLOSED`` not disabled (it is then forced on): a
   revocation-store outage rejects tokens instead of accepting revoked ones;
4. security-critical state (revocation marks, user blocks, RPC nonces,
   idempotency records; contract-command dedupe is already durable in the
   provider database) in a declared ``INFRA.atomic_store.security`` instance
   (never built from the cache's options or from memory) whose server differs
   from the best-effort cache's: it declares ``never_evicts`` and ``shared``,
   and at startup its ``isolation_identity()`` must differ from the module
   cache's (a ``None`` or raising identity fails). When
   ``INFRA.atomic_store.idempotency`` moves the idempotency records out, that
   instance meets the same requirement;
5. split deployments (``SERVICE_BINDINGS`` set): the contract compatibility
   check on (``CONTRACT_COMPAT_CHECK`` not ``off``) and asymmetric token
   signing (``JWT_ALGORITHM`` EdDSA / RS256 / ES256: ``JWT_PRIVATE_KEY`` on
   the issuer only, ``JWT_PUBLIC_KEYS`` everywhere) so a compromised
   non-issuer service cannot mint tokens. The accepted risk
   ``shared_hs_secret_split`` is refused. A process that loads no token
   issuer is split too even without bindings: the kernel's startup role check
   (:func:`sandloom.core.auth.tokens.token_signing_problem`) refuses an HS secret
   there under this profile, with no opt-out;
6. no notification instance with ``use_tls`` false, whatever selects it (the
   composition, a pin or ``INFRA``): recovery links and SMTP credentials never
   travel in clear (no opt-out).

Configuration problems refuse the boot
(:func:`sandloom.core.app_factory.enforce_configuration`, run by ``create_app``);
the database checks run as a critical startup task. The
per-profile value of every semantic setting — this profile's column included
— is tabulated in :mod:`sandloom.core.config.tiers` (``prod_strict``), which also
reports overrides; the refusals stay here.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import sandloom.core.tenant as _tenant
from sandloom.core import rpc_pair_mac
from sandloom.core.config import accepted_risks
from sandloom.core.config.common import strict_profile_active
from sandloom.core.infra import config as infra_config

if TYPE_CHECKING:
    from sandloom.core.infra.registry import InfraRegistry

logger = logging.getLogger("sandloom.production_profile")


class ProductionProfileError(RuntimeError):
    """The deployment does not meet its declared production profile."""


def _server(url: object) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return None
    return f"{host}:{parts.port or 6379}"


def _database_instances(infra: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    declared = infra.get("database") or {}
    instances: dict[str, Mapping[str, Any]] = {"default": {}}
    if isinstance(declared, Mapping):
        for name, options in declared.items():
            instances[str(name)] = options if isinstance(options, Mapping) else {}
    return instances


def _cache_default_url(cfg: Any, infra: Mapping[str, Any]) -> object:
    caches = infra.get("cache") or {}
    default_opts = caches.get("default") if isinstance(caches, Mapping) else None
    if isinstance(default_opts, Mapping) and default_opts.get("url"):
        return default_opts.get("url")
    return getattr(cfg, "cache_url", None)


def _atomic_instance(infra: Mapping[str, Any], role: str) -> Mapping[str, Any] | None:
    declared = infra.get("atomic_store") or {}
    options = declared.get(role) if isinstance(declared, Mapping) else None
    if isinstance(options, Mapping) and options.get("driver"):
        return options
    return None


def _memory_store_problem(role: str) -> str:
    return (
        f'INFRA["atomic_store"]["{role}"] uses the development-only memory '
        "driver: it is lost on restart and not shared between replicas"
    )


_SECURITY_STORE_UNDECLARED = (
    'INFRA["atomic_store"]["security"] is not declared: revocation marks, '
    "user blocks, RPC nonces and idempotency records need a durable, "
    "non-evicting atomic_store instance on its own server, e.g. "
    'INFRA={"atomic_store": {"security": {"driver": "redis", "url": '
    '"redis://security-redis:6379/0"}}} (SECURITY_CACHE_INSTANCE and '
    "IDEMPOTENCY_CACHE_INSTANCE are removed)"
)


def atomic_store_security_problem(cfg: Any) -> str | None:
    """The production refusal of spec section 4, on every profile: the security
    state needs a declared ``INFRA.atomic_store.security`` that is not the
    development-only memory driver (development and test get an implicit
    instance; production never does)."""
    infra = getattr(cfg, "infra", None) or {}
    security = _atomic_instance(infra, "security")
    if security is None:
        return _SECURITY_STORE_UNDECLARED
    if str(security.get("driver")) == "memory":
        return _memory_store_problem("security")
    return None


#: What the kernel requires of the ``security`` and ``idempotency`` atomic_store
#: instances in production (spec section 4): visible to every replica, and never
#: losing a revocation mark or a reservation before its TTL.
REQUIRED_STORE_FEATURES = ("shared", "never_evicts")
_FEATURE_CHECKED_ROLES = ("security", "idempotency")


def atomic_store_feature_problems(infra: InfraRegistry) -> list[str]:
    """The production refusal of spec section 4, on every profile: each
    configured ``security`` / ``idempotency`` atomic_store instance runs on a
    driver that declares :data:`REQUIRED_STORE_FEATURES` for its options (a
    database store on SQLite is not ``shared``). Build-free: the features are
    the driver's declaration, read from the registry; a driver that declares
    none of them is refused whatever its name."""
    problems: list[str] = []
    for role in _FEATURE_CHECKED_ROLES:
        configured = infra.instance_config("atomic_store", role)
        if configured is None:
            continue  # an undeclared security instance has its own refusal
        features = infra.declared_features("atomic_store", role)
        if features is None:
            problems.append(
                f'INFRA["atomic_store"]["{role}"]: driver {configured.driver!r} is '
                "not registered, so its features cannot be checked (install it "
                "or fix the driver name)"
            )
            continue
        missing = [name for name in REQUIRED_STORE_FEATURES if name not in features]
        if missing:
            problems.append(
                f'INFRA["atomic_store"]["{role}"]: driver {configured.driver!r} '
                f"does not declare {missing} for these options: revocation marks "
                "and idempotency records need a store that is shared by every "
                "replica and never evicts (e.g. a redis store, or a database "
                "store on PostgreSQL)"
            )
    return problems


def _atomic_instance_problems(
    cfg: Any, infra: Mapping[str, Any], role: str, options: Mapping[str, Any]
) -> list[str]:
    """``atomic_store/<role>`` runs on a driver that can hold security state, and
    a Redis one on a server other than the module cache's."""
    driver = str(options.get("driver"))
    if driver == "memory":
        return [_memory_store_problem(role)]
    server = _server(options.get("url"))
    if (
        driver == "redis"
        and server is not None
        and server == _server(_cache_default_url(cfg, infra))
    ):
        return [
            f"atomic_store/{role} runs on the same Redis server as the "
            "default cache; eviction / maxmemory policies are per server, so "
            "it must be a separate (noeviction, replicated) Redis"
        ]
    return []


def _atomic_store_problems(
    cfg: Any, infra: Mapping[str, Any], warnings: list[str]
) -> list[str]:
    """The security-critical state (revocation marks, user blocks, RPC nonces
    and idempotency records) lives in ``INFRA.atomic_store.security`` (and
    ``.idempotency`` when it moves the records), declared: never built from the
    cache's options or from memory."""
    security = _atomic_instance(infra, "security")
    if security is None:
        return [_SECURITY_STORE_UNDECLARED]
    problems = _atomic_instance_problems(cfg, infra, "security", security)
    idem = _atomic_instance(infra, "idempotency")
    if idem is not None:
        problems += _atomic_instance_problems(cfg, infra, "idempotency", idem)
        idem_server = _server(idem.get("url"))
        if idem_server is not None and idem_server == _server(security.get("url")):
            warnings.append(
                "atomic_store/idempotency runs on the same Redis server as "
                "atomic_store/security: idempotency writes still queue the "
                "revocation-mark reads on one Redis thread; give it its own "
                "Redis or drop it"
            )
    return problems


def _notification_problems(cfg: Any) -> list[str]:
    """A notification instance that sends without TLS (``use_tls`` false) puts
    credentials and recovery links on the wire in clear: refused, whatever
    selected it (the composition's ``DEFAULT_INFRA``, a pin, or ``INFRA``)."""
    try:
        instances = infra_config.resolved_instances(cfg, "notification")
    except infra_config.InfraConfigError:
        return []  # a malformed INFRA is refused by loading the configuration
    return [
        f"notification/{name}: use_tls is false — recovery links and SMTP "
        "credentials travel in clear; remove use_tls (it defaults to on) or set "
        "it true"
        for name, inst in sorted(instances.items())
        if inst.options.get("use_tls") is False
    ]


def config_problems(cfg: Any) -> tuple[list[str], list[str]]:  # noqa: C901
    """``(errors, warnings)`` of ``cfg`` against ``multi_tenant_strict``.

    Pure (no I/O): evaluated at boot in prod
    (:func:`sandloom.core.app_factory.enforce_configuration`) and by tests.
    ``errors`` abort startup; ``warnings`` are logged.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if getattr(cfg, "tenancy_mode", "multi") != "multi":
        errors.append(
            "TENANCY_MODE must be 'multi' (multi_tenant_strict isolates tenants)"
        )
    if getattr(cfg, "auth_revocation_fail_closed", None) is False:
        errors.append(
            "AUTH_REVOCATION_FAIL_CLOSED=false accepts revoked tokens during a "
            "cache outage; unset it (closed in prod) or set it to true"
        )
    infra = getattr(cfg, "infra", None) or {}
    for name, options in sorted(_database_instances(infra).items()):
        if options.get("tenant_rls") is not True:
            errors.append(
                f"database/{name}: tenant_rls is off — set "
                f'INFRA={{"database": {{"{name}": {{"tenant_rls": true}}}}}} '
                "(Row-Level Security on every tenant table, app role without "
                "BYPASSRLS / ownership; see docs/advanced/security-model.md)"
            )
    errors += _atomic_store_problems(cfg, infra, warnings)
    errors += _notification_problems(cfg)
    if getattr(cfg, "service_bindings", None):
        if getattr(cfg, "contract_compat_check", "ready") == "off":
            errors.append(
                "CONTRACT_COMPAT_CHECK=off: a split deployment must check remote "
                "contract compatibility (ready or strict)"
            )
        if not getattr(cfg, "jwt_asymmetric", False):
            errors.append(
                f"split deployment signs access tokens with a shared "
                f"{getattr(cfg, 'jwt_algorithm', 'HS*')} secret (any service "
                "holding JWT_SECRET can mint tokens): set JWT_ALGORITHM=EdDSA "
                "(or RS256 / ES256) with JWT_PUBLIC_KEYS on every service and "
                "JWT_PRIVATE_KEY on the issuer only (`sandloom keys generate`)"
            )
    if getattr(cfg, "service_bindings", None) or getattr(
        cfg, "rpc_dh_private_key", None
    ):
        if getattr(cfg, "rpc_commands_require_signature", "auto") == "off":
            errors.append(
                "RPC_COMMANDS_REQUIRE_SIGNATURE=off: a leaked provider X25519 "
                "key could forge COMMAND calls from any caller (ADR-0004, key-"
                "compromise impersonation)"
            )
        stale = rpc_pair_mac.stale_extra_kids(cfg)
        if stale:
            errors.append(
                f"RPC_DH_EXTRA_PRIVATE_KEYS {stale} are no longer published in "
                "RPC_SERVICE_DH_KEYS: a retired key still derives every caller's "
                "pair key towards this service — drop it (rotation overlap is "
                "bounded, docs/adr/0004)"
            )
    if accepted_risks.accepts(cfg, "shared_hs_secret_split"):
        errors.append(
            "the accepted risk shared_hs_secret_split is not allowed: a split "
            "deployment must sign access tokens asymmetrically"
        )
    return errors, warnings


def tenant_scoped_tables() -> list[str]:
    """Table names of every mapped ``TenantScoped`` model (imported so far)."""

    names: set[str] = set()
    stack: list[type] = list(_tenant.TenantScoped.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        table = getattr(cls, "__table__", None)
        name = getattr(table, "name", None)
        if isinstance(name, str):
            names.add(name)
    return sorted(names)


async def verify_tenant_rls_coverage(
    core: Any, tables: Iterable[str] | None = None
) -> list[str]:
    """Every ``TenantScoped`` table found in a database carries forced RLS.

    Checks each ``tenant_rls`` database instance for the tables that exist in
    it; returns the problems (empty = covered). A table found in NO database
    is reported too (migrations not applied, or a database without
    ``tenant_rls``).
    """
    wanted = sorted(set(tenant_scoped_tables() if tables is None else tables))
    if not wanted:
        return []
    infra = core.infra
    problems: list[str] = []
    seen: set[str] = set()
    for name in infra.instances("database"):
        provider = infra.provider("database", name)
        check = getattr(provider, "tenant_rls_coverage", None)
        if check is None:
            continue
        present, found = await check(wanted)
        seen |= set(present)
        problems += [f"database/{name}: {p}" for p in found]
    missing = [t for t in wanted if t not in seen]
    if missing:
        problems.append(
            f"TenantScoped table(s) {missing} not found in any tenant_rls database"
        )
    return problems


async def enforce_runtime(core: Any) -> None:
    """Startup half of ``multi_tenant_strict`` (critical startup task)."""
    cfg = core.settings
    if not strict_profile_active(cfg):
        return
    _errors, warnings = config_problems(cfg)
    for warning in warnings:
        logger.warning("multi_tenant_strict: %s", warning)
    problems = await verify_tenant_rls_coverage(core)
    if problems:
        raise ProductionProfileError(
            "PRODUCTION_PROFILE=multi_tenant_strict: Row-Level Security does not "
            "cover every tenant table: " + "; ".join(problems)
        )
    # Distinct host strings do not establish distinct Redis servers: DNS
    # aliases and proxies can resolve both URLs to the same process, so each
    # instance names its own failure and eviction domain (isolation_identity).
    await _attest_separate_from_default(core, "security")
    if core.infra.instance_config("atomic_store", "idempotency") is not None:
        await _attest_separate_from_default(core, "idempotency")


async def _attest_separate_from_default(core: Any, role: str) -> None:
    """``atomic_store/<role>`` declares ``never_evicts`` and ``shared`` and
    differs from the module cache by ``isolation_identity()``.

    A ``None`` or raising identity fails: an unknown domain is not attested.
    """
    try:
        store = core.infra.provider("atomic_store", role)
        default = core.infra.provider("cache", "default")
        identity = await store.isolation_identity()
        default_identity = await default.isolation_identity()
    except Exception as exc:
        raise ProductionProfileError(
            f"Cannot attest the {role} atomic_store apart from the default cache: "
            "its isolation_identity() failed; check both instances' "
            "reachability and credentials"
        ) from exc
    if not identity or not default_identity:
        raise ProductionProfileError(
            f"Cannot attest the {role} atomic_store apart from the default cache: "
            "a driver returned no isolation identity"
        )
    missing = sorted(set(REQUIRED_STORE_FEATURES) - set(store.features()))
    if missing:
        raise ProductionProfileError(
            f"The {role} atomic_store driver does not declare {missing}: "
            "revocation marks and idempotency records need a store that is "
            "shared and never evicts"
        )
    if identity == default_identity:
        raise ProductionProfileError(
            f"The {role} atomic_store and the default cache share one failure "
            "domain (same isolation identity); configure separate servers "
            "(different DB indexes or DNS aliases do not isolate eviction "
            "policies)"
        )


__all__ = [
    "REQUIRED_STORE_FEATURES",
    "ProductionProfileError",
    "atomic_store_feature_problems",
    "atomic_store_security_problem",
    "config_problems",
    "enforce_runtime",
    "tenant_scoped_tables",
    "verify_tenant_rls_coverage",
]
