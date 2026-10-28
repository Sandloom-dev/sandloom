"""Saga definitions and their deterministic checks (ADR-0008 §1, §3, §14).

Structure at construction, contracts at registry build (startup), the
consumed-contract requirements, the outcome table (§7) and the guarantee
window (§9). No database.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from sandloom.core import guarantee_windows as gw
from sandloom.core.descriptors import (
    ContractRequirement,
    ModuleDescriptor,
    saga_contract_requirements,
    synthetic_instance,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.loader import plugin_loader
from sandloom.core.module_api import (
    ContractArgumentError,
    ContractCallError,
    ContractCallInProgress,
    ContractIdempotencyConflict,
    ContractResultNotReplayable,
    Saga,
    SagaContext,
    SagaDefinitionError,
    StepKind,
    contract_method,
    local_step,
    operation_key,
    step,
)
from sandloom.core.module_api._registration import pending_saga_requirements
from sandloom.core.module_api._service_registry import (
    ServiceNotFound,
    ServiceUnavailableInDeployment,
)
from sandloom.core.rpc import RemoteServiceUnavailable
from sandloom.core.sagas.outcomes import Outcome, classify_exception, classify_status
from sandloom.core.sagas.registry import build_registry


class In(BaseModel):
    user_id: uuid.UUID
    amount: int


def grant_args(c: SagaContext[In]) -> dict[str, object]:
    return {"tenant_id": c.input.user_id, "amount": c.input.amount}


def typo_args(c: SagaContext[In]) -> dict[str, object]:
    return {"tennant_id": c.input.user_id, "amount": c.input.amount}


async def _apply(db: Any, c: SagaContext[In]) -> None:
    return None


class Credits:
    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def grant(cls, tenant_id: uuid.UUID, amount: int, operation_key: str) -> int:
        return amount

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def revoke(cls, tenant_id: uuid.UUID, amount: int, operation_key: str) -> int:
        return amount

    @classmethod
    @contract_method
    async def balance(cls, tenant_id: uuid.UUID) -> int:
        return 0


def _saga(*steps: Any, name: str = "org_setup", **kw: Any) -> Saga[In]:
    return Saga[In](
        name,
        version=1,
        input=In,
        deadline=timedelta(minutes=5),
        steps=list(steps),
        **kw,
    )


def _grant(**kw: Any) -> Any:
    base: dict[str, Any] = {
        "call": "dfbilling.credits.v1.grant",
        "args": grant_args,
        "compensate": "dfbilling.credits.v1.revoke",
    }
    base.update(kw)
    return step("grant_trial_credits", **base)


def _core(*sagas: Saga[Any], module: str = "sandloom_tenancy") -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core.register_service("dfbilling.credits.v1", Credits)
    for saga in sagas:
        core.register_saga(saga, module)
    return core


# --------------------------------------------------------------------------- #
# structure (construction)
# --------------------------------------------------------------------------- #
def test_kinds_default_around_the_pivot() -> None:
    saga = _saga(
        _grant(),
        local_step("activate", kind="pivot", apply=_apply),
        local_step("welcome", apply=_apply),
    )
    assert [s.kind for s in saga.steps] == [
        StepKind.COMPENSATABLE,
        StepKind.PIVOT,
        StepKind.RETRIABLE,
    ]
    assert saga.pivot_index == 1


@pytest.mark.parametrize(
    ("steps", "needle"),
    [
        ((_grant(), _grant()), "duplicate step name"),
        ((_grant(compensate=None),), "compensatable step without a compensation"),
        (
            (
                _grant(),
                local_step("a", kind="pivot", apply=_apply),
                local_step("b", kind="pivot", apply=_apply),
            ),
            "more than one pivot",
        ),
        (
            (local_step("a", kind="retriable", apply=_apply),),
            "a retriable step before the pivot",
        ),
        (
            (
                local_step("p", kind="pivot", apply=_apply),
                local_step("c", kind="compensatable", apply=_apply, compensate=_apply),
            ),
            "a compensatable step after the pivot",
        ),
        (
            (local_step("p", kind="pivot", apply=_apply, compensate=_apply),),
            "a compensation on a pivot step",
        ),
        ((_grant(call="billing.grant"),), "is not '<contract>.vN.<method>'"),
        ((_grant(refuse_on={503}),), "would treat a transient status"),
        ((_grant(refuse_on={429}),), "would treat a transient status"),
        ((), "no steps"),
    ],
)
def test_structure_is_refused_with_a_fix(steps: tuple[Any, ...], needle: str) -> None:
    with pytest.raises(SagaDefinitionError) as info:
        _saga(*steps)
    assert needle in str(info.value)
    assert "fix:" in str(info.value)


def test_refuse_on_may_add_a_business_status() -> None:
    saga = _saga(_grant(refuse_on={409}), local_step("p", kind="pivot", apply=_apply))
    assert saga.steps[0].refuse_on == frozenset({409})


def test_operation_key_rule() -> None:
    assert operation_key("enroll", uuid.UUID(int=1), 3) == (
        "enroll:00000000-0000-0000-0000-000000000001:3"
    )
    for bad in ("", "a:b", "a b"):
        with pytest.raises(ValueError, match="operation_key part"):
            operation_key("x", bad)
    with pytest.raises(ValueError):
        operation_key()


def test_context_key_and_typed_result() -> None:
    class Spent(BaseModel):
        balance: int

    ctx = SagaContext(
        saga_type="t.s",
        saga_id="9",
        input=In(user_id=uuid.uuid4(), amount=1),
        results={"charge": {"balance": 5}},
    )
    assert ctx.key("charge") == "saga:t.s:9:charge:do"
    assert ctx.key("charge", "undo") == "saga:t.s:9:charge:undo"
    assert ctx.result("charge", Spent) == Spent(balance=5)
    assert ctx.result("charge") == {"balance": 5}
    with pytest.raises(KeyError):
        ctx.result("missing")


def test_synthetic_instance_covers_wire_types() -> None:
    class Color(Enum):
        RED = "red"

    class Inner(BaseModel):
        n: int

    class Wide(BaseModel):
        a: uuid.UUID
        b: str
        c: Decimal
        d: Color
        e: Inner
        f: list[int]
        g: int | None
        h: str = "default"

    made = synthetic_instance(Wide)
    assert made.a == uuid.UUID(int=1) and made.d is Color.RED
    assert made.e.n == 1 and made.f == [] and made.h == "default"


def test_unregistered_definition_has_no_type() -> None:
    saga = _saga(_grant(), local_step("p", kind="pivot", apply=_apply))
    with pytest.raises(SagaDefinitionError, match="not registered"):
        _ = saga.saga_type
    assert saga.bind("sandloom_tenancy") == "tenancy.org_setup"
    with pytest.raises(SagaDefinitionError, match="already registered"):
        saga.bind("sandloom_billing")


# --------------------------------------------------------------------------- #
# contracts (startup)
# --------------------------------------------------------------------------- #
def test_a_matching_definition_builds() -> None:
    saga = _saga(_grant(), local_step("p", kind="pivot", apply=_apply))
    registry = build_registry(_core(saga))
    reg = next(iter(registry))
    assert reg.saga_type == "tenancy.org_setup"
    assert reg.channel == "tenancy.saga_advance.v1"
    assert reg.key_params[("grant_trial_credits", "do")] == "operation_key"
    assert reg.key_params[("grant_trial_credits", "undo")] == "operation_key"
    assert registry.describe()[0]["steps"][0]["call"] == "dfbilling.credits.v1.grant"


def test_parameter_typo_names_the_fix() -> None:
    saga = _saga(_grant(args=typo_args), local_step("p", kind="pivot", apply=_apply))
    with pytest.raises(SagaDefinitionError) as info:
        build_registry(_core(saga))
    text = str(info.value)
    assert 'has no parameter "tennant_id" (did you mean "tenant_id"?)' in text
    assert "fix: return the provider's parameter names from typo_args()" in text
    assert 'required parameter "tenant_id" is not supplied' in text


@pytest.mark.parametrize(
    ("kw", "needle"),
    [
        ({"call": "dfbilling.credits.v1.grnt"}, 'did you mean "grant"'),
        (
            {
                "call": "dfbilling.credits.v1.balance",
                "args": lambda c: {"tenant_id": 1},
            },
            "is a read contract",
        ),
        ({"call": "nobody.home.v1.grant"}, "no describe document"),
        (
            {"args": lambda c: {**grant_args(c), "operation_key": "x"}},
            "passes the idempotency key",
        ),
        ({"args": lambda c: c.result("nope", int) + None}, "raised TypeError"),
    ],
)
def test_contract_problems_are_refused(kw: dict[str, Any], needle: str) -> None:
    saga = _saga(_grant(**kw), local_step("p", kind="pivot", apply=_apply))
    with pytest.raises(SagaDefinitionError) as info:
        build_registry(_core(saga))
    assert needle in str(info.value)


def test_check_args_false_skips_the_name_check() -> None:
    saga = _saga(
        _grant(args=typo_args, check_args=False),
        local_step("p", kind="pivot", apply=_apply),
    )
    build_registry(_core(saga))


def test_a_remote_provider_is_checked_against_its_snapshot(tmp_path: Path) -> None:
    from sandloom.core.module_api import contract_schema

    snapshot = tmp_path / "contracts.snapshot.json"
    snapshot.write_text(
        json.dumps(
            {"contracts": {"dfbilling.credits.v1": contract_schema(Credits).describe()}}
        )
    )
    saga = _saga(_grant(args=typo_args), local_step("p", kind="pivot", apply=_apply))
    core = CoreFacade(app=FastAPI())  # no local provider
    core.settings.contract_snapshot_paths = [str(snapshot)]
    try:
        core.register_saga(saga, "sandloom_tenancy")
        with pytest.raises(SagaDefinitionError, match='did you mean "tenant_id"'):
            build_registry(core)
    finally:
        core.settings.contract_snapshot_paths = []


def test_requirements_cover_every_called_method() -> None:
    saga = _saga(_grant(), local_step("p", kind="pivot", apply=_apply))
    assert saga_contract_requirements(saga) == [
        ContractRequirement(
            "dfbilling.credits.v1",
            reason="saga 'org_setup' steps",
            methods=("grant", "revoke"),
        )
    ]


def test_register_saga_buffers_and_the_loader_merges_consumes() -> None:
    core = CoreFacade(app=FastAPI())
    saga = _saga(_grant(), local_step("p", kind="pivot", apply=_apply), name="onboard")
    with core.module_registration("sandloom_tenancy") as api:
        api.register_saga(saga)
        assert pending_saga_requirements(api)[0].methods == ("grant", "revoke")
        desc = ModuleDescriptor(name="sandloom_tenancy", version="1")
        plugin_loader._merge_saga_requirements(desc, api)
        plugin_loader._merge_saga_requirements(desc, api)  # idempotent
    assert [r.name for r in desc.consumes] == ["dfbilling.credits.v1"]
    assert core.sagas == [(saga, "sandloom_tenancy")]
    assert saga.saga_type == "tenancy.onboard"


def test_register_saga_rejects_a_non_definition() -> None:
    core = CoreFacade(app=FastAPI())
    with core.module_registration("sandloom_tenancy") as api, pytest.raises(TypeError):
        api.register_saga(object())


# --------------------------------------------------------------------------- #
# the outcome table (§7)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "code", "refuse_on", "expected"),
    [
        (400, None, (), Outcome.REFUSED),
        (402, None, (), Outcome.REFUSED),
        (403, None, (), Outcome.REFUSED),
        (404, None, (), Outcome.REFUSED),
        (410, None, (), Outcome.REFUSED),
        (422, "error", (), Outcome.REFUSED),
        (408, None, (), Outcome.TRANSIENT),
        (423, None, (), Outcome.TRANSIENT),
        (425, None, (), Outcome.TRANSIENT),
        (429, None, (), Outcome.TRANSIENT),
        (500, None, (), Outcome.TRANSIENT),
        (501, None, (), Outcome.TRANSIENT),
        (503, "remote_service_unavailable", (), Outcome.TRANSIENT),
        (503, "contract_call_in_progress", (), Outcome.TRANSIENT),
        (418, None, (), Outcome.TRANSIENT),  # unknown: retrying is the safe default
        (409, "error", (), Outcome.TRANSIENT),  # a domain 409 is not listed
        (409, "error", (409,), Outcome.REFUSED),  # ... unless the step narrows it
        (409, "contract_idempotency_conflict", (409,), Outcome.CONFLICT),
        (409, "contract_result_not_replayable", (), Outcome.CONFLICT),
        (401, None, (), Outcome.MISCONFIGURED),
        (422, "contract_argument_invalid", (), Outcome.MISCONFIGURED),
        (422, "contract_idempotency_key_missing", (), Outcome.MISCONFIGURED),
    ],
)
def test_outcome_table(
    status: int, code: str | None, refuse_on: tuple[int, ...], expected: Outcome
) -> None:
    assert classify_status(status, code, refuse_on=frozenset(refuse_on)) is expected


def test_exceptions_classify_through_the_table() -> None:
    def cls(exc: BaseException) -> Outcome:
        return classify_exception(exc).outcome

    assert cls(ContractArgumentError("x")) is Outcome.MISCONFIGURED
    assert cls(ContractIdempotencyConflict("x")) is Outcome.CONFLICT
    assert classify_exception(ContractResultNotReplayable("x")).conflict_reason() == (
        "result_not_replayable"
    )
    assert cls(ContractCallInProgress("x")) is Outcome.TRANSIENT
    assert cls(ContractCallError("no", status_code=404)) is Outcome.REFUSED
    assert cls(RemoteServiceUnavailable("s", reason="timeout")) is Outcome.TRANSIENT
    assert cls(ServiceUnavailableInDeployment("s")) is Outcome.TRANSIENT
    assert cls(ServiceNotFound("s")) is Outcome.MISCONFIGURED
    assert cls(HTTPException(status_code=403)) is Outcome.REFUSED
    assert cls(TimeoutError()) is Outcome.TRANSIENT
    assert cls(ConnectionResetError()) is Outcome.TRANSIENT
    assert cls(RuntimeError("bug")) is Outcome.TRANSIENT

    class Domain(Exception):
        http_status = 422

    assert cls(Domain("no seat")) is Outcome.REFUSED


# --------------------------------------------------------------------------- #
# the guarantee window (§9)
# --------------------------------------------------------------------------- #
class _Settings:
    contract_command_dedupe_ttl_seconds = 3600
    saga_compensation_deadline_seconds = 1800
    outbox_relay_backoff_base_seconds = 1.0
    outbox_relay_backoff_max_seconds = 1.0
    outbox_relay_max_attempts = 1
    is_prod = False
    sandloom_accepted_risks: list[str] = []


def test_saga_horizon_fits_the_dedupe_ttl() -> None:
    fits = _saga(
        _grant(), local_step("p", kind="pivot", apply=_apply)
    )  # 5 min + 30 min
    assert gw.saga_horizon_problems(_Settings(), [fits]) == []
    too_long = _saga(
        _grant(deadline=timedelta(hours=2)), local_step("p", kind="pivot", apply=_apply)
    )
    (problem,) = gw.saga_horizon_problems(_Settings(), [too_long])
    assert problem.risk_id == "saga_horizon_beyond_command_dedupe"
    assert "grant_trial_credits" in problem.message


def test_saga_horizon_refuses_in_prod_unless_accepted() -> None:
    too_long = _saga(
        _grant(),
        local_step("p", kind="pivot", apply=_apply),
        compensation_deadline=timedelta(hours=2),
    )
    prod = _Settings()
    prod.is_prod = True
    with pytest.raises(gw.GuaranteeWindowError, match="saga_horizon_beyond"):
        gw.enforce_guarantee_windows(prod, sagas=[too_long], accepted=())
    assert (
        gw.enforce_guarantee_windows(
            prod, sagas=[too_long], accepted={"saga_horizon_beyond_command_dedupe"}
        )
        == []
    )
    warned = gw.enforce_guarantee_windows(_Settings(), sagas=[too_long], accepted=())
    assert [p.risk_id for p in warned] == ["saga_horizon_beyond_command_dedupe"]
