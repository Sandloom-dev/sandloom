"""Event-version migration helpers (v1 → v2 rollout across services).

A versioned channel (``<module>.<event>.vN``) is a contract with consumers in
OTHER deployments. Switching a producer from ``.v1`` to ``.v2`` in one release
silently starves every ``.v1`` consumer that has not migrated yet — they keep
reading a stream nobody writes to. The supported rollout is:

1. the producer **dual-publishes** both versions (:func:`publish_event_versions`)
   — each payload validated against its own schema, both written to the outbox
   in the SAME transaction as the business change;
2. consumers move to ``.v2`` one by one (``sandloom streams consumers`` / the
   subscription inventory shows who still reads ``.v1``);
3. once no live consumer reads ``.v1`` (and its lag is 0), the producer drops it.

The bus warns (``sandloom_events_version_starvation_total``) when a process
publishes one version while live consumers of a sibling version receive
nothing from it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sandloom.core.unit_of_work.effects import publish_event

_VERSIONED = re.compile(r"^(?P<base>.+)\.v(?P<n>[0-9]+)$")


def _split(channel: str) -> tuple[str, int]:
    match = _VERSIONED.match(channel)
    if match is None:
        raise ValueError(
            f"{channel!r} is not a versioned channel (<module>.<event>.vN)"
        )
    return match.group("base"), int(match.group("n"))


async def publish_event_versions(
    payloads: Mapping[str, dict[str, Any]],
    *,
    database: str | None = None,
    bus: Any = None,
    required: bool | None = None,
) -> None:
    """Publish one domain event under several versions of its channel.

    ``payloads`` maps each versioned channel of ONE event to its payload, e.g.
    ``{"billing.paid.v1": old_shape, "billing.paid.v2": new_shape}``. Inside a
    Unit of Work every version is preflighted (channel name + its own schema +
    size) and written to the transactional outbox in the current transaction
    — all versions commit together or none do. Publishing is in ascending
    version order. ``bus`` (scripts/tests) publishes straight to that bus.
    Outside a Unit of Work (and without ``bus``) it raises like
    ``publish_event``; ``required=False`` opts into the best-effort direct
    publish (same semantics and ``sandloom check`` marker as ``publish_event``).
    """
    if len(payloads) < 2:
        raise ValueError("publish_event_versions needs at least two versions")
    parsed = sorted((_split(ch)[1], _split(ch)[0], ch) for ch in payloads)
    bases = {base for _n, base, _ch in parsed}
    if len(bases) != 1:
        raise ValueError(
            "publish_event_versions publishes ONE event: every channel must "
            f"share the same base, got {sorted(bases)}"
        )
    if len({n for n, _b, _ch in parsed}) != len(parsed):
        raise ValueError("publish_event_versions: duplicate version")
    if bus is not None:
        for _n, _base, channel in parsed:
            bus.preflight(channel, payloads[channel])
        for _n, _base, channel in parsed:
            await bus.publish_event(channel, payloads[channel])
        return
    for _n, _base, channel in parsed:
        await publish_event(
            channel, payloads[channel], database=database, required=required
        )


__all__ = ["publish_event_versions"]
