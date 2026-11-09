# W2+W3 review fixes: the ledger catches up with KB-13, KB-22, KB-41, KB-51, KB-53, KB-56 (2026-11-09)

The reviews of waves 2 and 3 found that these tasks built behaviour and ADR sections without
ledger entries, so the ADR sections stayed `planned`. This delta adds the entries and maps the
built sections. No statement of an existing entry changes and no test is removed.

## New entries

- G-CONTRACT-24 (KB-13, ADR-0014 decision 6): a contract that decides access is registered
  in-process only by the module its first segment names or the one the composition root assigns,
  checked by import path (SANDLOOM-B034); remotely `SERVICE_BINDINGS` pinned to the provider's
  `service_id` is the assignment.
- G-CONTRACT-25 (KB-22, ADR-0013 decisions 1 and 9): generated consumer artifacts live in the
  consuming distribution, the kernel ships none (`sandloom/shared/membership.py` is gone), and
  `_generated/` is reserved (SANDLOOM-C032).
- C-CONSOLE-05 (KB-41): the admin console serves in front of the composition's assigned token
  issuer and its declared login entry, never the `iss` of a token.
- G-RISK-15 (KB-51, ADR-0015 decision 4): module risks are `<module>.<name>`, checked against the
  declaring module, inert where the module is known but not loaded, refused otherwise.
- G-CONFIG-13 (KB-56, ADR-0015 decisions 6 and 7): the static settings gate of `sandloom check`
  and `sandloom test` (collisions, prefix rule with snapshot grandfathering, recorded tiers,
  per-service names and literal values).
- G-CONFIG-14 (KB-53, ADR-0015 decision 9): the refresh-session lifetimes are identity's declared
  settings; the access-JWT parameters stay in the kernel.
- C-TEST-17 (KB-56, ADR-0015 decision 5): the per-module semantic ratchet helper.
- G-AUTH-26 (kb-rf-trust, ADR-0014 amendment "W2+W3 review fixes"): `collect_claims` takes
  precomputed and issuer claims only from the claim authorities' issuer module, and a token
  without `cpv` gives no rights after the compatibility window.

## ADR section mapping

- ADR-0013 decisions 1, 9 and 10 map to G-CONTRACT-25 (decision 10 also to C-GEN-03, whose
  change KB-22 already recorded in `api10-ledger-delta-kb22.md`).
- ADR-0014 decision 6 maps to G-CONTRACT-24.
- ADR-0015 decisions 4, 5, 6, 7, 9 and 11 map to the entries above, G-CONFIG-02, G-CONFIG-12,
  G-BILLING-18, G-AUTH-19 and C-CONSOLE-02. Decision 11's kernel-tools row (B-C7 to B-C9, B-N8)
  closes with KB-55 and the console link target (B-C10) with KB-60; their entries come with those
  tasks.

No new SDK name, setting, risk kind or dedupe mechanism.
