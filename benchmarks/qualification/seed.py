"""Idempotent fixture creation; never truncate an existing qualification run."""

import asyncio

import asyncpg

from benchsuite import common
from benchsuite.setup_db import DDL, setup_s5


async def main() -> None:
    conn = await asyncpg.connect(common.asyncpg_dsn())
    try:
        await conn.execute(DDL)
    finally:
        await conn.close()
    await setup_s5()


if __name__ == "__main__":
    asyncio.run(main())
