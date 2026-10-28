"""Module wiring checks: approved route classes, boots alone (I-19).

The module's own pins (formerly central framework lists): its routes equal
``routes.snapshot.json`` (regenerate with ``sandloom routes snapshot --write``),
and ``create_app`` + lifespan start it with nothing else loaded.
"""

from __future__ import annotations

import time
from typing import Any

from sandloom.sdk.testing import assert_route_snapshot, boot_module_alone

MODULE = (__package__ or "").rsplit(".tests", 1)[0]

#: Required consumed contracts this module cannot start without (empty: it
#: must start alone).
EXPECTED_MISSING_CONTRACTS: frozenset[str] = frozenset()


def test_routes_are_approved(sandloom_app: Any) -> None:
    assert_route_snapshot(sandloom_app, MODULE)


async def test_module_boots_alone(db_engine: Any) -> None:
    started = time.perf_counter()
    outcome = await boot_module_alone(MODULE)
    assert outcome.missing_contracts == EXPECTED_MISSING_CONTRACTS
    assert outcome.started
    assert time.perf_counter() - started < 20
