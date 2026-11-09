# Kernel boundary: migration plan

tm `kb-adrs` (parent `kb-assess`), 2026-11-08; accepted with ADR-0012..0015 on 2026-11-08 (tm
`kb-adrs-accept`), its PRs tracked in tm under `kb-migrate`, one task per PR, alias `kb-NN-...`. Splits ADR-0012..0015 and the ADR-0010
amendment into small PRs, each green on its own (`sandloom test --full`, split conformance, the
CI steps of the areas it touches), in dependency order. Edge ids are rows of
[edges.yml](edges.yml); invariants are [requirements.md](requirements.md) ids; owner decisions
are [owner-decisions-adr.md](owner-decisions-adr.md) "#n" and [owner-answers.md](owner-answers.md)
"answer #n".

Already on `main`, not repeated here: PR #70 (`e264b09`, second writer and reserved keys), PR #71
(`816f587`, module production refusals in every entry point, identity's `JWT_SECRET` included),
PR #72 (`206fa2d`, registration ownership, SANDLOOM-B034), PR #73 (`dfde67a`, identity declares
`argon2-cffi`), PR #74 (`bf77bf9`, one secret rule for logs and `config dump`). The
`arch/kernel-boundary` branch merged `main` (PRs #71-#74) at `c9b9b05` (tm `kb-adrs-revise`):
the baseline every PR below measures against.

## Rules every PR follows

- **No extra I/O.** A PR adds no network call, database session or query, Redis command or RPC on
  any request path, and no I/O at import (G-SDK-01). Where a PR touches a request path it proves
  it with exact counts (database round trips per role, Redis commands, RPC requests per request,
  first and steady requests), base against candidate, mono and split. A PR that removes a call
  (KB-37) proves the decisions and freshness are unchanged.
- **Before/after measurement**, reported in the PR description, from the stands the PoCs used:

  | id | measurement | stand |
  |---|---|---|
  | M-calls | exact counts per policy or route, mono and split | `poc/authz/stand.py` (promoted to CI by KB-30), `poc/trust/bench/trust_stand.py` |
  | M-ir | cachegrind Ir per operation on the request, mint or decisive rung; budget +1 % where the bench resolves it (answer #5); a security fix is never blocked | `benchmarks/ladder`, `poc/authz/cg_poc.sh`, `poc/trust/bench/cg_micro.sh` |
  | M-lat | p50 / p95 / p99, base and candidate alternating in one session; gating only under the threshold procedure below | `poc/authz/run_perf.sh`, `poc/trust/bench/trust_stand.py run` |
  | M-boot | `create_app` and CLI wall time, 10 alternating runs, median against base-vs-base noise | new in KB-50 |
  | M-wheel | built wheels: file lists and digests, import closure | `tools/packaging/check_package_distributions.py` |
  | M-gen | `sandloom generate` output digest under hash seeds and orders | `poc/contracts/scripts/p5_byte_identical.sh` |

- **Latency threshold (I-14 part 4, G13).** No latency number is set below the bench's measured
  noise. The threshold is set by this procedure, and until it has run, M-lat is reported in the PR
  and never gates (call counts and the +1 % instruction budget gate):

  1. **Bench.** One dedicated machine (no other load, fixed CPU frequency, the stand's process
     pinned to its cores), the PoC stands as scripts, fixed commits; the bench id, Python and
     dependency versions are recorded with every result.
  2. **Noise.** Base against base: at least 10 alternating rounds per rung (request on the claims
     tier, local fact, remote fact, mint, the decisive authorization shape; mono and split), each
     round warm-up then a fixed request count. The noise band of a percentile (p50, p95, p99) is
     the largest paired round-to-round difference of base against itself.
  3. **Threshold.** The owner sets, per rung and percentile, a threshold at or above its noise band
     and records it with the band, the bench id and the SHA in `perf-dx-baseline.md`. A threshold
     below the band is refused: the bench cannot tell it from noise (perf-dx §2.4 measured p50
     moving up to 40 % on the shared machine, so no threshold is set from that machine).
  4. **Use.** A candidate fails M-lat when the median of its paired differences against base over
     the same number of alternating rounds exceeds the threshold on any rung and percentile; the
     owner may approve it with a reason; a security fix is never blocked (H-10 d).
  5. **Re-measure** the band when the bench, Python or a dependency on the measured path changes,
     and before the first PR that the threshold would gate.

- **Edge register.** KB-03 seeds `tests/architecture/kernel_edges.yml` with every row at
  `current_status: open` and its `target` from [edges.yml](edges.yml). From then on, a PR that
  carries a row to its target sets its `current_status` (`fixed`, or `retained` with the ADR id)
  in the same PR (the check fails otherwise) and prints `--report` in its description. `target`
  is the plan and never makes the check pass or fail.
- **Public surface.** Any change to `public_api_baseline.json` or the beginner tier is reviewed
  under I-17 with upgrade steps; no alias (H-9).
- **Ledger.** A changed guarantee statement is a reviewed `guarantees.yml` change; none weakens
  (I-16, H-2).

## Waves and parallel groups

PRs in one wave can run in parallel (separate claim areas). A wave starts when the PRs it
depends on are merged, not when the whole previous wave is.

Eight waves, W0 to W7: 35 PRs and one PoC gate (KB-49, not merged into `main`), plus the two
PRs of the later CLI phase.

| wave | PRs (parallel) | depends on |
|---|---|---|
| W0 | KB-01, KB-02, KB-03, KB-05, KB-06, KB-07, KB-08, KB-09; KB-49 (settings PoC gate) | `main` merged into the branch |
| W1 | KB-04, KB-10, KB-20, KB-40, KB-50 | KB-01 → KB-04; KB-08 → KB-10; KB-03 → KB-20, KB-40; KB-49 → KB-50 |
| W2 | KB-11, KB-13, KB-21, KB-41, KB-51, KB-53, KB-54, KB-56 | KB-10 → KB-11, KB-13, KB-41; KB-20 → KB-21; KB-50 → KB-51, KB-53, KB-54, KB-56 |
| W3 | KB-12, KB-22, KB-52 | KB-11 → KB-12; KB-21 → KB-22; KB-51 → KB-52 |
| W4 | KB-23, KB-30, KB-55 | KB-22 → KB-23; KB-22, KB-13 → KB-30; KB-52, KB-53, KB-54 → KB-55 |
| W5 | KB-31, KB-32, KB-33, KB-35, KB-36 | KB-30 (and KB-11 for KB-31) |
| W6 | KB-34, KB-37 | KB-31, KB-32, KB-33 → KB-34; KB-31 → KB-37 |
| W7 | KB-38 (optional); CLI phase KB-60, KB-61 | KB-34; ADR-0012..0015 implemented |

Every settings PR, KB-50 to KB-56, is blocked by KB-49: none starts until all 16 settings
scenarios (P1-P16) have a recorded result there, whatever the wave table's direct edges say.

Conflicts to watch inside a wave: KB-53 and KB-54 (W2) and KB-52 (W3) each delete fields from
`core/config/modules.py` (rebase, no shared logic); KB-10 and KB-09 both touch issuer handling in
`claim_trust_setup` (KB-09 lands the obligation check, KB-10 adds the map's issuer cross-check).

## Pre-implementation items (owner, "Before implementation")

| item | where |
|---|---|
| PoC SHAs, results and measurements recorded in the ADRs | done: ADR-0012..0015 "Evidence"; [validation.md](validation.md) §2 |
| PA-2 limit 1: contract incompatibility → indeterminate 501, not 422 | KB-30 (ADR-0014 decision 12) |
| PA-2 limit 2: the old domain atoms removed from the kernel | KB-34 (ADR-0014 decisions 14, 15) |
| atom parameters kept (`statuses=`, `claim_key=`, `Feature`/`WithinLimit` parameters) | KB-30, KB-33 |
| a real third-party provider capability in place of the live adapters | KB-13, then KB-34's G-AUTHZ-05 equivalence |
| billing without celery | KB-01 |
| every open silent degradation: D1 | KB-09 |
| D2 (`Subscribed` trusts any writer without billing) | KB-11 (view), KB-30 (501 without provider) |
| D3 (`Role`/`Perm` trust any writer without identity) | KB-10, KB-11 |
| D4 (consumer-only install skips the compatibility check) | KB-21 |
| `/ready` 200 with an unusable taskqueue | KB-02 |
| the `sandloom-standard` package | KB-08 |
| the register of 46 judged edges, plus row K1 (KB-08) | [edges.yml](edges.yml); enforced from KB-03 |

## The PRs

### KB-01 billing boots without celery (tm `billing-boots-without-celery`)

- **Scope:** `sdk/_prim/tasks.py` imports celery lazily (at decoration's enqueue path, not at
  import); a module whose `tasks.py` uses the typed `shared_task` loads on plain `sandloom`;
  `import sandloom.sdk.tasks` works without celery and raises a named error at enqueue. Identity
  checked the same way.
- **Edges:** none (problem #9). **Invariants:** I-19, I-20.
- **Tests:** `sandloom` + `sandloom-billing` wheels in a clean venv without celery: boot,
  `/health`, `/ready`; enqueue without celery refuses with the named error.
- **No extra I/O:** import-time only. **Measure:** M-wheel (import closure), M-boot.
- **Must not touch:** the celery driver, worker and beat behaviour when celery is installed.

### KB-02 `/ready` is not green with an unusable taskqueue

- **Scope:** when the configured taskqueue cannot open and a loaded module registers tasks or
  periodic tasks, `/ready` fails naming the capability; with no task user, readiness is unchanged.
- **Edges:** none (guarantee-map §11 list). **Invariants:** I-20.
- **Tests:** readiness matrix: celery absent × module with tasks / without tasks.
- **No extra I/O:** the readiness probe already opens the taskqueue. **Measure:** none beyond
  the readiness probe's duration.
- **Must not touch:** `/health` (never fails on a dependency).

### KB-03 edge register and kernel-edge check

- **Scope:** `tests/architecture/kernel_edges.yml` seeded from [edges.yml](edges.yml): every one
  of the 46 rows with `current_status: open` (its finding exists today) and its `target`
  (`fixed`, `retained` or `deferred`); the 18 class (a) rows and the family templates join with
  `current_status: retained`, reason "protocol". The AST scan, dynamic-import origin rule and
  wheel class (c) scan; SANDLOOM-T017 with `sandloom explain`; `--report`; absorbs
  `kernel_module_imports_allowlist.json`; replaces `test_module_names.py`'s literal scope.
- **T017 rules** (by `current_status`, never by `target`; ADR-0012 decision 2). Runtime rows
  (`kind: runtime`, the default) are strict: a finding that no runtime row's anchors match
  fails, naming the path, the pattern and the class it would have; a runtime row with
  `current_status: fixed` and a finding fails (regression); a runtime row with
  `current_status: open` or `retained` and no finding fails ("set `current_status: fixed`").
  Documentation rows (`kind: documentation`: a name only in a docstring or comment, which the
  scan excludes) are tracked separately and need no runtime finding: each must be `retained`
  with an ADR id and a reason, its anchor file must exist and contain the literal (else stale),
  and it never covers a runtime finding. A `retained` row without an ADR id and a reason fails.
  Since the W0 review fixes (tm `kb-w0-fix-checks`) the scan also reads static imports, and a
  `fixed` row's anchor literal is its regression in any kernel file; a `fixed` row needs a literal
  anchor.
  `--report` prints counts by kind, class, type, scope, current status and target, and the rows
  the change opens, fixes or retains.
- **Edges:** records the 45 runtime rows at `current_status: open`; B-N20, the one
  documentation row, seeded `retained` (ADR-0012, reason "docstring example").
  **Invariants:** I-02, I-03.
- **Tests:** mutation tests: a planted literal, a computed import, a contract literal, a module
  file in the wheel each fail; a `current_status: fixed` runtime row with a finding fails; an
  `open` runtime row without one fails; a documentation row passes without a finding, fails when
  its anchor no longer holds the literal, and does not cover the same literal planted as a
  runtime string; the seeded register passes on the commit it lands on.
- **No extra I/O:** static check. **Measure:** check wall time.
- **Must not touch:** runtime code.

### KB-04 sixteen module subsets in clean venvs

- **Scope:** the 16 subsets of identity, tenancy, billing, admin × install profiles P2 (published
  wheels) and P3 (repository) boot: `create_app`, `/health`, `/ready`, `sandloom --help`; an
  allowed refusal only for a predefined reason (answer #4).
- **Edges:** none. **Invariants:** I-19, A-B7. **Depends:** KB-01.
- **Tests:** the matrix itself (extends `core/tests/test_module_subset.py`).
- **No extra I/O:** test only. **Measure:** matrix duration in CI.
- **Must not touch:** module code (a failing subset opens its own PR).

### KB-05 kernel tests without batteries; replaceability fixtures as integration tests

- **Scope:** kernel unit and architecture tests run in a venv with no module wheel (195 ids fail
  at `ceb3d66`: move battery-dependent tests to integration or module suites); the
  `poc/replace` fixtures (`test_identity`, `grants`, `inventory`, `forged`) become integration
  tests, mono and split.
- **Edges:** none. **Invariants:** answer #2, I-13, I-22. Can be split per failing area.
- **Tests:** the no-battery job in CI.
- **No extra I/O:** test only. **Measure:** suite duration.
- **Must not touch:** assertions of moved tests (moved, not weakened).

### KB-06 B-I1 without a command move

- **Scope:** delete `core/cli/password_check.py`'s import of `sandloom_identity.password_policy`
  and its fallback 8/100 policy; `sandloom admin create` relies on the provider's
  `password_problem` answer over `identity.security.v1` (already called).
- **Edges:** B-I1 fixed. **Invariants:** I-02, I-20 (no silent fallback policy).
- **Tests:** weak password refused by the provider's answer; allowlist entry deleted.
- **No extra I/O:** the contract call exists today. **Measure:** none.
- **Must not touch:** command names, B-R4 (deferred).

### KB-07 B-N19 migration lint entry to identity

- **Scope:** the `external_identities.ix_external_identities_provider` ratchet entry moves to
  identity's tests (or identity drops the index).
- **Edges:** B-N19 fixed. **Invariants:** I-02. **Tests:** migration lint, both suites.
- **No extra I/O / measure:** none. **Must not touch:** other lint entries.

### KB-08 the `sandloom-standard` distribution

- **Scope:** `packages/sandloom-standard` with `sandloom_standard.main` (today's
  `default_modules()` composition); `sandloom[standard]` depends on it; Dockerfile, shipped compose
  profiles, deploy templates, worker, beat, `sandloom-migrate` and composed CLIs set
  `SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main`; T014 asserts one root per profile;
  `sandloom.main` stays the lean kernel root; `public-source.json`, publishing docs, upgrade step.
- **Edges:** adds register row K1, `retained` (ADR-0012 decision 3): the framework's `sandloom add
  deploy` names `sandloom_standard.main` (prepares X1). **Invariants:** I-01, I-19, answer #B,
  owner #1.
- **Tests:** process matrix (G-CONFIG-07 harness): API, worker, beat, `migrate`, composed CLIs of
  the shipped image build the same composition; lean `sandloom.main` boots with
  `SANDLOOM_MODULES=[]`.
- **No extra I/O:** same composition. **Measure:** M-wheel (six wheels), M-boot.
- **Must not touch:** module code; the trust map (KB-10).

### KB-09 kernel revocation applier and issuer obligation

- **Scope:** the kernel registers the durable `core.auth.tokens_revoked.v1` consumer, group
  `sandloom:auth@tokens_revoked@<SANDLOOM_SERVICE_NAME>`, in every composition with an event bus
  that can publish the channel; no database-identity fallback: a composition that needs the
  applier without `SANDLOOM_SERVICE_NAME` refuses to start in every entry point
  (`INVALID_CONFIGURATION`), and T014 refuses two services of one profile with the same id;
  identity's `sessions@tokens_revoked` is removed; `/ready` fails when the applier is needed and
  missing, when the oldest pending revocation or block event is older than the access-token
  lifetime, when an event is dead-lettered, or when the consumer is not running (a younger
  backlog is transient: ready, exported, WARNING above half the bound); an `issues_tokens=True`
  module without a durable `core.auth.user_blocked.v1` consumer, or without an explicit
  `refresh_sessions=False` declaration that startup verifies against its refresh routes, session
  models and consumers, refuses startup (SANDLOOM-B035 family).
- **Edges:** none of the 46; closes D1, T8. **Invariants:** I-20, I-21, owner #11, answer #16.
- **Tests:** lean `/ready` 200 without a bus; 3 replicas of one service share the group; two
  deployments on one bus each receive every event; two services, one database, one bus: both
  apply every revocation, a missing `SANDLOOM_SERVICE_NAME` refuses startup in every entry point,
  equal names refused by T014; restart and replay idempotent; channel version change; cache
  failure during apply → redelivered → applied; `/ready` criteria (transient backlog ready, aged
  backlog, dead letter and stopped consumer not ready, recovery ready); a stateless issuer's
  contradicted declaration refused; split conformance
  `test_revocation_across_services_survives_a_failing_mark_write` without identity's applier.
- **No extra I/O:** no request-path change; one consumer per deployment replaces identity's.
  **Measure:** consumer lag under the split conformance load; Redis commands per revocation equal.
- **Must not touch:** `revoke_user_tokens` semantics, mark format, G-REVOKE-07/08 statements.

### KB-10 claim authorities, mint check, violator rule

- **Scope:** `create_app(claim_authorities=...)` with `issuer`, verified local and remote
  authorities, startup refusals (SANDLOOM-B035); the mint check in the merge pass; the violator
  rule (whole result dropped, `claims_sources_failed`, ERROR, `claims_contributor_violations_total`;
  critical violator refuses naming itself); billing option (c): a non-critical authority accepted
  only for keys whose readers escalate; `issues_tokens` cross-checked with the map's issuer; the
  standard map in `sandloom_standard.main` filtered to the composition; `sandloom new` writes the
  app's map; PR #70's 52 "any foreign key → 503" tests rewritten to the violator rule.
- **Edges:** B-S1, B-S2 start (closed by KB-11). Closes T1-T3, T6; validation A3, B6 at mint;
  problem #14. **Invariants:** I-08, I-22, answers #9, #10, #12, #14, owner #3.
- **Tests:** trust rows T-1..T-8 on the replaceability fixtures, mono and split; billing down →
  login 200.
- **No extra I/O:** the check runs in the existing merge loop. **Measure:** M-ir mint rung (PoC:
  +0.68 %), M-calls mint (no extra call), M-lat mint.
- **Must not touch:** the read side (KB-11), `create_access_token` signature (KB-12).

### KB-11 trusted view, one platform admission, per-key `cpv`

- **Scope:** the view computed once per verified token in `decode_token`, cached with it; every
  reader through the view; one `platform_admission()` for `require_platform_admin` and
  `platform_operator`; per-key `cpv` stamped and checked; the view's one condition (one authority,
  current or `previous=`, matching `cpv` digest and kept source); `previous=` declared with the
  authority's verified identity; the staged initial enablement with `legacy_tokens` (an
  accepted temporary security risk, G-RISK-14: `until` is the transition end, the window ends at
  `until + ACCESS_TOKEN_EXPIRE_MINUTES + JWT_LEEWAY_SECONDS`, no default and no extension
  without a separate operator decision; shown in diagnostics, counted, mode parity in T014); 401
  `token_policy_outdated` inside the algebra and at `TenantScope` / platform gates only where a
  refresh fixes it, 503 `claims_policy_mismatch` otherwise; status order of indeterminates; B-N16
  comment in kernel terms; the adoption release gate in T014.
- **Edges:** B-S1, B-S2 fixed; B-S3, B-N16 retained. Closes D3, validation B6 read side, gate
  probe G2-G4. **Invariants:** I-09, I-10, H-5, answers #10, #11, owner #10.
- **Tests:** T-9 (incl. a real token from the previous release), T-10, T-13, T-14; the issuer ×
  verifier matrix (key change with and without `previous=`; rollback) in split conformance; the
  identity → acme_identity key change of ADR-0014 decision 4 (a real previous-release token,
  steps 0-3, rollback a-c, the negative rows), mono and split; the initial enablement stages
  0-3 and rollback, mono with mixed replicas and split; the window-end test of ADR-0014
  decision 4 (tokens without `cpv` carrying forged `roles` / `tenant_ids` give no rights at or
  after the window end, nor with `iat` at or after `until`), which moves G-RISK-14 from gap to
  enforced; at most one refresh per client; owner regression set; algebra laws over the 401
  status.
- **No extra I/O:** T-14: `Role`, `Perm`, `TenantScope` DB 0, Redis 1 `GET`, RPC 0, mono and split.
  **Measure:** M-ir micro and request rungs (PoC whole-map `cpv` +0.57 %; per-key re-measured
  against +1 %), M-lat.
- **Must not touch:** mint-side rules of KB-10; token TTLs.

### KB-12 sealed mint, service and test tokens

- **Scope:** `collect_claims` returns a sealed result; `create_access_token(data, claims=sealed)`
  refuses protected keys, provenance, `cpv` (SANDLOOM-R017); issuer resolved from the calling
  contributor's module; `precomputed` limited to the issuer's keys; `create_service_token`;
  kernel test authority only with the testing plugin active; `as_user`, `mint_test_token`,
  `sealed_claims`; about 40 hand-minting call sites in 15 files migrated; public API baseline diff.
- **Edges:** none of the 46; closes T4 for mistakes. **Invariants:** I-08 minting, answer #13.
- **Tests:** T-11, T-12; a test stating `claims_sources` gets that provenance checked.
- **No extra I/O:** mint path unchanged in calls. **Measure:** M-ir mint rung.
- **Must not touch:** the trust map, view rules.

### KB-13 ownership of access-bearing contract names; provider assignment

- **Scope:** B034 widened: a contract that carries an `access_check` is registered in-process only
  by the module of its first segment or the one `create_app(access_providers=...)` assigns;
  `SERVICE_BINDINGS` is the remote assignment.
- **Classification before KB-30, with no domain constant in the kernel.** KB-13 lands before
  `access_check` exists. A contract name is access-bearing when, in the composition being built:
  (a) it is a key of `create_app(access_providers=...)`; or (b) an adapter in the live-adapter
  table names it as its `service`, read generically from whatever adapters the composition holds
  (the two default adapters' names are the existing B-R1 row, not a new constant, and leave with
  KB-34); or, from KB-30 on, (c) a consumer's generated copy carries an `access_check` literal for
  it. All three are the consumer's side, so a provider cannot declassify its contract by omitting
  a declaration. KB-13 adds no contract, module or method literal to kernel code: the kernel-edge
  check (KB-03) fails if it does.
- **Edges:** A19 start. **Invariants:** I-22 (T5), I-03, owner #2.
- **Tests:** `acme_orgs` serving `tenancy.membership.v1` refused unassigned, accepted assigned,
  mono and split; the assignment checked by real identity (owner confirmation X3): an app-local
  module named `acme_orgs`, or one named `tenancy` from another import path, refused; remotely
  only the pinned `service_id`; a custom adapter for `acme.seats.v1` makes that name access-bearing with no
  kernel edit; with every adapter reset, an unlisted contract keeps the first-registrant rule;
  T017 finds no new literal.
- **No extra I/O:** registration time only. **Measure:** none.
- **Must not touch:** non-access contract names (first-registrant rule kept).

### KB-20 generator: module-local copies from `consumes`

- **Scope:** the pre-composition planner (AST of declarations); `consumes` as the single source
  incl. methods, arguments, DTO shapes, `extra`, codec; module-local `_generated/contracts`;
  expectation files narrowed to `consumes`; provenance header with body digest; final-layout DTO
  rendering without the project formatter; stem collisions; provider-absent refusal with its own
  code (SANDLOOM-T018); every writer (`generate`, `add contract`, `add module`, deploy scaffold);
  apps switch to the union of `consumes` only where every call is covered by a static or stated
  method list (an unresolved dynamic call is refused with a fix naming `consumes=`). Bundled
  artifacts stay in `sandloom.shared` until KB-22.
- **Edges:** C1, C2, C4 start. **Invariants:** I-12, I-15, owner #9, answer #3.
- **Tests:** byte-identical generation under seeds, orders, ruff absent, `line-length=60`;
  closed-DTO breaking rule; provider absent (keep / edited / missing); collisions; `--check`
  planned set equals composed `consumes`.
- **No extra I/O:** generate time only. **Measure:** M-gen digests; `generate` wall time.
- **Must not touch:** runtime compat (KB-21); the kernel wheel content (KB-22).

### KB-21 runtime expectations; D4 closed

- **Scope:** lookup beside the consumer's stub first; narrowing at startup and `/ready`; no
  expectation → `strict` refusal / `contract_unverified`; kernel families `<m>.claims.v1`,
  `<m>.facts.v1` compared against the kernel schema; hand-written `ContractRef` without a stub
  refused for a required remote contract; three split-readiness fixtures gain expectations.
- **Edges:** none directly; closes D4. **Invariants:** I-12, I-20, G-COMPAT-04, G-COMPAT-06.
- **Tests:** the pair harness (`poc/contracts/scripts/run_pair.sh`) N/N+1 both orders,
  consumer-only, `strict` and `ready`; a remote `grants.claims.v1` under `strict`.
- **No extra I/O:** the boot probe is today's; no request-path change. **Measure:** boot probe
  duration.
- **Must not touch:** generation (KB-20).

### KB-22 bundled artifacts leave the kernel

- **Scope:** delete `sandloom/shared/contracts/` (C1, C2), `shared/events/*.json` (C3),
  `shared/membership.py` (C5); billing and admin import their copies; `architecture_rules.yml`
  drops `shared_package`; kernel-home branch removed; C012, C025, `docs_wire`, `.importlinter`,
  recipes' imports, `docs/reference/testing.md`, `public-source.json`; C-GEN-03 ledger text
  exactly as quoted in ADR-0013 decision 10 (owner confirmation B5), with its test changes.
- **Edges:** C1, C2, C3, C4, C5 fixed. **Invariants:** I-05, I-11, H-8.
- **Tests:** kernel wheel scan; consumer-only install from wheels; rolling-upgrade test 21/21;
  module suites unchanged.
- **No extra I/O:** no runtime path change. **Measure:** M-wheel (kernel wheel file list without
  module files; unchanged by an additive module change).
- **Must not touch:** contract shapes, event payloads.

### KB-23 replacement-provider DTO decoding per contract and shape

- **Scope:** the copy registry keyed per contract and shape, so a replacement provider's results
  decode into the consumer's copy on the untyped path.
- **Edges:** none. **Invariants:** I-06. **Tests:** contracts PoC §3.3 `acme_orgs` untyped path.
- **No extra I/O:** decode only. **Measure:** M-ir on a contract call rung.
- **Must not touch:** the typed path.

### KB-30 access checks, generated atoms, one evaluator

- **Scope:** `access_check`, `From`, `Category` (advanced names); describe and snapshot; compat
  rule (removing, re-binding, lowering risk = T004); generator writes `ContractCheck` and
  `ClaimFactCheck` classes beside the consumer's stub; the generic evaluator; `has_credits_at_least`;
  generated `Subscribed` with `statuses=` / `claim_key=` bound to billing's declaration and 501
  without a provider; shape mismatch → indeterminate 501 `contract_mismatch`, user input → 422;
  402/403/501/503 distinctions; the authorization stand promoted to a CI count test. Kernel atoms
  stay beside the generated ones (no caller changes yet).
- **Edges:** B-R1, B-R3, B-N12, B-N13 start. **Invariants:** I-07, I-13, I-23, H-16, owners #4,
  #5, #8, answer #24.
- **Tests:** Z-1 decisions, Z-2 counts, Z-3 laws, Z-4 tier pins, Z-7 typing, Z-9 fail-closed,
  Z-10 (now 501 inside `Or`/`Not`), Z-11 generation, Z-14 `past_due`; `Account` neutrality (a
  third-party `has_seat` check on `Account.org(param=)` and `bound_tenant()` without billing,
  decisions and counts equal to the billing check; owner confirmation C4).
- **No extra I/O:** proven by the stand: decisive 3 calls / 9 DB mono, 3 RPC / 9 provider DB / 0
  consumer DB split; `Credits` 1 / 4; claims tier 0 / 0. **Measure:** M-calls, M-ir (decisive
  rung budget 5,885,658 Ir/req at the PoC base), M-lat split.
- **Must not touch:** the algebra, the kernel atoms' behaviour, K5/K6/witness (own PRs).

### KB-31 tenant kind from the assigned tenant authority (K1, the change of `_archive/alternatives-c-authz.md`; not register row K1)

- **Scope:** `tenant_kind` asks the witness declared by the authority of `tenant_ids` (or its
  assigned provider), found from the consumer's generated copy; `bound_tenant()` in a policy adds
  that contract to `consumes`; no witness → `unknown` and a startup report; the membership adapter
  slot no longer used.
- **Edges:** B-R2 fixed. **Invariants:** I-10, I-23, H-19, owner #7.
- **Tests:** four kinds kept; `tenant_ids` alone never `organization`; with and without witness;
  replacement tenant authority (`grants`) without a membership method.
- **No extra I/O:** the call is today's `has_role_at_least(org, user, "member")`; decisive shape
  stays 3 / 9. **Measure:** M-calls decisive shape, M-ir.
- **Must not touch:** freshness, the third call (KB-37).

### KB-32 archcheck from atom metadata

- **Scope:** rules read the literal (imported name → generated file, AST); `ORG_ATOMS`,
  `BILLING_ATOMS`, `require_org_role` lists removed; `handwritten-access-check` (SANDLOOM-C032);
  B017 / C027 texts over metadata; gate-order rule also reads `module_router()` routes (the
  pre-existing gap).
- **Edges:** B-N17, B-N18 fixed. **Invariants:** H-20, answer #22.
- **Tests:** the Z-8 fixture (third-party `ProjectRole`, `SeatsAvailable`, `CheapLead`,
  `HonestLead`).
- **No extra I/O:** static. **Measure:** `sandloom check` wall time.
- **Must not touch:** route classification at startup (the backstop).

### KB-33 `Feature` and `WithinLimit` to billing

- **Scope:** research and move as generated claim or access atoms with their parameters carried
  locally and over RPC (fixes `facts.v1` losing parameters remotely); no removal (answer #21).
- **Edges:** B-N12 (its `features`/`limits` part). **Invariants:** H-2, I-16, I-23.
- **Tests:** parameters across RPC; decisions equal to today's atoms on a test producer.
- **No extra I/O:** claims tier stays 0 / 0. **Measure:** M-calls, M-ir.
- **Must not touch:** other atoms.

### KB-34 kernel domain atoms removed; ADR-0010 amendment applied

- **Scope:** G-AUTHZ-05 equivalence scenarios pass first; then remove the kernel `OrgRole`,
  `SubscriptionActive`, `Credits`, `Subscribed`, `Feature`, `WithinLimit`,
  `ACTIVE_SUBSCRIPTION_STATUSES`, `WELL_KNOWN_FACT_OWNERS`, `register_live_adapter`,
  `EntitlementsAdapter`, `MembershipAdapter`, the billing branch of `fake_access_fact`; move
  `Roles` / `Permissions` to identity; beginner tier recounted; 51 code files and 13 doc pages
  migrated; ledger (`removed:` records, G-AUTHZ-05 restated); upgrade guide; ADR-0014 updated.
- **Edges:** A19, B-R1, B-R3, B-R6, B-N12, B-N13, B-N14, B-N15 fixed. **Invariants:** I-13, I-16,
  I-17, H-2, H-8, owners #2, #6.
- **Tests:** G-AUTHZ-05 equivalence; public API baseline and beginner tier diffs (I-17 review of
  the `Roles`/`Permissions` move, owner confirmation A6); the stand
  unchanged; docs build; examples' suites.
- **No extra I/O:** the stand's counts unchanged from KB-30. **Measure:** M-calls, M-ir.
- **Must not touch:** the algebra, `Role`, `Perm`, `Owner`, `Account`.

### KB-35 K5 provider down → indeterminate 503

- **Scope:** inside the evaluator: `remote_service_unavailable`, `rpc_timeout` → indeterminate 503
  `owner_unavailable`, a warning and `access_indeterminate_branch_total` per branch;
  authentication and allowlist failures stay errors.
- **Edges:** none. **Invariants:** I-07, H-4, answer #19.
- **Tests:** Z-9 with K5: `OrgRole | SubscriptionActive` allows on the subscription with tenancy
  down; `~OrgRole` never allows; `rpc_caller_forbidden` stays an error.
- **No extra I/O:** none. **Measure:** M-calls unchanged.
- **Must not touch:** other exception classes.

### KB-36 K6 per-atom request session; boot describe seeding

- **Scope:** `require()` asks `needs_request_session()` instead of `tier > TIER_CLAIM`; the boot
  probe describes every remote binding (not only those with a known snapshot) and seeds the proxy's
  describe cache.
- **Edges:** none. **Invariants:** I-14 (remote tier), H-19.
- **Tests:** Z-5: 0 describe `GET`s on a first request; Z-4 tier pins.
- **No extra I/O:** removes a request-path describe; adds one `GET` per binding at boot.
  **Measure:** M-calls first request, M-ir (PoC −8.9 % decisive).
- **Must not touch:** decisions.

### KB-37 witness 3 → 2 (answer #23)

- **Scope:** an allowing check with `witnesses_membership` records `organization` in the request's
  tenant-kind memo.
- **Edges:** none. **Invariants:** I-23 (allowed removal), membership freshness unchanged.
- **Tests:** decisions equal; only the second `has_role_at_least` disappears.
- **No extra I/O:** removes one call: 2 calls / 6 DB mono, 2 RPC / 6 provider DB split.
  **Measure:** M-calls, M-ir (PoC −11.7 %).
- **Must not touch:** freshness, tenant isolation.

### KB-38 K7 descriptor refresh off the request path (optional)

- **Scope:** claim-tier descriptors refreshed at boot and on a timer; before the first fetch,
  claim-mode facts escalate to live or answer 503.
- **Edges:** none. **Invariants:** answer #20, C-B8. Only if the owner wants it after Z-14.
- **Tests:** Z-14 table plus "provider down at boot".
- **No extra I/O:** moves the 300 s describe off the request path. **Measure:** M-calls.
- **Must not touch:** `past_due` semantics.

### KB-40 admin manifest through a generation extension

- **Scope:** a module declares a generated file; the kernel collects resource declarations and
  hands data; `core/admin/compiler.py`, `models.py` move to `sandloom_admin`; `sandloom admin export`
  keeps working; `generate --check` covers the file.
- **Edges:** B-N1, B-N2 fixed. **Invariants:** I-02, I-13, H-3, H-11.
- **Tests:** no admin → module import; `inventory` fixture resources reach the manifest (AD-5);
  AD-1, AD-2, AD-4.
- **No extra I/O:** generate time only. **Measure:** M-gen digest of the manifest equal.
- **Must not touch:** console runtime.

### KB-41 admin console issuer check (X4)

- **Scope:** `sandloom_admin/api.py` tests "an assigned issuer exists" instead of a module named
  `identity`; the operator-login path per the owner's X4 confirmation: the issuer declares its
  login entry point in its registration, the console finds the issuer through the map's `issuer`
  and consumes the declaration optionally; never the `iss` of an unverified token; no
  consumer-defined contract.
- **Edges:** none of the 46 (admin naming identity is module code). **Invariants:** I-13, AD-3.
- **Tests:** console with `test_identity` as issuer and its declared entry point; no issuer, or an
  issuer without a declaration → configuration error naming it; a token with a forged `iss`
  changes nothing.
- **No extra I/O / measure:** none. **Must not touch:** platform API.

### KB-49 settings PoC gate: all 16 scenarios (decision D; closes G6)

- **Scope:** a PoC branch, not merged into `main`, that builds ADR-0015's mechanism far enough to
  run every scenario P1-P16 of `_archive/alternatives-d-settings.md` §11 with the `inventory` fixture module outside
  `src/` and the bundled billing and identity ports (P10), mono and split, on the merged baseline.
  Each scenario's result (pass, or fail with the reason), the commit and the measurements (P13
  boot cost, P11 `config dump` wall time and socket spy, P12 DX counts) are recorded in
  `poc/settings/results.md`, in ADR-0015 "Evidence" and in [validation.md](validation.md).
- **Gate:** KB-50, KB-51, KB-52, KB-53, KB-54, KB-55 and KB-56 are each blocked by KB-49 in tm, and
  none starts until all 16 scenarios have a recorded result. A failed scenario reopens ADR-0015
  (or picks its fallback: D2b on P12, D3 for the dump on P11, H2 on P16) before any settings PR
  starts. Each later PR then turns its rows of the PoC into repository tests (listed under its
  **Tests**) and may not drop one.
- **Edges:** none (no change reaches `main`). **Invariants:** I-13, I-18, answers #25-#31, D-B1..D-B6.
- **No extra I/O / measure:** P11 socket spy, P13 boot medians against base-vs-base noise.
- **Must not touch:** `main`.

### KB-50 settings mechanism

- **Blocked by:** KB-49 (all 16 settings scenarios recorded).
- **Scope:** `define_module(settings=)` / `api.settings`, the source snapshot, validation before
  `setup` with context, `ModuleSettingsError`, the post-load check (collisions B036, removed
  settings, typo check over the union, override logging), `settings_of`, the testing override, a
  fixture module outside `src/`.
- **Edges:** B-C1 start. **Invariants:** I-13, I-18, answers #25, #29.
- **Tests:** P1, P2, P5, P7, P13, P15 of `_archive/alternatives-d-settings.md` §11.
- **No extra I/O:** one read per process replaces today's. **Measure:** M-boot (P13).
- **Must not touch:** bundled modules' fields (KB-52..54), risk ids (KB-51).

### KB-51 namespaced accepted risks

- **Blocked by:** KB-49 (all 16 settings scenarios recorded).
- **Scope:** the risk registry = kernel risks ∪ loaded modules' declared risks; the known-namespace
  rule; flat module ids refused with the replacement named where installed.
- **Edges:** B-C5 start. **Invariants:** I-18, I-22 (T10), answers #27, #28.
- **Tests:** P4 in a split with one shared anchor, one image.
- **No extra I/O / measure:** startup only, M-boot.
- **Must not touch:** kernel risk ids (stay flat).

### KB-52 billing settings to billing

- **Blocked by:** KB-49 (all 16 settings scenarios recorded).
- **Scope:** billing's eight fields, Stripe validator, fake-PSP production check, risk
  `billing.fake_psp_in_prod` (one-time migration, upgrade notes), its removed settings, semantic
  tier and ratchet, console-visible `billing_provider`; kernel ratchet 13 → 12 / 15 → 14.
- **Edges:** B-C4, B-C5, B-C9 fixed; B-C1, B-C6 billing part. **Invariants:** I-02, I-18.
- **Tests:** G-BILLING-18, G-RISK-07, G-CONFIG-07; P8, P10.
- **No extra I/O / measure:** M-boot.
- **Must not touch:** env names.

### KB-53 identity settings to identity

- **Blocked by:** KB-49 (all 16 settings scenarios recorded).
- **Scope:** `TWOFA_ENCRYPTION_KEY` (presence and strength), SSO and Telegram fields,
  `REFRESH_TOKEN_EXPIRE_DAYS`, `MAX_SESSION_DAYS` (owner #13).
- **Edges:** B-C3 fixed; B-C1, B-C6 identity part. **Invariants:** I-02, I-18.
- **Tests:** G-AUTH-19, G-AUTH-05, G-CONFIG-07; refresh-session tests.
- **No extra I/O / measure:** M-boot.
- **Must not touch:** `JWT_*`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `AUTH_REVOCATION_*`.

### KB-54 tenancy and admin settings

- **Blocked by:** KB-49 (all 16 settings scenarios recorded).
- **Scope:** `TENANT_CLAIMS_MAX_ORGANIZATIONS` to tenancy; `ADMIN_CONSOLE_*` to admin; the kernel
  CLI's link-target choice reads the composed app's console route instead of
  `admin_console_enabled`.
- **Edges:** B-C2, B-C10 fixed; B-C1 closed with KB-52, KB-53; B-C6 admin part.
  **Invariants:** I-02, I-03, I-18.
- **Tests:** tenancy claims bound; `sandloom admin create` link with and without the console.
- **No extra I/O / measure:** none beyond M-boot.
- **Must not touch:** the link path (B-N3, deferred).

### KB-55 kernel tools read declarations; `config dump`

- **Blocked by:** KB-49 (all 16 settings scenarios recorded).
- **Scope:** tiers and `/diagnostics/core`, doctor, backup notes, console list, T014 shared keys
  and dev env, docs generator, deploy scaffold env from declarations; `config dump` collect mode
  (context variable) and `--kernel-only`.
- **Edges:** B-C7, B-C8, B-N8 fixed. **Invariants:** I-03, I-18, answer #31, D-B2.
- **Tests:** P11 (socket spy, byte-identical `--kernel-only`, diagnosed invalid value, collect mode
  unset in serving entry points), P14.
- **No extra I/O:** the dump builds the composition without lifespan or connections. **Measure:**
  `config dump` wall time against today (answer #31 requires it measured).
- **Must not touch:** runtime validation (KB-50).

### KB-56 `sandloom check` settings gate

- **Blocked by:** KB-49 (all 16 settings scenarios recorded).
- **Scope:** collisions across all app modules, prefix rule with the snapshot as grandfathering
  baseline, per-module ratchet helper, per-service names and literal values of the chosen
  topology, wrong module names in `SANDLOOM_MODULES`.
- **Edges:** none directly. **Invariants:** answers #25, #26, #30.
- **Tests:** P6, P9.
- **No extra I/O:** static. **Measure:** `sandloom check` wall time.
- **Must not touch:** runtime refusals.

### Later phase (after ADR-0012..0015 are implemented)

| PR | scope | edges |
|---|---|---|
| KB-60 identity CLI | identity's user commands (`admin create/invite/list`, `security revoke-sessions/force-password-reset/grant-admin`) leave the kernel, kept where possible through the standard composition or a CLI extension; set-password link target; start page hint (owner #14) | B-R4, B-R5, B-N3, B-N4 |
| KB-61 starter, templates, recipes | `sandloom new`, wizard, deploy templates, keys text, skill template, recipes generated from declarations or scoped as composition text | B-N5, B-N6, B-N7, B-N9, B-N10, B-N11 |

## Edge closure by PR

The live register `tests/architecture/kernel_edges.yml` has 67 rows: the 46 judged rows of
[edges.yml](edges.yml), row K1 that KB-08 added (also in edges.yml), the 18 class (a) protocol rows
A1-A18 and the two family templates (`retained`, reason "protocol"). After W0: B-I1 and B-N19
`fixed`; B-N20, K1 and the 20 protocol rows `retained`; the other 43 `open`. Register row K1 is not
the tenant-kind change K1 of KB-31.

| PR | closes |
|---|---|
| KB-03 | B-N20 (documentation row, retained) |
| KB-06 | B-I1 |
| KB-07 | B-N19 |
| KB-08 | K1 (added and retained, ADR-0012 decision 3) |
| KB-11 | B-S1, B-S2; B-S3 and B-N16 retained |
| KB-22 | C1, C2, C3, C4, C5 |
| KB-31 | B-R2 |
| KB-32 | B-N17, B-N18 |
| KB-34 | A19, B-R1, B-R3, B-R6, B-N12, B-N13, B-N14, B-N15 |
| KB-40 | B-N1, B-N2 |
| KB-52 | B-C4, B-C5, B-C9 |
| KB-53 | B-C3 |
| KB-54 | B-C1, B-C2, B-C6, B-C10 (B-C1 and B-C6 with KB-52, KB-53) |
| KB-55 | B-C7, B-C8, B-N8 |
| KB-60 | B-R4, B-R5, B-N3, B-N4 (deferred rows) |
| KB-61 | B-N5, B-N6, B-N7, B-N9, B-N10, B-N11 (deferred rows) |
