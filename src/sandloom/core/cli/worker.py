"""``sandloom worker`` / ``sandloom beat`` — the background-task processes.

    sandloom worker [--queues Q[,Q...]] [--concurrency N] [--loglevel L]
        run a task worker for the loaded modules (every deploy profile's
        ``<service>-worker``); the process model is the driver's own option
        (``worker_pool`` of the celery instance in INFRA: on macOS set solo or
        threads, the note in docs/modules/background/tasks.md)
    sandloom beat [--loglevel L] [--schedule PATH]
        run the scheduler; it schedules only while it holds the singleton
        lease, a second one stands by (run one per broker and module set)
    sandloom worker check [--timeout SECONDS]
        container healthcheck: exit 0 when the worker is alive (the driver's
        heartbeat touched within ``--timeout`` seconds, default 60), 1 when
        missing or stale. It boots no composition root and asks no broker.

Each command makes one call into the worker code (:func:`_run_worker`,
:func:`_run_beat`, :func:`_check`), which reaches the taskqueue driver's
``worker_host()``; the rest is argument parsing. ``worker`` and ``beat`` boot the
composition root (``SANDLOOM_COMPOSITION_ROOT``) like the API, so the checks of
``create_app`` run first. ``--loglevel`` is the process's log level and
``--schedule`` the scheduler's state file (the host ignores it when it keeps
none). See docs/modules/background/tasks.md.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Sequence

import sandloom.core.infra.tool_infra as _tool_infra
import sandloom.core.worker as _worker

#: ``--timeout`` default: the heartbeat is touched every 10 s, so 60 s is six misses.
DEFAULT_CHECK_TIMEOUT_SECONDS = 60.0
#: ``--timeout`` ceiling (one day): a larger one is a typo, ``inf`` never stale.
MAX_CHECK_TIMEOUT_SECONDS = 86400.0


def _run_worker(
    queues: list[str] | None, concurrency: int | None, loglevel: str
) -> int:
    """The one call site of ``sandloom worker``: the driver's worker host."""
    return _worker.run_worker(queues=queues, concurrency=concurrency, loglevel=loglevel)


def _run_beat(loglevel: str, schedule: str | None) -> int:
    """The one call site of ``sandloom beat``: the driver's scheduler host,
    under the beat lease."""
    return _worker.run_scheduler(loglevel=loglevel, state_file=schedule)


def _check(timeout: float) -> tuple[bool, str]:
    """The one call site of ``sandloom worker check``: the driver's host, over
    an infra registry built from the settings (no application boots)."""
    return _worker.check_worker(_tool_infra.build_tool_infra(), timeout)


def _add_loglevel(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--loglevel",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="log level of the process (default INFO)",
    )


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {raw!r}")
    return value


def _positive_seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not (math.isfinite(value) and 0 < value <= MAX_CHECK_TIMEOUT_SECONDS):
        raise argparse.ArgumentTypeError(
            f"must be a positive number of at most {MAX_CHECK_TIMEOUT_SECONDS:g} "
            f"seconds, got {raw!r}"
        )
    return value


def worker_main(argv: Sequence[str] | None = None) -> int:
    """``sandloom worker`` and ``sandloom worker check``."""
    args_in = list(sys.argv[1:] if argv is None else argv)
    if args_in[:1] == ["check"]:
        check = argparse.ArgumentParser(
            prog="sandloom worker check",
            description="exit 0 when the worker's heartbeat is fresh",
        )
        check.add_argument(
            "--timeout",
            type=_positive_seconds,
            default=DEFAULT_CHECK_TIMEOUT_SECONDS,
            help="the heartbeat counts as stale after this many seconds "
            f"(default {DEFAULT_CHECK_TIMEOUT_SECONDS:g})",
        )
        ok, reason = _check(check.parse_args(args_in[1:]).timeout)
        if not ok:
            print(reason, file=sys.stderr)
        return 0 if ok else 1
    parser = argparse.ArgumentParser(
        prog="sandloom worker",
        description="run a task worker for the loaded modules "
        "(`sandloom worker check`: the container healthcheck)",
    )
    parser.add_argument(
        "--queues",
        "-Q",
        default=None,
        help="comma-separated queues to consume (default: the default queue)",
    )
    parser.add_argument(
        "--concurrency",
        "-c",
        type=_positive_int,
        default=None,
        help="child processes (default: WORKER_CONCURRENCY, else the CPU quota)",
    )
    _add_loglevel(parser)
    args = parser.parse_args(args_in)
    queues = (
        [q.strip() for q in args.queues.split(",") if q.strip()]
        if args.queues
        else None
    )
    return _run_worker(queues or None, args.concurrency, args.loglevel)


def beat_main(argv: Sequence[str] | None = None) -> int:
    """``sandloom beat``."""
    parser = argparse.ArgumentParser(
        prog="sandloom beat",
        description="run the scheduler (one per broker and module set; a second "
        "one stands by on the lease)",
    )
    _add_loglevel(parser)
    parser.add_argument(
        "--schedule",
        default=None,
        help="the scheduler's state file (default: celerybeat-schedule in the "
        "working directory; containers pass /tmp/celerybeat-schedule)",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    return _run_beat(args.loglevel, args.schedule)


__all__ = ["beat_main", "worker_main"]
