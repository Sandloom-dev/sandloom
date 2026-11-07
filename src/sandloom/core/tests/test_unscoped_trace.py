"""``unscoped()`` is never silent: counted by calling module, its reason logged."""

from __future__ import annotations

import logging

import pytest

from sandloom.core.tenant_context import _UNSCOPED_TOTAL, unscoped


def _count(caller: str) -> float:
    return _UNSCOPED_TOTAL.labels(caller=caller)._value.get()


def test_each_scope_is_counted_for_its_calling_module(
    caplog: pytest.LogCaptureFixture,
) -> None:
    before = _count(__name__)
    with caplog.at_level(logging.DEBUG, logger="sandloom.tenant.unscoped"):
        with unscoped(reason="nightly cleanup of every tenant"):
            pass
        with unscoped():
            pass
    assert _count(__name__) == before + 2
    messages = [r.getMessage() for r in caplog.records]
    assert any(__name__ in m and "nightly cleanup" in m for m in messages)
    assert any("no reason given" in m for m in messages)
