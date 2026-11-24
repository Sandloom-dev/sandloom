"""DX sagas/projections S2: declared planner reads are discovered and an
undeclared one is refused.

``define_module`` registers every ``ReadOperation`` a module declares at module
level (the operation itself, or one level of a tuple, list or dict), once per
operation; ``Reads.one`` / ``Reads.page`` refuse, on the first use of an
operation on an adapter, one that no loaded module declared (``PlanningError``
with the fix), except on a fixed ``adapter=``. The registry is filled when a
module registers, so a test app that never runs its startup tasks knows its
modules' operations.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from importlib.machinery import ModuleSpec
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import Column, String, Uuid
from sqlalchemy.orm import DeclarativeBase

import sandloom.core.planner as planner
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import ModuleAPI, ModuleRegistrationDraft
from sandloom.core.planner import ListOperation, PlanningError, ReadOperation, Reads
from sandloom.core.sql_alchemy.models.consumer_inbox import ConsumerInboxEntry
from sandloom.core.testing.fixtures import *  # noqa: F403 - the harness app for the refusal tests
from sandloom.sdk.module import define_module
from sandloom.sdk.testing import registration_surface


class _Row(BaseModel):
    id: UUID
    consumer: str


class _Missing(BaseModel):
    nope: int


def _entity(module: str, table: str) -> type:
    """A mapped class that claims to live in ``module`` (its own base: it never
    joins the framework's metadata)."""

    class _Base(DeclarativeBase):
        pass

    return type(
        "Thing",
        (_Base,),
        {
            "__module__": module,
            "__tablename__": table,
            "id": Column(Uuid, primary_key=True),
            "label": Column(String(10)),
        },
    )


def _op(entity: type, name: str) -> ReadOperation[Any]:
    return ReadOperation(entity=entity, result=_Row, by=("id",), name=name)


def _inbox_op(name: str, result: type[BaseModel] = _Row) -> ReadOperation[Any]:
    """A read of a kernel table, so it can run against the test database."""
    return ReadOperation(
        entity=ConsumerInboxEntry, result=result, by=("id",), name=name
    )


def _declared(facade: Any) -> list[ReadOperation[Any]]:
    registry = facade.__dict__.get(planner.DECLARED_OPERATIONS_ATTR, {})
    return [op for _module, op in registry.values()]


def _prepare_tasks(facade: Any) -> list[Any]:
    return [
        task
        for task, _critical in facade.startup_task_specs
        if task.__name__ == "prepare_declared_operations"
    ]


# -- discovery ----------------------------------------------------------------


def _package(monkeypatch: pytest.MonkeyPatch, **modules: dict[str, Any]) -> str:
    """``dxs2pkg`` with the given submodules (each a namespace) in ``sys.modules``."""
    name = "dxs2pkg"
    namespaces = {name: {}} | {f"{name}.{sub}": ns for sub, ns in modules.items()}
    for dotted, namespace in namespaces.items():
        module = types.ModuleType(dotted)
        module.__spec__ = ModuleSpec(dotted, None)  # find_spec reads it
        vars(module).update(namespace)
        monkeypatch.setitem(sys.modules, dotted, module)
    return name


def _registered(package: str) -> list[str]:
    """The operations ``define_module(package)`` registers, as the surface prints them."""
    calls = registration_surface(define_module(package)).of("register_operations")
    assert len(calls) <= 1, "one call carries every discovered operation"
    return [str(op) for call in calls for op in call[1:]]


def test_discovery_registers_the_operations_a_module_declares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mine = _entity("dxs2pkg.models", "dxs2_thing")
    other = _entity("dxs2other.models", "dxs2_other_thing")
    one = _op(mine, "dxs2.one")
    in_tuple = (_op(mine, "dxs2.tuple_b"), _op(mine, "dxs2.tuple_a"))
    in_dict = {"k": _op(mine, "dxs2.dict")}
    in_list = [_op(mine, "dxs2.list")]
    package = _package(
        monkeypatch,
        models={"Thing": mine},
        api={"one": one, "OPERATIONS": in_tuple, "BY_KEY": in_dict},
        services={
            "one_again": one,  # the same operation under a second name
            "LISTED": in_list,
            "foreign": _op(other, "dxs2.foreign"),  # another module's: not ours
            "nested": ((_op(mine, "dxs2.too_deep"),),),  # two levels: not scanned
            "plain": (1, "x", None, {"a": 1}),
        },
    )
    # every operation once, in name order whichever module imports it first
    assert _registered(package) == [
        "ReadOperation('dxs2.dict')",
        "ReadOperation('dxs2.list')",
        "ReadOperation('dxs2.one')",
        "ReadOperation('dxs2.tuple_a')",
        "ReadOperation('dxs2.tuple_b')",
    ]


def test_a_module_without_operations_registers_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mine = _entity("dxs2pkg.models", "dxs2_thing")
    package = _package(monkeypatch, models={"Thing": mine}, api={"answer": 42})
    assert _registered(package) == []


def test_discovered_operations_reach_the_draft_and_the_registry_on_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mine = _entity("dxs2pkg.models", "dxs2_thing")
    op = _op(mine, "dxs2.discovered")
    package = _package(monkeypatch, models={"Thing": mine}, api={"op": op})
    facade = CoreFacade(app=FastAPI())
    draft = ModuleRegistrationDraft()
    define_module(package)(ModuleAPI(facade, draft=draft))
    assert [ops for _module, ops in draft.operations] == [(op,)]
    assert _declared(facade) == [], "buffered until the module commits"
    draft.apply(facade)
    assert _declared(facade) == [op]
    assert len(_prepare_tasks(facade)) == 1


# -- register_operations is idempotent per operation ----------------------------


def test_register_operations_is_idempotent_per_operation(
    core_facade: CoreFacade,
) -> None:
    mine = _entity("dxs2pkg.models", "dxs2_thing")
    a, b, c = _op(mine, "dxs2.a"), _op(mine, "dxs2.b"), _op(mine, "dxs2.c")
    api = ModuleAPI(core_facade)
    api.register_operations(a, b)
    api.register_operations(a, b)  # a setup that still lists them
    api.register_operations(b, a, a)  # in another order, repeated
    assert len(_prepare_tasks(core_facade)) == 1
    assert _declared(core_facade) == [a, b]
    api.register_operations(a, c)  # a new one still registers, alone
    assert len(_prepare_tasks(core_facade)) == 2
    assert sorted(op.name for op in _declared(core_facade)) == [
        "dxs2.a",
        "dxs2.b",
        "dxs2.c",
    ]


def test_register_operations_through_a_draft_records_each_operation_once(
    core_facade: CoreFacade,
) -> None:
    mine = _entity("dxs2pkg.models", "dxs2_thing")
    a, b = _op(mine, "dxs2.a"), _op(mine, "dxs2.b")
    draft = ModuleRegistrationDraft()
    api = ModuleAPI(core_facade, draft=draft)
    api.register_operations(a)
    api.register_operations(a, b)
    api.register_operations(a, b)
    assert [ops for _module, ops in draft.operations] == [(a,), (b,)]
    assert [task.__name__ for task, _critical in draft.startup] == [
        "prepare_declared_operations"
    ] * 2


def test_register_operations_still_refuses_a_non_operation(
    core_facade: CoreFacade,
) -> None:
    mine = _entity("dxs2pkg.models", "dxs2_thing")
    with pytest.raises(TypeError, match="not a declared operation"):
        ModuleAPI(core_facade).register_operations(_op(mine, "dxs2.a"), object())
    assert _declared(core_facade) == [], "a refused call registers nothing"


def test_a_failed_apply_takes_its_declared_operations_back(
    core_facade: CoreFacade,
) -> None:
    mine = _entity("dxs2pkg.models", "dxs2_thing")
    kept, dropped = _op(mine, "dxs2.kept"), _op(mine, "dxs2.dropped")
    first = ModuleRegistrationDraft()
    ModuleAPI(core_facade, draft=first).register_operations(kept)
    first.apply(core_facade)
    second = ModuleRegistrationDraft()
    ModuleAPI(core_facade, draft=second).register_operations(dropped)
    second.routers.append(("not a router", {}))  # include_router raises, last
    with pytest.raises(AttributeError):
        second.apply(core_facade)
    assert _declared(core_facade) == [kept]


# -- the refusal ----------------------------------------------------------------


@pytest.fixture
def facade(sandloom_app: Any, db_engine: Any) -> Iterator[Any]:
    """The harness app's facade (on the migrated test database), its
    registrations put back after the test (the app is shared by the whole run)."""
    core = sandloom_app.state.core
    checkpoint = core.snapshot_registrations()
    yield core
    core.restore_registrations(checkpoint)


async def test_an_undeclared_one_is_refused_with_the_fix(facade: Any) -> None:
    op = _inbox_op("dxs2.inbox.undeclared")
    with pytest.raises(PlanningError) as raised:
        await Reads(facade.runtime_view()).one(op, id=uuid4())
    message = str(raised.value)
    assert message.startswith("dxs2.inbox.undeclared: no module declares")
    assert "Declare it at module level" in message
    assert "services/operations.py" in message
    assert "ConsumerInboxEntry" in message


async def test_an_undeclared_page_is_refused_with_the_fix(facade: Any) -> None:
    op = ListOperation(
        entity=ConsumerInboxEntry,
        result=_Row,
        order_by=("id",),
        name="dxs2.inbox.undeclared_page",
    )
    with pytest.raises(PlanningError, match="no module declares this operation"):
        await Reads(facade.runtime_view()).page(op, limit=5)


async def test_a_declared_read_runs_without_the_startup_task(facade: Any) -> None:
    """Declared at registration: the harness never runs the startup tasks, and
    the module's read must still work in its tests."""
    op = _inbox_op("dxs2.inbox.declared")
    ModuleAPI(facade).register_operations(op)
    assert await Reads(facade.runtime_view()).one(op, id=uuid4()) is None


async def test_the_refusal_is_a_cache_miss_check_only(facade: Any) -> None:
    """The hot path carries no check: the plan a declared read compiled stays
    usable (the registry is consulted once per adapter and mode)."""
    op = _inbox_op("dxs2.inbox.cached")
    ModuleAPI(facade).register_operations(op)
    core = facade.runtime_view()
    assert await Reads(core).one(op, id=uuid4()) is None
    facade.__dict__[planner.DECLARED_OPERATIONS_ATTR].clear()
    assert await Reads(core).one(op, id=uuid4()) is None


async def test_a_fixed_adapter_skips_the_refusal(facade: Any) -> None:
    core = facade.runtime_view()
    adapter = planner._adapter(core, "default")
    op = _inbox_op("dxs2.inbox.fixed")
    assert await Reads(core, adapter=adapter).one(op, id=uuid4()) is None
    # an undeclared operation on a handle made on the core only: refused (a
    # second operation, since the first one's plan is cached on the adapter now)
    other = _inbox_op("dxs2.inbox.fixed_other")
    with pytest.raises(PlanningError, match="no module declares"):
        await Reads(core).one(other, id=uuid4())


async def test_a_declaration_error_is_not_turned_into_the_hint(facade: Any) -> None:
    """Only an undeclared operation gets the hint: a declared one that does not
    match its table still fails with the planner's own reason."""
    op = _inbox_op("dxs2.inbox.bad", _Missing)
    ModuleAPI(facade).register_operations(op)
    with pytest.raises(PlanningError, match="nope") as raised:
        await Reads(facade.runtime_view()).one(op, id=uuid4())
    assert "no module declares" not in str(raised.value)


async def test_operations_of_two_modules_may_share_a_name(facade: Any) -> None:
    """The registry is keyed by operation, not by name: a second module's
    operation named alike is declared too (and listed under its own module)."""
    first, second = _inbox_op("dxs2.inbox.same"), _inbox_op("dxs2.inbox.same")
    planner.declare(facade, "dxs2_first", (first,))
    planner.declare(facade, "dxs2_second", (second,))
    core = facade.runtime_view()
    assert await Reads(core).one(first, id=uuid4()) is None
    assert await Reads(core).one(second, id=uuid4()) is None
    listed = [e for e in planner.diagnostics(facade) if e["name"] == "dxs2.inbox.same"]
    assert sorted(e["module"] for e in listed) == ["dxs2_first", "dxs2_second"]


async def test_a_wrong_argument_is_a_type_error_not_the_refusal(facade: Any) -> None:
    """Argument checks come first and keep their own error type."""
    op = _inbox_op("dxs2.inbox.args")
    with pytest.raises(TypeError, match="expected key arguments"):
        await Reads(facade.runtime_view()).one(op, wrong="x")


def test_an_operation_is_declared_once_by_identity() -> None:
    mine = _entity("dxs2pkg.models", "dxs2_thing")
    a = _op(mine, "dxs2.a")
    facade = CoreFacade(app=FastAPI())
    planner.declare(facade, "m", (a,))
    planner.declare(facade, "m", (a,))
    assert _declared(facade) == [a]
    # unknown to the facade: refused; the entry must be this very object
    twin = _op(mine, "dxs2.a")
    with pytest.raises(PlanningError, match="no module declares"):
        planner._require_declared(facade.runtime_view(), twin)
    planner._require_declared(facade.runtime_view(), a)
