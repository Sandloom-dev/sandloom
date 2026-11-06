"""A saga used by the kernel's saga tests: charge -> grant (pivot) -> notify.

The providers are harness fakes (``saga_harness.fake_contract``) registered
per test, so every command call goes through the real durable command log.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import Column, Integer, MetaData, String, Table, func, insert, select

import sandloom.core.sql_alchemy.portable as _portable
from sandloom.core import effects
from sandloom.core.drivers.sql_dialects import PostgresqlSql
from sandloom.core.module_api import Saga, SagaContext, local_step, step
from sandloom.core.sql_alchemy.models.contract_command import ContractCommandRecord
from sandloom.core.sql_alchemy.models.saga import SagaStepRecord
from sandloom.core.unit_of_work.request import db

BILLING = "sgbilling.credits.v1"
MAIL = "sgmail.outbox.v1"


class EnrollInput(BaseModel):
    customer_id: uuid.UUID
    course_id: uuid.UUID
    price: int


class Ledger:
    """In-memory provider state shared by the fakes of one test."""

    def __init__(self) -> None:
        self.balance = 100
        self.charges: dict[str, int] = {}
        self.refunds: dict[str, int] = {}
        self.mails: list[str] = []
        self.granted: list[str] = []

    async def spend(
        self, customer_id: uuid.UUID, amount: int, operation_key: str
    ) -> int:
        self.balance -= amount
        self.charges[operation_key] = amount
        return self.balance

    async def reverse(
        self,
        customer_id: uuid.UUID,
        amount: int,
        of_operation_key: str,
        operation_key: str,
    ) -> int:
        # "nothing to undo" is a success: refund only what was charged.
        charged = self.charges.pop(of_operation_key, 0)
        if charged:
            self.balance += charged
            self.refunds[of_operation_key] = charged
        return self.balance

    async def send(
        self, customer_id: uuid.UUID, template: str, operation_key: str
    ) -> str:
        self.mails.append(operation_key)
        return f"mail-{len(self.mails)}"


_EFFECTS = MetaData()
#: The provider's effect as a row: one per charge (``amount`` > 0) and one per
#: refund (``amount`` < 0), keyed by the call's operation key.
CHARGES = Table(
    "saga_test_charges",
    _EFFECTS,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("customer_id", String(36), nullable=False, index=True),
    Column("operation_key", String(400), nullable=False, index=True),
    Column("amount", Integer, nullable=False),
)


async def _committed_balance(customer_id: uuid.UUID) -> int:
    spent = (
        await db().execute(
            select(func.coalesce(func.sum(CHARGES.c.amount), 0)).where(
                CHARGES.c.customer_id == str(customer_id)
            )
        )
    ).scalar_one()
    return 100 - int(spent)


class DurableLedger(Ledger):
    """A :class:`Ledger` whose provider also writes its effect as a row in the
    command's own unit (``db()``), beside the command log's dedupe record.

    The in-memory state is not transactional: it keeps what the body did even
    when its unit rolls back. The row commits iff the provider's unit
    commits: on its own (PostgreSQL), or with the caller's transaction it
    joins on a serial-writer database (G-UOW-09). So the rows are what the
    provider durably did, on every database, and the result it returns is
    the balance they leave."""

    async def spend(
        self, customer_id: uuid.UUID, amount: int, operation_key: str
    ) -> int:
        await db().execute(
            insert(CHARGES).values(
                customer_id=str(customer_id), operation_key=operation_key, amount=amount
            )
        )
        await super().spend(customer_id, amount, operation_key)
        return await _committed_balance(customer_id)

    async def reverse(
        self,
        customer_id: uuid.UUID,
        amount: int,
        of_operation_key: str,
        operation_key: str,
    ) -> int:
        charged = (
            await db().execute(
                select(func.coalesce(func.sum(CHARGES.c.amount), 0)).where(
                    CHARGES.c.operation_key == of_operation_key
                )
            )
        ).scalar_one()
        if charged:
            await db().execute(
                insert(CHARGES).values(
                    customer_id=str(customer_id),
                    operation_key=operation_key,
                    amount=-charged,
                )
            )
        await super().reverse(customer_id, amount, of_operation_key, operation_key)
        return await _committed_balance(customer_id)


async def create_effect_tables(engine: Any) -> None:
    """Create :data:`CHARGES` afresh (the test database keeps no stale shape)."""
    async with engine.begin() as conn:
        await conn.run_sync(_EFFECTS.drop_all)
        await conn.run_sync(_EFFECTS.create_all)


async def committed_effects(engine: Any, operation_key: str) -> list[int]:
    """The amounts the provider durably committed under ``operation_key``."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(CHARGES.c.amount)
            .where(CHARGES.c.operation_key == operation_key)
            .order_by(CHARGES.c.id)
        )
        return [int(a) for (a,) in rows.all()]


async def durable_balance(engine: Any, saga_id: str) -> int:
    """The customer's balance from the committed rows of one saga's calls."""
    async with engine.connect() as conn:
        spent = (
            await conn.execute(
                select(func.coalesce(func.sum(CHARGES.c.amount), 0)).where(
                    CHARGES.c.operation_key.contains(f":{saga_id}:")
                )
            )
        ).scalar_one()
    return 100 - int(spent)


async def command_records(engine: Any, method: str, operation_key: str) -> int:
    """Committed command-log records of ``method`` (``"<contract>.<name>"``)
    under ``operation_key``: no tenant, a system caller (the saga runtime)."""
    dedupe_key = effects.key_digest(method, "-", operation_key)
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    select(func.count())
                    .select_from(ContractCommandRecord)
                    .where(ContractCommandRecord.dedupe_key == dedupe_key)
                )
            ).scalar_one()
        )


async def step_status(engine: Any, operation_key: str) -> str | None:
    """The committed ``saga_steps`` status of the call keyed ``operation_key``
    (``started``: only the marker; ``succeeded``; ``None``: no row)."""
    async with engine.connect() as conn:
        status = (
            await conn.execute(
                select(SagaStepRecord.status).where(
                    SagaStepRecord.operation_key == operation_key
                )
            )
        ).scalar_one_or_none()
    return None if status is None else str(status)


def charge_args(c: SagaContext[EnrollInput]) -> dict[str, object]:
    return {"customer_id": c.input.customer_id, "amount": c.input.price}


def refund_args(c: SagaContext[EnrollInput]) -> dict[str, object]:
    return {
        "customer_id": c.input.customer_id,
        "amount": c.input.price,
        "of_operation_key": c.key("charge"),
    }


def notify_args(c: SagaContext[EnrollInput]) -> dict[str, object]:
    return {"customer_id": c.input.customer_id, "template": "enrolled"}


GRANTS: list[str] = []


async def grant_access(db: Any, c: SagaContext[EnrollInput]) -> dict[str, object]:
    GRANTS.append(c.saga_id)
    return {"course_id": str(c.input.course_id), "balance": c.result("charge")}


def enroll_saga(
    name: str = "enroll",
    *,
    deadline: timedelta = timedelta(minutes=30),
    compensation_deadline: timedelta | None = None,
) -> Saga[EnrollInput]:
    return Saga[EnrollInput](
        name,
        version=1,
        input=EnrollInput,
        deadline=deadline,
        compensation_deadline=compensation_deadline,
        steps=[
            step(
                "charge",
                call=f"{BILLING}.spend",
                args=charge_args,
                compensate=f"{BILLING}.reverse",
                compensate_args=refund_args,
            ),
            local_step("grant_access", kind="pivot", apply=grant_access),
            step("notify", call=f"{MAIL}.send", args=notify_args),
        ],
    )


def install_fakes(harness: Any, ledger: Ledger, *, database: str | None = None) -> None:
    """The fakes of :data:`BILLING` and :data:`MAIL`; ``database``: the
    instance the billing provider's unit runs on."""
    harness.fake_contract(
        BILLING, spend=ledger.spend, reverse=ledger.reverse, database=database
    )
    harness.fake_contract(MAIL, send=ledger.send)


def enroll_input(price: int = 30) -> EnrollInput:
    return EnrollInput(customer_id=uuid.uuid4(), course_id=uuid.uuid4(), price=price)


class _PostgresqlWithoutSkipLocked(PostgresqlSql):
    """PostgreSQL's renderer as a database without ``skip_locked`` would be:
    it does not declare the feature and takes every row lock waiting."""

    def features(self) -> frozenset[str]:
        return super().features() - {"skip_locked"}

    def for_update(self, stmt: Any, *, skip_locked: bool, read: bool = False) -> Any:
        return super().for_update(stmt, skip_locked=False, read=read)


def without_skip_locked_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve PostgreSQL with :class:`_PostgresqlWithoutSkipLocked` for one
    test (restored after it), so every kernel path takes its compare-and-set
    claim on a real database."""
    _portable.sql_dialect("postgresql")  # the entry points are read first
    monkeypatch.setitem(
        _portable._registered, "postgresql", _PostgresqlWithoutSkipLocked()
    )
