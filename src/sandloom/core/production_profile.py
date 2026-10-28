"""Production profiles: the multi-tenant trust model as an enforced contract.

``PRODUCTION_PROFILE`` names the guarantees a production deployment promises
its tenants. ``standard`` (default) keeps every existing knob as configured.
``multi_tenant_strict`` turns the documented trust model
(``docs/reference/security-model.md`` — "Production profile") into startup
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
4. security-critical cache state (revocation marks, user blocks, idempotency
   records — contract-command dedupe is already durable in the provider
   database) on its own cache instance
   (``SECURITY_CACHE_INSTANCE``) whose Redis server differs from the
   best-effort cache's — an evicting cache can never drop a revocation mark;
   when ``IDEMPOTENCY_CACHE_INSTANCE`` moves the idempotency records out, that
   instance must meet the same requirement (declared, its own Redis server,
   attested at startup);
5. split deployments (``SERVICE_BINDINGS`` set): the contract compatibility
   check on (``CONTRACT_COMPAT_CHECK`` not ``off``) and asymmetric token
   signing (``JWT_ALGORITHM`` EdDSA / RS256 / ES256: ``JWT_PRIVATE_KEY`` on
   the issuer only, ``JWT_PUBLIC_KEYS`` everywhere) so a compromised
   non-issuer service cannot mint tokens. The accepted risk
   ``shared_hs_secret_split`` is refused. A process that loads no token
   issuer is split too even without bindings: the kernel's startup role check
   (:func:`sandloom.core.auth.tokens.token_signing_problem`) refuses an HS secret
   there under this profile, with no opt-out.

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
from typing import Any
from urllib.parse import urlsplit

import sandloom.core.tenant as _tenant
from sandloom.core import rpc_pair_mac
from sandloom.core.config import accepted_risks
from sandloom.core.config.common import strict_profile_active

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


def _separate_cache_problems(
    cfg: Any, caches: object, name: str, setting: str
) -> list[str]:
    """``name`` is declared with a url on a Redis server other than ``default``'s."""
    declared = caches.get(name) if isinstance(caches, Mapping) else None
    if not isinstance(declared, Mapping) or not declared.get("url"):
        return [f'{setting}={name!r} is not declared with a url in INFRA["cache"]']
    default_opts = caches.get("default") if isinstance(caches, Mapping) else None
    default_url = (
        default_opts.get("url")
        if isinstance(default_opts, Mapping) and default_opts.get("url")
        else getattr(cfg, "cache_url", None)
    )
    if _server(declared.get("url")) == _server(default_url):
        return [
            f"cache/{name} runs on the same Redis server as the default cache; "
            "eviction / maxmemory policies are per server, so it must be a "
            "separate (noeviction, replicated) Redis"
        ]
    return []


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
                "BYPASSRLS / ownership; see docs/reference/security-model.md)"
            )
    security = str(getattr(cfg, "security_cache_instance", "default") or "default")
    caches = infra.get("cache") or {}
    if security == "default":
        errors.append(
            "SECURITY_CACHE_INSTANCE is 'default': revocation marks, user blocks "
            "and idempotency records share the best-effort cache — declare a "
            'separate instance, e.g. INFRA={"cache": {"security": {"driver": '
            '"redis", "url": "redis://security-redis:6379/0"}}} and '
            "SECURITY_CACHE_INSTANCE=security"
        )
    else:
        errors += _separate_cache_problems(
            cfg, caches, security, "SECURITY_CACHE_INSTANCE"
        )
    idem = str(getattr(cfg, "idempotency_cache_instance", "") or "")
    if idem and idem != security:
        if idem == "default":
            errors.append(
                "IDEMPOTENCY_CACHE_INSTANCE is 'default': idempotency records "
                "would live in the best-effort (evicting) cache — declare a "
                'separate instance, e.g. INFRA={"cache": {"idempotency": '
                '{"driver": "redis", "url": "redis://idempotency-redis:6379/0"}}} '
                "and IDEMPOTENCY_CACHE_INSTANCE=idempotency, or unset it to keep "
                "them in the security cache"
            )
        else:
            errors += _separate_cache_problems(
                cfg, caches, idem, "IDEMPOTENCY_CACHE_INSTANCE"
            )
            idem_opts = caches.get(idem) if isinstance(caches, Mapping) else None
            sec_opts = caches.get(security) if isinstance(caches, Mapping) else None
            if (
                isinstance(idem_opts, Mapping)
                and isinstance(sec_opts, Mapping)
                and _server(idem_opts.get("url")) is not None
                and _server(idem_opts.get("url")) == _server(sec_opts.get("url"))
            ):
                warnings.append(
                    f"cache/{idem} runs on the same Redis server as "
                    f"cache/{security}: idempotency writes still queue the "
                    "revocation-mark reads on one Redis thread — point "
                    "IDEMPOTENCY_CACHE_INSTANCE at its own Redis or unset it"
                )
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
    # aliases and proxies can resolve both URLs to the same Redis process.
    security_name = cfg.security_cache_instance
    await _attest_separate_from_default(core, security_name, "security")
    idem_name = getattr(cfg, "idempotency_cache_instance", "") or ""
    if idem_name and idem_name != security_name:
        await _attest_separate_from_default(core, idem_name, "idempotency")


async def _attest_separate_from_default(core: Any, name: str, kind: str) -> None:
    """Cache instance ``name`` reaches a different Redis process than ``default``."""
    try:
        provider = core.infra.provider("cache", name)
        default = core.infra.provider("cache", "default")
        identity = await provider.deployment_identity()
        default_identity = await default.deployment_identity()
    except Exception as exc:
        raise ProductionProfileError(
            f"Cannot attest separate {kind} Redis processes; permit INFO server "
            "for both cache credentials and check their reachability"
        ) from exc
    if identity == default_identity:
        raise ProductionProfileError(
            f"{kind.capitalize()} cache and default cache reached the same Redis "
            "process; configure separate Redis processes/instances (different DB "
            "indexes or DNS aliases do not isolate eviction policies)"
        )


__all__ = [
    "ProductionProfileError",
    "config_problems",
    "enforce_runtime",
    "tenant_scoped_tables",
    "verify_tenant_rls_coverage",
]
