"""An intent insert keeps working on a long-lived pooled connection.

The insert names the live-request partial index as its ``ON CONFLICT``
arbiter. PostgreSQL plans a prepared statement with its parameter values five
times, then may switch to a generic plan; a predicate given as bound
parameters can then no longer be matched to the index ("no unique or exclusion
constraint matching the ON CONFLICT specification"). Production connections
live long, so every checkout and portal request would fail on a connection
that had served five before. The predicate is literal SQL, as in the index.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_billing.models import BillingCustomer
from sandloom_billing.services.operation_intents import OperationIntentService
from sandloom_billing.services.tenant_resolver import TenantContext


async def test_the_intent_insert_survives_a_generic_plan(test_db: AsyncSession) -> None:
    user_id = uuid.uuid4()
    customer = BillingCustomer(user_id=user_id)
    test_db.add(customer)
    await test_db.flush()
    ctx = TenantContext(tenant_type="user", tenant_id=user_id, user_id=user_id)
    for n in range(8):  # one connection: past the five custom plans
        intent = await OperationIntentService._insert(
            test_db,
            operation="checkout",
            ctx=ctx,
            customer=customer,
            hashed_key=f"request-{n}",
            params_digest="digest",
            price_id=None,
            version=None,
        )
        assert intent.request_key == f"request-{n}"
