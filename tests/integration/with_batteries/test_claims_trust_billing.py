"""Billing's generated ``Subscribed`` honours the trusted view (SEC-3).

Moved from ``src/sandloom/core/tests/test_claims_trust.py``, which runs
without batteries: ``Subscribed`` is billing's generated atom (a
``ClaimFactCheck``; its stub rendered from billing's contract, its fact
registered locally, nothing called). The view removed ``subscription_status``
because the token predates the claim policy (401) or issuer and verifier
disagree (503): the atom answers that indeterminate, never a 402 for the
token's life; an unassigned key or an absent authority is 501
``access_unknown``; ``Not`` never allows. Billing declares its four keys
fail-safe itself (B-N12), and a standard composition reads them so.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

import pytest

from sandloom.core.app_factory.claim_authorities import install_claim_authorities
from sandloom.core.auth import claims_view as view
from sandloom.core.cli.generated import render_stub
from sandloom.core.tests.test_claims_trust import (
    IDP,
    _authz,
    _core,
    _descriptor,
    _unknown,
)
from sandloom.shared.authz import Account, Not
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.subject import UserContext
from sandloom_billing import BILLING_CLAIM_KEYS
from sandloom_billing.access import SUBSCRIPTION_ACTIVE_FACT
from sandloom_billing.claims import contribute_billing_claims
from sandloom_billing.contracts import EntitlementsContract


@pytest.fixture(scope="module")
def billing_gen(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """Billing's generated atoms (``billing.entitlements.v1``), imported as a
    consumer imports them: ``Subscribed`` is a ``ClaimFactCheck``."""
    path = tmp_path_factory.mktemp("stub") / "billing_entitlements_v1.py"
    path.write_text(render_stub("billing.entitlements.v1", EntitlementsContract))
    spec = importlib.util.spec_from_file_location("_trust_billing_v1", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


#: ``Subscribed`` on its default and its verbatim path (KB-33f: it answers
#: the view's 401 / 503 as the kernel atom did).
SUBSCRIBED: list[Callable[[Any], Any]] = [
    lambda b: b.Subscribed(of=Account.user()),
    lambda b: b.Subscribed(frozenset({"active"}), of=Account.user()),
]
IDS = ["Subscribed", "Subscribed-statuses"]
KEY = "subscription_status"


def _viewed(reason: str) -> AuthzContext:
    """A context whose ``subscription_status`` the trusted view removed for
    ``reason``; the billing authority ran (its fact registered locally)."""
    subject = UserContext(
        user_id=uuid4(),
        claims_sources={"idp", "plans", "billing"},
        claims_sources_failed=set(),
        **{view.UNTRUSTED_KEY: {KEY: reason}},
    )
    ctx: AuthzContext = _authz(subject)
    core = ctx.core()
    core.get_access_fact = lambda name: (
        SUBSCRIPTION_ACTIVE_FACT if name == SUBSCRIPTION_ACTIVE_FACT.name else None
    )
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("atom", SUBSCRIBED, ids=IDS)
@pytest.mark.parametrize(
    ("reason", "status"), [(view.OUTDATED, 401), (view.MISMATCH, 503)]
)
async def test_subscribed_honours_the_views_indeterminate(
    billing_gen: Any, atom: Callable[[Any], Any], reason: str, status: int
) -> None:
    """401 / 503 from the view, never a 402; its negation never allows."""
    ctx = _viewed(reason)
    decision = await atom(billing_gen).decide(ctx)
    assert decision.indeterminate and decision.http_status == status
    assert decision.code == reason
    negated = await Not(atom(billing_gen)).decide(ctx)
    assert not negated.allowed and negated.indeterminate
    assert (negated.http_status, negated.code) == (status, reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("atom", SUBSCRIBED, ids=IDS)
async def test_subscribed_never_allows_on_a_definite_untrusted_reason(
    billing_gen: Any, atom: Callable[[Any], Any]
) -> None:
    """Unassigned, or its authority absent at the mint: 501, never allowed."""
    for reason in (view.UNASSIGNED, view.SOURCE_ABSENT):
        ctx = _viewed(reason)
        assert _unknown(await atom(billing_gen).decide(ctx)), reason
        assert _unknown(await Not(atom(billing_gen)).decide(ctx)), reason


def test_billing_declares_its_keys_fail_safe(billing_gen: Any) -> None:
    """B-N12: billing's own declaration, not a kernel list, lets its
    non-critical contributor write its four keys, which are protected."""
    core = _core(None)
    with core.module_registration(IDP) as api:
        api.add_claims_contributor(contribute_billing_claims, critical=True)
    with core.module_registration("sandloom_billing") as api:
        api.add_claims_contributor(
            contribute_billing_claims, fail_safe=BILLING_CLAIM_KEYS
        )
    mapping = {"issuer": IDP, **dict.fromkeys(BILLING_CLAIM_KEYS, "sandloom_billing")}
    policy = install_claim_authorities(
        core,
        [_descriptor(IDP, issues_tokens=True), _descriptor("sandloom_billing")],
        claim_authorities=mapping,
        default_claim_authorities=None,
        bindings={},
        log=logging.getLogger("test"),
    )
    assert policy.fail_safe == set(BILLING_CLAIM_KEYS)
    assert set(BILLING_CLAIM_KEYS) <= policy.protected
