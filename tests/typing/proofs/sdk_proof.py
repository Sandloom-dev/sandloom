"""Type proof of the L1 surface (spec §8.1, Appendix E).

Checked with ``mypy --strict`` (the repository's pydantic plugin settings)
and pyright (strict). Every ``# type: ignore[...]`` marks a real error: both
checkers fail on an unneeded one (``warn_unused_ignores``,
``reportUnnecessaryTypeIgnoreComment``), so each pins an expected refusal.
Never imported at run time.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any, Protocol, assert_type
from uuid import UUID, uuid4

from pydantic import BaseModel

from sandloom.sdk import (
    Contract,
    CurrentUser,
    Event,
    command,
    contract,
    module_router,
    on,
)
from sandloom.sdk.contracts import ContractRef, OptionalContract
from sandloom.sdk.events import EventMeta
from sandloom.sdk.sagas import Saga, step
from sandloom.sdk.testing import as_user, deliver, fake_contract, published


# -- contracts: the generated stub -------------------------------------------
class StatsQuotaV1(Protocol):
    async def may_create_note(self) -> bool: ...
    async def reserve(self, amount: int, operation_key: str) -> int: ...


STATS_QUOTA_V1: ContractRef[StatsQuotaV1] = ContractRef("stats.quota.v1")


# -- contracts: the provider -----------------------------------------------------
@contract("stats.quota.v1")
class Quota:
    async def may_create_note(self) -> bool:
        return True

    @command(idempotency_key="operation_key")
    async def reserve(self, amount: int, operation_key: str) -> int:
        return amount

    @contract.read(planned=False, database="stats")
    async def remaining(self) -> int:
        return 1


class BrokenQuota:
    async def may_create_note(self) -> int:
        return 1

    async def reserve(self, amount: int, operation_key: str) -> int:
        return amount


_conforms: type[StatsQuotaV1] = Quota
_broken: type[StatsQuotaV1] = BrokenQuota  # type: ignore[assignment]


async def caller() -> None:
    quota = Contract(STATS_QUOTA_V1)
    assert_type(quota, StatsQuotaV1)
    assert_type(await quota.may_create_note(), bool)
    maybe = Contract(STATS_QUOTA_V1, optional=True)
    assert_type(maybe, OptionalContract[StatsQuotaV1])
    assert_type(maybe.client(), StatsQuotaV1 | None)
    reserve: Callable[[int, str], Coroutine[Any, Any, int]] = Quota().reserve
    assert_type(await reserve(1, "k"), int)
    await Quota().reserve("x", "k")  # type: ignore[arg-type]
    await quota.reserve(1)  # type: ignore[call-arg]


# -- events and consumers ----------------------------------------------------------
class NoteCreated(Event, channel="notes.note.created.v1"):
    note_id: UUID
    tenant_id: UUID


class Other(Event, channel="other.thing.happened.v1"):
    x: int


@on(NoteCreated)
async def count_note(event: NoteCreated) -> None:
    assert_type(event.meta, EventMeta)
    assert_type(event.tenant_id, UUID)


@on(NoteCreated, name="n", durable=False, tenant=None)
async def other_options(event: NoteCreated) -> None: ...


@on(NoteCreated)  # type: ignore[arg-type]
async def wrong(event: Other) -> None: ...


async def publishing() -> None:
    created = NoteCreated(note_id=uuid4(), tenant_id=uuid4())
    await created.publish()
    NoteCreated(note_id=1, tenant_id=uuid4())  # type: ignore[arg-type]
    assert_type(published(NoteCreated), list[NoteCreated])
    assert_type(await deliver(created, times=2), int)
    assert_type(await deliver("notes.note.created.v1", {"note_id": "x"}), int)


# -- routes ---------------------------------------------------------------------
router = module_router()


@router.post("/", status_code=201, tenant="required")
async def create(user: CurrentUser) -> dict[str, str]:
    return {"user": str(user.user_id)}


@router.get("/x", tenant="sometimes")  # type: ignore[arg-type]
async def bad_binding() -> None: ...


# -- sagas ------------------------------------------------------------------------
class ReserveInput(BaseModel):
    amount: int


quota_caller = Contract(STATS_QUOTA_V1)
RESERVE = Saga(
    "reserve",
    version=1,
    input=ReserveInput,
    deadline=timedelta(minutes=1),
    steps=[step("reserve", call=quota_caller.reserve, args=lambda ctx: {"amount": 1})],
)


# -- testing ---------------------------------------------------------------------
def testing_kit() -> None:
    user_id, headers = as_user(tenant_id=uuid4(), roles=["admin"])
    assert_type(user_id, UUID)
    assert_type(headers, dict[str, str])
    with fake_contract(STATS_QUOTA_V1, Quota()):
        pass
