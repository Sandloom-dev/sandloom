"""Parallel test isolation of a driver whose options hold URLs (spec 2.6).

``InfraProvider.test_isolation(options, worker_id)`` is the hook; a driver whose
options name a URL implements it with :func:`isolated_url_options`. What a URL
becomes for one worker is not the kernel's to know: the harness family of the
backend that owns the URL (``sandloom.driverkit.testing_family``) installs a
*mover* with :func:`set_url_mover` before the harness calls a hook, a function
``(url, worker number) -> url`` that returns a URL it does not own unchanged.
A driver reads no environment and holds no layout.
``sandloom.driverkit.testing_isolation`` re-exports these names for a driver,
which imports only ``sandloom.driverkit`` (ADR-0016). Standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

_WORKER_ID = re.compile(r"^gw(\d+)$")

_mover: Callable[[str, int], str] | None = None


def set_url_mover(mover: Callable[[str, int], str] | None) -> None:
    """Make ``mover`` (``(url, worker number) -> url``) this process's mover;
    ``None`` removes it.

    Test processes only: the harness family calls it before the harness asks a
    driver.
    """
    global _mover
    _mover = mover


def isolated_url_options(
    options: Mapping[str, Any],
    worker_id: str,
    keys: Sequence[str] = ("url",),
) -> Mapping[str, Any]:
    """``options`` with each URL under ``keys`` as worker ``worker_id``
    (``gw<N>``) uses it: the process's mover decides.

    The ``test_isolation`` hook of a driver whose options hold URLs. Options
    without such a URL, a ``worker_id`` that is not ``gw<N>``, or a process with
    no mover (no harness family claimed a URL) come back unchanged.
    """
    match = _WORKER_ID.match(worker_id)
    if match is None or _mover is None:
        return options
    number = int(match.group(1))
    out = dict(options)
    for key in keys:
        url = out.get(key)
        if isinstance(url, str):
            out[key] = _mover(url, number)
    return out


__all__ = ["isolated_url_options", "set_url_mover"]
