"""Review fixes of the task API (PR #200): aliased payloads round-trip, one
derived name from two files is refused, and a refused payload carries no value
of its input."""

from __future__ import annotations

import sys
import types
from typing import Any

import orjson
import pytest
from pydantic import BaseModel, Field

from sandloom.core.infra.errors import InvalidArgument
from sandloom.core.tasks.registry import (
    DeclaredTask,
    declared_task,
    encode_payload,
    register_task,
    run_task,
)
from sandloom.sdk.tasks import task

SECRET = "s3cr3t-card-4111"
ran: list[Any] = []


class Aliased(BaseModel):
    """An aliased field without ``populate_by_name``."""

    card_number: str = Field(alias="cardNumber")


@task
def takes_aliased(payload: Aliased) -> None:
    ran.append(payload.card_number)


class Strict(BaseModel):
    amount: int


@task
def strict(payload: Strict) -> None: ...


def test_an_aliased_payload_round_trips_through_encode_and_run() -> None:
    declared = declared_task(takes_aliased.name)
    assert declared is not None
    body = encode_payload(declared, Aliased(cardNumber="x1"))
    assert orjson.loads(body) == {"cardNumber": "x1"}
    run_task(takes_aliased.name, body)
    assert ran[-1] == "x1"
    # A mapping under the alias is accepted too.
    run_task(takes_aliased.name, encode_payload(declared, {"cardNumber": "x2"}))
    assert ran[-1] == "x2"


async def test_enqueue_passes_the_model_itself_to_the_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    class Layer:
        async def enqueue(self, name: str, payload: Any, **options: Any) -> str:
            seen.append(payload)
            return "id"

    monkeypatch.setattr("sandloom.sdk._prim.tasks.taskqueue", lambda: Layer())
    model = Aliased(cardNumber="x3")
    await takes_aliased.enqueue(model)
    assert seen == [model]


def _moved() -> None:
    return None


def _define_elsewhere(filename: str) -> Any:
    """``_moved`` as if defined in the file ``filename`` (same line)."""
    code = _moved.__code__.replace(co_filename=filename)
    return types.FunctionType(code, globals(), "moved")


def _declared(function: Any) -> DeclaredTask:
    return DeclaredTask(
        name="review.fixes.moved",
        function=function,
        payload_type=None,
        queue="default",
        at_least_once=False,
        is_async=False,
    )


def test_one_derived_name_from_two_files_is_refused() -> None:
    first = _define_elsewhere("/a/review_fixes.py")
    register_task(_declared(first))
    # The same function again (a module imported twice): accepted.
    register_task(_declared(_define_elsewhere("/a/review_fixes.py")))
    with pytest.raises(ValueError, match="two tasks are named"):
        register_task(_declared(_define_elsewhere("/b/review_fixes.py")))


def _import_fresh(name: str, filename: str) -> Any:
    """``moved`` as defined by the module ``name`` (a new module object put in
    ``sys.modules``, as an import does) from the file ``filename``."""
    module = types.ModuleType(name)
    sys.modules[name] = module
    code = _moved.__code__.replace(co_filename=filename)
    function = types.FunctionType(code, module.__dict__, "moved")
    function.__module__ = name
    return function


def test_a_module_dropped_and_imported_again_replaces_its_own_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebuilt app (a test project in a new directory under one package
    name, a reload after the module left ``sys.modules``) re-declares its
    tasks: the second import replaces the first, from the same file or another.
    While the first module is still live, another file is refused."""
    name = "review_fixes_reloaded.tasks"
    monkeypatch.delitem(sys.modules, name, raising=False)

    def declared(function: Any) -> DeclaredTask:
        return DeclaredTask(
            name=f"{name}.moved",
            function=function,
            payload_type=None,
            queue="default",
            at_least_once=False,
            is_async=False,
        )

    try:
        first = _import_fresh(name, "/a/tasks.py")
        register_task(declared(first))
        # The module dropped and imported again from another directory.
        del sys.modules[name]
        second = _import_fresh(name, "/b/tasks.py")
        register_task(declared(second))
        known = declared_task(f"{name}.moved")
        assert known is not None and known.function is second
        # The same name from a third file while the second module is live.
        other = _define_elsewhere("/c/tasks.py")
        other.__module__ = name
        with pytest.raises(ValueError, match="two tasks are named"):
            register_task(declared(other))
    finally:
        sys.modules.pop(name, None)


def _assert_no_input(error: BaseException) -> None:
    assert SECRET not in str(error) and SECRET not in repr(error)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_a_refused_payload_carries_no_input_value() -> None:
    declared = declared_task(takes_aliased.name)
    assert declared is not None
    with pytest.raises(InvalidArgument) as encode_error:
        encode_payload(declared, {"cardNumber": 7, "extra": SECRET, "bad": SECRET})
    _assert_no_input(encode_error.value)

    with pytest.raises(InvalidArgument) as run_error:
        run_task(strict.name, orjson.dumps({"amount": SECRET}))
    _assert_no_input(run_error.value)
    declared_strict = declared_task(strict.name)
    assert declared_strict is not None
    with pytest.raises(InvalidArgument) as encode_strict:
        encode_payload(declared_strict, {"amount": SECRET})
    _assert_no_input(encode_strict.value)
