"""SQLAlchemy URL conversion for Alembic (leaf module, stdlib only).

Re-exported by :mod:`sandloom.core.migrations.migrations`; imported directly by
:mod:`sandloom.core.migrations.alembic_support` so the Alembic env helpers do not
import the migration runner.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode

#: The sync PostgreSQL driver migrations run on, always spelled out: a bare
#: ``postgresql://`` resolves to a *different* DBAPI depending on the SQLAlchemy
#: release (psycopg2 in 2.0, psycopg 3 in 2.1), so the framework never relies
#: on the dialect default.
SYNC_POSTGRES_DRIVER = "postgresql+psycopg2"

_IMPLICIT_POSTGRES_SCHEMES = frozenset(
    {"postgresql", "postgres", "postgresql+asyncpg", "postgres+asyncpg"}
)


def to_sync_url(url: str) -> str:
    """Convert a SQLAlchemy DSN to the explicit sync form Alembic needs.

    ``postgresql+asyncpg://`` and a bare ``postgresql://`` become
    ``postgresql+psycopg2://``; any other explicit driver (``+psycopg``,
    ``+psycopg2``, ``sqlite://`` …) is returned unchanged.
    """
    scheme, sep, rest = url.partition("://")
    if not sep or scheme.lower() not in _IMPLICIT_POSTGRES_SCHEMES:
        return url
    if scheme.lower().endswith("+asyncpg"):
        rest = _asyncpg_ssl_to_libpq(rest)
    return f"{SYNC_POSTGRES_DRIVER}://{rest}"


#: asyncpg's ``ssl=`` URL values -> libpq ``sslmode`` (psycopg2 rejects ``ssl=``).
_ASYNCPG_SSL_TO_SSLMODE = {
    "true": "require",
    "false": "disable",
    "disable": "disable",
    "allow": "allow",
    "prefer": "prefer",
    "require": "require",
    "verify-ca": "verify-ca",
    "verify-full": "verify-full",
}


def _asyncpg_ssl_to_libpq(rest: str) -> str:
    """Rewrite an asyncpg ``?ssl=<mode>`` query parameter as libpq ``sslmode``."""
    base, qsep, query = rest.partition("?")
    if not qsep:
        return rest
    pairs = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key == "ssl":
            key, value = "sslmode", _ASYNCPG_SSL_TO_SSLMODE.get(value.lower(), value)
        pairs.append((key, value))
    return f"{base}?{urlencode(pairs)}"
