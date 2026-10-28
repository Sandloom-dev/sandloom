"""Billing test fixtures, on top of the framework harness (``sandloom.core.testing``)."""

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.testing import as_user as _mint
from sandloom.sdk.testing.fixtures import *  # noqa: F403
from sandloom_billing.models import (
    INTERVAL_MONTH,
    PRODUCT_TYPE_ONE_TIME,
    PRODUCT_TYPE_SUBSCRIPTION,
    BillingPrice,
    BillingProduct,
)

WEBHOOK_SECRET = "whsec_test_secret"


@pytest.fixture
def test_db(db_uow: AsyncSession) -> AsyncSession:
    """``db`` bound as the Unit of Work: services called directly on it write
    their events / audits into its outbox (0.7: both raise outside a UoW)."""
    return db_uow


@pytest.fixture
def fake_psp() -> Any:
    """Fresh FakeStripeClient enforcing the test webhook secret."""
    from sandloom_billing.psp import FakeStripeClient

    return FakeStripeClient(webhook_secret=WEBHOOK_SECRET)


@pytest.fixture
def _installed_psp(fake_psp: Any) -> Iterator[Any]:
    """Swap billing's private PSP client for the per-test fake (restored after)."""
    from sandloom_billing.psp import install_psp_client

    previous = install_psp_client(fake_psp)
    yield fake_psp
    install_psp_client(previous)


@pytest_asyncio.fixture
async def async_client(
    client: AsyncClient, _installed_psp: Any, sandloom_cache: Any, sandloom_core: Any
) -> AsyncIterator[AsyncClient]:
    """The harness ``client`` (requests share ``test_db``) with the fake PSP and
    the idempotency cache on the test Redis.

    The class-F routes (checkout / portal) do not run in the request's
    session: each step is its own committed Unit of Work (``db_session()``,
    ADR-0003 G1 operation intents) and their membership reads are contract
    calls of their own. The app's ``default`` database therefore points at the
    test database too, so those transactions land where ``test_db`` can see
    them (rows they need must be COMMITTED by the test).
    """
    import os

    from sandloom.sdk.testing import override_instance

    # ``db_session()`` resolves the process's active core: the harness keeps
    # it bound to THIS app (an app another test built is unbound after that
    # test - sandloom.core.testing.isolation).
    async with override_instance(
        sandloom_core,
        "database",
        driver="sqlalchemy",
        url=os.environ["DATABASE_URL"],
    ):
        yield client


@pytest_asyncio.fixture
async def uow_client(real_uow_client: AsyncClient, _installed_psp: Any) -> AsyncClient:
    """Client running requests through the REAL request UoW (no get_db override):
    every request commits / rolls back exactly as in production."""
    return real_uow_client


def make_user_headers(user_id: uuid.UUID | None = None) -> tuple[uuid.UUID, dict]:
    """A bare access token: only ``sub`` + ``token_type`` (no tenant / email)."""
    return _mint(user_id, tenant=False, email=None, email_verified=None)


@pytest.fixture
def user_a() -> tuple[uuid.UUID, dict[str, Any]]:
    return make_user_headers()


@pytest.fixture
def user_b() -> tuple[uuid.UUID, dict[str, Any]]:
    return make_user_headers()


@pytest_asyncio.fixture
async def catalog(test_db: AsyncSession) -> dict[str, Any]:
    """Seed one subscription product and one one-time credit pack."""
    pro = BillingProduct(
        name="Pro", type=PRODUCT_TYPE_SUBSCRIPTION, stripe_product_id="prod_pro"
    )
    credits_pack = BillingProduct(
        name="100 credits", type=PRODUCT_TYPE_ONE_TIME, stripe_product_id="prod_credits"
    )
    test_db.add_all([pro, credits_pack])
    await test_db.flush()

    pro_month = BillingPrice(
        product_id=pro.id,
        amount=9900,
        currency="USD",
        recurring_interval=INTERVAL_MONTH,
        stripe_price_id="price_pro_month",
    )
    pack_price = BillingPrice(
        product_id=credits_pack.id,
        amount=1900,
        currency="USD",
        recurring_interval=None,
        stripe_price_id="price_credits_100",
        credits=100,
    )
    test_db.add_all([pro_month, pack_price])
    await test_db.commit()
    return {
        "pro": pro,
        "pro_month": pro_month,
        "credits_pack": credits_pack,
        "pack_price": pack_price,
    }
