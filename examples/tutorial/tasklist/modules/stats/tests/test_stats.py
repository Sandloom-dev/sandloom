"""Tests of the stats module (fixtures: the sandloom.sdk.testing plugin)."""

from typing import Any
from uuid import uuid4

from sandloom.sdk.testing import as_user, deliver

from ..consumers import ItemCreated


# --8<-- [start:tests]
async def test_a_redelivered_event_is_counted_once(client: Any, db: Any) -> None:
    tenant = uuid4()
    _, headers = as_user(tenant_id=tenant)
    event = ItemCreated(item_id=uuid4(), tenant_id=tenant)
    await deliver(event, times=2, session=db)  # the same envelope id twice
    response = await client.get("/api/v1/stats/count", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"items": 1}


async def test_counts_are_per_tenant(client: Any, db: Any) -> None:
    await deliver(ItemCreated(item_id=uuid4(), tenant_id=uuid4()), session=db)
    _, headers = as_user(tenant_id=uuid4())  # another tenant
    response = await client.get("/api/v1/stats/count", headers=headers)
    assert response.json() == {"items": 0}


# --8<-- [end:tests]
