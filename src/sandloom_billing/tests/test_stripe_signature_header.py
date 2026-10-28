"""Stripe-Signature with non-ASCII bytes is a clean rejection (review hand-off).

The ``v1=`` candidates come straight from a request header (latin-1 decoded):
``hmac.compare_digest(str, str)`` raised ``TypeError`` on a non-ASCII one, so
a forged header answered 500 instead of the signature error.
"""

from __future__ import annotations

import time

import pytest

from sandloom_billing.psp import (
    StripeSignatureError,
    sign_stripe_payload,
    verify_stripe_signature,
)


def test_non_ascii_signature_candidate_is_a_mismatch() -> None:
    forged = "\xe9" * 64
    header = f"t={int(time.time())},v1={forged}"
    with pytest.raises(StripeSignatureError, match="mismatch"):
        verify_stripe_signature(b"{}", header, "whsec_test")


def test_valid_signature_still_verifies() -> None:
    header = sign_stripe_payload(b"{}", "whsec_test")
    verify_stripe_signature(b"{}", header, "whsec_test")
