"""Which machines a database URL can connect to.

One reading of a URL's hosts for every check that asks whether a database is
on this machine: ``sandloom dev token`` (its gate and its child process) and
the startup refusal of an unset ``ENVIRONMENT`` with a remote database
(SANDLOOM-B043). Read the way SQLAlchemy reads a URL, never a second parser: a
password holding ``/`` or ``#`` puts the host elsewhere for a hand-written one.

The judgement is an allow-list: a url is on this machine only when every
connection target it names is (a loopback address, ``localhost``, a unix socket
directory), or when it is a file database. Whatever the url does not let us
read, a ``dsn`` or ``service`` parameter that takes the connection from
elsewhere, a backend that names no host, is not shown to be local, so it is
treated as remote.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.engine import URL

#: Query parameters that move the connection out of the url: ``dsn`` is a whole
#: connection string (asyncpg), ``service`` a ``pg_service.conf`` entry (libpq).
#: Either can name any host, whatever the url's own authority says.
_INDIRECT_PARAMETERS = ("dsn", "service")


def _query_hosts(value: str | tuple[str, ...] | None) -> list[str]:
    """The comma-separated hosts of one query parameter (a repeated parameter
    is a tuple)."""
    values = (value,) if isinstance(value, str) else (value or ())
    return [host.strip() for text in values for host in text.split(",") if host.strip()]


def _parse(url: str) -> URL:
    """``url`` as SQLAlchemy reads it; ``ValueError`` when it does not parse.

    ``sqlalchemy`` is imported here, not at the top: the CLI loads this module
    for every ``sandloom dev`` command, and only a token run or a boot check
    with ``ENVIRONMENT`` unset reads a database url."""
    import sqlalchemy as sa  # noqa: PLC0415 - cold: the CLI loads this module for every `sandloom dev` command; only a token run or a boot check with ENVIRONMENT unset reads a database url

    try:
        return sa.engine.make_url(url)
    except (
        sa.exc.ArgumentError,
        ValueError,
    ) as exc:  # a multi-host list with ports, no scheme
        raise ValueError("does not parse") from exc


def _named_hosts(parsed: URL) -> list[str]:
    """The hosts ``parsed`` names itself: its authority (a comma list
    included) and its ``host`` / ``hostaddr`` query parameters."""
    return [
        *_query_hosts(parsed.host),
        *_query_hosts(parsed.query.get("host")),
        *_query_hosts(parsed.query.get("hostaddr")),
    ]


def database_hosts(url: str, pghost: str | None = None) -> list[str]:
    """Every host the database driver can connect to for ``url``: the host of
    its authority (a comma list included) and its ``host`` / ``hostaddr``
    query parameters. A PostgreSQL url that names none connects where
    ``PGHOST`` says, so those hosts are ``pghost``'s. Empty for a file
    database or a default unix socket. This lists the named hosts only: a
    ``dsn`` / ``service`` parameter is no host, :func:`remote_database_target`
    judges it. ``ValueError`` for a url that does not parse."""
    parsed = _parse(url)
    hosts = _named_hosts(parsed)
    if not hosts and parsed.get_backend_name() == "postgresql":
        hosts = _query_hosts(pghost)
    return hosts


def is_local_host(host: str) -> bool:
    """Whether ``host`` is this machine: a unix socket directory, ``localhost``
    or a loopback address (127.0.0.0/8, ``::1``)."""
    if host.startswith("/"):  # a unix socket directory
        return True
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def remote_database_target(url: str, pghost: str | None = None) -> str | None:
    """What of ``url`` is not shown to be on this machine, as a phrase that
    follows "points at" (``None`` when it all is). Never the url: it carries
    credentials.

    * a file database (``sqlite``) is on this machine;
    * a ``dsn`` or ``service`` query parameter takes the connection from
      elsewhere, so the url proves nothing;
    * a PostgreSQL url takes its hosts from the url, else from ``PGHOST``
      (``pghost``), and none at all is libpq's default unix socket;
    * every other backend must name a host: one that names none is not shown
      to be local, and so is refused.

    ``ValueError`` for a url that does not parse."""
    parsed = _parse(url)
    backend = parsed.get_backend_name()
    if backend == "sqlite":
        return None
    indirect = [key for key in parsed.query if key.lower() in _INDIRECT_PARAMETERS]
    if indirect:
        return f"a {indirect[0].lower()!r} query parameter (it can name any host)"
    hosts = _named_hosts(parsed)
    if not hosts and backend == "postgresql":
        hosts = _query_hosts(pghost)
        if not hosts:
            return None  # the default unix socket
    if not hosts:
        return f"a {backend} url that names no host"
    for host in hosts:
        if not is_local_host(host):
            return f"host {host!r}"
    return None
