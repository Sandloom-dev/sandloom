"""Hidden DX product probes: copied into the agent's checkout only for scoring."""

from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio

from sandloom.sdk.testing.fixtures import *  # noqa: F403


@pytest_asyncio.fixture
async def live(real_uow_client: Any, sandloom_app: Any) -> AsyncIterator[Any]:
    """``real_uow_client`` with the app's lifespan running.

    The harness never starts the lifespan, so registered event consumers never
    subscribe and an event chain can't be observed end to end (``deliver``
    drives one consumer, not the chain). The product is judged as it runs in
    production: startup tasks, consumers and the relay are live.
    """
    async with sandloom_app.router.lifespan_context(sandloom_app):
        yield real_uow_client
