"""Parallel test isolation of a driver that talks to Redis (spec 2.6).

``InfraProvider.test_isolation(options, worker_id)`` is the hook; the Redis
drivers implement it with :func:`isolated_redis_options`. There is one layout per
test process, computed by the test harness (``sandloom.core.testing``: the
stride, or the shared-Redis ``auto`` allocation) and handed over with
:func:`set_worker_layout` before it calls a hook, so a driver reads no
environment. ``sandloom.driverkit.testing_isolation`` re-exports these names for
a driver, which imports only ``sandloom.driverkit`` (ADR-0016); the harness sets
the layout through this module. Standard library only.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

#: The URL schemes whose database index is moved.
REDIS_SCHEMES = ("redis://", "rediss://")
#: Database indexes between two workers' instances, by default.
DEFAULT_STRIDE = 16
_WORKER_ID = re.compile(r"^gw(\d+)$")


@dataclass(frozen=True, slots=True)
class WorkerLayout:
    """Which database index each worker's URL moves to.

    ``mapping`` is the allocation of the shared-Redis ``auto`` mode,
    ``{base index: (index of gw0, index of gw1, ...)}``; without it the index
    moves by ``stride * (N + 1)``.
    """

    stride: int = DEFAULT_STRIDE
    mapping: Mapping[int, Sequence[int]] | None = None


_layout = WorkerLayout()


def set_worker_layout(layout: WorkerLayout | None = None) -> None:
    """Make ``layout`` (the default one when omitted) this process's layout.

    Test processes only: the test harness calls it before it asks a driver.
    """
    global _layout
    _layout = layout or WorkerLayout()


def worker_layout() -> WorkerLayout:
    """The layout :func:`isolated_redis_options` applies."""
    return _layout


def _plain_index(url: str) -> tuple[SplitResult, int] | None:
    parts = urlsplit(url)
    raw = parts.path.lstrip("/")
    try:
        return parts, (int(raw) if raw else 0)
    except ValueError:
        return None  # not a plain index (unix socket path, ...)


def worker_redis_url(url: str, number: int, stride: int) -> str:
    """``url`` with its database index offset by ``stride * (number + 1)``."""
    found = _plain_index(url)
    if found is None:
        return url
    parts, index = found
    return urlunsplit(parts._replace(path=f"/{index + stride * (number + 1)}"))


def mapped_redis_url(
    url: str, number: int, mapping: Mapping[int, Sequence[int]]
) -> str:
    """``url`` moved to worker ``number``'s allocated index (``auto``)."""
    found = _plain_index(url)
    if found is None:
        return url
    parts, index = found
    indexes = mapping.get(index)
    if not indexes:
        known = ";".join(
            f"{b}={','.join(map(str, i))}" for b, i in sorted(mapping.items())
        )
        raise RuntimeError(
            f"SANDLOOM_TEST_REDIS_DB_STRIDE=auto: no Redis index allocated for base "
            f"index {index}, worker gw{number} "
            f"(SANDLOOM_TEST_REDIS_WORKER_DBS={known!r})"
        )
    if number >= len(indexes):
        # A replacement for crashed workers beyond the allocated spares:
        # deterministic (never a conftest import failure cascade), but it
        # shares an index with another worker: say so on stderr.
        slot = number % len(indexes)
        print(
            f"sandloom testing: worker gw{number} has no Redis index of its own for "
            f"base {index}; sharing gw{slot}'s index {indexes[slot]} (a crashed "
            "worker was replaced more often than the spares allow)",
            file=sys.stderr,
        )
        return urlunsplit(parts._replace(path=f"/{indexes[slot]}"))
    return urlunsplit(parts._replace(path=f"/{indexes[number]}"))


def moved_redis_url(url: str, number: int, layout: WorkerLayout) -> str:
    """``url`` as worker ``number`` uses it under ``layout``."""
    if layout.mapping is not None:
        return mapped_redis_url(url, number, layout.mapping)
    return worker_redis_url(url, number, layout.stride)


def isolated_redis_options(
    options: Mapping[str, Any],
    worker_id: str,
    keys: Sequence[str] = ("url",),
) -> Mapping[str, Any]:
    """``options`` with each Redis URL under ``keys`` moved to worker
    ``worker_id``'s (``gw<N>``) database index under the process's layout.

    The ``test_isolation`` hook of the Redis drivers. Options without such a
    URL, a URL naming no plain index, or a ``worker_id`` that is not ``gw<N>``
    come back unchanged.
    """
    match = _WORKER_ID.match(worker_id)
    if match is None:
        return options
    number = int(match.group(1))
    out = dict(options)
    for key in keys:
        url = out.get(key)
        if isinstance(url, str) and url.startswith(REDIS_SCHEMES):
            out[key] = moved_redis_url(url, number, _layout)
    return out


__all__ = [
    "DEFAULT_STRIDE",
    "REDIS_SCHEMES",
    "WorkerLayout",
    "isolated_redis_options",
    "mapped_redis_url",
    "moved_redis_url",
    "set_worker_layout",
    "worker_layout",
    "worker_redis_url",
]
