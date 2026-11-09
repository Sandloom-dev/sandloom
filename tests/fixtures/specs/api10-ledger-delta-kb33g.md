# Ledger delta: KB-33g, `Subscribed()` and `Subscribed(statuses=...)` agree

KB-33g (2026-11-09, owner decision: `statuses=` changes the accepted
statuses, not the trust rules) gives both reads of billing's generated
`Subscribed` for `Account.user()` one trust rule, in
`ClaimFactCheck` (`sandloom/shared/authz/contract_check.py`). Billing's own
claim proven absent (the key is the fact's claim, assigned to billing in the
verifier's map, billing ran OK at the mint, the token's `cpv` entry is the
digest the verifier accepts for billing, the view removed nothing) is
billing's 402 on both reads; billing failed at the mint is 503
`claim_authority_failed` on both, with zero I/O (the default read no longer
asks billing live); every other absence, or a value that is not a string, is
501 `access_unknown` on both. Billing is unchanged (`subscription_claims()`
is `{}` for a user with no billing customer).

| Id | Disposition |
| --- | --- |
| G-AUTH-12 | Strengthened for billing's generated `Subscribed` (the facts' live escalation unchanged): with billing failed at the mint it is 503 `claim_authority_failed` with zero I/O on both reads, never an allow or a deny. Enforced by `tests/integration/test_claims_provenance_and_subscribed.py::test_subscribed_is_503_when_billing_failed_at_mint` and `src/sandloom/core/tests/test_subscribed_agree.py`. |
| G-AUTHZ-12 | Strengthened: both reads agree on every row (proven absent 402, present outside the statuses 402, failed 503, the view's 401 / 503, unassigned / unproven / wrong type 501); `~Subscribed` allows only on a proven deny; zero I/O for `Account.user()`. Enforced by `src/sandloom/core/tests/test_subscribed_agree.py` and `tests/integration/with_batteries/test_subscribed_no_customer.py` (the real billing contributor, mint and view). |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
