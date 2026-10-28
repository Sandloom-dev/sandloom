"""Constants and configuration shared by every implementation and the runner.

All servers read the SAME environment variables (``DATABASE_URL``,
``CACHE_URL``, ``JWT_SECRET``, ``DB_POOL_SIZE``, ``DB_MAX_OVERFLOW``) — the Sandloom
app reads them through its own settings, the bare apps through this module — so
no implementation gets a differently sized pool or a different backend.

Scenario semantics (identical across implementations):

S1  ``GET /s1``          -> 200 ``{"ok": true}``
S2  ``GET /s2``          -> HS256 JWT verify (``token_type == "access"``,
                            ``iat`` present) + ONE Redis ``GET`` of the
                            subject's revocation mark (Sandloom's key layout);
                            200 ``{"ok": true, "user_id": "<sub>"}``, 401 when
                            missing/invalid/revoked
S3  ``GET /s3/{id}``     -> one indexed ``SELECT`` by primary key of a small
                            row; 200 ``{"id", "name", "value"}``, 404 if absent
S4  ``POST /s4``         -> ``{"name", "value"}`` body; INSERT a
                            ``bench_writes`` row AND an outbox row in ONE
                            transaction; 201 ``{"id": <new id>}``
S5  ``GET /s5/{id}``     -> the realistic SaaS read (Sandloom only): access JWT
                            (``get_current_user_id``), ``TenantScope()``,
                            ``require(Subscribed(of=Account.user()))`` (claim-cheap), one PK
                            read of the ``TenantScoped`` table
                            ``bench_tenant_items`` under PostgreSQL RLS, the
                            app connected as the NON-superuser, NOBYPASSRLS
                            role ``S5_APP_ROLE``. 200 ``{"id","name","value"}``;
                            404 for another tenant's id; 401/402/403 for a
                            bad token / no subscription / foreign tenant.
                            Variants: ``/s5`` (DbSession), ``/s5ro``
                            (ReadOnlyDbSession, ``DB_READ_ONLY_SESSIONS=true``),
                            ``/s5fast`` (raw asyncpg, BEGIN READ ONLY +
                            set_config merged into one round trip), ``/s5fn``
                            (one round trip through a plpgsql function that
                            sets the tenant GUC before its query).
"""

from __future__ import annotations

import os
import re

DEFAULT_DATABASE_URL = "postgresql+asyncpg://bench:bench@127.0.0.1:5432/bench"
DEFAULT_CACHE_URL = "redis://127.0.0.1:6379/0"
# Local-only default. The workflow generates a random secret per job.
DEFAULT_JWT_SECRET = "bench-local-secret-0123456789abcdef0123456789abcdef"

#: Mirrors ``sandloom.core.auth.marks.REVOCATION_KEY_PREFIX`` (asserted by
#: ``benchsuite.check_versions``) so the bare apps do the same Redis lookup.
REVOCATION_KEY_PREFIX = "sandloom:auth:tokens_valid_after:"

#: The S4 event channel (Sandloom registers its schema; bare apps store it verbatim).
EVENT_CHANNEL = "bench.item.created.v1"

#: Rows seeded into ``bench_items`` (S3 picks ids uniformly in 1..9999).
SEED_ROWS = 10_000

SCENARIOS = ("s1", "s2", "s3", "s4")

# --- S5 (secure tenant read) ------------------------------------------------ #
#: Tenants seeded into ``bench_tenant_items``; tenant ``i`` owns the ids
#: ``i*S5_ROWS_PER_TENANT + 1 .. (i+1)*S5_ROWS_PER_TENANT``.
S5_TENANTS = (
    "5e000000-0000-4000-8000-00000000000a",
    "5e000000-0000-4000-8000-00000000000b",
)
S5_ROWS_PER_TENANT = 5_000
#: The application role S5 connects as (never a superuser, never BYPASSRLS).
#: Cluster-wide (roles are not per database): override it (``S5_APP_ROLE``)
#: when several stands share one PostgreSQL server.
S5_APP_ROLE = os.environ.get("S5_APP_ROLE", "bench_app")
S5_APP_PASSWORD_DEFAULT = "bench-app-local-password"
#: The ``bench`` module's S5 routes, by variant.
S5_ROUTES = {
    "uow": "/s5/",
    # the S5 read as a class-R route with a 5 s revocation budget
    "bud": "/s5bud/",
    "ro": "/s5ro/",
    "fast": "/s5fast/",
    "fn": "/s5fn/",
    # execution planner: the declared ReadOperation on the general (ORM)
    # executor, and the same route with PLANNER_OPTIMIZED_EXECUTOR=true
    "plan": "/s5plan/",
    "planopt": "/s5plan/",
    # TWO reads per request: the ORM route, and its PlannedReads twin on the
    # general / optimized executor
    "uow2": "/s5two/",
    "plan2": "/s5plan2/",
    "planopt2": "/s5plan2/",
}

#: S5 cost-attribution stages (DIAGNOSTIC ONLY, ``benchsuite.bench.api.diag_s5``):
#: stage -> (route, tenant_rls + app role, revocation check on).
S5_DIAG_STAGES = {
    "d0": ("/d5/0/", False, False),
    "d1": ("/d5/1/", False, False),
    "d2": ("/d5/2/", False, False),
    "d3": ("/d5/2/", True, False),
    "d4": ("/d5/2/", True, True),
    "d5": ("/d5/5/", True, True),
    "d6": ("/s5/", True, True),
}


def s5_app_password() -> str:
    return os.environ.get("S5_APP_PASSWORD", S5_APP_PASSWORD_DEFAULT)


def s5_tenant_of(item_id: int) -> str:
    """The tenant owning ``item_id`` in the S5 seed."""
    return S5_TENANTS[(item_id - 1) // S5_ROWS_PER_TENANT]


def s5_app_database_url() -> str:
    """``DATABASE_URL`` rewritten to log in as the S5 application role."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(raw_database_url())
    host = parts.hostname or "127.0.0.1"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{S5_APP_ROLE}:{s5_app_password()}@{host}{port}"
    return urlunsplit(parts._replace(netloc=netloc))


def raw_database_url() -> str:
    """``DATABASE_URL`` exactly as configured (Sandloom's sync migration runner
    goes through psycopg2, which rejects asyncpg's ``ssl=`` option)."""
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def database_url() -> str:
    """``DATABASE_URL`` with TLS negotiation disabled unless set explicitly.

    The benchmark Postgres has no TLS. asyncpg's default ``sslmode=prefer``
    probes TLS on connect and then swaps the transport's protocol, which
    rloop's transport does not implement (``NotImplementedError: TCPTransport
    protocol cannot be changed``) — so every pool connect under granian
    ``--loop rloop`` would crash. ``ssl=disable`` is applied to ALL
    implementations alike (it only affects connection setup, not requests).
    """
    url = raw_database_url()
    if "ssl=" not in url and "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "ssl=disable"
    return url


def asyncpg_dsn() -> str:
    """``DATABASE_URL`` as a libpq-style DSN for raw asyncpg.

    SQLAlchemy's asyncpg dialect takes ``ssl=``; a raw asyncpg DSN takes the
    libpq name ``sslmode=`` (an unknown ``ssl`` would be sent as a server GUC).
    """
    dsn = database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    return re.sub(r"([?&])ssl=", r"\1sslmode=", dsn)


def cache_url() -> str:
    return os.environ.get("CACHE_URL", DEFAULT_CACHE_URL)


def broker_url() -> str:
    """Sandloom's Celery/broker URL (unused by the bare apps)."""
    return os.environ.get("BROKER_URL", cache_url())


def jwt_secret() -> str:
    return os.environ.get("JWT_SECRET", DEFAULT_JWT_SECRET)


def pool_size() -> int:
    """Same default as Sandloom's ``DB_POOL_SIZE``."""
    return int(os.environ.get("DB_POOL_SIZE", "10"))


def max_overflow() -> int:
    """Same default as Sandloom's ``DB_MAX_OVERFLOW``."""
    return int(os.environ.get("DB_MAX_OVERFLOW", "20"))


def cache_max_connections() -> int:
    """Same default as Sandloom's ``CACHE_MAX_CONNECTIONS``."""
    return int(os.environ.get("CACHE_MAX_CONNECTIONS", "64"))
