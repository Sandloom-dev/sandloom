"""Provider process for the kill-mid-command scenario (not a test module).

Run as ``python _crash_provider.py <SERVICE> <amount> <operation_key>``:
serves ONE ``spend`` command on the real test database, prints ``CLAIMED``
once its body has run its effect (claim row + effect row written, NOT
committed) and then hangs until it is killed.
"""

from __future__ import annotations

import asyncio
import os
import sys

from fastapi import FastAPI
from sqlalchemy import text

from sandloom.core.drivers.sqlalchemy_database import create_sqlalchemy_database
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.module_api import contract_method, contract_session


class Ledger:
    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def spend(cls, amount: int, operation_key: str) -> int:
        await contract_session().execute(
            text("INSERT INTO idem_demo_effects (op, amount) VALUES (:op, :amount)"),
            {"op": operation_key, "amount": amount},
        )
        print("CLAIMED", flush=True)
        await asyncio.sleep(3600)  # killed here, mid-command
        return 100 - amount


async def main(service: str, amount: int, key: str) -> None:
    provider = create_sqlalchemy_database(
        InstanceConfig(
            capability="database",
            name="default",
            driver="sqlalchemy",
            options={"url": os.environ["DATABASE_URL"]},
        )
    )
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": provider  # type: ignore[method-assign]
    core.register_service(service, Ledger)
    await core.get_service(service).spend(amount, key)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]), sys.argv[3]))
