# KB-10: claim authorities, the mint check and the violator rule (2026-11-08)

- G-AUTH-20 (new, enforced): a protected claim is minted only from the
  authority the composition root assigned it
  (`create_app(claim_authorities=...)`, ADR-0014 decisions 1, 2, 7); an
  optional violator loses its whole result (`claims_sources_failed`, ERROR,
  `claims_contributor_violations_total`), a critical one refuses the mint
  naming itself; startup refuses an assignment the composition cannot honour
  (SANDLOOM-B035), incl. a non-critical authority of any claim but the four
  billing claims. Enforced by `src/sandloom/core/tests/test_claims_trust.py`,
  the trust rows of `tests/integration/replaceability/test_trust_rows.py`
  and `tests/integration/with_batteries/test_billing_down_login.py`.

No guarantee weakens. PR #70's rule "any foreign claim key refuses every mint
(503)" had no ledger entry; it is replaced by the violator rule, which still
never mints a token with a claim its authority did not write. New SDK names:
the `claim_authorities=` and `default_claim_authorities=` parameters of
`create_app`. No new setting or risk id of `SANDLOOM_ACCEPTED_RISKS`.
