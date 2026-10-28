"""Discovery (spec §5.7; ledger G-MODULE-14, G-DECL-03, G-MODULE-19).

``define_module`` imports the conventional submodules, collects only what the
module owns, keeps today's plain-router rule, derives ``consumes`` and
refuses a wrong declaration before registering anything (atomic).
"""

from __future__ import annotations

import itertools
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core.facade import CoreFacade
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.testing import registration_surface
from sandloom.sdk.errors import (
    ConsumerDefinitionError,
    ContractDefinitionError,
    EventDefinitionError,
    ModuleDiscoveryError,
)
from sandloom.sdk.module import ContractRequirement, define_module

_COUNTER = itertools.count()


@pytest.fixture
def make_module(tmp_path: Path) -> Iterator[Any]:
    """Write a module package from ``{relative path: source}``; returns its name."""
    created: list[str] = []
    sys.path.insert(0, str(tmp_path))

    def make(files: dict[str, str]) -> str:
        name = f"sdkdisc{next(_COUNTER)}"
        for rel, source in {"__init__.py": "", **files}.items():
            path = tmp_path / name / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            text = textwrap.dedent(source).replace("PKG", name)
            path.write_text(text, encoding="utf-8")
            init = path.parent / "__init__.py"
            if not init.exists():
                init.write_text("", encoding="utf-8")
        created.append(name)
        return name

    yield make
    sys.path.remove(str(tmp_path))
    for name in created:
        for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            del sys.modules[mod]


EVENTS = """
from uuid import UUID
from sandloom.sdk import Event

class Thing(Event, channel="PKG.thing.created.v1"):
    thing_id: UUID
    tenant_id: UUID
"""


def _surface(pkg: str, **kwargs: Any) -> Any:
    return registration_surface(define_module(pkg, **kwargs))


def test_producers_are_registered_and_views_are_not(make_module: Any) -> None:
    pkg = make_module(
        {
            "events.py": EVENTS,
            "consumers.py": """
                from uuid import UUID
                from sandloom.sdk import Event, on

                class Other(Event, channel="elsewhere.x.happened.v1"):
                    tenant_id: UUID

                @on(Other)
                async def react(event: Other) -> None: ...
            """,
        }
    )
    surface = _surface(pkg)
    schemas = surface.of("register_event_schema")
    assert [c[1] for c in schemas] == [repr(f"{pkg}.thing.created.v1")]
    consumers = surface.of("register_event_consumer")
    assert [c[1] for c in consumers] == ["'elsewhere.x.happened.v1'"]


def test_a_declaration_outside_the_conventional_submodules_is_refused(
    make_module: Any,
) -> None:
    pkg = make_module(
        {
            "services.py": EVENTS,
            "api.py": "from . import services  # noqa: F401\n",
        }
    )
    with pytest.raises(ModuleDiscoveryError, match=r"(?s)SANDLOOM-B006.*move it to"):
        _surface(pkg)


def test_two_producers_of_one_channel_are_refused(make_module: Any) -> None:
    again = '\nclass Again(Event, channel="PKG.thing.created.v1"):\n    x: int\n'
    pkg = make_module({"events.py": EVENTS + again})
    with pytest.raises(EventDefinitionError, match="two producers"):
        _surface(pkg)


def test_duplicate_consumer_names_are_refused(make_module: Any) -> None:
    pkg = make_module(
        {
            "consumers.py": """
                from uuid import UUID
                from sandloom.sdk import Event, on

                class Other(Event, channel="elsewhere.x.happened.v1"):
                    tenant_id: UUID

                @on(Other, name="same")
                async def one(event: Other) -> None: ...

                @on(Other, name="same")
                async def two(event: Other) -> None: ...
            """
        }
    )
    with pytest.raises(ConsumerDefinitionError, match="two consumers"):
        _surface(pkg)


def test_provider_bases_must_be_inside_the_package(make_module: Any) -> None:
    base = make_module({"bases.py": "class Base:\n    pass\n"})
    pkg = make_module(
        {
            "contracts.py": f"""
                from sandloom.sdk import contract
                from {base}.bases import Base

                @contract("x.y.v1")
                class Provider(Base):
                    async def m(self) -> int:
                        return 1
            """
        }
    )
    with pytest.raises(ContractDefinitionError, match="outside"):
        _surface(pkg)


def test_plain_routers_keep_todays_rule_and_module_routers_join(
    make_module: Any,
) -> None:
    pkg = make_module(
        {
            "api/__init__.py": """
                from fastapi import APIRouter
                router = APIRouter(prefix="/plain")

                @router.get("/x")
                async def x() -> None: ...
            """,
            "api/extra.py": """
                from sandloom.sdk import module_router, public
                more = module_router()

                @more.get("/y", dependencies=[public(reason="r")])
                async def y() -> None: ...
            """,
        }
    )
    surface = _surface(pkg)
    routers = [c[1] for c in surface.of("include_router")]
    assert routers == ["router[GET /plain/x]", f"router[GET /api/v1/{pkg}/y]"]
    assert _surface(pkg, router=None).of("include_router") == []


def test_consumes_are_derived_and_merged_never_weaker(make_module: Any) -> None:
    pkg = make_module(
        {
            "api.py": """
                from sandloom.sdk import Contract
                from sandloom.sdk.contracts import ContractRef

                REF = ContractRef("stats.quota.v1")
                OPT = ContractRef("stats.extra.v1")
                quota = Contract(REF)
                extra = Contract(OPT, optional=True)

                async def f() -> None:
                    await quota.may_create_note()
                    await quota.reserve(1, "k")
            """
        }
    )
    descriptor = _surface(pkg).descriptor
    assert descriptor.consumes == [
        ContractRequirement("stats.extra.v1", required=False, methods=()),
        ContractRequirement(
            "stats.quota.v1", required=True, methods=("may_create_note", "reserve")
        ),
    ]
    merged = _surface(
        pkg,
        consumes=[
            ContractRequirement("stats.extra.v1", required=True, reason="gate"),
            ContractRequirement("stats.quota.v1", required=False, methods=("other",)),
            "billing.facts.v1",
        ],
    ).descriptor.consumes
    assert merged == [
        ContractRequirement("stats.extra.v1", required=True, reason="gate", methods=()),
        ContractRequirement(
            "stats.quota.v1",
            required=True,
            methods=("may_create_note", "other", "reserve"),
        ),
        ContractRequirement("billing.facts.v1"),
    ]


def test_route_policies_add_the_contracts_their_atoms_call(make_module: Any) -> None:
    """A live or fact-backed gate needs its owner's contract in a split
    deployment: it joins the derived ``consumes`` (optional), so the
    preflight reports it unbound and ``sandloom add deploy`` binds it."""
    pkg = make_module(
        {
            "api.py": """
                from fastapi import Depends
                from sandloom.sdk import module_router
                from sandloom.sdk.authz import (
                    Account, Entitled, OrgRole, SubscriptionActive, require,
                )

                router = module_router()
                PAID = SubscriptionActive(of=Account.org(param="org_id"))

                @router.get(
                    "/{org_id}/a",
                    dependencies=[Depends(require(OrgRole("admin") & PAID))],
                )
                async def a(org_id: str) -> None: ...

                @router.get(
                    "/b",
                    dependencies=[
                        Depends(require(Entitled("organization.subscription.active")))
                    ],
                )
                async def b() -> None: ...
            """
        }
    )
    consumes = {r.name: r for r in _surface(pkg).descriptor.consumes}
    assert set(consumes) == {
        "billing.entitlements.v1",
        "billing.facts.v1",
        "tenancy.membership.v1",
    }
    assert not any(r.required for r in consumes.values())
    assert "route policy" in consumes["billing.facts.v1"].reason


def test_a_refused_module_registers_nothing(make_module: Any) -> None:
    pkg = make_module(
        {
            "api/__init__.py": """
                from fastapi import APIRouter
                router = APIRouter(prefix="/plain")

                @router.get("/x")
                async def x() -> None: ...
            """,
            "contracts.py": """
                from sandloom.sdk import contract

                @contract("x.ok.v1")
                class Fine:
                    async def m(self) -> int:
                        return 1
            """,
            "services.py": EVENTS,
            "consumers.py": "from . import services  # noqa: F401\n",
        }
    )
    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(InMemorySchemaRegistry())
    routes_before = len(core.app.router.routes)
    register = define_module(pkg)
    with pytest.raises(ModuleDiscoveryError), core.module_registration(pkg) as api:
        register(api)
    assert len(core.app.router.routes) == routes_before
    assert core.service_registry.try_get("x.ok.v1") is None


def test_events_override_skips_producer_schemas(make_module: Any) -> None:
    pkg = make_module({"events.py": EVENTS})
    assert _surface(pkg, events=None).of("register_event_schema") == []
    assert len(_surface(pkg).of("register_event_schema")) == 1


def test_a_module_named_like_a_submodule_owns_its_declarations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A business module named ``tasks`` (a conventional submodule name) is
    the owner of its ``api`` module, never the ``<app>.modules`` container."""
    import sys
    import types

    from sandloom.sdk._decl._owner import module_package

    container = types.ModuleType("ownapp.modules")
    container.__path__ = []  # type: ignore[attr-defined]
    tasks = types.ModuleType("ownapp.modules.tasks")
    tasks.__path__ = []  # type: ignore[attr-defined]
    tasks.register = lambda core: None  # type: ignore[attr-defined]
    notes = types.ModuleType("ownapp.modules.notes")
    notes.__path__ = []  # type: ignore[attr-defined]
    notes.register = lambda core: None  # type: ignore[attr-defined]
    for module in (container, tasks, notes):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    assert module_package("ownapp.modules.tasks.api") == "ownapp.modules.tasks"
    assert module_package("ownapp.modules.tasks.api.routes") == "ownapp.modules.tasks"
    assert module_package("ownapp.modules.tasks") == "ownapp.modules.tasks"
    assert module_package("ownapp.modules.notes.tasks") == "ownapp.modules.notes"
    assert module_package("ownapp.modules.notes.api.models") == "ownapp.modules.notes"
    assert module_package("ownapp.modules.notes.helpers") == "ownapp.modules.notes"


def test_a_subscripted_saga_records_its_constructing_module() -> None:
    """``Saga[Input](...)`` goes through ``typing``'s generic alias; the owner
    is still the module that wrote it, not ``typing``."""
    from datetime import timedelta

    from pydantic import BaseModel
    from sdkfix_notes.sagas import _args, quota

    from sandloom.sdk._decl._owner import OWNER_ATTR
    from sandloom.sdk.sagas import Saga, step

    class In(BaseModel):
        amount: int

    plain = Saga(
        "plain",
        version=1,
        input=In,
        deadline=timedelta(minutes=1),
        steps=[step("s", call=quota.reserve, args=_args, compensate=quota.release)],
    )
    typed = Saga[In](
        "typed",
        version=1,
        input=In,
        deadline=timedelta(minutes=1),
        steps=[step("s", call=quota.reserve, args=_args, compensate=quota.release)],
    )
    assert getattr(plain, OWNER_ATTR) == __name__
    assert getattr(typed, OWNER_ATTR) == __name__
