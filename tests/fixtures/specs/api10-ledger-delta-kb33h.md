# Ledger delta: KB-33h, an absent claim is the fact owner's declared semantics

KB-33h (2026-11-09, owner decision after #158 / #159: provenance proves who
ran, not that absence is a negative fact) adds one declared property to the
public `AccessFact`: `absent_claim="unknown" | "deny"`, default `"unknown"`.
KB-33g had `ClaimFactCheck` (`sandloom/shared/authz/contract_check.py`) read
the primary claim of ANY owner's fact, proven absent, as that owner's deny.
Now only a fact that declares `absent_claim="deny"` does (billing's
`subscription.active`, so billing's 402 is unchanged); every other fact's
proven absence is 501 `access_unknown`, and `Not` over it never allows. The
field travels in the generated atom's literal, the contract's describe
document and the `<owner>.facts.v1` descriptor; a literal or descriptor
without it means `"unknown"`. A provider that withdraws `"deny"` is a
breaking contract change (`compare_contract`).

| Id | Disposition |
| --- | --- |
| G-AUTH-12 | Strengthened: a claim atom's proven absence is a deny only where the fact declares `absent_claim="deny"`, else 501. Enforced by `src/sandloom/core/tests/test_absent_claim_policy.py::test_a_successful_contributor_without_the_claim_is_501_by_default`. |
| G-AUTHZ-12 | Strengthened: the KB-33g proven-absence deny is the owner's declaration (default 501, `"deny"` with the fact's own category, primary claim only, every provenance condition required; old metadata means "unknown"). The superseded "default path unchanged ... asked live" and "still asked live" clauses are removed (ADR review L5). Enforced by `src/sandloom/core/tests/test_absent_claim_policy.py` (a third-party module, no battery) and `tests/integration/with_batteries/test_subscribed_agree.py` (moved from `src/sandloom/core/tests/` so the kernel-without-batteries job collects no billing import; billing's rows unchanged). |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
