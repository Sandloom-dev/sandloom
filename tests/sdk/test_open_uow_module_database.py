"""``open_uow()`` without a name opens the calling module's primary database.

A module whose migration chain lives on a non-default database gets that
database, as its contracts' units do; code that belongs to no loaded module
(a script) and an explicit name keep their meaning.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

import sandloom.core.di as di
import sandloom.sdk._prim.data as data_prim
import sandloom.sdk._unit as unit_module
from sandloom.core.descriptors import (
    CapabilityRequirement,
    MigrationTargetError,
    ModuleDescriptor,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.runtime import use_active_core
from sandloom.sdk._unit import open_uow_for_module
from sandloom.sdk.data import open_uow

NOTES_PACKAGE = "app.modules.notes"


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The database names ``db_session`` was asked for."""
    names: list[str] = []

    def fake_db_session(name: str = "default") -> AbstractAsyncContextManager[Any]:
        names.append(name)
        return _Unit()

    monkeypatch.setattr(data_prim, "db_session", fake_db_session)
    monkeypatch.setattr(unit_module, "db_session", fake_db_session)
    return names


class _Unit:
    async def __aenter__(self) -> AsyncSession:
        raise AssertionError("the test only reads the name")

    async def __aexit__(self, *exc: object) -> None:
        return None


def _core(*descriptors: ModuleDescriptor) -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core.module_descriptors = list(descriptors)
    return core


def _called_from(module: str) -> Callable[..., Any]:
    """``open_uow`` as code of the module named ``module`` calls it."""
    return eval(  # noqa: S307 - a lambda over a fixed globals dict, no input
        "lambda *a: open_uow(*a)", {"__name__": module, "open_uow": open_uow}
    )


@pytest.fixture
def two_databases() -> Iterator[CoreFacade]:
    core = _core(
        ModuleDescriptor(
            name=NOTES_PACKAGE, version="1", migrations_database="notes_db"
        ),
        ModuleDescriptor(name="app.modules.billing", version="1"),
        ModuleDescriptor(
            name=f"{NOTES_PACKAGE}.audit", version="1", migrations_database="audit_db"
        ),
    )
    with use_active_core(core):
        yield core


def test_open_uow_opens_the_calling_modules_database(
    two_databases: CoreFacade, opened: list[str]
) -> None:
    _called_from(f"{NOTES_PACKAGE}.tasks.refresh")()
    assert opened == ["notes_db"]


def test_a_module_on_the_default_database_gets_default(
    two_databases: CoreFacade, opened: list[str]
) -> None:
    _called_from("app.modules.billing.tasks")()
    assert opened == ["default"]


def test_the_innermost_module_package_wins(
    two_databases: CoreFacade, opened: list[str]
) -> None:
    _called_from(f"{NOTES_PACKAGE}.audit.jobs")()
    assert opened == ["audit_db"]


def test_a_package_that_only_shares_a_name_prefix_is_not_the_module(
    two_databases: CoreFacade, opened: list[str]
) -> None:
    _called_from(f"{NOTES_PACKAGE}_extras.tasks")()
    assert opened == ["default"]


def test_an_explicit_name_wins(two_databases: CoreFacade, opened: list[str]) -> None:
    _called_from(f"{NOTES_PACKAGE}.tasks")("analytics")
    assert opened == ["analytics"]


def test_module_names_the_module_the_unit_acts_for(
    two_databases: CoreFacade, opened: list[str]
) -> None:
    # Called from billing code (default database), the unit is opened for
    # notes, as a projection rebuild does for the projection's owner.
    eval(  # noqa: S307 - a lambda over a fixed globals dict, no input
        "lambda: open_uow_for_module(owner)",
        {
            "__name__": "app.modules.billing.tasks",
            "open_uow_for_module": open_uow_for_module,
            "owner": f"{NOTES_PACKAGE}.projections",
        },
    )()
    assert opened == ["notes_db"]


def test_a_module_with_several_databases_and_no_primary_one_refuses(
    opened: list[str],
) -> None:
    core = _core(
        ModuleDescriptor(
            name="app.modules.reports",
            version="1",
            capabilities=[
                CapabilityRequirement("database", instance="a"),
                CapabilityRequirement("database", instance="b"),
            ],
        )
    )
    with use_active_core(core), pytest.raises(MigrationTargetError):
        _called_from("app.modules.reports.tasks")()
    assert opened == []


def test_code_of_no_loaded_module_gets_default(
    two_databases: CoreFacade, opened: list[str]
) -> None:
    _called_from("scripts.nightly")()
    assert opened == ["default"]


def test_without_an_active_core_the_name_is_default(
    monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    monkeypatch.setattr(di, "get_active_core", lambda: None)
    _called_from(f"{NOTES_PACKAGE}.tasks")()
    assert opened == ["default"]
