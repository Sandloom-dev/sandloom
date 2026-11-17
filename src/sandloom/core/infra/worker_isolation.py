"""Parallel test isolation of a driver whose options hold URLs (spec 2.6).

``InfraProvider.test_isolation(options, worker_id)`` is the hook; a driver whose
options name a URL implements it with :func:`isolated_url_options`. What a URL
becomes for one worker is not the kernel's to know: the harness family of the
backend that owns the URL (``sandloom.driverkit.testing_family``) installs its
*mover* with :func:`set_url_mover` before the harness calls a hook, a function
``(url, worker number) -> url`` that returns a URL it does not own unchanged.
Movers are keyed by family name: each family has one, and
:func:`isolated_url_options` applies every installed mover in turn.
A driver reads no environment and holds no layout.
``sandloom.driverkit.testing_isolation`` re-exports these names for a driver,
which imports only ``sandloom.driverkit`` (ADR-0016). Standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

_WORKER_ID = re.compile(r"^gw(\d+)$")

_movers: dict[str, Callable[[str, int], str]] = {}


def set_url_mover(family: str, mover: Callable[[str, int], str] | None) -> None:
    """Make ``mover`` (``(url, worker number) -> url``) the mover of harness
    family ``family`` in this process, replacing the one it had; ``None`` removes
    it. The movers of other families stay.

    Test processes only: the harness family calls it before the harness asks a
    driver.
    """
    if mover is None:
        _movers.pop(family, None)
    else:
        _movers[family] = mover


def isolated_url_options(
    options: Mapping[str, Any],
    worker_id: str,
    keys: Sequence[str] = ("url",),
) -> Mapping[str, Any]:
    """``options`` with each URL under ``keys`` as worker ``worker_id``
    (``gw<N>``) uses it: the installed movers decide, each in turn (one returns
    a URL of another family unchanged).

    The ``test_isolation`` hook of a driver whose options hold URLs. Options
    without such a URL, a ``worker_id`` that is not ``gw<N>``, or a process with
    no mover (no harness family claimed a URL) come back unchanged.
    """
    match = _WORKER_ID.match(worker_id)
    if match is None or not _movers:
        return options
    number = int(match.group(1))
    out = dict(options)
    for key in keys:
        url = out.get(key)
        if isinstance(url, str):
            for mover in tuple(_movers.values()):
                url = mover(url, number)
            out[key] = url
    return out


__all__ = ["isolated_url_options", "set_url_mover"]
