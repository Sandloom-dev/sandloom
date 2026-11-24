"""``db()`` is the one way in where a session parameter used to be: the
``local_step`` apply, the ``authorize_resource`` loader, an L2 projection
handler and ``monotonic_upsert`` / ``monotonic_update`` / ``monotonic_delete``.

The earlier form (the session as a parameter) is gone: a function that takes it
is refused with a ``TypeError`` when it is declared, naming the rewrite, not at
the first run. The framework carries no deprecated API: the old form is not a
``DeprecationWarning``, it is an error.
"""

from __future__ import annotations

import functools
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import BigInteger, Column, DateTime, MetaData, Table, Text, select
from sqlalchemy.orm import Session

from sandloom.core.module_api import Saga
from sandloom.core.projections import (
    monotonic_delete,
    monotonic_update,
    monotonic_upsert,
)
from sandloom.core.projections import operations as projection_operations
from sandloom.core.projections.descriptors import ProjectionConsumerDescriptor
from sandloom.core.testing.fixtures import *  # noqa: F403 - saga_harness + DB fixtures
from sandloom.core.tests.saga_fixtures import EnrollInput, enroll_input
from sandloom.core.utils import takes_one_argument
from sandloom.sdk import db as unit_db  # the fixture `db` below keeps its name
from sandloom.sdk._decl.sagas import _takes_context_only
from sandloom.sdk.authz import Owner
from sandloom.sdk.sagas import SagaContext, local_step
from sandloom.shared.authz import authorize_resource
from sandloom.shared.subject import UserContext

# --------------------------------------------------------------------------- #
# local_step: apply(ctx), the session being db()
# --------------------------------------------------------------------------- #


async def _ctx_only(ctx: Any) -> dict[str, Any]:
    return {"ctx_only": ctx.input.price}


async def _with_session(session: Any, ctx: Any) -> dict[str, Any]:
    return {"session": session, "price": ctx.input.price}


_CTX = SimpleNamespace(input=SimpleNamespace(price=7))


async def test_a_ctx_only_apply_is_called_with_the_context_alone() -> None:
    step = local_step("reserve", apply=_ctx_only, compensate=_ctx_only)
    assert await step.apply("session", _CTX) == {"ctx_only": 7}
    assert step.compensate is not None
    assert await step.compensate("session", _CTX) == {"ctx_only": 7}


async def test_a_defaulted_second_parameter_is_not_the_session() -> None:
    """``apply(ctx, dry_run=False)`` takes the context alone: its option keeps
    its default, it is never handed the session."""

    async def apply(ctx: Any, dry_run: bool = False) -> dict[str, Any]:
        return {"dry_run": dry_run, "price": ctx.input.price}

    step = local_step("reserve", apply=apply, compensate=apply)
    assert await step.apply("the-session", _CTX) == {"dry_run": False, "price": 7}
    assert step.compensate is not None
    assert await step.compensate("the-session", _CTX) == {
        "dry_run": False,
        "price": 7,
    }


def test_the_session_form_of_a_local_step_is_refused_when_declared() -> None:
    with pytest.raises(
        TypeError, match=r"local_step\(apply=\.\.\.\) takes `async def f\(ctx\)`"
    ) as caught:
        local_step("reserve", apply=_with_session)
    assert "drop the `session` parameter and call `db()`" in str(caught.value)
    with pytest.raises(TypeError, match=r"local_step\(compensate=\.\.\.\) takes"):
        local_step("reserve", apply=_ctx_only, compensate=_with_session)


def test_a_decorated_session_form_is_still_refused() -> None:
    """A decorator that hides the signature (``*args``) does not hide the form:
    the function behind it is read too."""

    @functools.wraps(_with_session)
    async def decorated(*args: Any) -> dict[str, Any]:
        return await _with_session(*args)

    with pytest.raises(TypeError, match=r"local_step\(apply=\.\.\.\) takes"):
        local_step("reserve", apply=decorated)


def test_a_step_function_already_adapted_is_not_adapted_again() -> None:
    """``step.apply`` is the runtime's ``(session, ctx)`` adapter; passing it to
    another ``local_step`` is refused, not wrapped into a function that would
    hand the context in as the session."""
    step = local_step("reserve", apply=_ctx_only)
    with pytest.raises(TypeError, match=r"local_step\(apply=\.\.\.\) takes"):
        local_step("again", apply=step.apply)


def test_the_inspection_names_the_authors_function_of_a_local_step() -> None:
    """``step.apply`` is the framework's adapter (its code lives in
    ``sandloom/sdk/_decl/sagas.py``, outside any project root): the inspection
    index follows ``functools.wraps`` to the author's function. Without that it
    would record no source for the step at all."""
    from sandloom.core.cli.inspection.processes import _location

    here = Path(__file__).resolve()
    step = local_step("reserve", apply=_ctx_only, compensate=_ctx_only)
    expected = {"path": here.name, "function": "_ctx_only"}
    assert _location(step.apply, here.parent) == expected
    assert _location(step.compensate, here.parent) == expected


@pytest.mark.parametrize(
    "bad",
    [lambda: None, lambda a, b, c: None, lambda *, ctx: None],
    ids=["no-parameter", "three-parameters", "keyword-only"],
)
def test_a_function_that_fits_no_shape_is_refused_when_declared(bad: Any) -> None:
    with pytest.raises(TypeError, match=r"local_step\(apply=\.\.\.\) takes"):
        local_step("reserve", apply=bad)
    with pytest.raises(TypeError, match=r"local_step\(compensate=\.\.\.\) takes"):
        local_step("reserve", apply=_ctx_only, compensate=bad)


async def test_db_is_the_transitions_session_in_a_local_step(saga_harness: Any) -> None:
    """The real runtime: a ctx-only step reads and writes through ``db()``."""

    async def reserve(ctx: SagaContext[EnrollInput]) -> dict[str, Any]:
        return {"one": (await unit_db().execute(select(1))).scalar_one()}

    async def release(ctx: SagaContext[EnrollInput]) -> dict[str, Any]:
        return {}

    async def grant(ctx: SagaContext[EnrollInput]) -> dict[str, Any]:
        return {"two": (await unit_db().execute(select(2))).scalar_one()}

    saga = Saga[EnrollInput](
        "dx_db_everywhere",
        version=1,
        input=EnrollInput,
        deadline=timedelta(minutes=5),
        steps=[
            local_step("reserve", apply=reserve, compensate=release),
            local_step("grant", kind="pivot", apply=grant),
        ],
    )
    run = await saga_harness.run(saga, input=enroll_input())
    assert run.status == "completed"
    assert run.results["reserve"] == {"one": 1}
    assert run.results["grant"] == {"two": 2}


# --------------------------------------------------------------------------- #
# authorize_resource: loader(id), reading through db()
# --------------------------------------------------------------------------- #

_OWNER = uuid.uuid4()


async def _authorize(loader: Any) -> Any:
    guard = authorize_resource(
        loader=loader, by="id", policy=Owner(lambda note: note["owner"])
    )
    request = SimpleNamespace(path_params={"id": "n1"}, state=SimpleNamespace())
    return await guard(request, UserContext(user_id=_OWNER), "the-request-session")


async def test_a_one_argument_loader_reads_through_db(ambient_uow: Any) -> None:
    seen: list[Any] = []

    async def load(note_id: str) -> dict[str, Any]:
        seen.append(unit_db())
        return {"id": note_id, "owner": _OWNER}

    note = await _authorize(load)
    assert note["id"] == "n1"
    assert seen == [ambient_uow]


async def test_a_loader_with_a_defaulted_second_parameter_takes_the_id_alone(
    ambient_uow: Any,
) -> None:
    """``load(id, include_archived=False)``: the option keeps its default, so
    archived rows stay hidden; it is never handed the session (truthy)."""
    got: list[Any] = []

    async def load(note_id: str, include_archived: bool = False) -> dict[str, Any]:
        got.append(include_archived)
        return {"id": note_id, "owner": _OWNER}

    await _authorize(load)
    assert got == [False]


async def test_a_loader_that_returns_nothing_is_a_404(ambient_uow: Any) -> None:
    async def load(note_id: str) -> None:
        return None

    with pytest.raises(HTTPException) as caught:
        await _authorize(load)
    assert caught.value.status_code == 404


def test_the_two_argument_loader_is_refused_at_declaration() -> None:
    async def load(note_id: str, session: Any) -> dict[str, Any]:
        return {"id": note_id, "owner": _OWNER}

    with pytest.raises(
        TypeError, match=r"takes `async def load\(id\)` and reads through `db\(\)`"
    ) as caught:
        authorize_resource(loader=load, by="id", policy=Owner(lambda note: note))
    assert "drop the `session` parameter and call `db()`" in str(caught.value)


@pytest.mark.parametrize(
    "bad", [lambda: None, lambda a, b, c: None], ids=["no-parameter", "three"]
)
def test_a_loader_that_fits_no_shape_is_refused_at_declaration(bad: Any) -> None:
    with pytest.raises(TypeError, match=r"takes `async def load\(id\)`"):
        authorize_resource(loader=bad, by="id", policy=Owner(lambda note: note))


# --------------------------------------------------------------------------- #
# L2 projection handlers: handler(envelope), the session being db()
# --------------------------------------------------------------------------- #


def _descriptor(handler: Any) -> ProjectionConsumerDescriptor:
    return ProjectionConsumerDescriptor(
        name="dxdb@task",
        source_channel="tasks.task.updated.v1",
        projection="dxdb",
        handler=handler,
    )


async def test_an_envelope_only_handler_runs_on_db(ambient_uow: Any) -> None:
    seen: list[Any] = []

    async def handle(envelope: Any) -> None:
        seen.append((envelope, unit_db()))

    # the consumer callback hands the runtime form the unit's session first
    await _descriptor(handle).apply("ignored-session", "envelope")
    assert seen == [("envelope", ambient_uow)]


async def test_a_handler_with_a_defaulted_second_parameter_takes_the_envelope_alone(
    ambient_uow: Any,
) -> None:
    """``handle(envelope, retries=3)``: the option keeps its default, it is
    never handed the session."""
    seen: list[Any] = []

    async def handle(envelope: Any, retries: int = 3) -> None:
        seen.append((envelope, retries))

    await _descriptor(handle).apply("the-session", "e")
    assert seen == [("e", 3)]


def test_the_session_form_of_a_handler_is_refused_when_declared() -> None:
    async def handle(session: Any, envelope: Any) -> None:
        return None

    with pytest.raises(
        TypeError, match=r"handler takes `async def f\(envelope\)`"
    ) as caught:
        _descriptor(handle)
    assert "drop the `session` parameter and call `db()`" in str(caught.value)


@pytest.mark.parametrize(
    "bad", [lambda: None, lambda a, b, c: None], ids=["no-parameter", "three"]
)
def test_a_handler_that_fits_no_shape_is_refused_when_declared(bad: Any) -> None:
    with pytest.raises(TypeError, match="handler takes"):
        _descriptor(bad)


def test_a_descriptor_built_from_an_adapted_handler_is_refused() -> None:
    """``descriptor.apply`` is the runtime's two-parameter adapter: it is not a
    handler, and is refused as one."""

    async def handle(envelope: Any) -> None:
        return None

    with pytest.raises(TypeError, match="handler takes"):
        _descriptor(_descriptor(handle).apply)


async def test_watermark_guarded_keeps_an_envelope_only_handler(
    monkeypatch: pytest.MonkeyPatch, ambient_uow: Any
) -> None:
    """The guard wraps the handler in a handler of the same shape that reads the
    watermark through ``db()``; the new descriptor is accepted as it stands."""
    seen: list[Any] = []

    async def handle(envelope: Any) -> None:
        seen.append(envelope)

    async def no_watermark(session: Any, projection: str) -> None:
        assert session is ambient_uow
        return None

    monkeypatch.setattr(projection_operations, "locked_watermark", no_watermark)
    monkeypatch.setattr(projection_operations, "check_watermark", lambda *_: None)
    guarded = projection_operations.watermark_guarded(_descriptor(handle))
    await guarded.handler("envelope")
    assert seen == ["envelope"]


# --------------------------------------------------------------------------- #
# monotonic_upsert / update / delete: the session is db()
# --------------------------------------------------------------------------- #

_MD = MetaData()
_ROWS = Table(
    "dx_db_everywhere_rows",
    _MD,
    Column("item_id", Text, primary_key=True),
    Column("title", Text, nullable=False),
    Column("group_id", Text, nullable=False),
    Column("group_name", Text, nullable=True),
    Column("item_version", BigInteger, nullable=True),
    Column("group_version", BigInteger, nullable=True),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
)


async def _create_table(session: Any) -> None:
    """In the test's rolled-back transaction: PostgreSQL DDL is transactional."""
    await session.run_sync(lambda s: _MD.create_all(s.connection()))


async def _rows(session: Any) -> dict[str, tuple[Any, ...]]:
    result = await session.execute(
        select(
            _ROWS.c.item_id,
            _ROWS.c.title,
            _ROWS.c.group_name,
            _ROWS.c.item_version,
            _ROWS.c.deleted_at,
        )
    )
    return {row[0]: tuple(row[1:]) for row in result}


async def test_monotonic_helpers_write_through_db_without_a_session(
    db_uow: Any,
) -> None:
    await _create_table(db_uow)
    assert await monotonic_upsert(
        _ROWS,
        key={"item_id": "a"},
        values={"title": "t1", "group_id": "g"},
        version_column="item_version",
        incoming_version=1,
    )
    # the same version again changes nothing: the version guard is intact
    assert not await monotonic_upsert(
        _ROWS,
        key={"item_id": "a"},
        values={"title": "stale", "group_id": "g"},
        version_column="item_version",
        incoming_version=1,
    )
    assert (
        await monotonic_update(
            _ROWS,
            match={"group_id": "g"},
            values={"group_name": "G"},
            version_column="group_version",
            incoming_version=2,
        )
        == 1
    )
    assert await monotonic_delete(
        _ROWS,
        key={"item_id": "b"},
        values={"title": "gone", "group_id": "g"},
        version_column="item_version",
        incoming_version=5,
        deleted_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    rows = await _rows(db_uow)
    assert rows["a"] == ("t1", "G", 1, None)
    assert rows["b"][0] == "gone" and rows["b"][3] is not None


@pytest.mark.parametrize("count", [0, 2])
@pytest.mark.parametrize(
    "helper", [monotonic_upsert, monotonic_update, monotonic_delete]
)
async def test_the_earlier_session_form_is_refused(helper: Any, count: int) -> None:
    """``helper(session, table, ...)`` has two positional arguments and no
    ``db()`` is read: it is a ``TypeError`` on the call, never ``target=session``."""
    kwargs: dict[str, Any] = {
        "values": {},
        "version_column": "v",
        "incoming_version": 1,
    }
    kwargs["match" if helper is monotonic_update else "key"] = {}
    with pytest.raises(TypeError, match="positional argument"):
        await helper(*(["x"] * count), **kwargs)


@pytest.mark.parametrize("kind", ["async", "sync"])
@pytest.mark.parametrize(
    "helper", [monotonic_upsert, monotonic_update, monotonic_delete]
)
async def test_the_earlier_session_form_names_the_rewrite(
    helper: Any, kind: str, db_uow: Any
) -> None:
    """``helper(session, table, ...)``, the session being a real one, is a
    ``TypeError`` that names ``db()`` and the call without the session (Python's
    own message would only count positional arguments); it writes nothing."""
    kwargs: dict[str, Any] = {
        "values": {"title": "t", "group_id": "g"},
        "version_column": "item_version",
        "incoming_version": 1,
    }
    kwargs["match" if helper is monotonic_update else "key"] = {"item_id": "a"}
    session = db_uow if kind == "async" else Session()
    name = helper.__name__
    with pytest.raises(
        TypeError, match=rf"{name}\(session, \.\.\.\) is gone.*`db\(\)`"
    ):
        await helper(session, _ROWS, **kwargs)
    await _create_table(db_uow)  # the refusal came before any statement ran
    assert await _rows(db_uow) == {}


# --------------------------------------------------------------------------- #
# The shape rule: a function that can be called with one argument
# --------------------------------------------------------------------------- #

#: ``(function, takes one positional argument)``: the kernel's
#: ``takes_one_argument`` and the declarative level's copy of it (it imports no
#: kernel module) must agree.
_SHAPES = [
    (lambda: None, False),
    (lambda a: None, True),
    (lambda a, b: None, False),
    (lambda a, b, c: None, False),
    (lambda a, b=1: None, True),
    (lambda a, /, b=1: None, True),
    (lambda a=1, b=2: None, True),
    (lambda *a: None, True),
    (lambda a, *rest: None, True),
    (lambda a, *, flag: None, False),
    (lambda a, *, flag=False: None, True),
]
_SHAPE_IDS = [
    "none",
    "one",
    "two",
    "three",
    "default-second",
    "positional-only-default-second",
    "all-default",
    "star",
    "rest",
    "required-keyword",
    "optional-keyword",
]


@pytest.mark.parametrize(("fn", "expected"), _SHAPES, ids=_SHAPE_IDS)
def test_takes_one_argument_counts_what_can_be_bound(fn: Any, expected: bool) -> None:
    assert takes_one_argument(fn) is expected


@pytest.mark.parametrize(("fn", "expected"), _SHAPES, ids=_SHAPE_IDS)
def test_the_declarative_levels_copy_of_the_rule_agrees(
    fn: Any, expected: bool
) -> None:
    assert _takes_context_only(fn) is expected
