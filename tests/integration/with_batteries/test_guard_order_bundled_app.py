"""A-03: ``create_app`` refuses an idempotency guard solved before authentication.

Moved from ``sandloom/core/tests/test_guard_order.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

from sandloom.core.guard_order import (
    guard_order_problems,
    resolution_order,
)


def test_bundled_app_routes_pass() -> None:
    # create_app already refuses a violation; this pins that the bundled
    # routes really carry guards and authentication the check can see.
    from sandloom_standard.main import app

    from sandloom.core.guard_order import is_authenticator, is_idempotency_guard

    guarded = 0
    for route in app.routes:
        order = resolution_order(getattr(route, "dependant", None))
        if any(is_idempotency_guard(c) for c in order):
            guarded += 1
            assert any(is_authenticator(c) for c in order), route
    assert guarded >= 10
    assert guard_order_problems(app.routes) == []
