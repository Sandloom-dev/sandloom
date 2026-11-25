"""Which machines a database URL can connect to.

One reading of a URL's hosts for every check that asks whether a database is
on this machine: ``sandloom dev token`` (its gate and its child process) and
the startup refusal of an unset ``ENVIRONMENT`` with a remote database
(SANDLOOM-B043). Read the way SQLAlchemy reads a URL, never a second parser: a
password holding ``/`` or ``#`` puts the host elsewhere for a hand-written one.
"""

from __future__ import annotations

import ipaddress


def _query_hosts(value: str | tuple[str, ...] | None) -> list[str]:
    """The comma-separated hosts of one query parameter (a repeated parameter
    is a tuple)."""
    values = (value,) if isinstance(value, str) else (value or ())
    return [host.strip() for text in values for host in text.split(",") if host.strip()]


def database_hosts(url: str, pghost: str | None = None) -> list[str]:
    """Every host the database driver can connect to for ``url``: the host of
    its authority (a comma list included) and its ``host`` / ``hostaddr``
    query parameters. A PostgreSQL url that names none connects where
    ``PGHOST`` says, so those hosts are ``pghost``'s. Empty for a file
    database or a default unix socket. ``ValueError`` for a url that does not
    parse."""
    import sqlalchemy as sa  # noqa: PLC0415 - cold: the CLI loads this module for every `sandloom dev` command; only a token run or a boot check with ENVIRONMENT unset reads a database url

    try:
        parsed = sa.engine.make_url(url)
    except (
        sa.exc.ArgumentError,
        ValueError,
    ) as exc:  # a multi-host list with ports, no scheme
        raise ValueError("does not parse") from exc
    hosts = [
        *_query_hosts(parsed.host),
        *_query_hosts(parsed.query.get("host")),
        *_query_hosts(parsed.query.get("hostaddr")),
    ]
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


def remote_database_host(url: str, pghost: str | None = None) -> str | None:
    """The first host of ``url`` that is not this machine, or ``None`` when
    every host is local (a file database and a default unix socket have none).
    ``ValueError`` for a url that does not parse."""
    for host in database_hosts(url, pghost):
        if not is_local_host(host):
            return host
    return None
