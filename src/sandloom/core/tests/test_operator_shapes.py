"""The shapes the kernel's operator commands call an owner's methods with.

Before the first side effect an operator command compares each method's
described schema (local or live) with :data:`OPERATOR_METHOD_SHAPES`, and it
checks each answer where it consumes it (:func:`answer_problem`). Here: the
bundled owners (identity, tenancy) and the third-party OIDC example conform;
an incompatible schema or answer is named, expected against got. The
bundled owners' conformance: ``tests/integration/test_operator_owner_standard.py``.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from sandloom.core.cli.operator_owner import (
    answer_problem,
    schema_problem,
)
from sandloom.core.module_api import contract_method
from sandloom.core.module_api._provider import contract_schema


def _described(impl: Any) -> dict[str, Any]:
    return {
        name: method.describe()
        for name, method in contract_schema(impl).methods.items()
    }


class Counted(BaseModel):
    affected: int


class _Narrow(BaseModel):
    affected: int
    ended: list[Literal["refresh_sessions"]]


class _Incompatible:
    @classmethod
    @contract_method
    async def require_password_reset(
        cls, user_ids: list[uuid.UUID], reason: str, operator: str, key: str
    ) -> Counted:
        """A DTO where the kernel reads an int."""
        return Counted(affected=0)

    @classmethod
    @contract_method
    async def end_sessions(
        cls, user_ids: list[uuid.UUID], reason: str, operator: str, key: str
    ) -> int:
        """A count where the kernel reads which kinds ended."""
        return 0

    @classmethod
    @contract_method
    async def list_user_ids(cls, after: str | None, limit: int) -> list[str]:
        """Strings where the kernel sends and reads ids."""
        return []

    @classmethod
    @contract_method
    async def is_platform_admin(cls, user_id: uuid.UUID, tenant: str) -> bool:
        """A required parameter the kernel does not send."""
        return False


class _Compatible:
    @classmethod
    @contract_method
    async def end_sessions(
        cls,
        user_ids: list[uuid.UUID],
        reason: str,
        operator: str,
        key: str,
        dry: bool = False,
    ) -> _Narrow:
        """A narrower answer and an optional extra parameter conform."""
        return _Narrow(affected=0, ended=[])

    @classmethod
    @contract_method
    async def set_password_token(cls, user_id: uuid.UUID) -> str:
        """Never None: narrower than ``str | None``."""
        return "t"


@pytest.mark.parametrize(
    ("method", "expected", "got"),
    [
        (
            "require_password_reset",
            "-> int",
            "-> {affected: int}",
        ),
        (
            "end_sessions",
            "-> {affected: int, ended: list[Literal['access_tokens', "
            "'refresh_sessions', 'provider_sessions']]}",
            "-> int",
        ),
        ("list_user_ids", "(uuid | None, int) -> list[uuid]", "(str | None, int)"),
        ("is_platform_admin", "(uuid) -> bool", "(uuid, str) -> bool"),
    ],
)
def test_an_incompatible_schema_is_named_expected_against_got(
    method: str, expected: str, got: str
) -> None:
    problem = schema_problem(method, _described(_Incompatible())[method])
    assert problem is not None
    lead, _, found = problem.partition(", got ")
    assert expected in lead
    assert got in found


def test_narrower_answers_and_optional_extra_parameters_conform() -> None:
    described = _described(_Compatible())
    assert schema_problem("end_sessions", described["end_sessions"]) is None
    assert schema_problem("set_password_token", described["set_password_token"]) is None


def test_a_method_without_a_schema_is_incompatible() -> None:
    assert schema_problem("end_sessions", None) == (
        "expected (list[uuid], str, str, str) -> {affected: int, ended: "
        "list[Literal['access_tokens', 'refresh_sessions', 'provider_sessions']]}"
        ", got no schema"
    )


def test_a_method_the_kernel_never_calls_has_no_shape() -> None:
    with pytest.raises(ValueError, match="no expected shape"):
        schema_problem("drop_everything", {})
    with pytest.raises(ValueError, match="no expected shape"):
        answer_problem("drop_everything", None)


@pytest.mark.parametrize(
    ("method", "answer"),
    [
        ("require_password_reset", 3),
        ("end_sessions", {"affected": 1, "ended": ["refresh_sessions"]}),
        ("end_sessions", _Narrow(affected=1, ended=["refresh_sessions"])),
        ("list_user_ids", [uuid.uuid4(), str(uuid.uuid4())]),
        ("account_by_email", None),
        (
            "account_by_email",
            {"user_id": str(uuid.uuid4()), "email": "a@b.c", "email_verified": True},
        ),
        ("set_password_token", None),
        ("appoint_platform_admin", False),
    ],
)
def test_answers_of_the_stated_shape_pass(method: str, answer: Any) -> None:
    assert answer_problem(method, answer) is None


@pytest.mark.parametrize(
    ("method", "answer", "problem"),
    [
        ("require_password_reset", Counted(affected=1), "expected int, got Counted"),
        ("require_password_reset", True, "expected int, got bool"),
        ("end_sessions", 2, "got int"),
        ("end_sessions", {"affected": 1}, "got no 'ended' in dict"),
        (
            "end_sessions",
            {"affected": 1, "ended": ["everything"]},
            "ended: expected Literal[",
        ),
        ("list_user_ids", ["nope"], "expected uuid, got str"),
        ("appoint_platform_admin", 1, "expected bool, got int"),
        (
            "account_by_email",
            {"user_id": str(uuid.uuid4()), "email": "a@b.c", "email_verified": "yes"},
            "email_verified: expected bool, got str",
        ),
    ],
)
def test_answers_of_another_shape_are_named(
    method: str, answer: Any, problem: str
) -> None:
    found = answer_problem(method, answer)
    assert found is not None
    assert problem in found
