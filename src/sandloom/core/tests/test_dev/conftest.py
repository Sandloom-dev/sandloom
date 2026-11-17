"""``sandloom dev`` asks the installed driver package for the stack's keyed
server (:mod:`sandloom.core.cli.stack_service`); these tests run on every lane,
so they ask an in-memory stand-in (:mod:`sandloom.core.tests._fake_stack`)."""

from __future__ import annotations

import pytest

from sandloom.core.tests._fake_stack import FakeStack, install_fake_stack


@pytest.fixture(autouse=True)
def fake_stack(monkeypatch: pytest.MonkeyPatch) -> FakeStack:
    return install_fake_stack(monkeypatch)
