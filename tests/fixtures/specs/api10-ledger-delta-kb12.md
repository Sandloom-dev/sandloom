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

## Mint instruction budget exception (2026-11-11)

The owner accepts the measured instruction increase for KB-12 after a paired
recheck. The +1% instruction budget applies to subsequent changes. The
optimization follow-up is `kb-mint-budget-optimize` in tm.

The recheck used the original three-owner in-process mint workload from
[PR #117](https://github.com/Sandloom-dev/sandloom/pull/117), baseline
`ee26108` and final candidate `7c56345`. It used the same Cachegrind image
`sandloom-ladder-cg:3.13` (`c3ed3beb1946`, Linux arm64), `PYTHONHASHSEED=0`
and `Ir/op = (Ir(200) - Ir(0)) / 200`. Each pair ran the baseline followed
by the candidate with container networking disabled.

| Pair | Baseline Ir/op | Candidate Ir/op | Increase |
| --- | ---: | ---: | ---: |
| 1 | 1,347,403 | 1,364,535 | +1.271% |
| 2 | 1,347,368 | 1,364,545 | +1.275% |
| 3 | 1,347,265 | 1,364,322 | +1.266% |

Absolute wall-clock latency below is the median of the three run percentiles.
Each run measured 4,000 mints after 200 warm-ups on the same image with
Cachegrind disabled. The latency covers the isolated mint workload.

| Build | p50 (µs) | p95 (µs) | p99 (µs) |
| --- | ---: | ---: | ---: |
| Baseline | 158.21 | 234.37 | 443.98 |
| Candidate | 164.33 | 257.10 | 467.15 |

The recheck workload uses an in-memory database stub. The prior real-app
trace on these revisions recorded identical SQL and Redis operations
on both sides across three runs. Login used 5 SQL and 2 Redis calls.
Refresh used 6 SQL and 1 Redis call. The trace covered monolith operation.
Diff review found that issuer resolution, validation of precomputed claims,
reserved-key checks and the seal add local CPU work. The contributor dispatch
and transport calls follow the existing path. [PR #117 measurement report](https://github.com/Sandloom-dev/sandloom/pull/117)
