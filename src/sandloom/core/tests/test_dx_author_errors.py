"""The author error types ``NotFound``, ``Conflict`` and ``Refused(status,
detail)`` of ``sandloom.sdk``: what a service, a contract method or a saga local
step raises instead of ``fastapi.HTTPException``.

Each is one error that every layer already understands: the HTTP handler
answers its status and ``code``, the contract caller receives a
``ContractCallError`` with the same status and code, and a saga classifies its
status (a refusal is final, a 409 is retried).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from sandloom.core.http.error_handlers import http_exception_handler
from sandloom.core.module_api import ContractCallError, normalize_contract_error
from sandloom.core.sagas.outcomes import Outcome, classify_exception
from sandloom.sdk import Conflict, NotFound, Refused

# --------------------------------------------------------------------------- #
# The types
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("error", "status", "code", "phrase"),
    [
        (NotFound(), 404, "not_found", "Not Found"),
        (Conflict(), 409, "conflict", "Conflict"),
        (Refused(402), 402, None, "Payment Required"),
        # a client status with no standard phrase (HTTPStatus has no 499): a
        # fixed detail, not the ValueError of the lookup
        (Refused(499), 499, None, "Client Error"),
        (Refused(422, "reason"), 422, None, "reason"),
        (Refused(403, code="needs_admin"), 403, "needs_admin", "Forbidden"),
        (NotFound("note gone", code="note_gone"), 404, "note_gone", "note gone"),
    ],
)
def test_each_type_carries_its_status_code_and_detail(
    error: Refused, status: int, code: str | None, phrase: str
) -> None:
    assert isinstance(error, HTTPException)
    assert error.status_code == status
    assert error.code == code
    assert error.detail == phrase


def test_headers_go_with_the_error() -> None:
    error = Refused(429, "slow down", headers={"Retry-After": "3"})
    assert error.headers == {"Retry-After": "3"}


@pytest.mark.parametrize("status", [200, 302, 399, 500, 503, 0, -404])
def test_refused_takes_only_a_client_status(status: int) -> None:
    """A server fault is a plain exception (answered as a 500 and masked): a
    ``Refused(500)`` would leak a message the framework hides on purpose."""
    with pytest.raises(ValueError, match="400-499"):
        Refused(status, "boom")


@pytest.mark.parametrize("status", ["404", 404.0, None, True])
def test_refused_takes_an_integer_status(status: Any) -> None:
    with pytest.raises(TypeError, match="integer status"):
        Refused(status, "boom")


# --------------------------------------------------------------------------- #
# HTTP: the body a client reads
# --------------------------------------------------------------------------- #


def _client() -> TestClient:
    app = FastAPI()
    app.add_exception_handler(HTTPException, http_exception_handler)

    @app.get("/missing")
    async def missing() -> None:
        raise NotFound("note not found")

    @app.get("/taken")
    async def taken() -> None:
        raise Conflict("slug already taken")

    @app.get("/quota")
    async def quota() -> None:
        raise Refused(402, "item quota reached", code="quota_reached")

    @app.get("/plain")
    async def plain() -> None:
        raise HTTPException(status_code=404, detail="plain")

    return TestClient(app)


@pytest.mark.parametrize(
    ("path", "status", "body"),
    [
        ("/missing", 404, {"detail": "note not found", "code": "not_found"}),
        ("/taken", 409, {"detail": "slug already taken", "code": "conflict"}),
        ("/quota", 402, {"detail": "item quota reached", "code": "quota_reached"}),
        ("/plain", 404, {"detail": "plain", "code": None}),
    ],
)
def test_a_route_answers_the_status_and_the_code(
    path: str, status: int, body: dict[str, Any]
) -> None:
    response = _client().get(path)
    assert response.status_code == status
    assert response.json() == body


# --------------------------------------------------------------------------- #
# Contracts: the caller sees the same status and code
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (NotFound("gone"), 404, "not_found"),
        (Conflict("taken"), 409, "conflict"),
        (Refused(422, "no", code="invalid_plan"), 422, "invalid_plan"),
    ],
)
def test_a_contract_caller_receives_the_same_status_and_code(
    error: Refused, status: int, code: str
) -> None:
    caller_side = normalize_contract_error(error)
    assert isinstance(caller_side, ContractCallError)
    assert caller_side.status_code == status
    assert caller_side.code == code
    assert caller_side.detail == error.detail


def test_a_plain_exception_is_still_no_refusal() -> None:
    """The mapping stays an allow-list: only an HTTP-status error is a refusal."""
    assert normalize_contract_error(RuntimeError("bug")) is None


# --------------------------------------------------------------------------- #
# Sagas: what a local step's refusal means
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (NotFound("student unknown"), Outcome.REFUSED),
        (Refused(402, "short balance"), Outcome.REFUSED),
        (Refused(422, "invalid"), Outcome.REFUSED),
        # a 409 is a state clash: retried until the deadline, never final
        (Conflict("seat taken"), Outcome.TRANSIENT),
        (Refused(429, "later"), Outcome.TRANSIENT),
        # a server fault is not a refusal: it must not compensate the saga
        (RuntimeError("bug"), Outcome.TRANSIENT),
    ],
)
def test_a_saga_step_classifies_the_error(
    error: BaseException, outcome: Outcome
) -> None:
    assert classify_exception(error).outcome is outcome


def test_the_saga_outcome_keeps_the_code_and_status() -> None:
    result = classify_exception(NotFound("student unknown"))
    assert (result.status, result.code) == (404, "not_found")
