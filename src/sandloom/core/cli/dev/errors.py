"""One line instead of a traceback when the CLI cannot reach PostgreSQL / Redis.

The ``sandloom`` entry point (:func:`sandloom.main`) and ``sandloom migrate``
hand an uncaught exception to :func:`connection_failure`. Only the outermost
exception decides: it must be a driver's or SQLAlchemy's error, the kernel's
``DatabaseUnavailable`` / ``BrokerUnavailableError``, or an ``OSError`` /
``TimeoutError``; its reason is read from it and the exceptions
it was raised ``from`` (``__cause__``) or wraps (SQLAlchemy's ``.orig``),
never from ``__context__`` (an error raised while handling a connection
failure is a real error and keeps its traceback). Three one-liners::

    error: cannot reach PostgreSQL at 127.0.0.1:5432 (connection refused).
    Start the dev services: uv run sandloom dev up   [SANDLOOM-R014]

    error: PostgreSQL at 127.0.0.1:5432 refused the login (authentication
    failed): check the user and password in the URL   [SANDLOOM-R014]

    error: the database on PostgreSQL at 127.0.0.1:5432 does not exist:
    uv run sandloom dev up creates it (else create it, then uv run sandloom
    migrate upgrade-all --apply)   [SANDLOOM-R014]

(each one line; wrapped here) and exit code 1. ``--debug`` before the
subcommand (``sandloom --debug migrate ...``) or ``SANDLOOM_DEBUG=1`` shows the
traceback instead; a ``--debug`` after the subcommand belongs to it
(``sandloom test -- --debug`` reaches pytest).

:func:`warn_env_drift` warns when an exported ``DATABASE_URL`` /
``CACHE_URL`` / ``BROKER_URL`` differs from the ``.env`` block ``sandloom dev up``
wrote (the process environment wins over ``.env``).
"""

from __future__ import annotations

import errno
import os
import re
import sys
import traceback
from collections.abc import Iterator, Mapping, MutableSequence
from pathlib import Path
from typing import TextIO

from sandloom.core.cli.dev.envfile import ENV_FILE, exported_drift, read_block

CODE = "SANDLOOM-R014"
DEBUG_FLAG = "--debug"
DEBUG_ENV = "SANDLOOM_DEBUG"

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


def environment() -> Mapping[str, str]:
    """The process environment: the CLI's own switches (``SANDLOOM_DEBUG``,
    ``CI``, ``DOCKER_HOST``) are process flags, not settings."""
    return os.environ


def debug_requested(argv: MutableSequence[str], environ: Mapping[str, str]) -> bool:
    """``--debug`` before the subcommand (removed from ``argv``) or
    ``SANDLOOM_DEBUG=1``. A ``--debug`` after the subcommand is left to it."""
    found = False
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--" or not arg.startswith("-"):
            break  # the subcommand (or the end of the options) starts here
        if arg == DEBUG_FLAG:
            del argv[index]
            found = True
            continue
        index += 1
    raw = (environ.get(DEBUG_ENV) or "").strip().lower()
    return found or raw in {"1", "true", "yes", "on"}


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


def report(exc: BaseException, *, debug: bool, out: TextIO | None = None) -> bool:
    """Print the one line for a connection failure (``True``); ``False``:
    not one, or ``debug`` (the caller re-raises for the traceback)."""
    if debug:
        return False
    line = connection_failure(exc)
    if line is None:
        return False
    print(line, file=out if out is not None else sys.stderr)
    return True


def warn_env_drift(
    root: Path, environ: Mapping[str, str], out: TextIO | None = None
) -> list[str]:
    """Warn about exported URLs that differ from the ``.env`` block."""
    try:
        block = read_block((root / ENV_FILE).read_text(encoding="utf-8"))
    except OSError:
        return []
    drift = exported_drift(block, dict(environ))
    for key in drift:
        print(
            f"warning: {key} is exported in this shell and differs from the "
            f"`sandloom dev` block of {root / ENV_FILE}; the exported value wins "
            f"(unset {key} to use this checkout's services)",
            file=out if out is not None else sys.stderr,
        )
    return drift


__all__ = [
    "CODE",
    "DEBUG_ENV",
    "connection_failure",
    "debug_requested",
    "environment",
    "report",
    "warn_env_drift",
]
