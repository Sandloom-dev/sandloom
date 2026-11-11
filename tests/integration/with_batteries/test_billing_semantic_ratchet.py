"""Billing's one semantic setting is ratcheted by billing's own baseline.

ADR-0015 decision 5 (P8): the semantic setting billing classifies left the
kernel's ratchet (13 -> 12, 15 -> 14) and is ratcheted by billing's own
baseline; its tier is recorded in billing's ``module.snapshot.json``. It needs
``sandloom_billing`` installed, so it runs where the batteries do (the kernel's
own suite runs without them).
"""

from __future__ import annotations

from sandloom.core.config import tiers
from sandloom.core.testing import settings_gate
from sandloom_billing.config import BillingSettings


def test_billing_keeps_its_own_semantic_ratchet() -> None:
    name = "BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS"
    assert name not in {s.name for s in tiers.SEMANTIC}
    assert name not in tiers.OPS
    assert (
        settings_gate.semantic_ratchet_problems(
            BillingSettings, baseline=[name], ceiling=1
        )
        == []
    )
    # a second semantic setting without an ADR, or the first dropped from
    # the baseline, is refused
    assert settings_gate.semantic_ratchet_problems(
        BillingSettings, baseline=[], ceiling=1
    )
