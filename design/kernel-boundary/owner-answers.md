# Owner answers to the 31 kernel-boundary questions (2026-11-08)

Not "yes to all". The overall architecture goal is confirmed. Decisions must keep the guarantee ledger, the existing authorization algebra, DX, public capabilities and monolith/split behaviour. kb-research-settings is accepted as the reason not to build a separate two-phase bootstrap: prefer the minimal option O2 (module settings validated before its setup, common checks after loading). Note: not every entry point goes through create_app — static commands, deploy-profile tooling and part of the admin CLI have their own path. The A–D target is decided after alternatives and PoC.

## Requirements
1. Yes, conditionally: I-01…I-23 and H-1…H-19 approved as target requirements after the refinements below. Not an approval of every proposed mechanism or exception.
2. Yes. Tests are outside the shipped kernel boundary. Kernel unit/architecture tests must run without batteries; integration tests may load real modules explicitly.
3. Yes. Same generator version and declaration → byte-identical. On a generator update any diff needs an explanation, a compatibility check and snapshot review.
4. Yes. Expected outcome for all 16 combinations in clean environments. A refusal only for a predefined reason (e.g. a missing required contract), never an undeclared dependency.
5. Amended. Zero extra RPC/DB calls caused by moving an atom: hard. The +1 % instruction budget stays but applies only where measured with sufficient precision. CI must check call counts, absence of I/O and semantics; latency and instructions on a reproducible bench with measured noise. Security fixes are not blocked by the budget.
6. Yes. The only approved backend exception for now is SQLite nested write units. New exceptions only with their own justification and tests.
7. Fix to 15, the actual current value. After a domain field leaves the kernel, recount the ratchet downward; do not keep the old ceiling automatically.
8. Yes, refined: one active application composition per production process. Tests may create several apps sequentially with explicit state cleanup.

## Trust and security
9. Yes. claim_authorities is set in the composition root code, not in ordinary settings. A local owner is verified by the module actually registered, a remote one by the trusted bound RPC provider. A string name is not proof of authority.
10. Wider rule. Always protect roles, permissions, tenant_ids. Any billing claim used to grant access (subscription_status, plan_code, features, limits) must also have an assigned authority. email and email_verified only from the trusted issuer. Do not hard-code billing as an owner in the kernel.
11. No, as worded. No global 401 on every old token: it breaks ordinary authentication and can break Or. On a policy-version mismatch the protected claims are untrusted. If a policy really needs such a claim and no other sufficient allowing branch exists, answer 401 with machine code token_policy_outdated so the client refreshes. Old tokens keep working on routes that need no protected claim until they expire.
12. Yes, limited. An optional contributor that violates ownership loses its whole result; the failure goes to claims_sources_failed, the log and a metric. A required authority is always critical and its failure blocks minting. A violator's partial result is never kept.
13. Yes for security-sensitive claims, keeping supported SDK scenarios. create_access_token does not accept arbitrarily assembled protected claims and provenance; they come from the verified result of collect_claims. System fields are formed and controlled by the kernel. Allowed service/test tokens get an explicitly limited path that cannot grant user rights.
14. Yes. The authority of a protected claim is critical; absent or failing → minting refused.
15. After the PoC. A manifest of declared claim keys helps detect errors at startup but does not replace the runtime ownership check.
16. Responsibility refined. The kernel guarantees reliable access-token revocation and the presence of a durable revocation applier. Ending refresh sessions is implemented by the issuer, which owns those sessions; the kernel checks that this obligation is registered and executed. A missing implementation is never hidden behind a warning.

## Authorization
17. Yes for the comparative PoC. access_check on a contract read method is the main candidate, not the approved final architecture. Compare with consumer-owned Requirement and parameterized AccessFact.
18. Yes with an ADR-0010 amendment. Domain atoms may be imported from the generated consumer API. The public sdk contract and beginner DX get a formally approved migration. The policy expression stays the same.
19. Yes, as a separate change. An unavailable provider becomes indeterminate with reason 503 inside the algebra. A | B allows only if the other branch reliably allowed; ~indeterminate never allows. Do not turn every security exception into an ordinary indeterminate.
20. Not approved: moving past_due into a claim without a freshness check changes behaviour when settings or subscription status change during the token's life. Compare a claim at mint with a descriptor refreshed off the hot path. Keep the needed freshness for money-sensitive operations; write a semantic-equivalence table.
21. No, do not remove automatically. Feature and WithinLimit are public capabilities even without a bundled producer; non-use does not license breaking H-2/I-16. First research moving them to billing with correct parameter passing and split behaviour. Removal only as a separately approved exception with a capability-ledger change.
22. Yes, but a warning is not enough. Archcheck must recognise calls of declared access checks inside hand-written Requirements and check metadata, risk, tier and parameters. Where static proof is impossible: independent route classification and runtime checks. A hand-made atom is not safe just because the analyser did not find it.
23. Yes. Remove the third bound_tenant call as a separate change with its own baseline. Do not weaken membership freshness or tenant isolation to save an RPC.
24. Yes. Billing may provide has_credits_at_least as a read contract. Comparison, account binding, consistency and result must match the current check. It does not replace the atomic credit-spend check.

## Settings
25. Yes, conditionally. Variables of a module absent from the process are not validated and do not interfere. But sandloom check/test must detect errors for the chosen topology and wrong names of active modules.
26. Yes. Each third-party module has its own semantic ratchet; the kernel checks completeness of classification, not a sum of arbitrary third-party settings.
27. Refined. A risk id of an absent module may be inert only for a namespace known in the deployment. An arbitrary unknown prefix must not be accepted silently (a typo in a module or risk name must not bypass the check). For an active module an unknown risk id → refusal.
28. Yes. Namespace risk ids (billing.fake_psp_in_prod) with an explicit one-time environment migration, documentation and no permanent compatibility aliases.
29. Yes. A configuration error of an explicitly selected optional module is fatal. The loader must not swallow ModuleSettingsError like an ordinary optional import failure.
30. Yes. Keep the existing 23 env names. Forbid collisions. Generate prefixed names for new modules and check them with sandloom check.
31. Yes. A slower config dump is acceptable if measured and the command does no activation/lifespan/module I/O. Add --kernel-only; keep secret redaction, provenance and diagnostics of wrong settings.

## Small fixes A and B — approved now, separate PRs
A. Check ownership of AccessFact.owner_module, owner= and the reserved contract families <m>.claims.v1 / <m>.facts.v1 against the module actually registering. Negative tests for name spoofing, registration outside a module context, monolith/split. No hard-coded list of standard batteries.
B. Move identity/billing prod refusals to a common check reached by API, worker, beat, migrations and operational commands. A process matrix asserts the same refusal reason. Static CLIs that need no application composition are not forced to build the app.

## Conditions to continue
1. Record these answers in requirements.md and the related ADR/ledger, naming the changed invariants.
2. Implement A and B as separate small PRs with tests.
3. No mass refactoring of batteries before the alternatives and PoCs for authority, portable atoms and module settings.
4. Do not forget D1 (missing durable revocation applier without identity) and D4 (compatibility check skipped in a consumer-only deployment), the billing-without-celery defect and /ready answering 200 with an unusable taskqueue.
5. For every decision keep: no extra I/O, monolith/split parity, the guarantee ledger and the DX baseline.
