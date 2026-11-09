# Ledger delta: KB-12, the sealed mint, service tokens and test tokens

KB-12 (2026-11-09) builds ADR-0014 decision 5 (owner answer #13, adversarial
C-a2): protected claims, provenance and `cpv` reach a token only through the
sealed `collect_claims` result; the issuer is resolved from its registered
contributor; a service token grants no user rights; the kernel test authority
exists only in a test composition.

| Id | Disposition |
| --- | --- |
| G-AUTH-25 | New, enforced: `create_access_token(data, claims=sealed)` refuses protected claims, tenant selectors, provenance, `cpv` and `UserContext`'s structural fields in `data`, an unsealed or spread result and a `data` key overriding a sealed claim (SANDLOOM-R017); `collect_claims(issuer=<contributor>)` resolves the issuer from the registering module, never a name, and `precomputed` only with the issuer's session, for its own contributors and assigned keys; `create_service_token` refuses the same keys and every gate denies it (403); `as_user`, `mint_test_token` and `sealed_claims` mint only in a test composition, a stated `claims_sources` checked. Enforced by `src/sandloom/core/tests/test_sealed_mint.py`. Gap (S): mistakes only (T4); kernel code can still seal a dict, and a module holding the core runtime can still mint a real user's rights (impersonation, C-a6), outside the threat model. |

No guarantee, enforcing assertion, or residual-risk requirement is removed. New
SDK names: `create_service_token` (`sandloom.sdk.security`), `mint_test_token`
and `sealed_claims` (`sandloom.sdk.testing`); `create_access_token` gains
`claims=`; `collect_claims(issuer=...)` takes a contributor, not a name. No new
setting or risk id of `SANDLOOM_ACCEPTED_RISKS`.
