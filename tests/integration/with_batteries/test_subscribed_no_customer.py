"""A user with no billing customer is 402 on both ``Subscribed`` reads (KB-33g).

The real chain, on the standard composition (identity, tenancy, billing):
the user logs in, the issuer collects billing's real claims contributor
(``EntitlementsService.subscription_claims()``: ``{}`` for a user with no
billing customer, by billing's contract), mints the token with billing in
``claims_sources`` and the claim policy (``cpv``) of the root's map, the
verifier applies its trusted view, and billing's generated ``Subscribed``
(as ``sandloom generate`` writes it beside a consumer's stub of
``billing.entitlements.v1``) decides. Billing's own claim is proven absent,
so ``Subscribed()`` and ``Subscribed(statuses=...)`` agree on billing's 402,
``statuses={"none"}`` does not match the absent key, and ``~Subscribed``
allows on that proven deny. For ``Account.user()`` the decision asks no
contract, resolves no fact live and runs no SQL.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from fastapi import APIRouter, Depends
from sqlalchemy import event

import sandloom.shared.authz.facts as facts
from sandloom.core.auth.tokens import decode_token
from sandloom.core.cli.generated import render_stub
from sandloom.core.runtime import use_active_core
from sandloom.core.testing.fixtures import *  # noqa: F403 - client, db
from sandloom.sdk import require
from sandloom.shared.authz import Account, Not
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.subject import UserContext
from sandloom_billing.contracts import EntitlementsContract
from sandloom_billing.entitlements import EntitlementsService
from sandloom_identity.models.user import User
from sandloom_identity.services.auth_service import AuthService

PASSWORD = "NoCustomerPassw0rd!x"
PREFIX = "/api/v1/_kb33g_subscribed"
KEY = "subscription_status"
STATUSES = frozenset({"active", "trialing"})


@pytest.fixture(scope="module")
def gen(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """Billing's generated atoms, imported as a consumer imports them."""
    path = tmp_path_factory.mktemp("kb33g") / "billing_entitlements_v1.py"
    path.write_text(render_stub("billing.entitlements.v1", EntitlementsContract))
    spec = importlib.util.spec_from_file_location("_kb33g_no_customer_stub", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _policies(gen: Any) -> dict[str, Any]:
    return {
        "default": gen.Subscribed(of=Account.user()),
        "statuses": gen.Subscribed(STATUSES, of=Account.user()),
        "none": gen.Subscribed(frozenset({"none"}), of=Account.user()),
        "not-default": Not(gen.Subscribed(of=Account.user())),
        "not-statuses": Not(gen.Subscribed(STATUSES, of=Account.user())),
    }


@pytest.fixture(scope="module", autouse=True)
def _gated_routes(sandloom_app: Any, gen: Any) -> Iterator[None]:
    router = APIRouter(prefix=PREFIX)
    for name, policy in _policies(gen).items():

        async def _gated() -> dict[str, str]:
            return {"ok": "subscribed"}

        router.add_api_route(
            f"/{name}",
            _gated,
            methods=["GET"],
            name=f"kb33g_{name}",
            dependencies=[Depends(require(policy))],
        )
    sandloom_app.include_router(router)
    yield
    sandloom_app.router.routes = [
        r
        for r in sandloom_app.router.routes
        if not getattr(r, "path", "").startswith(PREFIX)
    ]


async def _token_without_customer(client: Any, db: Any, email: str) -> str:
    """Log a fresh user (no billing customer) in through the real issuer."""
    db.add(
        User(
            email=email,
            full_name="No Customer",
            hashed_password=AuthService.get_password_hash(PASSWORD),
            is_active=True,
            is_verified=True,
        )
    )
    await db.commit()
    response = await client.post(
        "/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


async def test_no_billing_customer_is_402_on_both_reads(
    client: Any, db: Any, gen: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    contribute = EntitlementsService.subscription_claims

    async def _spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        claims = await contribute(*args, **kwargs)
        seen.append(dict(claims))
        return claims

    monkeypatch.setattr(EntitlementsService, "subscription_claims", _spy)
    token = await _token_without_customer(client, db, "kb33g-no-customer@example.com")

    # Billing's contract: no customer -> {} (never "none"); billing ran OK.
    assert seen == [{}]
    minted = jwt.decode(token, options={"verify_signature": False})
    assert KEY not in minted
    assert "billing" in minted["claims_sources"]
    assert minted["claims_sources_failed"] == []
    assert KEY in minted["cpv"]["k"]

    headers = {"Authorization": f"Bearer {token}"}
    outcomes = {
        name: (await client.get(f"{PREFIX}/{name}", headers=headers)).status_code
        for name in _policies(gen)
    }
    assert outcomes == {
        "default": 402,
        "statuses": 402,
        "none": 402,
        "not-default": 200,
        "not-statuses": 200,
    }


async def test_the_decision_does_no_io(
    client: Any, db: Any, gen: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same token decided directly: 402 on both reads with zero SQL,
    zero contract calls and zero fact lookups or live resolutions."""
    token = await _token_without_customer(client, db, "kb33g-no-io@example.com")
    core = client._transport.app.state.core
    with use_active_core(core):
        payload = decode_token(token)
    assert payload is not None
    subject = UserContext(**payload)
    assert subject.claim(KEY) is None and KEY not in subject.untrusted_claims

    asked: list[str] = []
    get_service = core.get_service

    def _counted_service(name: str, *args: Any, **kwargs: Any) -> Any:
        asked.append(name)
        return get_service(name, *args, **kwargs)

    calls = {"lookup": 0, "resolve": 0}

    async def _lookup(*args: Any, **kwargs: Any) -> Any:
        calls["lookup"] += 1
        raise AssertionError("a fact lookup")

    async def _resolve(*args: Any, **kwargs: Any) -> Any:
        calls["resolve"] += 1
        raise AssertionError("a live fact")

    monkeypatch.setattr(core, "get_service", _counted_service)
    monkeypatch.setattr(facts, "lookup_access_fact", _lookup)
    monkeypatch.setattr(facts, "resolve_fact", _resolve)
    statements: list[str] = []

    def _count(_conn: Any, _cursor: Any, sql: str, *_args: Any) -> None:
        statements.append(sql)

    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", _count)
    try:
        ctx = AuthzContext(subject, core=core)
        decisions = {
            name: await policy.decide(ctx) for name, policy in _policies(gen).items()
        }
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    for name in ("default", "statuses", "none"):
        decision = decisions[name]
        assert not decision.allowed and not decision.indeterminate, decision
        assert (decision.http_status, decision.code) == (402, "Subscribed")
    assert decisions["not-default"].allowed and decisions["not-statuses"].allowed
    assert asked == [] and statements == []
    assert calls == {"lookup": 0, "resolve": 0}
