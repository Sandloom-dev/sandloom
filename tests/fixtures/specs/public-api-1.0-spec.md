# Public API 1.0 normative migration fixtures

Extracted unchanged from the pre-release specification. These tables and ledger
dispositions are regression inputs, not current user-facing guidance.

## 9. Migration plan

### 9.1 Modules, examples, templates, docs

No compatibility layer: each step deletes the old form. A one-off codemod
(`scripts/codemod_public_api_1_0.py`, not shipped) rewrites imports and
mechanical patterns; review does the rest.

- **Bundled modules** (identity, tenancy, billing): imports → `aja.sdk`
  public modules and the DTO packages; function-local imports removed
  (identity 25, tenancy 7, billing 16); `setup` registrations become
  discovered L1 objects, while startup and shutdown tasks, claims
  contributors and planner operations stay L2 calls in a short `setup` with
  top-level imports; route markers removed; `public(reason=)` on login,
  refresh, SSO callbacks, verification and PSP webhooks
  (`issues_tokens=True` where they mint tokens); risk tags at their source
  (identity credential services `security`, billing credit commands
  `risk="money"`, the ledger `MoneyLedger`, admin gates already tagged);
  checkout and portal under `operation_intent` (§4.6); billing gates state
  `of=` (§7); own channels become typed events, consumed ones views;
  contracts become `@contract` with stubs generated into
  `aja.shared.contracts`; snapshots merge into `module.snapshot.json`.
- **Examples first** (linkly, helpdesk, projecthub), because agents copy
  them; their 11 function-local imports go.
- **Templates** (`aja new`, `aja add`) emit beginner-tier L1 code only
  (AJA-T009) and a product AGENTS.md listing the 27 names; `aja-scaffold`
  and `aja-migrate` give way to `aja add` and `aja migrate`, and deploy
  files and runbooks are updated in the same series.
- **Tests:** `make_user_headers` → `as_user` and `deliver_event` →
  `deliver`, both with identical parameters and results.
- **Docs**, written once against `aja.sdk` (docs strategy §7): generated
  reference per public module (beginner names first), the error catalogue,
  a regenerated testing reference; rewritten concepts (modules and levels,
  units and `db()`, write admission, events, contracts, route policy and
  sessions, the lifecycle matrix, guarantees and labels, projections,
  authorization subjects); how-tos per generator; a command-level tutorial
  with code from snippet includes; the root and module AGENTS files and the
  skills rewritten for 1.0; `Docs/reference/rules/` dissolved into concepts
  and the error catalogue.
- **Stale-symbol lint** (docs CI; ADRs and `Docs/design/` exempt): the
  removed and renamed names of Appendix C, `tenant_type=`, `EVENT_SCHEMAS`,
  `AUTH_REVOCATION_STRICT_PATH_PREFIXES`, `aja-scaffold`, `aja-migrate`.

### 9.2 Affected ledger entries (exhaustive)

A keyword scan of all 491 entries for every name, setting and command this
wave renames, moves or removes matched **114 entries**. Each is assigned
below. Every other entry is unaffected; a migration test
(`test_ledger_migration.py`) compares the ledger before and after the wave
and requires each changed id to appear in this table.

**Semantic impact by mechanism.** The keyword scan is complemented by a
review of every ledger area a mechanism touches. These entries are affected
semantically even where their text names no changed API:

| Mechanism | Entries | Disposition |
| --- | --- | --- |
| units, `db()`, lazy units (§4.1–§4.3) | G-UOW-01, 02, 04, 05, 08; G-EFFECT-01, 02, 03; C-EFFECT-02 | semantics unchanged (commit on success, pipeline order, savepoints, function scope); tests extended to lazy read units and `db()` handles |
| write store and admission (§4.4, §6.1) | G-UOW-06, 07; G-OUTBOX-01, 07, 08; G-AUDIT-01, 02; G-IDEM-09 | G-UOW-06 strengthened (startup error, write-store default); G-UOW-07 gap closed by G-MSTORE-03; outbox, audit and durable records land in the write store (same database in every single-store module, so unchanged there) |
| re-check (§4.5) | G-REVOKE-12, 17, 18; C-AUTH-07; G-BLOCK-01..05 | G-REVOKE-17 **unchanged in strength** (every committing unit) and now automatic on writes; G-REVOKE-18 reworded (propagate instead of refuse) |
| idempotency (§4.7) | G-IDEM-01..12, 14..16; G-GUARD-01, 02; G-BILLING-05 | explicit guards unchanged; automatic modes B/S/X added (G-IDEM-17); G-BILLING-05 unchanged under mode X |
| read units and contracts (§4.2) | G-CONTRACT-01, 02, 06, 07; G-RLS-12 | unchanged; `db()` replaces `contract_session()` in tests |
| authorization (§7, §8.3) | G-AUTHZ-01..08; C-AUTH-06; G-BILLING-14, 17 | the algebra is unchanged, so G-AUTHZ-01 (three-valued statuses) and G-AUTHZ-02 (cheapest tier first, short-circuit, zero I/O for claims-only policies) keep their exact semantics; the tests only add an explicit `of=Account.user()` to billing atoms; G-AUTHZ-03/07 unchanged; G-AUTHZ-05 unchanged (`adapter=` kept) |
| events and consumers (§5.2, §5.4) | G-EVENT-01..14; G-INBOX-01..04; C-EVENT-01..03 | unchanged; G-EVENT-05 gap closed by `deliver` tests |
| projections (§6.4) | C-PROJ-01..06; G-TENANT-18 | unchanged; G-MSTORE-08/09 add the watermark and field ownership |
| discovery and manifest (§5.7) | G-MODULE-14..16; C-MODULE-04, 06, 07, 09; C-SPLIT-03 | plain-router rule unchanged; new discovery tested; bundled pins kept as oracles |
| imports and surface (§2) | G-MODULE-01..13; G-KERNEL-01..13 | whitelist reworded to `aja.sdk`; kernel rules unchanged |
| billing account selection (§7.4) | G-BILLING-01, 02, 05, 19 | unchanged protocol; G-BILLING-19 gap closed; the single-organization inference change has no ledger entry (BILLING_SPEC §6 updated) |

`test_ledger_migration.py` requires every id changed by the wave to appear
either in this table or in the lists below.

**Name-only rewording (semantics unchanged; tests change imports or command
names only), 70:** G-TENANT-01, 02, 04, 05, 06, 08, 09, 10, 13, 14, 16, 18;
G-RLS-12; G-PROFILE-02, 05; G-OUTBOX-08; G-INBOX-02; G-IDEM-01, 09, 14;
G-EVENT-07; G-GUARD-01, 02; G-CONTRACT-05, 12, 16; G-COMPAT-05;
G-MODULE-04, 06; G-KERNEL-01, 02, 05, 08, 13; G-INFRA-02; G-CONFIG-07;
G-RISK-11; G-MIGRATE-01, 02, 03, 05, 06, 07, 09, 10; G-OPS-16, 18;
G-SAGA-02; G-BILLING-14, 17; G-CONTRACT-17; C-TENANT-01, 02; C-EVENT-01,
02; C-EFFECT-02; C-AUTH-04, 08; C-MIGRATE-01–07; C-PROJ-01, 02, 04;
C-TEST-01; C-MODULE-08.

**False positives (no change), 3:** G-OUTBOX-14, G-EVENT-06, C-DEPLOY-04
("budget" there is a time or connection budget).

**Semantic rewording, same or stronger (new or changed tests), 31:**

| Id | Change | Tests |
| --- | --- | --- |
| G-TENANT-12 | still `400`; additionally a `MissingTenantError` at the request boundary maps to the same `400`; gap closed | `test_tenant_scope.py` |
| G-REVOKE-10, 11, 12 | "strict and budgeted routes" → every authenticated route; one `GET` of the subject record | auth tests |
| G-REVOKE-17, C-AUTH-07 | the re-check runs before the COMMIT of every committing unit of the request, now automatic on writes and on writing GETs; `before_commit`; `at_commit()` for explicit commit obligations | session tests, plus a foreign revocation between two commits |
| G-REVOKE-18 | remote commands under an armed re-check are **propagated** (provider re-checks in its unit) instead of refused; reads unchanged | RPC tests with a revocation between call and provider commit |
| G-BLOCK-01, 02, 03 | the block is a value of the subject record; read live on every authenticated request; unblock by compare-and-set after commit | auth tests |
| G-OPCLASS-03 | per-module policy snapshot pins every route's resolved policy | snapshot tests |
| G-OPCLASS-04 | declarative markers (`public`, `sensitive`, `no_idempotency`, `operation_intent`, `writes`) pruned from `route.dependant` | marker tests |
| G-OPCLASS-05, 06, 07 | "class A/RS" → "admin-gated" / "platform-admin gated"; locks and audits unchanged | unchanged files |
| G-AUTHZ-01, 02, 03, C-AUTH-06 | atoms take a required `of=`; claims-only `Subscribed(of=Account.user())` still does no I/O | authz tests |
| G-BILLING-01, 02, 05 | the same protocol under `operation_intent`; `release_check` wraps today's two re-reads | billing tests unchanged + release-rule test |
| G-BILLING-19 | enforced by write admission rule 5; gap closed | new AJA-R008 test |
| G-COMPAT-01 | the `contracts` section of `module.snapshot.json`; compatibility checked before any write | snapshot tests |
| G-MODULE-02, 13 | imports restricted to `aja.sdk` public modules + DTO packages + own package | architecture tests |
| G-MODULE-14, C-MODULE-04 | discovery; `ModuleAPI.include_router` / `register_event_schema` replace `router=` / `events=` | define_module tests |
| G-MODULE-15, 16 | boots-alone and chains: hand-written pins for the bundled modules stay as independent oracles (§10.1); apps derive them | unchanged + derived tests |
| G-MODULE-18, C-MODULE-01 | `aja add module` replaces `aja-scaffold` | generator tests |
| G-KERNEL-11 | the `aja.sdk` surface in `public_api_baseline.json`; gap closed | baseline test |

**Removed, each with a `removed:` record and superseding id, 10:**
G-OPCLASS-01 → G-ROUTE-01; G-OPCLASS-02 → G-ROUTE-01/04; G-REVOKE-13, 14,
15, 16, 20 → G-REVOKE-21; G-IDEM-13, G-GUARD-03 → G-SENS-05; C-TEST-04 →
C-TEST-07. None weakens: each removal follows ADR-0009 (accepted) and its
replacement is at least as strict (presence of authentication is checked;
money dedupe is required on every path; every route is strict).

### 9.3 ADR-0009 planned sections → ledger ids

| Section | Ids |
| --- | --- |
| Defaults every route gets | G-ROUTE-01 (authenticated xor `public()`), G-ROUTE-02 (`public()` reason; issuers keep the live issuance check), G-ROUTE-03 (webhooks public + signature), G-REVOKE-21 (one live subject-record `GET`, memoised), G-REVOKE-22 (re-check before the COMMIT of every committing unit of an armed request, including local and remote commands; units that commit nothing skip it; writing GETs arm it; intent routes use `release_check`), G-REVOKE-23 (self-revocation "check, then revoke", including savepoint rollback, lost RPC response, command replay and failed caller commit), G-REVOKE-24 (propagation to remote commands), G-HTTP-12 (`no-store`), G-IDEM-17 (automatic guard always mode S: never a body; `replay_safe(reason=)` opts in to B; explicit guards unchanged; namespace, fingerprint, replay decided after every route-level dependency), G-IDEM-18 (fail-closed in-flight form without a request unit), C-ROUTE-01 (`no_idempotency`, explicit guards with all parameters) |
| Risk declared at its source | G-SENS-01 (admin gates), G-SENS-02 (money sources), G-SENS-03 (security step-up; identity's current-password re-authentication via `mark_reauthenticated()` unchanged), C-ROUTE-02 (`sensitive()`) |
| What risk triggers | G-SENS-04 (automatic audit), G-SENS-05 (money dedupe, `428`), G-SENS-06 (RFC 9470 step-up on `auth_time`) |
| One subject-state record | G-BLOCK-06, G-BLOCK-07; reworded G-REVOKE-01/03/09/10/11, G-BLOCK-01–03 |
| The route snapshot | G-ROUTE-04 (resolved policy incl. write store, labels, authz subjects; weakening fails before write), G-ROUTE-05 (observed effects informational) |
| Removed outright | the removal records of §9.2; `AUTH_REVOCATION_STRICT_PATH_PREFIXES` in `config_tiers.REMOVED` (G-CONFIG reworded) |
| Checks | G-ROUTE-01, G-SENS-05, G-SENS-06, G-REVOKE-21, G-REVOKE-22, G-GUARD-01, C-ROUTE-03 (`untagged-sensitive`) |

### 9.4 New entries

| Prefix | Ids (owner) |
| --- | --- |
| G-ROUTE, C-ROUTE, G-SENS, G-REVOKE-21..24, G-BLOCK-06..07, G-IDEM-17..18, G-HTTP-12 | §9.3 (WS-A) |
| G-UNIT, C-UNIT | G-UNIT-01 (closed-handle refusal; lazy unit commit/rollback per §4.2), C-UNIT-01 (`db()` refuses a non-owner task; child tasks use `open_uow()`), C-UNIT-02 (**best-effort footgun guard**, worded as detection, not a guarantee: session objects used from another task are detected at the points kept by the performance gate; the known undetectable forms are listed in the entry), G-UNIT-02 (operation-intent mode, release rule and the lifetime exclusion of `external_call` and `release_check`, AJA-R008/R012) (WS-A) |
| G-MSTORE, C-MSTORE | multistore §6.1, with G-MSTORE-03/04 restated as the write-admission guarantee and its stated scope (§4.4), and G-MSTORE-08 restated by §6.4 (full-state convergence; deltas only with the inbox) (WS-A) |
| G-AUTHZ-09..11 | required `of=` (with AJA-C027); `bound_tenant()` resolution by tenant kind with its test table; the algebra-pinning probe (value, evaluation count, tier order) (WS-A) |
| G-BILLING-20..21 | personal `spend` / `get_balance` checked against the signed subject; billing API account chosen explicitly (no single-organization inference) (WS-A tests, WS-C code) |
| G-UNIT-03 | write admission covers the cursor boundary (`exec_driver_sql`) (WS-A) |
| G-SDK, C-SDK | G-SDK-01 (side-effect-free import, §2.4), G-SDK-02 (import-linter contracts), G-SDK-03 (L1 registration equals L2), G-SDK-04 (beginner-tier pin), G-SDK-05 (every L1 dependency is a public L2 name), C-SDK-01 (`aja docs`), C-SDK-02 (generated reference blocks) (WS-B) |
| G-DECL, C-DECL | G-DECL-01 (event role, producer schema snapshotted, compatibility before write), G-DECL-02 (`@on` options), G-DECL-03 (derived `consumes`), G-DECL-04 (`optional=True` is `None` only when not deployed), G-DECL-05 (no function-local imports, AJA-C021), C-DECL-01..05 (`Event`/`on`, `contract`/`command`/`Contract`, `module_router`, `Projection`, `db()`) (WS-B; C-DECL-05 with WS-A) |
| C-GEN, C-TEST, G-MODULE | C-GEN-01..06 (one per generator), C-GEN-07 (`aja migrate new` renders RLS), C-TEST-07 (`route_policy` / `assert_route_snapshot`), C-TEST-08 (`published`), C-TEST-09 (`fake_contract`), C-TEST-10 (`deliver` drives the real runtime), G-MODULE-19 (derived lists for app modules) (WS-B) |

ADR-0010's sections stay `planned` until these land.

---

## 10. Acceptance

### 10.1 Independent oracles

Derived artifacts (snapshots, derived test lists, deploy plans) are checked
against **behaviour**, not against the manifest that produced them:

- **Hand-written expectations stay for the platform:** the bundled modules'
  route-policy expectations (a pinned table at least as strict as today's
  markers), boots-alone expected refusals and chain lists remain hand-kept
  tests for identity, tenancy and billing. Derivation is used for app
  modules, and the two are compared.
- **Fault and concurrency tests** per guarantee, written against the
  database and the bus, not the manifest: crash after COMMIT, provider
  killed mid-command, two real transactions racing an at-commit re-check,
  a revocation between the RPC call and the provider commit, a foreign
  revocation between two commits of one request, a second writer in a child
  task, a second writer through `exec_driver_sql()` on the session's
  connection under `unscoped()`, a session passed to another task, a
  savepoint rollback around a self-revocation, a lost RPC response followed
  by a command replay, a failed caller commit after a self-revoking command,
  a GET that writes (re-check armed, `on_commit` kept), an intent route
  returning 2xx without `release_check()` or with two statements after it,
  and the negation probe against today's algebra.
- **Compatibility** is always computed against the previously committed
  snapshot before the gate writes anything (§5.8).
- **Split conformance matrix** including the projection, opt-out, refusal
  and unbound cases (multistore §6.2) and the operation-intent routes.

### 10.2 Performance scenarios (A7, measured before and after)

| Scenario | Measure |
| --- | --- |
| eager write unit under concurrency: N concurrent writes, each with a 50 ms contract call inside the handler | pool occupancy, checkout wait p99, throughput |
| lazy read units | connections per read route that never calls `db()` (target 0) |
| automatic durable guard | the three extra statements per keyed write |
| live organization gates | requests per second with `of=Account.org()` vs claims-only `of=Account.user()` |
| `db()` ownership instrumentation | per-call owner check on session methods and, if kept, per-assignment attribute listeners (§10.4) |
| write-admission hooks | per-statement overhead (contextvar check) |
| projection fan-out and consistency tokens | apply throughput, token wait p99 under lag |

### 10.3 Final gate (once, on the integration branch)

1. ledger green; ADR-0009 and ADR-0010 have no `planned` section left;
   `test_ledger_migration.py` green;
2. `aja test --full`;
3. split conformance matrix;
4. **the performance gate of §10.4 passed**, its report committed;
5. the agent eval once: 9 small tasks + the Academy product suite; targets:
   concepts ≤ 10, kernel reads 0, framework edits 0, footgun escapes 0.

### 10.4 Performance gate (binding)

The kernel rule (`backend/src/aja/core/AGENTS.md`: hot paths change only
with cachegrind instruction counts) applies to this wave.

- **Method.** Baseline = the integration branch's merge base with `main`;
  candidate = the integration branch head. Instruction counts per operation
  from the existing cachegrind stand (`benchmarks/ladder/cg/run_cg.sh`,
  `CG_K=200`, `Ir(K) − Ir(0)` per request, Linux container, quiet exclusive
  host), as in `Docs/design/hot-path-profile-2026-10-21.md`.
- **Rungs.** `L6S5` (authenticated read through the full request path),
  `L6W1`–`L6W6` (the write stand: request unit, outbox, durable guard,
  tenant scope), and a new rung **`L6WM`**: one authenticated write that
  loads 20 mapped instances and assigns 10 attributes on each (200 mapped
  assignments), to price the attribute listeners and the session-method
  checks.
- **Budget.** At most **+1 % instructions per operation** on each rung for
  the checks this wave adds in-process (write admission hooks, ownership
  checks, unit bookkeeping, marker handling, replay-mode selection). Costs
  that ADR-0009 already accepted (the subject-record `GET` before writing
  commits; the durable guard's statements on keyed writes) are measured and
  reported separately, as ADR-0009 phase 6 requires, and are not part of
  the 1 %.
- **Rule.** A check that exceeds the budget is dropped or moved off the hot
  path (for example enabled only under the test harness, where it still
  catches the mistake in the agent's inner loop); it is not kept on the hot
  path unless the owner approves the higher cost in writing. Best-effort
  footgun detection (§4.3 item 3) is the first candidate to drop; a
  guarantee's enforcement is never dropped, it is optimised or the owner
  decides.
- **Artifact.** `benchmarks/ladder/RESULTS.md` gains a "Public API 1.0"
  table: rung, baseline Ir/op, candidate Ir/op, delta, verdict, plus the
  separately reported ADR-0009 costs.

---

## 11. Workstreams

### 11.1 Single owners

| File or area | Owner |
| --- | --- |
| `backend/src/aja/core/**` except `cli/`, `archcheck/`, `testing/`; `aja/shared/authz/**`, `aja/shared/guards/**`; `aja/core/error_codes.py`; `aja/core/loader/manifest.py` | **WS-A** |
| `backend/.importlinter` (kernel **and** sdk contracts) | **WS-A** (WS-B supplies the sdk contract text at H0) |
| `backend/src/aja/sdk/**`; `aja/core/cli/**`, `aja/core/archcheck/**`, `aja/core/testing/**`, `aja/core/contract_testing.py`; generator and `aja new` templates; `architecture_rules.yml`; `public_api_baseline.json`; `pyproject.toml` / `uv.lock`; `backend/src/tests/typing/`; the scanner script | **WS-B** |
| `aja/{identity,tenancy,billing}/**` (including billing's explicit account selection, the personal-spend check and the intent-mode migration), `aja/main.py`; `aja/shared/schemas/**`; generated `aja/shared/contracts/**` and `aja/shared/events/**` (produced by running WS-B's generator); `examples/**` (including the helpdesk export gate and its test, §7); `deploy/**`; `Docs/**` except ADRs; every `AGENTS.md`; `.claude/**`; `mkdocs.yml`; `benchmarks/dx/**` | **WS-C** |
| `backend/guarantees.yml`, `Docs/reference/guarantees.md`, ADR-0009/0010 `adr_sections` changes, ADR text amendments | **integrator** (WS-C lead), applied only at hand-off points from each workstream's ledger delta |

No file has two owners. Workstream branches never edit the ledger; they
deliver a ledger delta (new entries with test node ids, rewordings,
removals) with each hand-off, and the integrator applies it on the
integration branch, where the ledger must be green after every hand-off.

### 11.2 Sequence and hand-offs

| Point | Content | From → to |
| --- | --- | --- |
| **H0** (Phase 0, WS-A + WS-B) | lazy settings and the import audit fixes; every error code of Appendix A in the catalogue; the `AppManifest` dataclass; the `aja/sdk` package skeleton (WS-B) and its import-linter contracts (WS-A); the §2.3 signatures frozen | both → all |
| **H1** | WS-A: model E (subject record, deny-by-default, write defaults, sourced risk, snapshot fields), units and `db()`, write admission, intent mode, `release_check`, authz subjects, idempotency modes | WS-A → WS-B (switches L1 to the real L2), integrator |
| **H2** | WS-B: facades, `_decl` (events, consumers, contracts + stubs, router, discovery, projections, typed saga steps), checks C021–C027, `aja add`, `aja migrate new`, `aja test` steps, testing kit, typing tests | WS-B → WS-C, integrator |
| **H3** | WS-A: projections to any store, perf (§10.2). WS-C: examples, bundled modules, generated stubs, deploy profiles | → integrator |
| **H4** | WS-C: docs, AGENTS files, skills, stale-symbol lint; final gate (§10.3) and the eval | integrator |

### 11.3 Acceptance per workstream

| WS | Criteria |
| --- | --- |
| A | every G-/C- id it owns has failing-first behavioural tests (§10.1); split matrix green; the platform policy pinned table green; the §10.4 gate on its hot-path additions (admission hooks, ownership checks, re-check wiring) passed with the report committed |
| B | import-isolation test; the §10.4 gate on the `L6S5` / `L6W*` / `L6WM` rungs for any request-path code it adds (router injection, replay-mode selection); `lint-imports`; `test_l1_uses_only_public_l2`; `test_l1_equals_l2_registration`; typing tests under mypy and pyright; generators produce green apps; the scanner reports no unmapped name |
| C | AJA-C021 clean; `aja add module x && aja test x` touches only `x/` plus the stated generated exception; `mkdocs build --strict` and the stale-symbol lint; examples' boots-alone gates |

---

## 12. Risks

| Risk | Mitigation |
| --- | --- |
| ambient `db()` misuse (captured sessions, child tasks) | owner-task check (AJA-R013), closed-session hooks (AJA-R002), explicit sessions remain |
| write-admission hooks miss a write path | stated scope (§4.4); raw DBAPI unavailable to modules; property test over unit/task sequences |
| intent routes return a secret without the re-check | the release rule discards the body (AJA-R012) |
| required `of=` adds friction | one argument with three named choices; generators and examples show it; the cost of a wrong default is a billing-account confusion |
| organization gates become live calls | measured (§10.2); a per-organization claim is a later option |
| automatic idempotency replays status only, so clients that relied on a replayed body must re-read | `replay_safe(reason=)` for a reviewed exception; explicit guards unchanged; status-only replay is still idempotent |
| discovery side effects | fixed submodule list, owned objects only, atomic draft |
| generated files in the repository | written only by `aja test` after a compatibility check; headed "generated" |
| self-referential acceptance | independent oracles (§10.1) |
| removing the `aja-scaffold` / `aja-migrate` console scripts | WS-C updates deploy files and runbooks in the same series |

## 13. Owner decisions

The owner accepted the recommended answers to v1's 13 questions (recorded in
ADR-0010). Three were revised by the overriding rule:

- **Q3** (refuse ambiguous unions): not adopted. Codec 1 preserves
  `UUID | str` and similar unions (`contract_wire.py`); unions keep working
  and AJA-C026 is an information-level hint.
- **Q4** (Kleene `And`): not adopted. Strong Kleene logic would allow
  `~(U & F)`, which today denies; complete evaluation under `Not` (v3)
  would break G-AUTHZ-02's short-circuiting. The algebra stays as it is
  (§8.3).
- **Q6** (tenant-binding default and the organization inference built on
  it): the binding derivation stays for isolation only; the billing subject
  is never inferred (§7).

The owner then decided the three questions v2 raised (2026-10-25):

1. **Billing gates require `of=`** (`Account.user()`,
   `Account.org(param=...)`, `Account.bound_tenant()`), with no default,
   including the billing additions of §7 (AJA-C027, explicit accounts in the
   billing API, the personal-spend check, `of=` on `Feature` and
   `WithinLimit`, `bound_tenant()` only with its test table, the helpdesk
   fix). `Account.org()` does not narrow header or bound-tenant resolution,
   custom adapters or non-HTTP use. *Rationale:* the only application gate
   in the repository picked the wrong account and its test pinned it;
   payment APIs always take the customer explicitly.
2. **The automatic guard always replays status only (mode S).** A stored
   body is replayed only where the developer opts in: `replay_safe(reason=)`
   (recorded in the route snapshot) or an explicit guard, exactly as today.
   Explicit guards keep all their options. Sensitive, issuer and intent
   routes never replay a body (§4.7). *Revised after the second review:* v2
   replayed bodies whenever caller and tenant matched. *Tightened after the
   fourth review:* the owner's rule "body only when authorization is
   route-level" needed a static proof of complete authorization, which the
   reviewer showed unsound (an ownership condition in plain code is
   invisible to it); always-S plus an explicit opt-in is stricter and
   weakens nothing compared with today's code.
3. **A GET unit that actually writes arms the commit-time re-check
   automatically**; `at_commit()` stays for explicit commit obligations;
   queued `on_commit` / `before_commit` effects are never dropped; existing
   writable GETs are not banned (§4.2); an explicit `at_commit()` checks
   every unit the request opens, empty ones included (§4.5). *Revised after the second review:*
   v2 rolled back read units with no writes and did not re-check writing
   GETs, which could drop queued effects and let a write commit unchecked.


## Appendix A: error codes

| Code | Slug | Raised by |
| --- | --- | --- |
| AJA-B008 | `route-policy` | route neither authenticated nor `public()`, both, or no reason |
| AJA-B009 | `write-store-ambiguous` | several writable sessions without `writes()` / opt-out |
| AJA-B010 | `guarantee-unsupported` | write store lacks a feature the defaults need |
| AJA-B011 | `underived-store-write` | non-transactional store injected into a route with defaults |
| AJA-B012 | `projection-contract` | target lacks `conditional_write`; key not tenant-first; column owned twice; delta mapper outside the inbox case |
| AJA-B013 | `multi-store-waiver` | invalid `allow_multi_db_write` |
| AJA-B014 | `money-effect-outside-write-store` | money effect in an opted-out secondary |
| AJA-B015 | `event-definition` | channel, non-wire field, two producers, producer outside its module |
| AJA-B016 | `consumer-definition` | handler signature, duplicate name, bad tenant field |
| AJA-B017 | `authz-subject-missing` | billing atom (`Subscribed`, `SubscriptionActive`, `Credits`, `Feature`, `WithinLimit`) without `of=` |
| AJA-B018 | `contract-definition` | stateful class, non-async member, `@command` without key, base outside the package |
| AJA-B019 | `capability-replay` | token issuer or intent route with a guard other than mode X; intent route without the required guard or with a request session |
| AJA-R007 | `multi-store-write` | second writer, at its first write |
| AJA-R008 | `nested-or-concurrent-unit` | nested committing unit; overlapping units in an intent route; `external_call` or `release_check` entered with a unit open or a read-scope connection held; any unit, read scope or checkout started while an `external_call` or `release_check` is active (other than the re-read's own read-contract calls) |
| AJA-R009 | `money-effect-without-dedupe` | ADR-0009 item 11 |
| AJA-R010 | `idempotency-key-required` | `428` |
| AJA-R011 | `step-up-required` | `401`, RFC 9470 |
| AJA-R012 | `intent-release-unchecked` | intent route answering 2xx that matches neither the issue case nor the issued-retry case of §4.6, or calls `release_check` without the authority for an organization account |
| AJA-R013 | `concurrent-session-use` | `db()` from a task that does not own the unit (complete); best-effort: a session method, `sync_session` method, attribute assignment or cursor execution from such a task |
| AJA-C021 | `module-local-imports` | function-local import in a module |
| AJA-C022 | `untagged-sensitive` | warning (ADR-0009) |
| AJA-C023 | `multi-store-waivers` | opt-out listing |
| AJA-C024 | `unsafe-tenant-query` | raw non-relational query outside `unscoped()` |
| AJA-C025 | `event-view-compat` | view incompatible or unverified; `.publish()` on a view |
| AJA-C026 | `contract-union-hint` | information: an ambiguous union uses codec 1 |
| AJA-C027 | `account-route-mismatch` | organization route parameter with `of=Account.user()` and no `reason` |
| AJA-T005 | `event-schema-compat` | breaking change on the same `.vN` |
| AJA-T006 | `migration-drift` | models differ from the chain head |
| AJA-T007 | `contract-stub-stale` | generated stub out of date |
| AJA-T008 | `public-api-baseline` | surface or reference block changed |
| AJA-T009 | `beginner-tier` | generated code or an example uses an advanced name without a marker |

Retired: AJA-B003, AJA-B004, AJA-B007, AJA-C016. Unchanged: AJA-R001
`missing-tenant` (now delivered as the G-TENANT-12 `400`), AJA-R002 (also
`db()`), AJA-T001 (policy snapshot).


## Appendix C: generated capability table

Generated at `d89189b6` (scanner: `public_api_migration.py`, committed by
WS-B) by scanning the 13 public modules
(`aja.core.{di, module_api, descriptors, orm, projections, facade,
testing, contract_testing}`, `aja.shared.authz`,
`aja.shared.guards.{auth, platform}`, `aja.shared.schemas.{context,
membership}`) with `inspect` and the ledger with a word-boundary search.
"Files using it" counts files in identity, tenancy, billing and `examples/`
(tests included) that mention the name. Statuses: `kept` (same name and
parameters, new home), `renamed`, `replaced` (same semantics through the
named API), `changed` (one argument replaced, same resolutions), `adr0009` (removed by ADR-0009, superseding mechanism named).
Rows marked "(imported directly)" come from the import scan of the
bundled modules and examples. `CoreFacade` appears once, only in a test comment.

| today | 1.0 home | status | files using it (modules + examples) | parameters (all kept) | ledger entries naming it |
| --- | --- | --- | ---: | --- | --- |
| `core.di.AuditUnavailableError` | aja.sdk.errors | kept | 1 | args, kwargs | G-AUDIT-02 |
| `core.di.BlockUnavailableError` | aja.sdk.errors | kept | 0 | args, kwargs | G-BLOCK-02 |
| `core.di.Capability` | aja.sdk.web | kept | 0 | capability, contract, name, features | C-INFRA-05 |
| `core.di.ClaimsUnavailable` | aja.sdk.security | kept | 1 | owner | G-AUTH-11 |
| `core.di.CoreRuntime` | aja.sdk.infra | kept | 6 | facade |  |
| `core.di.Database` | aja.sdk.web | kept | 7 | name | G-UOW-02, C-INFRA-05 |
| `core.di.DbSession` | aja.sdk.web | kept | 23 | args, kwargs | G-UOW-02, G-IDEM-09, G-BILLING-19 |
| `core.di.EventEnvelope` | aja.sdk.events | kept | 2 | args, kwargs |  |
| `core.di.MAX_REVOCATION_STALENESS_SECONDS` | every route strict (G-REVOKE-21) | adr0009 | 0 |  |  |
| `core.di.MissingTenantError` | aja.sdk.errors | kept | 0 | args, kwargs | G-TENANT-01, G-TENANT-04, G-TENANT-07 |
| `core.di.Notification` | aja.sdk.infra | kept | 3 | channel, recipient, subject, body, data, dedupe_key | C-INFRA-07 |
| `core.di.PlannedReads` | aja.sdk.data | kept | 11 | args, kwargs |  |
| `core.di.PoisonEventError` | aja.sdk.events | kept | 0 | args, kwargs | G-EVENT-07 |
| `core.di.ReadOnlyDbSession` | aja.sdk.web | kept | 0 | args, kwargs |  |
| `core.di.RemoteServiceUnavailable` | aja.sdk.errors | kept | 2 | service_name, reason, code | G-CONTRACT-11, G-RPC-18 |
| `core.di.SagaHandle` | aja.sdk.sagas | kept | 0 | saga_type, saga_id, instance_id, status, created, step, park_reason, version |  |
| `core.di.SagaInputConflict` | aja.sdk.sagas | kept | 0 | args, kwargs |  |
| `core.di.ServiceNotFound` | aja.sdk.errors | kept | 3 | args, kwargs | C-SPLIT-02 |
| `core.di.ServiceUnavailableInDeployment` | aja.sdk.errors | kept | 3 | service_name, reason | G-CONTRACT-10 |
| `core.di.SnapshotReads` | aja.sdk.data | kept | 0 | args, kwargs |  |
| `core.di.TaskEnqueueConflict` | aja.sdk.errors | kept | 0 |  |  |
| `core.di.TaskEnqueueUnavailable` | aja.sdk.errors | kept | 0 |  |  |
| `core.di.TaskEnqueueUncertain` | aja.sdk.errors | kept | 0 | task_id |  |
| `core.di.TenantMismatchError` | aja.sdk.errors | kept | 0 | args, kwargs | G-TENANT-04, G-TENANT-18, C-PROJ-02 |
| `core.di.TenantScope` | aja.sdk.web | kept | 14 | header, claim_keys, required | G-TENANT-10, G-TENANT-12, G-TENANT-13, G-TENANT-14, C-TENANT-02 |
| `core.di.UnitOfWorkRequiredError` | aja.sdk.errors | kept | 0 | args, kwargs | G-OUTBOX-02 |
| `core.di.UnsafeTenantOperationError` | aja.sdk.errors | kept | 0 | args, kwargs | G-TENANT-05, G-TENANT-06 |
| `core.di.UowReads` | aja.sdk.data | kept | 1 | args, kwargs |  |
| `core.di.apply_pending_revocation` | aja.sdk.security | kept | 1 | payload |  |
| `core.di.authenticates` | aja.sdk.web | kept | 4 | fn | G-GUARD-01 |
| `core.di.before_commit` | aja.sdk.effects | kept | 1 | callback, database | G-UOW-04, G-UOW-05, G-EFFECT-03, G-REVOKE-17, C-EFFECT-02, C-AUTH-07 |
| `core.di.bind_contract_session` | aja.sdk.testing | kept | 13 | session |  |
| `core.di.bind_tenant` | aja.sdk.tenant | kept | 9 | tenant_id | C-TENANT-02 |
| `core.di.block_user` | aja.sdk.security | kept | 6 | user_id, reason, actor_id, database | G-REVOKE-06, G-BLOCK-02, G-BLOCK-05, C-AUTH-04 |
| `core.di.cache` | aja.sdk.infra | kept | 29 | name | G-TENANT-08, G-PROFILE-01, G-PROFILE-06, G-PROFILE-07, G-PROFILE-08, G-INBOX-02 … |
| `core.di.contract_session` | aja.sdk.data.db() (inside a contract method) | replaced | 11 |  | G-RLS-12 |
| `core.di.core_from_request` | aja.sdk.infra | kept | 0 | request |  |
| `core.di.create_access_token` | aja.sdk.security | kept | 5 | data, expires_delta |  |
| `core.di.current_tenant_id` | aja.sdk.tenant | kept | 0 |  | G-TENANT-07 |
| `core.di.current_tenant_id_optional` | aja.sdk.tenant | kept | 3 |  |  |
| `core.di.db_session` | aja.sdk.data.open_uow | renamed | 11 | name | G-OUTBOX-08, G-BILLING-19 |
| `core.di.decode_token` | aja.sdk.security | kept | 9 | token |  |
| `core.di.decode_token_cached` | aja.sdk.security | kept | 2 | request, token |  |
| `core.di.durable_inbox` | aja.sdk.effects | kept | 0 | consumer | G-INBOX-01 |
| `core.di.ensure_token_not_revoked` | aja.sdk.security | kept | 3 | request, claims |  |
| `core.di.ensure_user_not_blocked` | aja.sdk.security | kept | 4 | user_id | G-REVOKE-02, G-BLOCK-04 |
| `core.di.get_client_ip` | aja.sdk.web | kept | 2 | request, config |  |
| `core.di.get_core` | aja.sdk.infra | kept | 4 | request | G-MODULE-09 |
| `core.di.get_db` | aja.sdk.testing | kept | 5 | request | G-UOW-02, G-MODULE-06 |
| `core.di.get_planned_reads` | aja.sdk.data | kept | 0 | request |  |
| `core.di.get_read_only_db` | aja.sdk.data | kept | 0 | request |  |
| `core.di.get_snapshot_reads` | aja.sdk.data | kept | 0 | request |  |
| `core.di.get_uow_reads` | aja.sdk.data | kept | 0 | request, db |  |
| `core.di.idempotency_guard` | aja.sdk.web | kept | 5 | scope, ttl_seconds, required, fail_closed, store_response, durable | G-IDEM-01, G-IDEM-09, G-GUARD-01, G-GUARD-02 |
| `core.di.is_user_blocked` | aja.sdk.security | kept | 1 | user_id |  |
| `core.di.make_db_dependency` | aja.sdk.data | kept | 0 | name |  |
| `core.di.notifier` | aja.sdk.infra | kept | 7 | name | G-INFRA-02, G-RISK-06, G-OPS-14, C-INFRA-07, C-INFRA-11 |
| `core.di.on_commit` | aja.sdk.effects | kept | 5 | callback, database | G-UOW-04, G-UOW-05, G-EFFECT-01, G-BLOCK-03, G-CONTRACT-02, C-EFFECT-02 |
| `core.di.operation_class` | deny-by-default + snapshot (G-ROUTE-01/04) | adr0009 | 27 | name | G-IDEM-13, G-OPCLASS-04, G-GUARD-03 |
| `core.di.operation_key` | aja.sdk.effects (also aja.core.di) | kept | 29 | parts |  |
| `core.di.password_hasher` | aja.sdk.security | kept | 5 |  |  |
| `core.di.publish_event` | aja.sdk.events | kept | 17 | channel, payload, schema_version, database, required | G-UOW-06, G-OUTBOX-01, G-OUTBOX-02, G-OUTBOX-03, G-OUTBOX-04, G-MODULE-04 … |
| `core.di.publish_event_versions` | aja.sdk.events | kept | 0 | payloads, database, bus, required | G-OUTBOX-02, G-OUTBOX-03, C-EVENT-01 |
| `core.di.rate_limiter` | aja.sdk.web | kept | 3 | requests, period, fail_closed |  |
| `core.di.record_audit` | aja.sdk.effects | kept | 22 | action, actor_id, actor_type, resource_type, resource_id, data, required, database | G-UOW-06, G-OUTBOX-03, G-AUDIT-01, G-AUDIT-02, G-MODULE-08 |
| `core.di.register_event_consumer` | ModuleAPI.register_event_consumer (same parameters) | replaced | 3 | core, channel, handler, name, durable, database, required, group, start_from, bind_tenant_from, dead_letter | G-MODULE-04, C-EVENT-02 |
| `core.di.rehash_password_if_needed` | aja.sdk.security | kept | 1 | plain_password, stored_hash, hasher |  |
| `core.di.revocation_at_commit` | aja.sdk.web.at_commit (ADR-0009) | renamed | 3 |  | G-REVOKE-17, C-AUTH-07 |
| `core.di.revocation_freshness` | every route strict (G-REVOKE-21) | adr0009 | 2 | max_staleness_s | G-REVOKE-13, G-REVOKE-16, G-OPCLASS-02, G-OPCLASS-04 |
| `core.di.revoke_user_tokens` | aja.sdk.security | kept | 4 | user_id, valid_after | G-REVOKE-15 |
| `core.di.revoke_user_tokens_on_commit` | aja.sdk.security | kept | 6 | user_id, reason, valid_after, database, require_immediate | G-REVOKE-04 |
| `core.di.run_async` | aja.sdk.effects | kept | 3 | coro | G-TASK-04, C-WORKER-04 |
| `core.di.run_password_hashing` | aja.sdk.security | kept | 2 | fn, args |  |
| `core.di.saga_status` | aja.sdk.sagas | kept | 5 | definition, saga_id | C-SAGA-01 |
| `core.di.scoped` | aja.sdk.tenant | kept | 25 | stmt, model | G-TENANT-06, G-TENANT-07, G-TENANT-16, G-TENANT-18, G-RLS-08, G-RLS-12 … |
| `core.di.settings` | aja.sdk.infra | kept | 48 |  | G-TENANT-14, G-AUTH-03, G-AUTH-15, G-HTTP-03, G-HTTP-05, G-RPC-29 … |
| `core.di.start_saga` | aja.sdk.sagas | kept | 1 | definition, saga_id, input | G-SAGA-05, C-SAGA-01 |
| `core.di.strict_revocation` | every route strict (G-REVOKE-21) | adr0009 | 20 |  | G-REVOKE-16, G-OPCLASS-04 |
| `core.di.subject_commit_check` | aja.sdk.security | kept | 2 | request | G-REVOKE-17, C-AUTH-07 |
| `core.di.tenant_filter` | aja.sdk.tenant | kept | 0 | model | G-TENANT-07 |
| `core.di.unblock_user` | aja.sdk.security | kept | 2 | user_id, reason, actor_id, database | G-BLOCK-03, C-AUTH-04 |
| `core.di.unscoped` | aja.sdk.tenant | kept | 5 |  | G-TENANT-01, G-TENANT-05, G-TENANT-17, G-RLS-03, C-TENANT-03 |
| `core.module_api.AUTO` | aja.sdk.module | kept | 0 |  |  |
| `core.module_api.BACKWARD` | aja.sdk.contracts | kept | 0 |  |  |
| `core.module_api.ContractArgumentError` | aja.sdk.errors | kept | 1 | detail, status_code, headers, code | G-CONTRACT-04, G-CONTRACT-06 |
| `core.module_api.ContractCallError` | aja.sdk.errors | kept | 1 | detail, status_code, headers, code | G-CONTRACT-09 |
| `core.module_api.ContractCallInProgress` | aja.sdk.errors | kept | 0 | detail, status_code, headers, code | G-CMD-02 |
| `core.module_api.ContractIdempotencyConflict` | aja.sdk.errors | kept | 0 | detail, status_code, headers, code | G-CMD-03 |
| `core.module_api.ContractIdempotencyKeyMissing` | aja.sdk.errors | kept | 1 | detail, status_code, headers, code | G-CONTRACT-03 |
| `core.module_api.ContractIdempotencyUnavailable` | aja.sdk.errors | kept | 0 | detail, status_code, headers, code | G-CMD-09 |
| `core.module_api.ContractMethodRuntime` | aja.sdk.contracts (advanced: provider binding and the runtime) | kept | 0 | name, kind, signature, annotations, returns, idempotency_key, idempotency_scope |  |
| `core.module_api.ContractMethodSpec` | aja.sdk.contracts | kept | 0 | kind, idempotency_key, database, planned, idempotency_scope |  |
| `core.module_api.ContractProvider` | aja.sdk.contracts (advanced: provider binding and the runtime) | kept | 0 | impl, core, database, name |  |
| `core.module_api.ContractReadScope` | aja.sdk.contracts | kept | 0 | memo, idle_seconds |  |
| `core.module_api.ContractReadScopeMiddleware` | aja.sdk.contracts (advanced: provider binding and the runtime) | kept | 0 | app |  |
| `core.module_api.ContractResultNotReplayable` | aja.sdk.errors | kept | 0 | detail, status_code, headers, code | G-CMD-06 |
| `core.module_api.ContractSchema` | aja.sdk.contracts | kept | 0 | methods, hash |  |
| `core.module_api.DEFAULT_CONTRACT_DTO_PACKAGES` | aja.sdk.contracts | kept | 0 |  |  |
| `core.module_api.FORWARD` | aja.sdk.contracts | kept | 0 |  |  |
| `core.module_api.ModuleAPI` | aja.sdk.module | kept | 14 | facade, draft |  |
| `core.module_api.ModuleRegistrationConflict` | aja.sdk.errors | kept | 0 | args, kwargs |  |
| `core.module_api.ModuleRegistrationDraft` | aja.sdk.testing | kept | 1 |  |  |
| `core.module_api.ReadContractWriteError` | aja.sdk.errors | kept | 0 | args, kwargs | G-CONTRACT-02 |
| `core.module_api.Saga` | aja.sdk.sagas | kept | 1 | name, version, input, deadline, steps, compensation_deadline | G-SAGA-03, C-SAGA-01 |
| `core.module_api.SagaContext` | aja.sdk.sagas | kept | 1 | saga_type, saga_id, input, results, synthetic |  |
| `core.module_api.SagaDefinitionError` | aja.sdk.errors | kept | 0 | args, kwargs |  |
| `core.module_api.StepKind` | aja.sdk.sagas | kept | 0 | args, kwds |  |
| `core.module_api.bind_contract` | aja.sdk.contracts (advanced: provider binding and the runtime) | kept | 0 | impl, core, database, name |  |
| `core.module_api.bind_contract_session` (dup) | aja.sdk.testing | kept | 13 | session |  |
| `core.module_api.contract_dto_problems` | aja.sdk.contracts | kept | 0 | name, impl, provider_module, module_packages, shared_packages |  |
| `core.module_api.contract_error` | aja.sdk.contracts | kept | 0 | status_code, detail, headers, code |  |
| `core.module_api.contract_kind` | aja.sdk.contracts | kept | 0 | method |  |
| `core.module_api.contract_method` | aja.sdk.contracts | kept | 12 | fn, command, idempotency_key, idempotency_scope, database, planned | G-CONTRACT-05 |
| `core.module_api.contract_read_scope` | aja.sdk.contracts | kept | 0 | memo, idle_ms |  |
| `core.module_api.contract_reads` | aja.sdk.contracts | kept | 1 |  |  |
| `core.module_api.contract_schema` | aja.sdk.contracts | kept | 0 | impl |  |
| `core.module_api.contract_session` (dup) | aja.sdk.data.db() (inside a contract method) | replaced | 11 |  | G-RLS-12 |
| `core.module_api.contract_session_bound` | aja.sdk.contracts | kept | 0 |  |  |
| `core.module_api.contract_spec` | aja.sdk.contracts | kept | 0 | method |  |
| `core.module_api.current_contract_read_scope` | aja.sdk.contracts | kept | 0 |  |  |
| `core.module_api.define_module` | aja.sdk.module | kept | 15 | package, version, optional, consumes, capabilities, depends_on, migrations_database, migrations_chain, health_check, setup, name, router, events, issues_tokens | G-MODULE-14, G-MODULE-18, C-MODULE-04 |
| `core.module_api.invalidate_contract_read_memo` | aja.sdk.contracts | kept | 1 |  |  |
| `core.module_api.local_step` | aja.sdk.sagas | kept | 1 | name, apply, compensate, kind, deadline | C-SAGA-01 |
| `core.module_api.normalize_contract_error` | aja.sdk.contracts (advanced: provider binding and the runtime) | kept | 0 | exc |  |
| `core.module_api.operation_key` (dup) | aja.sdk.effects (also aja.core.di) | kept | 29 | parts |  |
| `core.module_api.run_contract_call` | aja.sdk.contracts (advanced: provider binding and the runtime) | kept | 0 | func, args, kwargs, spec, core, database, contract |  |
| `core.module_api.step` | aja.sdk.sagas | kept | 30 | name, call, args, compensate, compensate_args, kind, refuse_on, check_args, deadline | G-AUTH-14, G-BILLING-19, G-RPC-30, G-SAGA-04, G-SAGA-06, G-SAGA-07 … |
| `core.descriptors.BACKWARD` (dup) | aja.sdk.contracts | kept | 0 |  |  |
| `core.descriptors.CapabilityRequirement` | aja.sdk.module | kept | 3 | capability, instance, version, features, required | G-INFRA-06, G-RISK-06 |
| `core.descriptors.CommandStep` | aja.sdk.sagas | kept | 0 | name, call, args, compensate, compensate_args, kind, refuse_on, check_args, deadline |  |
| `core.descriptors.ContractRequirement` | aja.sdk.module | kept | 4 | name, required, reason, methods | G-CONTRACT-12, G-COMPAT-05, G-SAGA-02 |
| `core.descriptors.Direction` | aja.sdk.sagas | kept | 1 | args, kwargs |  |
| `core.descriptors.FORWARD` (dup) | aja.sdk.contracts | kept | 0 |  |  |
| `core.descriptors.FailedModule` | aja.sdk.module (loader results) | kept | 0 | name, error, required |  |
| `core.descriptors.HealthCheck` | aja.sdk.module | kept | 0 | args, kwargs |  |
| `core.descriptors.HealthResult` | aja.sdk.module | kept | 0 | status, details |  |
| `core.descriptors.HealthStatus` | aja.sdk.module | kept | 0 | args, kwargs |  |
| `core.descriptors.LocalStep` | aja.sdk.sagas | kept | 0 | name, apply, compensate, kind, deadline |  |
| `core.descriptors.MigrationTargetError` | aja.sdk.module (loader results) | kept | 0 | args, kwargs |  |
| `core.descriptors.ModuleDescriptor` | aja.sdk.module | kept | 2 | name, version, optional, routes, health_check, migrations_path, depends_on, capabilities, migrations_database, migrations_chain, consumes, issues_tokens | G-CONTRACT-12 |
| `core.descriptors.ModuleLoadSummary` | aja.sdk.module (loader results) | kept | 0 | loaded, skipped, failed, duplicates, explicit, consumes |  |
| `core.descriptors.Saga` (dup) | aja.sdk.sagas | kept | 1 | name, version, input, deadline, steps, compensation_deadline | G-SAGA-03, C-SAGA-01 |
| `core.descriptors.SagaContext` (dup) | aja.sdk.sagas | kept | 1 | saga_type, saga_id, input, results, synthetic |  |
| `core.descriptors.SagaDefinitionError` (dup) | aja.sdk.errors | kept | 0 | args, kwargs |  |
| `core.descriptors.SagaStep` | aja.sdk.sagas | kept | 0 |  |  |
| `core.descriptors.SkippedModule` | aja.sdk.module (loader results) | kept | 0 | name, reason |  |
| `core.descriptors.StepKind` (dup) | aja.sdk.sagas | kept | 0 | args, kwds |  |
| `core.descriptors.local_step` (dup) | aja.sdk.sagas | kept | 1 | name, apply, compensate, kind, deadline | C-SAGA-01 |
| `core.descriptors.operation_key` (dup) | aja.sdk.effects (also aja.core.di) | kept | 29 | parts |  |
| `core.descriptors.saga_contract_requirements` | aja.sdk.sagas | kept | 0 | saga |  |
| `core.descriptors.saga_step_key` | aja.sdk.sagas | kept | 0 | saga_type, saga_id, step, direction |  |
| `core.descriptors.step` (dup) | aja.sdk.sagas | kept | 30 | name, call, args, compensate, compensate_args, kind, refuse_on, check_args, deadline | G-AUTH-14, G-BILLING-19, G-RPC-30, G-SAGA-04, G-SAGA-06, G-SAGA-07 … |
| `core.descriptors.synthetic_instance` | aja.sdk.sagas | kept | 0 | model |  |
| `core.orm.Base` | aja.sdk.data | kept | 35 | kwargs |  |
| `core.orm.InvalidCursor` | aja.sdk.data | kept | 0 | detail |  |
| `core.orm.JoinRead` | aja.sdk.data | kept | 6 | entity, result, by, tenant_scoped, database, name, lock, join, via, join_by, join_where, joined_fields |  |
| `core.orm.ListOperation` | aja.sdk.data | kept | 16 | entity, result, by, tenant_scoped, database, name, lock, where, order_by, descending, max_limit, require_index, join, via, join_by, join_where, joined_fields, optional, prefix, prefix_ignore_case |  |
| `core.orm.Page` | aja.sdk.data | kept | 1 | items, next_cursor |  |
| `core.orm.PageLimitError` | aja.sdk.data | kept | 0 | detail |  |
| `core.orm.ReadOperation` | aja.sdk.data | kept | 5 | entity, result, by, tenant_scoped, database, name, lock |  |
| `core.orm.Reads` | aja.sdk.data | kept | 2 | core, unit, adapter, shared, contract, isolation |  |
| `core.orm.TenantScoped` | aja.sdk.data | kept | 20 | args, kwargs | G-TENANT-01, G-TENANT-02, G-TENANT-04, G-TENANT-05, G-TENANT-06, G-TENANT-08 … |
| `core.orm.disable_tenant_rls` | aja.sdk.data | kept | 4 | op, table, schema | G-RLS-07, C-TENANT-05 |
| `core.orm.enable_tenant_rls` | aja.sdk.data | kept | 7 | op, table, column, schema, system_access, system_role | G-RLS-07, G-RLS-08, G-PROFILE-05, C-TENANT-05 |
| `core.orm.make_cursor` | aja.sdk.data | kept | 0 | op, position, key |  |
| `core.orm.session_reads` | aja.sdk.data | kept | 4 | session |  |
| `core.projections.EventEnvelope` (dup) | aja.sdk.events | kept | 2 | args, kwargs |  |
| `core.projections.ProjectionConsumerDescriptor` | aja.sdk.projections | kept | 2 | name, source_channel, projection, handler, durable, required, module, group, start_from, bind_tenant_from |  |
| `core.projections.ProjectionHandler` | aja.sdk.projections | kept | 0 | args, kwargs |  |
| `core.projections.ProjectionRebuildResult` | aja.sdk.projections | kept | 1 | projection, rows, cleared, details |  |
| `core.projections.ProjectionRebuilder` | aja.sdk.projections | kept | 1 | args, kwargs | C-PROJ-05 |
| `core.projections.ProjectionRegistryError` | aja.sdk.projections | kept | 0 | args, kwargs |  |
| `core.projections.QueryProjectionDescriptor` | aja.sdk.projections | kept | 1 | name, owner_module, version, source_events, storage, consistency, rebuildable | C-PROJ-05 |
| `core.projections.QueryProjectionRegistry` | aja.sdk.projections | kept | 0 |  |  |
| `core.projections.make_consumer_callback` | aja.sdk.projections | kept | 1 | descriptor, storage, opener |  |
| `core.projections.monotonic_delete` | aja.sdk.projections | kept | 0 | session, target, key, version_column, incoming_version, values, insert_only, deleted_column, deleted_at |  |
| `core.projections.monotonic_update` | aja.sdk.projections | kept | 1 | session, target, match, values, version_column, incoming_version | G-TENANT-18, C-PROJ-01 |
| `core.projections.monotonic_upsert` | aja.sdk.projections | kept | 4 | session, target, key, values, version_column, incoming_version, insert_only | G-TENANT-18, C-PROJ-01, C-PROJ-02, C-PROJ-04 |
| `core.projections.start_projection_consumers` | aja.sdk.projections | kept | 0 | core, session_opener |  |
| `core.facade.CLAIMS_CONTRACT_SUFFIX` | aja.sdk.infra | kept | 0 |  |  |
| `core.facade.ClaimsContributorContract` | aja.sdk.infra (advanced) | kept | 0 | facade, owner |  |
| `core.facade.ClaimsUnavailable` (dup) | aja.sdk.security | kept | 1 | owner | G-AUTH-11 |
| `core.facade.CoreFacade` | aja.sdk.infra (advanced) | kept | 1 | app, settings_obj, service_registry, get_db |  |
| `core.facade.CoreRuntime` (dup) | aja.sdk.infra | kept | 6 | facade |  |
| `core.facade.FACTS_CONTRACT_SUFFIX` | aja.sdk.infra | kept | 0 |  |  |
| `core.facade.ShutdownTask` | aja.sdk.infra | kept | 0 |  |  |
| `core.facade.StartupTask` | aja.sdk.infra | kept | 0 |  |  |
| `core.facade.claims_contract_name` | aja.sdk.infra | kept | 0 | owner |  |
| `core.facade.facts_contract_name` | aja.sdk.infra | kept | 0 | owner |  |
| `shared.authz.ACTIVE_SUBSCRIPTION_STATUSES` | aja.sdk.authz | kept | 1 |  |  |
| `shared.authz.AccessDecision` | aja.sdk.authz | kept | 0 | allowed, code, category, reason, http_status, failed_requirement, policy_code, metadata, indeterminate |  |
| `shared.authz.AccessDenied` | aja.sdk.authz | kept | 0 | decision, status_code |  |
| `shared.authz.AccessFact` | aja.sdk.authz | kept | 2 | name, owner_module, claim_key, projection_reader, live_service, live_resolver, min_freshness, default_mode, deny_category, claim_rule |  |
| `shared.authz.And` | aja.sdk.authz | kept | 1 | parts |  |
| `shared.authz.AuthzContext` | aja.sdk.authz | kept | 1 | subject, request, db, resource, loader, source, core |  |
| `shared.authz.Category` | aja.sdk.authz | kept | 2 | args, kwargs |  |
| `shared.authz.ClaimRule` | aja.sdk.authz | kept | 1 | kind, values | G-AUTHZ-04 |
| `shared.authz.Credits` | aja.sdk.authz: required of=Account.user()/org(param=)/bound_tenant() replaces tenant_type + org_param (same resolutions); other parameters kept | changed | 14 | minimum, tenant_type, org_param, adapter | G-AUTHZ-01, G-AUTHZ-03, C-AUTH-06 |
| `shared.authz.ENTITLEMENTS` | aja.sdk.authz | kept | 0 |  |  |
| `shared.authz.Entitled` | aja.sdk.authz | kept | 4 | fact_name, category, code, mode | C-AUTH-06 |
| `shared.authz.EntitlementsAdapter` | aja.sdk.authz | kept | 0 | args, kwargs |  |
| `shared.authz.FactDenied` | aja.sdk.authz | kept | 1 | reason, category, code |  |
| `shared.authz.FactOwnerUnreachable` | aja.sdk.authz | kept | 0 | args, kwargs |  |
| `shared.authz.FactUnavailable` | aja.sdk.authz | kept | 1 | args, kwargs |  |
| `shared.authz.Feature` | aja.sdk.authz: required of=Account.user()/org(...) added; claim_key and every other parameter kept | changed | 1 | key, claim_key |  |
| `shared.authz.MEMBERSHIP` | aja.sdk.authz | kept | 0 |  |  |
| `shared.authz.MembershipAdapter` | aja.sdk.authz | kept | 0 | args, kwargs |  |
| `shared.authz.Not` | aja.sdk.authz | kept | 6 | inner |  |
| `shared.authz.Or` | aja.sdk.authz | kept | 0 | parts |  |
| `shared.authz.OrgRole` | aja.sdk.authz | kept | 4 | minimum, org_param, adapter |  |
| `shared.authz.Owner` | aja.sdk.authz | kept | 6 | get_owner | C-AUTH-06 |
| `shared.authz.Perm` | aja.sdk.authz | kept | 0 | perm | C-AUTH-06 |
| `shared.authz.Permissions` | aja.sdk.authz | kept | 4 | args, kwargs |  |
| `shared.authz.PolicyIndeterminate` | aja.sdk.authz | kept | 0 | decision |  |
| `shared.authz.Requirement` | aja.sdk.authz | kept | 0 | args, kwargs |  |
| `shared.authz.Role` | aja.sdk.authz | kept | 26 | role | C-AUTH-06 |
| `shared.authz.Roles` | aja.sdk.authz | kept | 6 | args, kwargs |  |
| `shared.authz.SOURCE_HTTP` | aja.sdk.authz | kept | 0 |  |  |
| `shared.authz.SOURCE_INTERNAL` | aja.sdk.authz | kept | 0 |  |  |
| `shared.authz.SOURCE_SYSTEM` | aja.sdk.authz | kept | 0 |  |  |
| `shared.authz.SOURCE_TASK` | aja.sdk.authz | kept | 0 |  |  |
| `shared.authz.Subscribed` | aja.sdk.authz: required of=Account.user()/org(param=)/bound_tenant() replaces tenant_type + org_param (same resolutions); other parameters kept | changed | 5 | statuses, claim_key, tenant_type, org_param | G-AUTHZ-01, G-AUTHZ-02, C-AUTH-06 |
| `shared.authz.SubscriptionActive` | aja.sdk.authz: required of=Account.user()/org(param=)/bound_tenant() replaces tenant_type + org_param (same resolutions); other parameters kept | changed | 4 | tenant_type, org_param, adapter | G-AUTHZ-03, C-AUTH-06 |
| `shared.authz.TIER_CLAIM` | aja.sdk.authz | kept | 0 |  |  |
| `shared.authz.TIER_LIVE` | aja.sdk.authz | kept | 0 |  |  |
| `shared.authz.TIER_RESOURCE` | aja.sdk.authz | kept | 0 |  |  |
| `shared.authz.WithinLimit` | aja.sdk.authz: required of=Account.user()/org(...) added; claim_key and every other parameter kept | changed | 0 | key, used, claim_key |  |
| `shared.authz.authorize` | aja.sdk.authz | kept | 12 | resource, policy, ctx, request, db, status_code | G-BILLING-01 |
| `shared.authz.authorize_resource` | aja.sdk.authz | kept | 0 | loader, by, policy, cast, not_found_detail, status_code |  |
| `shared.authz.current_context` | aja.sdk.authz | kept | 5 | request, credentials |  |
| `shared.authz.enforce` | aja.sdk.authz | kept | 1 | policy, subject, db, request, resource, loader, source, core, policy_code | C-AUTH-06 |
| `shared.authz.lookup_access_fact` | aja.sdk.authz | kept | 0 | ctx, name |  |
| `shared.authz.make_rebuild_gate` | aja.sdk.authz | kept | 0 | rebuilder, name |  |
| `shared.authz.register_authz_projection` | aja.sdk.authz | kept | 0 | core, descriptor, consumers, rebuilder, rebuild_on_startup, critical |  |
| `shared.authz.register_live_adapter` | aja.sdk.authz | kept | 0 | kind, adapter |  |
| `shared.authz.require` | aja.sdk.authz | kept | 17 | policy, status_code | G-AUTHZ-02, G-AUTHZ-06, G-BILLING-18, G-OPS-12, C-AUTH-06 |
| `shared.authz.resolve_fact` | aja.sdk.authz | kept | 0 | fact, ctx, mode |  |
| `shared.authz.service_subject` | aja.sdk.authz | kept | 0 | roles |  |
| `shared.authz.system_subject` | aja.sdk.authz | kept | 0 | roles |  |
| `shared.guards.auth.get_current_user_id` | aja.sdk.web.CurrentUser (.id); the function stays as aja.sdk.web.current_user_id | replaced | 21 | subject |  |
| `shared.guards.platform.require_platform_admin` | aja.sdk.web | kept | 3 | subject |  |
| `shared.schemas.context.UserContext` | aja.shared.schemas (unchanged, contract DTO package) | kept | 3 | data |  |
| `shared.schemas.membership.MemberEventAction` | aja.shared.schemas (unchanged, contract DTO package) | kept | 0 | args, kwargs |  |
| `shared.schemas.membership.MemberState` | aja.shared.schemas (unchanged, contract DTO package) | kept | 3 | data |  |
| `shared.schemas.membership.member_event_action` | aja.shared.schemas (unchanged, contract DTO package) | kept | 0 | payload, stored_version |  |
| `shared.schemas.membership.member_event_version` | aja.shared.schemas (unchanged, contract DTO package) | kept | 0 | payload |  |
| `shared.schemas.membership.refreshed_state_wins` | aja.shared.schemas (unchanged, contract DTO package) | kept | 0 | state, stored_version |  |
| `core.testing.BootOutcome` | aja.sdk.testing | kept | 0 | module, started, missing_contracts |  |
| `core.testing.Captured` | aja.sdk.testing | kept | 1 | args, kwargs |  |
| `core.testing.FakeClock` | aja.sdk.testing | kept | 0 | start |  |
| `core.testing.Headers` | aja.sdk.testing | kept | 1 | args, kwargs |  |
| `core.testing.InMemoryCache` | aja.sdk.testing | kept | 2 | broken |  |
| `core.testing.MigrationChain` | aja.sdk.testing (advanced) | kept | 0 | key, module, script_location, version_table |  |
| `core.testing.ModuleBootError` | aja.sdk.testing | kept | 0 | args, kwargs |  |
| `core.testing.RecordingSession` | aja.sdk.testing | kept | 0 | info, audits, events |  |
| `core.testing.RegistrationSurface` | aja.sdk.testing | kept | 1 | descriptor, calls |  |
| `core.testing.RoutePolicy` | aja.sdk.testing | kept | 0 | always_strict, declares_budget, staleness_budget |  |
| `core.testing.SagaHarness` | aja.sdk.testing | kept | 0 | core |  |
| `core.testing.SagaRun` | aja.sdk.testing | kept | 0 | definition, instance, calls, keys, steps, transitions, crashes |  |
| `core.testing.SimulatedCrash` | aja.sdk.testing | kept | 0 | args, kwargs |  |
| `core.testing.TestResourceOwnershipError` | aja.sdk.testing (advanced) | kept | 0 | args, kwargs |  |
| `core.testing.apply_worker_env` | aja.sdk.testing (advanced) | kept | 0 | environ, create_database |  |
| `core.testing.assert_operation_classes` | aja.sdk.testing.assert_route_snapshot (C-TEST-07) | adr0009 | 4 | app | G-OPCLASS-01, C-TEST-04 |
| `core.testing.assert_route_snapshot` | aja.sdk.testing | kept | 3 | app, modules |  |
| `core.testing.bind_test_uow` | aja.sdk.testing | kept | 1 | session, database, outbox, audit | C-EFFECT-02, C-TEST-06 |
| `core.testing.block_mark_key` | aja.sdk.testing | kept | 1 | user_id |  |
| `core.testing.boot_module_alone` | aja.sdk.testing | kept | 7 | module, lifespan |  |
| `core.testing.capture_effects` | aja.sdk.testing | kept | 2 | monkeypatch, modules |  |
| `core.testing.chain_coverage_problems` | aja.sdk.testing (advanced) | kept | 0 | core |  |
| `core.testing.committing_uow` | aja.sdk.testing | kept | 7 | session_factory, database |  |
| `core.testing.deliver_event` | aja.sdk.testing.deliver | renamed | 0 | channel, payload, times, event_id, session, core |  |
| `core.testing.email_of` | aja.sdk.testing | kept | 8 | user_id |  |
| `core.testing.fake_access_fact` | aja.sdk.testing | kept | 0 | name, value, deny_category, core |  |
| `core.testing.flush_owned_redis` | aja.sdk.testing | kept | 2 | client, owner | G-OPS-07 |
| `core.testing.make_user_headers` | aja.sdk.testing.as_user | renamed | 23 | user_id, tenant_id, email, email_verified, tenant, claims |  |
| `core.testing.migrate_module` | aja.sdk.testing | kept | 4 | url, package, revision, downgrade, chain |  |
| `core.testing.migration_chains` | aja.sdk.testing (advanced) | kept | 0 | core |  |
| `core.testing.model_tables` | aja.sdk.testing (advanced) | kept | 0 | core |  |
| `core.testing.open_saga_harness` | aja.sdk.testing | kept | 0 | core |  |
| `core.testing.operation_classes` | aja.sdk.testing.assert_route_snapshot (C-TEST-07) | adr0009 | 1 | app |  |
| `core.testing.override_instance` | aja.sdk.testing | kept | 2 | core, capability, name, driver, options |  |
| `core.testing.record_revocation_checks` | aja.sdk.testing | kept | 1 | monkeypatch |  |
| `core.testing.recording_uow` | aja.sdk.testing | kept | 3 | database | C-EFFECT-02, C-TEST-06 |
| `core.testing.register_event_schemas` | aja.sdk.testing | kept | 0 | channels, target |  |
| `core.testing.registration_surface` | aja.sdk.testing | kept | 3 | register, api |  |
| `core.testing.reload_test_env` | aja.sdk.testing (advanced) | kept | 0 | path | C-TEST-03 |
| `core.testing.revocation_mark_key` | aja.sdk.testing | kept | 3 | user_id |  |
| `core.testing.route_policy` | aja.sdk.testing | kept | 1 | route |  |
| `core.testing.run_on_commit` | aja.sdk.testing | kept | 5 | session, database | G-EFFECT-03, C-EFFECT-02 |
| `core.testing.run_sql` | aja.sdk.testing | kept | 7 | url, sql, params |  |
| `core.testing.set_environment` | aja.sdk.testing | kept | 2 | monkeypatch, name |  |
| `core.testing.stop_revocation_listener` | aja.sdk.testing | kept | 1 |  |  |
| `core.testing.tables_created_by` | aja.sdk.testing (advanced) | kept | 0 | chain |  |
| `core.testing.temporary_database` | aja.sdk.testing (advanced) | kept | 0 | base_url |  |
| `core.testing.upgrade_core_chain` | aja.sdk.testing | kept | 4 | url |  |
| `core.testing.use_security_cache` | aja.sdk.testing | kept | 2 | monkeypatch, provider |  |
| `core.testing.wait_revocation_listener_subscribed` | aja.sdk.testing | kept | 1 | timeout |  |
| `core.testing.worker_env_values` | aja.sdk.testing (advanced) | kept | 0 | path |  |
| `core.contract_testing.ContractCase` | aja.sdk.testing | kept | 4 | method, args, kwargs, tenant, label | C-RPC-05 |
| `core.contract_testing.ContractConformanceError` | aja.sdk.testing | kept | 0 | args, kwargs |  |
| `core.contract_testing.Outcome` | aja.sdk.testing | kept | 1 | ok, value, error |  |
| `core.contract_testing.RpcKeys` | aja.sdk.testing (advanced) | kept | 0 | service_id, kid, private_key, public_key, dh_private_key, dh_public_key |  |
| `core.contract_testing.UNIQUE` | aja.sdk.testing | kept | 14 |  | G-INBOX-01 |
| `core.contract_testing.assert_contract_conformance` | aja.sdk.testing | kept | 4 | core, contract_name, cases, transport | G-CONTRACT-17, C-RPC-05 |
| `core.contract_testing.check_contract_conformance` | aja.sdk.testing | kept | 0 | core, contract_name, cases, transport |  |
| `core.contract_testing.in_process_rpc_client` | aja.sdk.testing | kept | 2 | core, settings |  |
| `core.contract_testing.keyed_rpc_settings` | aja.sdk.testing (advanced) | kept | 0 | cfg, contracts, service_id |  |
| `core.contract_testing.rpc_client_for` | aja.sdk.testing (advanced) | kept | 0 | core, transport, settings |  |
| `core.contract_testing.rpc_env` | aja.sdk.testing (advanced) | kept | 0 | service_id, peers, allowed_calls |  |
| `core.contract_testing.rpc_keys` | aja.sdk.testing (advanced) | kept | 0 | service_id |  |
| `core.contract_testing.rpc_proxy_for` | aja.sdk.testing | kept | 2 | core, contract_name, client, base_url, settings |  |
| `core.contract_testing.rpc_request_headers` | aja.sdk.testing (advanced) | kept | 0 | settings, target, body, http_method, provider, sign |  |
| `core.contract_testing.rpc_response_headers` | aja.sdk.testing (advanced) | kept | 0 | settings, request_headers, status_code, body |  |
| `core.contract_testing.rpc_settings` | aja.sdk.testing | kept | 2 | service_id, peers, allowed_calls |  |
| `core.contract_testing.served_rpc` | aja.sdk.testing (advanced) | kept | 0 | core, app, settings |  |
| `core.app_factory.create_app` (imported directly) | aja.sdk.app (new public module: the app composition root) | kept | 1 imports |  | G-IDEM-12, G-IDEM-13, G-IDEM-14, G-REVOKE-13, G-OPCLASS-02, G-OPCLASS-04 … |
| `core.cli.contracts.main` (imported directly) | aja.sdk.tooling (new public module: CLI entry points for tests and scripts) | kept | 1 imports |  | G-KERNEL-13 |
| `core.config.settings.settings` (imported directly) | aja.sdk.infra | kept | 1 imports |  | G-TENANT-14, G-AUTH-03, G-AUTH-15, G-HTTP-03, G-HTTP-05, G-RPC-29 … |
| `core.migrations.run_module_migrations` (imported directly) | aja.sdk.migrations (new public module) | kept | 9 imports |  | G-MODULE-18, C-MODULE-09, C-MIGRATE-08 |
| `core.migrations.alembic_support.run_migrations` (imported directly) | aja.sdk.migrations (new public module) | kept | 3 imports |  |  |
| `core.signing_keys.generate_private_key` (imported directly) | aja.sdk.security (key generation helpers) | kept | 1 imports |  |  |
| `core.signing_keys.generate_x25519_key` (imported directly) | aja.sdk.security (key generation helpers) | kept | 1 imports |  |  |
| `core.signing_keys.private_pem` (imported directly) | aja.sdk.security (key generation helpers) | kept | 1 imports |  |  |
| `core.signing_keys.public_pem` (imported directly) | aja.sdk.security (key generation helpers) | kept | 1 imports |  |  |
| `core.signing_keys.x25519_private_pem` (imported directly) | aja.sdk.security (key generation helpers) | kept | 1 imports |  |  |
| `core.signing_keys.x25519_public_pem` (imported directly) | aja.sdk.security (key generation helpers) | kept | 1 imports |  |  |
| `core.testing.fixtures.*` (imported directly) | aja.sdk.testing.fixtures (star-import for framework-tree conftests) | kept | 3 imports |  |  |
| `core.testing.migrations.migrate_module` (imported directly) | aja.sdk.testing | kept | 3 imports |  |  |
| `core.testing.migrations.run_sql` (imported directly) | aja.sdk.testing | kept | 3 imports |  |  |
| `core.testing.served.ReservedPort` (imported directly) | aja.sdk.testing (served-process helpers) | kept | 1 imports |  |  |
| `core.testing.served.SpawnedServer` (imported directly) | aja.sdk.testing (served-process helpers) | kept | 1 imports |  |  |
| `core.testing.served.SpawnedServerError` (imported directly) | aja.sdk.testing (served-process helpers) | kept | 1 imports |  |  |
| `core.testing.served.probe` (imported directly) | aja.sdk.testing (served-process helpers) | kept | 1 imports |  | G-RPC-26 |
| `core.testing.served.spawn_served` (imported directly) | aja.sdk.testing (served-process helpers) | kept | 1 imports |  |  |
| `shared.authz.atoms_claims.ACTIVE_SUBSCRIPTION_STATUSES` (imported directly) | aja.sdk.authz | kept | 1 imports |  |  |
| `shared.authz.context.AuthzContext` (imported directly) | aja.sdk.authz | kept | 1 imports |  |  |
| `shared.authz.facts.FactDenied` (imported directly) | aja.sdk.authz | kept | 1 imports |  |  |
| `shared.authz.facts.FactUnavailable` (imported directly) | aja.sdk.authz | kept | 1 imports |  |  |
| `shared.authz.facts.MODE_LIVE` (imported directly) | aja.sdk.authz (fact freshness modes) | kept | 1 imports |  |  |
