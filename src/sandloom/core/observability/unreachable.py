"""Whether an exception means PostgreSQL or Redis cannot be reached, and the
one line that says so (SANDLOOM-R014).

Only the outermost exception decides: a driver's or SQLAlchemy's error, the
kernel's ``DatabaseUnavailable`` / ``BrokerUnavailableError``, or an
``OSError`` / ``TimeoutError``; the reason is read from it and the exceptions
it was raised ``from`` (``__cause__``) or wraps (SQLAlchemy's ``.orig``), never
from ``__context__``. :func:`unreachable` looks inside any exception's
``__cause__`` chain instead (a startup task's ``ContractCallError`` raised
from a ``DatabaseUnavailable``). The ``sandloom`` CLI
(:mod:`sandloom.core.cli.dev.errors`) and the startup lifecycle use it.
"""

from __future__ import annotations

import errno
import re
import traceback
from collections.abc import Iterator

CODE = "SANDLOOM-R014"

#: Exception class names that mean "the server is not there / refused us".
_PG_ERRORS = {
    "InvalidPasswordError": "authentication failed",
    "InvalidAuthorizationSpecificationError": "authentication failed",
    "AuthenticationError": "authentication failed",
    "AuthenticationWrongNumberOfArgsError": "authentication failed",
    "InvalidCatalogNameError": "database does not exist",
    "CannotConnectNowError": "the server is starting up",
}
#: Packages whose exceptions may stand outermost for a connection failure.
_DRIVERS = ("sqlalchemy", "asyncpg", "psycopg2", "psycopg", "redis", "kombu")
#: The kernel's own "backend unavailable" wrappers (raised ``from`` the
#: driver's error), which may stand outermost too.
_WRAPPERS = {
    ("sandloom.core.exceptions", "DatabaseUnavailable"),
    ("sandloom.core.infra.contracts", "BrokerUnavailableError"),
}
#: Reasons that mean nothing answers at the address.
_UNREACHABLE = {
    "connection refused",
    "timed out",
    "unknown host",
    "connection error",
    "the server is starting up",
}
_ADDRESS = re.compile(
    r"""(?:\(\s*['"]?(?P<h1>[\w.:-]+?)['"]?\s*,\s*(?P<p1>\d+)\s*\))"""
    r"""|(?:(?:host|server at|at)\s+"?(?P<h2>[\w.-]+)"?.*?port\s+(?P<p2>\d+))"""
    r"""|(?:(?P<h3>\d+\.\d+\.\d+\.\d+|localhost|\[[0-9a-f:]+\]):(?P<p3>\d+))""",
    re.IGNORECASE,
)


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """``exc``, then what it was raised from or wraps, outermost first:
    ``.orig`` (SQLAlchemy) and ``__cause__`` only, never ``__context__``."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        orig = getattr(current, "orig", None)
        nxt = orig if isinstance(orig, BaseException) else current.__cause__
        current = nxt if isinstance(nxt, BaseException) else None


def _driver_error(exc: BaseException) -> bool:
    """Whether ``exc`` may stand outermost for a connection failure."""
    if isinstance(exc, OSError | TimeoutError):
        return True
    if any((cls.__module__, cls.__name__) in _WRAPPERS for cls in type(exc).__mro__):
        return True
    return any(
        (getattr(cls, "__module__", "") or "").startswith(_DRIVERS)
        for cls in type(exc).__mro__
    )


def _module_of(exc: BaseException) -> str:
    return type(exc).__module__ or ""


def _service(exc: BaseException, chain: list[BaseException]) -> str | None:
    for item in chain:
        names = {cls.__name__ for cls in type(item).__mro__}
        if "DatabaseUnavailable" in names:
            return "PostgreSQL"
        if "BrokerUnavailableError" in names:
            return "Redis"
        module = _module_of(item)
        if module.startswith(("redis", "kombu")):
            return "Redis"
        if module.startswith(("asyncpg", "psycopg2", "psycopg")):
            return "PostgreSQL"
    files = " ".join(
        frame.filename
        for item in chain
        for frame in traceback.extract_tb(item.__traceback__)
    )
    if "/redis/" in files or "/kombu/" in files:
        return "Redis"
    if any(name in files for name in ("/asyncpg/", "/psycopg2/", "/psycopg/")):
        return "PostgreSQL"
    return None


def _reason(item: BaseException) -> str | None:
    name = type(item).__name__
    if name in _PG_ERRORS:
        return _PG_ERRORS[name]
    if isinstance(item, ConnectionRefusedError) or (
        isinstance(item, OSError) and item.errno == errno.ECONNREFUSED
    ):
        return "connection refused"
    if isinstance(item, TimeoutError) or name in {"TimeoutError", "ConnectTimeout"}:
        return "timed out"
    if isinstance(item, OSError) and (
        "nodename nor servname" in str(item) or "Name or service" in str(item)
    ):
        return "unknown host"
    return _text_reason(item, name)


def _text_reason(item: BaseException, name: str) -> str | None:
    """The reason a driver states only in its message."""
    text = str(item)
    if any(
        mark in text
        for mark in ("password authentication failed", "WRONGPASS", "NOAUTH")
    ) or ("invalid password" in text):
        return "authentication failed"
    if re.search(r'database ".*" does not exist', text):
        return "database does not exist"
    if "Connection refused" in text or "Connect call failed" in text:
        return "connection refused"
    if name == "OperationalError" and "could not connect" in text:
        return "connection refused"
    if name == "ConnectionError" and _module_of(item).startswith("redis"):
        return "connection refused" if "refused" in text.lower() else "connection error"
    return None


def _address(chain: list[BaseException]) -> str | None:
    for item in chain:
        match = _ADDRESS.search(str(item))
        if match:
            host = match.group("h1") or match.group("h2") or match.group("h3")
            port = match.group("p1") or match.group("p2") or match.group("p3")
            return f"{host}:{port}"
    return None


def _database(chain: list[BaseException]) -> str | None:
    for item in chain:
        match = re.search(r'database "([^"]+)" does not exist', str(item))
        if match:
            return match.group(1)
    return None


def connection_failure(exc: BaseException) -> str | None:
    """The one-line message for ``exc``, or ``None`` when it is not a
    failure to reach PostgreSQL / Redis (see the module doc)."""
    if not _driver_error(exc):
        return None
    chain = list(_chain(exc))
    reason = next((r for r in (_reason(item) for item in chain) if r), None)
    if reason is None:
        return None
    service = _service(exc, chain)
    if service is None:
        return None
    where = _address(chain)
    at = f" at {where}" if where else ""
    if reason == "authentication failed":
        return (
            f"error: {service}{at} refused the login (authentication failed): "
            f"check the user and password in the URL   [{CODE}]"
        )
    if reason == "database does not exist":
        name = _database(chain)
        which = f"the database {name}" if name else "the database"
        return (
            f"error: {which} does not exist on {service}{at}: uv run sandloom "
            "dev up creates it (else create it, then uv run sandloom migrate "
            f"upgrade-all --apply)   [{CODE}]"
        )
    if reason not in _UNREACHABLE:
        return None
    return (
        f"error: cannot reach {service}{at} ({reason}). Start the dev services: "
        f"uv run sandloom dev up   [{CODE}]"
    )


def unreachable(exc: BaseException) -> str | None:
    """The :func:`connection_failure` line of ``exc`` or of an exception it
    was raised from, without the ``error:`` prefix; ``None`` otherwise."""
    for item in _chain(exc):
        line = connection_failure(item)
        if line is not None:
            return line.removeprefix("error: ")
    return None


__all__ = ["CODE", "connection_failure", "unreachable"]
