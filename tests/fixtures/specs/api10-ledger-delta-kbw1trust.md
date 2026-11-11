# KB-W1 fix (trust): the W1 review findings on KB-10 (2026-11-09)

The W1 review of KB-10 found four gaps in the claim-authority guarantees. This change closes
them and records each in the ledger.

## Changed entries

- G-AUTH-13 (changed, strengthened): `email` and `email_verified` are the map's issuer's
  fields. Identity writes them through the mint check (`collect_claims(issuer_claims=...)`),
  never after it, so another module's value is dropped as a violator rather than silently
  overwritten. Without a core (no check) they are not minted. The statement no longer says
  "cannot override". The original enforcing test keeps its name; three tests are added.

## New entries

- G-AUTH-21 (new, enforced): a composition that loads a token issuer with no
  `claim_authorities` map refuses the boot (SANDLOOM-B035, naming
  `SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main` or a map). It no longer runs an app whose
  every mint is refused (503). A map that gives an issuer field to another module, or names
  one without an issuer, refuses the boot. ADR-0014 decision 1; ADR-0012 decision 3 (amended).
- G-AUTH-22 (new, enforced): `Feature` and `WithinLimit` on the user's token are indeterminate
  (503) when the claim's authority failed at the mint. An authority that ran and granted
  nothing denies. ADR-0014 decision 7.
