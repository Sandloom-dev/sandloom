"""``db()`` works where a session parameter used to be the only way in: the
``local_step`` apply, the ``authorize_resource`` loader, an L2 projection
handler and ``monotonic_upsert`` / ``monotonic_update`` / ``monotonic_delete``.

Each keeps its earlier form (the session as a parameter) and refuses a
function that fits neither form when it is declared, not at the first run. The
parameter is documented as the earlier form, never a ``DeprecationWarning``
(the framework carries no deprecated API).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import BigInteger, Column, DateTime, MetaData, Table, Text, select

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
from sandloom.sdk import db as unit_db  # the fixture `db` below keeps its name
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


async def _greedy(*args: Any) -> dict[str, Any]:
    return {"args": len(args)}


_CTX = SimpleNamespace(input=SimpleNamespace(price=7))


async def test_a_ctx_only_apply_is_called_with_the_context_alone() -> None:
    step = local_step("reserve", apply=_ctx_only, compensate=_ctx_only)
    assert await step.apply("session", _CTX) == {"ctx_only": 7}
    assert step.compensate is not None
    assert await step.compensate("session", _CTX) == {"ctx_only": 7}


async def test_the_earlier_session_and_ctx_form_still_gets_both() -> None:
    step = local_step("reserve", apply=_with_session)
    assert await step.apply("the-session", _CTX) == {
        "session": "the-session",
        "price": 7,
    }
    assert step.compensate is None


async def test_a_function_that_fits_both_shapes_keeps_the_earlier_form() -> None:
    step = local_step("reserve", apply=_greedy)
    assert await step.apply("s", "c") == {"args": 2}


@pytest.mark.parametrize(
    "bad",
    [lambda: None, lambda a, b, c: None, lambda *, ctx: None],
    ids=["no-parameter", "three-parameters", "keyword-only"],
)
def test_a_function_that_fits_neither_shape_is_refused_when_declared(bad: Any) -> None:
    with pytest.raises(TypeError, match=r"local_step\(apply=\.\.\.\) takes"):
        local_step("reserve", apply=bad)
    with pytest.raises(TypeError, match=r"local_step\(compensate=\.\.\.\) takes"):
        local_step("reserve", apply=_ctx_only, compensate=bad)


async def test_db_is_the_transitions_session_in_a_local_step(saga_harness: Any) -> None:
    """The real runtime: a ctx-only step reads through ``db()``, and in the
    earlier form ``db()`` is the very session the step was handed."""

    async def reserve(ctx: SagaContext[EnrollInput]) -> dict[str, Any]:
        return {"one": (await unit_db().execute(select(1))).scalar_one()}

    async def release(ctx: SagaContext[EnrollInput]) -> dict[str, Any]:
        return {}

    async def grant(session: Any, ctx: SagaContext[EnrollInput]) -> dict[str, Any]:
        return {"same_session": unit_db() is session}

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
    assert run.results["grant"] == {"same_session": True}


# --------------------------------------------------------------------------- #
# authorize_resource: loader(id), reading through db()
# --------------------------------------------------------------------------- #

_OWNER = uuid.uuid4()
_REQUEST_SESSION = "the-request-session"


async def _authorize(loader: Any) -> Any:
    guard = authorize_resource(
        loader=loader, by="id", policy=Owner(lambda note: note["owner"])
    )
    request = SimpleNamespace(path_params={"id": "n1"}, state=SimpleNamespace())
    return await guard(request, UserContext(user_id=_OWNER), _REQUEST_SESSION)


async def test_a_one_argument_loader_reads_through_db(ambient_uow: Any) -> None:
    seen: list[Any] = []

    async def load(note_id: str) -> dict[str, Any]:
        seen.append(unit_db())
        return {"id": note_id, "owner": _OWNER}

    note = await _authorize(load)
    assert note["id"] == "n1"
    assert seen == [ambient_uow]


async def test_the_earlier_two_argument_loader_gets_the_request_session() -> None:
    got: list[Any] = []

    async def load(note_id: str, session: Any) -> dict[str, Any]:
        got.append(session)
        return {"id": note_id, "owner": _OWNER}

    await _authorize(load)
    assert got == [_REQUEST_SESSION]


async def test_a_loader_that_returns_nothing_is_a_404(ambient_uow: Any) -> None:
    async def load(note_id: str) -> None:
        return None

    with pytest.raises(HTTPException) as caught:
        await _authorize(load)
    assert caught.value.status_code == 404


@pytest.mark.parametrize(
    "bad", [lambda: None, lambda a, b, c: None], ids=["no-parameter", "three"]
)
def test_a_loader_that_fits_neither_form_is_refused_at_declaration(bad: Any) -> None:
    with pytest.raises(TypeError, match=r"takes `async def load\(id\)`"):
        authorize_resource(loader=bad, by="id", policy=Owner(lambda note: note))


# --------------------------------------------------------------------------- #
# L2 projection handlers: apply(envelope), the session being db()
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

    async def apply(envelope: Any) -> None:
        seen.append((envelope, unit_db()))

    await _descriptor(apply).apply("ignored-session", "envelope")
    assert seen == [("envelope", ambient_uow)]


async def test_the_earlier_session_and_envelope_handler_gets_both() -> None:
    seen: list[Any] = []

    async def apply(session: Any, envelope: Any) -> None:
        seen.append((session, envelope))

    await _descriptor(apply).apply("s", "e")
    assert seen == [("s", "e")]


@pytest.mark.parametrize(
    "bad", [lambda: None, lambda a, b, c: None], ids=["no-parameter", "three"]
)
def test_a_handler_that_fits_neither_form_is_refused_when_declared(bad: Any) -> None:
    with pytest.raises(TypeError, match="handler takes"):
        _descriptor(bad)


async def test_watermark_guarded_keeps_the_shape_of_an_envelope_only_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard wraps a handler the descriptor already adapted: the new
    descriptor reads the wrapper's own two-parameter shape, not the
    one-parameter function ``functools.wraps`` points at."""
    seen: list[Any] = []

    async def apply(envelope: Any) -> None:
        seen.append(envelope)

    async def no_watermark(session: Any, projection: str) -> None:
        return None

    monkeypatch.setattr(projection_operations, "locked_watermark", no_watermark)
    monkeypatch.setattr(projection_operations, "check_watermark", lambda *_: None)
    guarded = projection_operations.watermark_guarded(_descriptor(apply))
    await guarded.apply("session", "envelope")
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


async def test_the_earlier_session_form_writes_through_the_given_session(
    db: Any,
) -> None:
    """No unit is bound here: a helper that fell back to ``db()`` would raise
    (SANDLOOM-R002), so this proves the explicit session is the one used."""
    await _create_table(db)
    assert await monotonic_upsert(
        db,
        _ROWS,
        key={"item_id": "a"},
        values={"title": "t1", "group_id": "g"},
        version_column="item_version",
        incoming_version=1,
    )
    assert (
        await monotonic_update(
            db,
            _ROWS,
            match={"group_id": "g"},
            values={"group_name": "G"},
            version_column="group_version",
            incoming_version=2,
        )
        == 1
    )
    assert await monotonic_delete(
        db,
        _ROWS,
        key={"item_id": "b"},
        values={"title": "gone", "group_id": "g"},
        version_column="item_version",
        incoming_version=5,
        deleted_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert set(await _rows(db)) == {"a", "b"}


@pytest.mark.parametrize("count", [0, 3])
@pytest.mark.parametrize(
    "helper", [monotonic_upsert, monotonic_update, monotonic_delete]
)
async def test_a_wrong_positional_count_is_refused(helper: Any, count: int) -> None:
    kwargs: dict[str, Any] = {
        "values": {},
        "version_column": "v",
        "incoming_version": 1,
    }
    kwargs["match" if helper is monotonic_update else "key"] = {}
    with pytest.raises(TypeError, match="positional arguments"):
        await helper(*(["x"] * count), **kwargs)
