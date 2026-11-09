"""At startup, a console with no platform administrator says how to make one."""

from __future__ import annotations

import logging
from typing import Any, Protocol

import pytest

from sandloom.sdk.contracts import (
    ContractRef,  # sandloom: advanced the owner's reserved contract
)
from sandloom.sdk.errors import ContractCallError
from sandloom.sdk.testing import fake_contract
from sandloom_admin import startup as admin_setup


class _Operator(Protocol):
    async def platform_admin_exists(self) -> bool: ...


#: The roles owner's reserved contract; the owner here is a module named
#: ``people``, not identity: the check follows the composition's map.
PEOPLE_SECURITY_V1: ContractRef[_Operator] = ContractRef(
    "people.security.v1", _Operator
)


def _owner() -> str | None:
    return "people"


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
        fake_contract(PEOPLE_SECURITY_V1, security),
        caplog.at_level(logging.WARNING),
    ):
        await admin_setup.warn_without_admin(_owner)
        await admin_setup.warn_without_admin(_owner)
    warnings = [
        r for r in caplog.records if "No platform administrator" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "uv run sandloom admin create --email" in warnings[0].getMessage()
    assert security.calls == 1


async def test_it_says_nothing_once_an_administrator_exists(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        fake_contract(PEOPLE_SECURITY_V1, _Security(exists=True)),
        caplog.at_level(logging.WARNING),
    ):
        await admin_setup.warn_without_admin(_owner)
    assert "No platform administrator" not in caplog.text


async def test_a_failed_check_is_tried_again_at_the_next_start() -> None:
    class _Down:
        async def platform_admin_exists(self) -> bool:
            raise RuntimeError("no table yet")

    with fake_contract(PEOPLE_SECURITY_V1, _Down()), pytest.raises(RuntimeError):
        await admin_setup.warn_without_admin(_owner)
    assert admin_setup._warned is False


async def test_the_module_registers_the_check_and_asks_the_roles_owner() -> None:
    tasks: list[Any] = []
    asked: list[str] = []

    class _Api:
        def add_startup_task(self, task: Any, critical: bool = False) -> None:
            tasks.append((task, critical))

        def claim_owner(self, key: str) -> str | None:
            asked.append(key)
            return "people"

    admin_setup.setup(_Api())  # type: ignore[arg-type]
    ((task, critical),) = tasks
    assert critical is False
    security = _Security(exists=True)
    with fake_contract(PEOPLE_SECURITY_V1, security):
        await task()
    assert asked == ["roles"] and security.calls == 1


async def test_no_roles_owner_is_not_checked_and_not_a_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        await admin_setup.warn_without_admin(lambda: None)
    assert "not made: no owner of roles" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert admin_setup._warned is False


async def test_an_owner_without_the_method_or_not_deployed_is_not_checked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Without:
        pass

    with caplog.at_level(logging.DEBUG):
        await admin_setup.warn_without_admin(_owner)  # not deployed
        with fake_contract(PEOPLE_SECURITY_V1, _Without()):
            await admin_setup.warn_without_admin(_owner)
    assert caplog.text.count("not made") == 2
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert admin_setup._warned is False


async def test_a_withheld_grant_is_not_checked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Forbidden:
        async def platform_admin_exists(self) -> bool:
            raise ContractCallError("not allowed", status_code=403)

    with (
        fake_contract(PEOPLE_SECURITY_V1, _Forbidden()),
        caplog.at_level(logging.DEBUG),
    ):
        await admin_setup.warn_without_admin(_owner)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_an_unreachable_database_is_not_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stopped dev services: identity answers 503. The check is skipped
    quietly (the app reports the database once), and tried at the next start."""

    class _Unavailable:
        async def platform_admin_exists(self) -> bool:
            raise ContractCallError(
                "Database temporarily unavailable; retry the request", status_code=503
            )

    with (
        fake_contract(PEOPLE_SECURITY_V1, _Unavailable()),
        caplog.at_level(logging.DEBUG),
    ):
        await admin_setup.warn_without_admin(_owner)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any(r.exc_info for r in caplog.records)
    assert admin_setup._warned is False
