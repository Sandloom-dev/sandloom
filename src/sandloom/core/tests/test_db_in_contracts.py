"""db() inside a contract method is contract_session() (spec §4.3): the
provider's own unit on the normal path, the test-bound session under
``bind_contract_session`` — never the caller's current unit."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import (
    bind_contract_session,
    contract_method,
    contract_session,
)
from sandloom.core.tests.test_contract_parity import _FakeDatabase
from sandloom.core.unit_of_work import scope as uow_scope
from sandloom.core.unit_of_work.request import db

pytestmark = pytest.mark.asyncio


class _Session:
    def __init__(self, name: str) -> None:
        self.name = name
        self.info: dict[str, Any] = {}


class Probe:
    @classmethod
    @contract_method
    async def which(cls) -> list[str]:
        return [db().name, contract_session().name]


async def test_db_in_a_bound_contract_is_the_bound_session() -> None:
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    core.register_service("demo.dbprobe.v1", Probe)
    caller = _Session("caller-unit")
    cur = uow_scope._current_uow_session.set(caller)
    try:
        with bind_contract_session(_Session("bound")):
            assert await core.get_service("demo.dbprobe.v1").which() == [
                "bound",
                "bound",
            ]
        assert db() is caller  # the caller's unit again after the call
    finally:
        uow_scope._current_uow_session.reset(cur)
