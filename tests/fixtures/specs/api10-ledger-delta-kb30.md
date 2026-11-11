# Ledger delta: KB-30, contract-anchored access checks and one evaluator

KB-30 (2026-11-09, ADR-0014 decisions 8 to 12) adds the provider's
`access_check` declaration on a boolean contract read (`From` binding
sources), the typed atoms `sandloom generate` writes beside a consumer's stub
(`ContractCheck` and `ClaimFactCheck` subclasses carrying the declaration as
a literal) and the one generic evaluator. The kernel atoms stay beside the
generated ones; no caller changes.

| Id | Disposition |
| --- | --- |
| G-AUTHZ-01 | Tests added, statement unchanged: a generated atom keeps the three-valued statuses; an absent provider, a provider whose signature no longer matches the literal and a non-boolean answer are each indeterminate 501 inside the algebra (`Or` allows on a genuine allow beside one, `Not` never allows). Enforced by `tests/laws/test_generated_atoms_laws.py` and `test_contract_check.py`. |
| G-AUTHZ-02 | Tests added, statement unchanged: the generated atoms ask the same provider methods, in the same order, through the same provider units as the kernel atoms, with equal decisions and equal I/O on every stand case, mono and split (decisive shape 3 calls / 9 database round trips mono, 3 RPC / 9 provider round trips / 0 consumer split; `Credits` 1 / 4 through `has_credits_at_least`; the claims tier 0 / 0). Enforced by `tests/integration/with_batteries/test_access_check_counts.py`. |
| G-AUTHZ-05 | Statement unchanged: a third-party provider declares its own access check and its consumers get the typed atom from the generated stub, no kernel change (`test_contract_check.py`, the `seatco.seats.v1` provider on `Account.org(param=)` and `Account.bound_tenant()` with no billing). The live adapters stay until KB-34. |

New public names (advanced, `sandloom.sdk.authz`): `access_check`, `From`,
`ContractCheck`, `ClaimFactCheck`. `AccessFact` gains `atom`, `atom_statuses`,
`atom_claim_keys` and `atom_describe` (defaults keep every existing
declaration); `lookup_access_fact` gains the keyword `owner=`;
`ContractMethodRuntime` gains `access_check`. A declared access check is part
of the contract's describe document and snapshot: removing it, re-binding it,
changing its category, narrowing its values or lowering a risk is
incompatible (SANDLOOM-T004).

No guarantee, enforcing assertion, or residual-risk requirement is removed.
