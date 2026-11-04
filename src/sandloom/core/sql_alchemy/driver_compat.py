"""Capability probes for the driver internals behind the DB fast paths.

Some round-trip optimisations reach PRIVATE attributes of SQLAlchemy and of
a DBAPI (there is no public API for them). This module holds the probes and
their verdicts; the SQLAlchemy ones are here, a DBAPI adapter registers its
own (:func:`register_probe`; asyncpg's merged BEGIN and dead-connection
retry: :mod:`sandloom.core.drivers.dbapi_asyncpg`):

* the autocommit outbox mark-delivered (:mod:`sandloom.core.sql_alchemy.outbox_store`)
  reads SQLAlchemy's ``SessionTransaction._connections``;
* the non-blocking checkout of
  :class:`sandloom.core.drivers.linger_pool.LingeringQueuePool` takes an idle
  connection straight from SQLAlchemy's pool queue (``QueuePool._pool``,
  ``AsyncAdaptedQueue.get_nowait`` / ``empty``, ``sqlalchemy.util.queue.Empty``)
  instead of the blocking ``asyncio.wait_for(queue.get())`` path (probe
  ``pool_fast_checkout``; on a mismatch every checkout takes the stock path).

A dependency upgrade may rename or change any of them. Each fast path
therefore asks its probe first; the probe runs ONCE per process (cached) and
checks every name it needs (and that it is an attribute / callable of the
expected kind) plus a cheap, I/O-free behavioural self-test:

* ``session_connections``: an in-memory SQLite ``Session`` must show its
  engine in ``SessionTransaction._connections`` exactly after a connection is
  procured.

A failing probe never fails a request: it logs ONE warning, increments
``sandloom_db_driver_fast_path_disabled_total{probe=...}`` and the fast path
falls back to the plain path (a separate ``BEGIN`` + ``set_config``; a normal
BEGIN for lazy reads; a transactional mark-delivered).

Verified driver versions (``docs/operate/operator-reference.md`` →
"Upgrading asyncpg / SQLAlchemy"): :data:`VERIFIED_DRIVER_RANGES`,
pinned in ``pyproject.toml``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from importlib import metadata
from typing import Any

import sandloom.core.observability.metrics as _metrics

logger = logging.getLogger("sandloom.db.driver_compat")

#: Driver versions the fast paths were verified against: ``[low, high)``.
#: Keep in sync with ``pyproject.toml`` (a test pins both together); raise the
#: upper bound only after the procedure in ``docs/operate/operator-reference.md``.
VERIFIED_DRIVER_RANGES: dict[str, tuple[str, str]] = {
    "asyncpg": ("0.32.0", "0.33"),
    "sqlalchemy": ("2.1.4", "2.2"),
}

SESSION_CONNECTIONS = "session_connections"
POOL_FAST_CHECKOUT = "pool_fast_checkout"


@dataclass(frozen=True)
class DriverProbe:
    """The verdict of one probe: ``ok`` or the list of what is missing."""

    name: str
    ok: bool
    problems: tuple[str, ...] = ()


# --- static checks ---------------------------------------------------------


def need_attr(cls: Any, name: str, problems: list[str]) -> None:
    """Add a problem when ``cls`` has no attribute ``name``."""
    if not hasattr(cls, name):
        problems.append(f"{cls.__module__}.{cls.__qualname__}.{name} is missing")


def need_callable(cls: Any, name: str, problems: list[str]) -> None:
    """Add a problem when ``cls.name`` is missing or not callable."""
    member = getattr(cls, name, None)
    if member is None:
        problems.append(f"{cls.__module__}.{cls.__qualname__}.{name}() is missing")
    elif not callable(member):
        problems.append(f"{cls.__module__}.{cls.__qualname__}.{name} is not callable")


def run_now(coro: Coroutine[Any, Any, Any]) -> Any:
    """Drive a coroutine that never suspends (stubbed I/O) to completion."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    coro.close()
    raise RuntimeError("the self-test coroutine suspended (unexpected I/O)")


def _session_connections_problems() -> list[str]:
    try:
        from sqlalchemy import create_engine  # noqa: PLC0415 - optional: probe of private driver internals; their absence is the reported fallback
        from sqlalchemy.orm import Session  # noqa: PLC0415 - optional: probe of private driver internals; their absence is the reported fallback

        engine = create_engine("sqlite://")
    except Exception as error:  # noqa: BLE001 - no in-memory engine: cannot verify
        return [f"cannot build the self-test engine: {error!r}"]
    problems: list[str] = []
    try:
        with Session(engine) as session:
            tx = session.begin()
            connections = getattr(tx, "_connections", None)
            if not isinstance(connections, dict):
                return ["sqlalchemy SessionTransaction._connections is not a dict"]
            if engine in connections:
                problems.append("SessionTransaction._connections pre-populated")
            session.connection()
            if engine not in connections:
                problems.append(
                    "SessionTransaction._connections does not track the "
                    "procured connection by engine"
                )
    except Exception as error:  # noqa: BLE001 - reported as a self-test problem
        problems.append(f"session self-test failed: {error!r}")
    finally:
        engine.dispose()
    return problems


def _pool_fast_checkout_problems() -> list[str]:  # noqa: C901
    """What ``LingeringQueuePool._do_get``'s non-blocking checkout relies on:
    the queue API it calls, and that the queue hands back the item the stock
    blocking ``get`` would (LIFO / FIFO order, ``Empty`` when drained)."""
    problems: list[str] = []
    try:
        from sqlalchemy.pool import AsyncAdaptedQueuePool, QueuePool  # noqa: PLC0415 - optional: probe of private driver internals; their absence is the reported fallback
        from sqlalchemy.util import queue as sqla_queue  # noqa: PLC0415 - optional: probe of private driver internals; their absence is the reported fallback
    except Exception as error:  # noqa: BLE001 - any import failure = unusable
        return [f"cannot import the SQLAlchemy pool internals: {error!r}"]
    empty = getattr(sqla_queue, "Empty", None)
    if not (isinstance(empty, type) and issubclass(empty, Exception)):
        problems.append("sqlalchemy.util.queue.Empty is missing")
    for meth in ("_do_get", "_do_return_conn", "_inc_overflow", "_dec_overflow"):
        need_callable(QueuePool, meth, problems)
    queue_class = getattr(AsyncAdaptedQueuePool, "_queue_class", None)
    if queue_class is None:
        problems.append("AsyncAdaptedQueuePool._queue_class is missing")
        return problems
    for meth in ("empty", "get_nowait", "put_nowait", "get", "qsize"):
        need_callable(queue_class, meth, problems)
    if problems:
        return problems
    try:
        for use_lifo, want in ((True, ["b", "a"]), (False, ["a", "b"])):
            q = queue_class(2, use_lifo=use_lifo)
            q.put_nowait("a")
            q.put_nowait("b")
            got = []
            while not q.empty():
                got.append(q.get_nowait())
            if got != want:
                problems.append(
                    f"pool queue (use_lifo={use_lifo}) order changed: {got!r}"
                )
            try:
                q.get_nowait()
            except empty:  # type: ignore[misc]
                pass
            else:
                problems.append("a drained pool queue no longer raises Empty")
    except Exception as error:  # noqa: BLE001 - a broken self-test = unusable
        problems.append(f"pool queue self-test failed: {error!r}")
    return problems


#: probe name -> the function listing its problems (tests monkeypatch these).
PROBES: dict[str, Callable[[], list[str]]] = {
    SESSION_CONNECTIONS: _session_connections_problems,
    POOL_FAST_CHECKOUT: _pool_fast_checkout_problems,
}

#: probe name -> what falls back when it fails (logged with the warning).
_FAST_PATHS = {
    SESSION_CONNECTIONS: "the outbox mark-delivered falls back to a normal "
    "transaction (BEGIN / UPDATE / COMMIT)",
    POOL_FAST_CHECKOUT: "every pool checkout takes SQLAlchemy's blocking "
    "asyncio.wait_for(queue.get()) path, also when an idle connection is free",
}

_results: dict[str, DriverProbe] = {}


def register_probe(name: str, problems: Callable[[], list[str]], fallback: str) -> None:
    """Add probe ``name``: ``problems`` lists what is missing (empty: ok),
    ``fallback`` says what the fast path falls back to when it fails. A DBAPI
    adapter registers the probes of its private internals this way."""
    PROBES[name] = problems
    _FAST_PATHS[name] = fallback


_lock = threading.Lock()


def _installed(dist: str) -> str:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return "not installed"


def probe(name: str) -> DriverProbe:
    """The (cached) verdict of probe ``name``; never raises."""
    result = _results.get(name)
    if result is not None:
        return result
    with _lock:
        result = _results.get(name)
        if result is not None:
            return result
        try:
            problems = tuple(PROBES[name]())
        except Exception as error:  # noqa: BLE001 - a probe never fails startup
            problems = (f"probe crashed: {error!r}",)
        result = DriverProbe(name, not problems, problems)
        _results[name] = result
    if not result.ok:
        _metrics.db_driver_fast_path_disabled_total.labels(probe=name).inc()
        logger.warning(
            "DB driver internals changed (asyncpg %s, SQLAlchemy %s): %s; %s. "
            "Requests keep working on the plain path; "
            "see docs/operate/operator-reference.md "
            "'Upgrading asyncpg / SQLAlchemy'.",
            _installed("asyncpg"),
            _installed("sqlalchemy"),
            "; ".join(result.problems),
            _FAST_PATHS[name],
            extra={"event": "db.driver_fast_path_disabled", "probe": name},
        )
    return result


def session_connections_ok() -> bool:
    """True if the autocommit mark-delivered may read ``_connections``."""
    return probe(SESSION_CONNECTIONS).ok


def pool_fast_checkout_ok() -> bool:
    """True if the lingering pool may take an idle connection non-blocking."""
    return probe(POOL_FAST_CHECKOUT).ok


def reset_driver_probes() -> None:
    """Forget cached verdicts (tests only)."""
    with _lock:
        _results.clear()


__all__ = [
    "POOL_FAST_CHECKOUT",
    "PROBES",
    "SESSION_CONNECTIONS",
    "VERIFIED_DRIVER_RANGES",
    "DriverProbe",
    "need_attr",
    "need_callable",
    "pool_fast_checkout_ok",
    "probe",
    "register_probe",
    "reset_driver_probes",
    "run_now",
    "session_connections_ok",
]
