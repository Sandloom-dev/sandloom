"""The stands' tokens come from the real mint (``benchsuite.claims_mint``).

A bare HS256 JWT carries no ``cpv`` and no provenance, so the verifier does not
trust its protected claims (401 ``token_policy_outdated``, 402 on a
``Subscribed`` route). The stand's token is the sealed mint's: the
``bench`` module is the authority of ``tenant_ids`` and
``subscription_status`` and stamps its digest and its name.
"""

from __future__ import annotations

import jwt

from benchsuite import common, s5
from benchsuite.bench.claims import AUTHORITY, CLAIM_AUTHORITIES

A, _B = common.S5_TENANTS


def _payload(token: str) -> dict[str, object]:
    return jwt.decode(token, options={"verify_signature": False})


def test_bench_token_is_a_sealed_mint() -> None:
    claims = _payload(s5.mint(A))
    assert claims["token_type"] == "access"
    assert claims["tenant_ids"] == [A]
    assert claims["subscription_status"] == "active"
    assert claims["claims_sources"] == ["bench"]
    assert claims["claims_sources_failed"] == []
    digests = claims["cpv"]["k"]  # type: ignore[index]
    assert set(CLAIM_AUTHORITIES) <= set(digests)
    assert AUTHORITY == CLAIM_AUTHORITIES["tenant_ids"]


def test_no_subscription_leaves_the_claim_out_but_stamps_its_authority() -> None:
    claims = _payload(s5.mint(A, subscription=None))
    assert "subscription_status" not in claims
    assert "subscription_status" in claims["cpv"]["k"]  # type: ignore[index]


def test_the_token_verifies_with_the_stand_secret_and_lifetime() -> None:
    token = s5.mint(A, hours=2)
    claims = jwt.decode(token, common.jwt_secret(), algorithms=["HS256"])
    assert 1.9 * 3600 < claims["exp"] - claims["iat"] <= 2 * 3600
