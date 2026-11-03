"""At startup, a console with no platform administrator says how to make one."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from sandloom.sdk.testing import fake_contract
from sandloom.shared.contracts.identity_security_v1 import IDENTITY_SECURITY_V1
from sandloom_admin import startup as admin_setup


class _Security:
    def __init__(self, exists: bool) -> None:
        self.exists = exists
        self.calls = 0

    async def platform_admin_exists(self) -> bool:
        self.calls += 1
        return self.exists


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_setup, "_warned", False)


async def test_it_warns_once_without_an_administrator(
    caplog: pytest.LogCaptureFixture,
) -> None:
    security = _Security(exists=False)
    with (
        fake_contract(IDENTITY_SECURITY_V1, security),
        caplog.at_level(logging.WARNING),
    ):
        await admin_setup.warn_without_admin()
        await admin_setup.warn_without_admin()
    warnings = [r for r in caplog.records if "No platform administrator" in r.message]
    assert len(warnings) == 1
    assert "uv run sandloom admin create --email" in warnings[0].message
    assert security.calls == 1


async def test_it_says_nothing_once_an_administrator_exists(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        fake_contract(IDENTITY_SECURITY_V1, _Security(exists=True)),
        caplog.at_level(logging.WARNING),
    ):
        await admin_setup.warn_without_admin()
    assert "No platform administrator" not in caplog.text


async def test_a_failed_check_is_tried_again_at_the_next_start() -> None:
    class _Down:
        async def platform_admin_exists(self) -> bool:
            raise RuntimeError("no table yet")

    with fake_contract(IDENTITY_SECURITY_V1, _Down()), pytest.raises(RuntimeError):
        await admin_setup.warn_without_admin()
    assert admin_setup._warned is False


def test_the_module_registers_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    tasks: list[Any] = []

    class _Api:
        def add_startup_task(self, task: Any, critical: bool = False) -> None:
            tasks.append((task, critical))

    admin_setup.setup(_Api())  # type: ignore[arg-type]
    assert tasks == [(admin_setup.warn_without_admin, False)]
