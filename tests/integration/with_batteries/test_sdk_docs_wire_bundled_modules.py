"""``sandloom docs`` prints the bundled modules' contract stubs and event schemas.

The wire listing reads the installed providers' ``module.snapshot.json``
(ledger C-SDK-04); these cases name billing, tenancy and identity, so they run
where the batteries are installed. The kernel's own cases (an unknown name,
an application's shared package, no stub or schema in the kernel) stay in
``tests/sdk/test_sdk_docs_wire.py``, which runs without them.
"""

from __future__ import annotations

import pytest

from sandloom.sdk._prim.docs import docs_main
from sandloom.sdk._prim.docs_wire import contract_stubs, event_schemas


def _out(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    assert docs_main(list(argv)) == 0, argv
    return capsys.readouterr().out


def test_a_contract_prints_each_method_with_its_semantics(
    capsys: pytest.CaptureFixture[str],
) -> None:
    by_name = _out(capsys, "billing.credits.v1")
    assert _out(capsys, "BILLING_CREDITS_V1") == by_name
    assert (
        "from <your_module>._generated.contracts.billing_credits_v1 "
        "import BILLING_CREDITS_V1" in by_name
    )
    assert "provider: sandloom_billing" in by_name
    assert (
        "await client.spend(tenant_type: str, tenant_id: UUID, amount: int" in by_name
    )
    for semantics in ("operation_key", "402", "409", "amount=0", "422", "403"):
        assert semantics in by_name, semantics


def test_an_event_prints_its_fields_and_the_producers_notes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    text = _out(capsys, "tenancy.member_added.v2")
    assert "tenant_id" in text and "version" in text
    assert "also on tenancy.member_added.v1" in text
    assert "tenant_from='tenant_id'" in text and "version_field='version'" in text
    assert 'class MemberAdded(Event, channel="tenancy.member_added.v2")' in text
    unversioned = _out(capsys, "billing.subscription.created.v1")
    assert "not a projection source" in unversioned


def test_the_listings_name_every_stub_and_schema(
    capsys: pytest.CaptureFixture[str],
) -> None:
    contracts = _out(capsys, "contracts")
    events = _out(capsys, "events")
    for name in ("billing.credits.v1", "tenancy.membership.v1", "identity.contact.v1"):
        assert name in contracts
    assert "tenancy.member_removed.v2" in events
    assert set(contract_stubs()) >= {"billing.entitlements.v1", "identity.security.v1"}
    assert "billing.credits.spent.v1" in event_schemas()
