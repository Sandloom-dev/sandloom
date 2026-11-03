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

import os
import sys
from collections.abc import Mapping, MutableMapping, MutableSequence
from pathlib import Path
from typing import TextIO

import sandloom.core.cli.ui as _ui
from sandloom.core.cli.dev.envfile import ENV_FILE, exported_drift, read_block
from sandloom.core.observability.unreachable import CODE, connection_failure

DEBUG_FLAG = "--debug"
DEBUG_ENV = "SANDLOOM_DEBUG"


def environment() -> Mapping[str, str]:
    """The process environment: the CLI's own switches are process flags,
    not settings (``src/sandloom/core/AGENTS.md`` lists them):
    ``SANDLOOM_DEBUG``, ``CI``, ``DOCKER_HOST``, the terminal's ``NO_COLOR``,
    ``FORCE_COLOR``, ``CLICOLOR_FORCE``, ``TERM``, ``COLORTERM``, ``UV`` and
    the coding-agent markers of :data:`sandloom.core.cli.ui.AGENT_MARKERS`."""
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


def enable_debug(environ: MutableMapping[str, str]) -> None:
    """``--debug`` for this process and the ones it starts: ``SANDLOOM_DEBUG=1``
    (tracebacks, Compose's own output) and ``LOG_LEVEL=DEBUG`` (the
    framework's startup lines) unless ``LOG_LEVEL`` is already exported."""
    environ[DEBUG_ENV] = "1"
    environ.setdefault("LOG_LEVEL", "DEBUG")


def report(exc: BaseException, *, debug: bool, out: TextIO | None = None) -> bool:
    """Print the one line for a connection failure (``True``); ``False``:
    not one, or ``debug`` (the caller re-raises for the traceback)."""
    if debug:
        return False
    line = connection_failure(exc)
    if line is None:
        return False
    stream = out if out is not None else sys.stderr
    style = _ui.style_for(stream, environment())
    if line.startswith("error: "):
        line = style.red("error:") + line.removeprefix("error:")
    print(line, file=stream)
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
