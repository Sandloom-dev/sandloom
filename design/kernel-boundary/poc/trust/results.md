# kb-poc-trust: the claims trust model (TA-1) against running code

tm task `kb-poc-trust` (parent `kb-assess`). Builds TA-1 of
[alternatives-c-authz.md](../../_archive/alternatives-c-authz.md) §2.2 (M1–M5, R1, R2) in a patched
kernel copy and runs rows T-1…T-18 of its §8.1, with the TA-0 subset as the control on the same
fixture. It decides nothing; ADR-C decides. Written for validation.md's owner to fold in (this
task does not edit validation.md).

Labels as in the alternatives: **[run]** a transcript in `out/`; **[read]** code read in the
patched copy; **[measured]** `out/perf.txt`.

## 1. Setup

| item | value |
|---|---|
| base kernel | `ceb3d66` (branch `arch/kernel-boundary`) **plus PR #72** (registration ownership, branch `claude/registration-ownership-e74a85`, not merged here): `a51a59d` + `971f80a` cherry-picked in a throwaway worktree. PR #72 *is* small fix A / the M3 base (owner of `AccessFact`, `owner=`, `<m>.claims.v1` / `<m>.facts.v1` checked against the registering module, SANDLOOM-B034). Patch: [`pr72-registration-ownership.patch`](pr72-registration-ownership.patch) |
| candidate | base + [`ta1-kernel.patch`](ta1-kernel.patch) (55 files, +1767/−152, incl. regenerated docs, API baseline, deploy profile; new: `core/auth/claim_trust.py` 550 lines, `core/app_factory/claim_trust_setup.py` 160, `core/tests/test_claim_trust.py`; 15 existing test files migrated). Nothing under `src/` or `packages/` of this checkout was edited |
| fixture | `app/pocapp` = `poc/replace/app` extended: `test_identity` (issuer; now also a durable `core.auth.user_blocked.v1` consumer, R2; `/hand-mint`, `/service-mint`), `forged` (switches: critical, `owner=`, contract squat), `identity` (app-local squat), `grants`, `notes` (RLS), `inventory`, `shop` (+ `/plain`, `Role \| AlwaysAllow`, `~Role`, `Role \| Entitled(missing)`, `~Entitled(missing)`, `Role & Perm`); `app/pocapp/roots/*.py` = one composition root per map (answer #9: code, not settings) |
| processes | real `uvicorn pocapp.roots.<root>:app`; mono = one process; split = P1 (`test_identity`, `notes`, `shop`) + P2 (`grants`, `inventory`, `forged`) over real internal RPC (pair MAC, Ed25519 signatures, `service_id` pins, EdDSA tokens, only P1 holds `JWT_PRIVATE_KEY`); P3 verifier for T-10 |
| venvs | lean wheels built from each worktree's `packages/sandloom` (`[redis]`, no `sandloom_identity`/`tenancy`/`billing`): `venv-kernel` (candidate), `venv-base` (base) |
| services | own containers `kbtrust-pg` (postgres:16-alpine, 127.0.0.1:38533) and `kbtrust-redis` (redis:7-alpine, 127.0.0.1:38480; fixture dbs 3–4, test-run dbs 9–15); `SANDLOOM_TEST_OWNER=kb-poc-trust`; removed after the run |

```bash
SCRATCH=<scratchpad>/poc-trust
git worktree add --detach $SCRATCH/kernel ceb3d66 && (cd $SCRATCH/kernel && git cherry-pick a51a59d 971f80a && git apply <this dir>/ta1-kernel.patch)
git worktree add --detach $SCRATCH/base 2beb38a        # = ceb3d66 + PR #72 (the cherry-picks)
docker run -d --name kbtrust-pg -e POSTGRES_PASSWORD=poc -e POSTGRES_USER=poc -e POSTGRES_DB=poc -p 127.0.0.1:38533:5432 postgres:16-alpine
docker run -d --name kbtrust-redis -p 127.0.0.1:38480:6379 redis:7-alpine
for w in kernel base; do uv build --wheel $SCRATCH/$w/packages/sandloom --out-dir $SCRATCH/dist-$w; uv venv $SCRATCH/venv-$w
  VIRTUAL_ENV=$SCRATCH/venv-$w uv pip install "$SCRATCH/dist-$w/*.whl[redis]" uvicorn httpx asyncpg pytest pytest-asyncio; done
$SCRATCH/venv-kernel/bin/python -I setup_db.py postgresql://poc:poc@127.0.0.1:38533/poc
DATABASE_URL=postgresql+asyncpg://poc:poc@127.0.0.1:38533/poc SANDLOOM_MODULES='[]' ... sandloom migrate upgrade-core --apply
export SANDLOOM_POC_PG=postgresql+asyncpg://poc_app:poc_app@127.0.0.1:38533/poc SANDLOOM_POC_REDIS=redis://127.0.0.1:38480
$SCRATCH/venv-base/bin/python run.py --root base legacy-base-mint            # a real pre-change token (T-9 L0)
POC_BASE_TOKEN=... $SCRATCH/venv-kernel/bin/python run.py T1-mono ... T16    # -> out/ta1.txt
POC_BASE_TOKEN=... $SCRATCH/venv-kernel/bin/python run.py --root ta0 T1-mono T2-mono T9-mono T9-split T10 T11-mono T13-mono   # -> out/ta0.txt
pytest -p sandloom.sdk.testing.plugin -o sandloom_composition_root=pocapp.roots.ta1 tests/test_as_user.py   # -> out/t12-as-user.txt
(cd $SCRATCH/kernel && uv run sandloom test --full)                           # -> out/full-suite.txt
bench/cg_trust.sh, bench/trust_stand.py run                                   # -> out/perf.txt
```

Transcripts: `out/ta1.txt` (every TA-1 row, mono and split), `out/ta0.txt` (control),
`out/t9-base-token.txt` (T-9 L0: a real base-wheel token, TA-1 and TA-0, run within the token's 15-minute life), `out/t12-as-user.txt`, `out/kernel-unit.txt` (73 kernel unit tests incl. the new
`test_claim_trust.py`), `out/full-suite.txt`, `out/perf.txt`. `USER`/`ADMIN`, `T_A`/`T_FOREIGN`
stand for fixed UUIDs.

## 2. What was built (TA-1 in the kernel copy)

| # | mechanism | where (patched copy) |
|---|---|---|
| M1 | `create_app(claim_authorities={key: module path or entry-point name})`; startup refusals SANDLOOM-B035: path names no module (typo), authority neither loaded in the issuing process nor bound, authority's contributor or binding not critical, kernel test authority assigned outside a test composition | `core/auth/claim_trust.py` (`build_policy`, `authority_problems`), `core/app_factory/claim_trust_setup.py`, `core/app_factory/__init__.py` |
| M2 | the merge accepts a protected key only from its authority's step (local: short name **and** the loaded import path; remote: the `<m>.claims.v1` binding); violator rule: an optional violator loses its whole result → `claims_sources_failed`, ERROR log naming contributor and key, counter `claims_contributor_violations_total`; a critical violator refuses the mint naming itself | `core/facade/_claims.py` (`_claim_violation`, `_merge_claim_outcomes`) |
| M3 | PR #72 only (reserved families + fact owner). The widened "every contract prefix" flag was **not built** | — |
| M4 | trusted view applied once per verified token inside `decode_token` (cached with the token, so a cache hit pays nothing extra); every reader (`UserContext`, `bearer_claims` → `TenantScope`, `platform_operator`, idempotency fingerprint) sees it; `UserContext.untrusted_claims` = `{key: reason}`; **one** `platform_admission()` used by `require_platform_admin` and `platform_operator`; claim-mode facts trusted by the claim key's authority (AND the existing owner check) | `claim_trust.ClaimTrustPolicy.view`, `core/auth/tokens.py`, `shared/subject.py`, `shared/guards/platform.py`, `core/admin/api/platform.py`, `shared/authz/facts.py` |
| M5a | `cpv = "1:<sha256(map)[:12]>"` stamped by the merge; no/other `cpv` → every assigned protected key untrusted with reason `token_policy_outdated`; `Role`/`Perm` raise `PolicyIndeterminate(401, code token_policy_outdated)` only then (unassigned / authority absent = definite 403); `TenantScope` answers 401 `token_policy_outdated` instead of 400/403 for such a grant; the first foreign `cpv` per value is logged once | `atoms_claims.py`, `core/http/tenant_scope.py`, `claim_trust._note_foreign_marker` |
| M5b | `collect_claims` returns `SealedClaims` (read-only `Mapping`; a `{**spread}` is a plain dict); `create_access_token(data, claims=sealed)` refuses protected keys, provenance and `cpv` in `data` (SANDLOOM-R017); `create_service_token` refuses every protected key, the tenant selectors and the subject structure and stamps the current `cpv`; kernel test authority (`sandloom-test`, not a valid module short name) enabled when the test harness is imported, `as_user` / `mint_test_token` / `sealed_claims` mint through it; a prod composition refuses it | `tokens.py`, `claim_trust.seal*`, `core/testing/{auth,fixtures,plugin}.py`, `sdk/testing`, `sdk/security` |
| R1 | the durable `core.auth.tokens_revoked.v1` applier registered by the kernel (moved out of identity), group `sandloom:auth@tokens_revoked@<service or database identity>` | `claim_trust_setup.register_kernel_revocation_applier`, `sandloom_identity/__init__.py` |
| R2 | an `issues_tokens=True` module without a durable `core.auth.user_blocked.v1` consumer refuses startup (B035) | `claim_trust_setup.issuer_obligation_problems` |
| TA-0 | `create_app(claim_trust_mode="ta0")`: M1 + M2 + A0-3 (platform admission = `admin` in raw `roles` and the assigned authority in `claims_sources`, not failed); no view, no `cpv`, no sealed mint | same files |
| bundled default | `sandloom.main.BUNDLED_CLAIM_AUTHORITIES` (`roles`/`permissions` → identity, `tenant_ids` → tenancy, `subscription_status`/`plan_code`/`features`/`limits` → billing), filtered to installed modules; billing's contributor made `critical=True` (answer #14); identity mints with `claims=` | `main.py`, `sandloom_billing/__init__.py`, `token_service.py` |

## 3. Results per row

Every row ran mono **and** split unless marked; split = P1/P2 over real RPC.

| # | scenario | mono | split | evidence (trimmed, `out/ta1.txt`) |
|---|---|---|---|---|
| T-1 | assigned `test_identity`, `sandloom_identity` not installed (lean wheel) | **pass** | **pass** | `ADMIN`: `role-admin`, `perm-write`, `role-and-perm`, `require_platform_admin`, kernel `/api/v1/platform/shop/services` all **200**; `/shop/tenant` T_A 200, RLS `note of A`; `USER` 403 on all. Fixture modules import only `sandloom.sdk`; no kernel edit after M1–M5 (the map is the root's argument) |
| T-2 | `forged` (optional, unassigned) writes `roles`, `permissions`, `tenant_ids`, `claims_sources`, `cpv`, `tenant_id` | **pass** | **pass** (remote `forged.claims.v1`) | every mint 200 with the authorities' values only, `claims_sources_failed: ["forged"]`; `forged` writing only its own key (`loyalty_tier`) contributes normally; 6 ERROR lines `claims contributor 'forged' wrote the claim 'roles' (a protected claim assigned to 'test_identity'); its whole contribution is dropped`; metric `sandloom_facade_counter_total{name="claims_contributor_violations_total"} 6.0`; also with `forged` registered first |
| T-3 | `forged` critical writes `roles` | **pass**, both orders | **pass** (critical binding) | mint **503** `Token claims from 'forged' are unavailable` registered after **and before** `test_identity` (log names `forged`, never the authority); own key only → 200 |
| T-4 | optional violator (`roles` + `jti`), 100 logins | **pass** | **pass** | `status counts {200: 100}; tokens with roles=admin: 0`; 100 ERROR lines; counter 100.0 |
| T-5 | authority missing / non-critical / typo | **pass** | **pass** | exit 1, `SANDLOOM-B035: claim 'roles' is assigned to 'pocapp.modules.test_identity', which is neither loaded in this issuing process nor bound (test_identity.claims.v1 is not in SERVICE_BINDINGS)`; `... whose claims contributor is registered critical=False ... (answer #14)`; `... 'pocapp.modules.test_identty', which names no module ... a typo?`; split: `... whose binding grants.claims.v1 is critical: false` |
| T-6 | no assignment (`roots/none.py`); `forged` writes `roles`, `tenant_ids` | **pass** | **pass** | both writers dropped (`claims_sources_failed: ["forged","test_identity"]`); every gate 403; `TenantScope` 400 / 403; `/notes/items` 403 (no RLS rows). With the issuer's contributor **critical** and no assignment: every mint 503 (fail closed, by design) |
| T-7 | local spoofing | **pass** | n/a (local) | (a) app-local `identity`, non-critical, grants admin to everyone: `USER` minted by it gets `roles: []`, `identity` in `claims_sources_failed`, all gates 403; critical → 503 naming `identity`. (b) `owner="identity"` / `owner="test_identity"`: boot refused, B034 (via the facade the PoC reaches it as "registered outside a module's registration"; through `ModuleAPI` there is no `owner=`). (c) registration order: T-2/T-3 forged-first |
| T-8 | remote squat | **pass** | **pass** | (a) P2 hosting `forged` registering `grants.claims.v1` / `test_identity.claims.v1`: P2 boot refused `SANDLOOM-B034: module 'pocapp.modules.forged' registers contract 'grants.claims.v1' under module 'grants'` (PR #72). (b) P1 binds `grants.claims.v1` to P2 without `grants`: mint **503** naming `grants` |
| T-9 | legacy tokens: L1 no `failed`, L2 no provenance, L3 `failed=[]` without `collect_claims`, **L0 a real token minted by the base wheel** | **pass** | **pass** (L1–L3) | L0 in `out/t9-base-token.txt`, L1–L3 in `out/ta1.txt`: `/plain` 200; `Role(admin)` → **401** `{"code":"token_policy_outdated"}`; `Perm` 401; `Role(admin) \| AlwaysAllow` **200**; `~Role(admin)` **401, never allow**; platform gates 401 `token_policy_outdated`; after re-mint: `role-admin`/`perm`/platform 200, `~Role` 403. Log once: `claim policy mismatch: a token minted under cpv=<none: legacy token> ...` |
| T-10 | issuer map A, verifier P3 map B (`roles` → `grants`) | — | **pass** | at P1: 200; at P3: `/plain` 200, `Role`/`Perm`/platform **401** `token_policy_outdated`; startup lines `claim trust: cpv=1:8264c48c7229` (P1) vs `cpv=1:aa015457855a` (P3); one mismatch line at P3 for repeated requests |
| T-11 | hand mint / service token | **pass** | **pass** | `create_access_token({"roles":["admin"],"claims_sources":[...],...})` → `UnsealedClaimsError: SANDLOOM-R017 ... carries claims_sources, claims_sources_failed, roles outside a sealed collect_claims result`; same for `roles`, `cpv`, `tenant_ids`; `create_service_token({"roles":...})` / `({"tenant_id":...})` refused; `create_service_token({})` → `/plain` 200, `Role`/`Perm`/`Role & Perm` 403, both platform gates 403, `TenantScope` 403. Deviation: `email` in `data` is accepted at mint (identity's no-core path); without the sealed `cpv` the verifier drops it (`out/kernel-unit.txt`: `test_issuer_email_without_a_sealed_cpv_is_untrusted`) |
| T-12 | `as_user(roles=["admin"])` in a test app; test authority in a production composition | **pass** | n/a | `tests/test_as_user.py::test_as_user_roles_pass_every_gate PASSED` (`role-admin`, `perm-write`, `role-and-perm`, `platform` 200, plain user 403, default tenant binding 200); `roots/testauth.py` without the harness: `SANDLOOM-B035: claim 'roles' is assigned to the kernel test authority 'sandloom-test', which only a test composition may assign`; harness loaded in a prod composition: `... the kernel test authority is enabled in a production composition` (unit test + `test_guarantee_windows` in the full suite hit it) |
| T-13 | owner regression set | **pass** | **pass**, identical | forged `Role(admin)` 403; platform admin with roles not from the authority 403 (both gates); `Role(admin) \| Entitled(missing.fact)` with trusted role **200**; `~Entitled(missing.fact)` **501** for both subjects (never allow); split table = mono table |
| T-14 | `Role` / `Perm` I/O | **pass** | **pass** | per request (20 requests after 3 warm-ups, PostgreSQL `log_statement=all` by role, Redis `MONITOR` on the fixture's db, P2 access log): `plain`, `Role(admin)`, `Perm`, `Role & Perm`, `TenantScope`: **DB 0, Redis 1 GET (the revocation mark), RPC 0**, mono and split (the `SELECT 0.05` is the marker connection) |
| T-15 | cost | **pass** | — | request rung +0.57 % Ir (micro rung, exact; whole request ±2 % = stand noise), mint +0.68 % Ir (1 rep), no latency difference above noise; §5 |
| T-16 | R1 / R2 | **pass** | **pass** (split conformance) | R2: `SANDLOOM-B035: module 'pocapp.modules.test_identity' issues tokens but registers no durable consumer of core.auth.user_blocked.v1`. R1: `/diagnostics/consumers` without identity lists `auth@tokens_revoked` group `sandloom:auth@tokens_revoked@url:…`, `module: sandloom`, running; `test_kernel_registers_and_runs_the_revocation_applier`: cache out → redelivered, back → mark written, older token 401. The split conformance suite's `test_revocation_across_services_survives_a_failing_mark_write` passes with identity's applier removed |
| T-17 | `sandloom test --full` on the patched copy | **pass except one by-design failure** | | final run 6: **6504 passed, 6 failed**; every other gate PASS (ruff, format, mypy, `sandloom check`, architecture tests, lint-imports, generated files, schema & deploy, contract/route snapshots, migration drift, public API baseline, typing). The 6: `test_the_kernel_names_no_module` (**by design of this PoC**: `sandloom.main` now names identity/tenancy/billing — the decision-A edge, finding 3) and 5 `test_dev_*` (fail identically on the base worktree: my Redis has no two empty indexes). Split conformance (26) and e2e split pass. Route snapshots unchanged; API baseline diff = the reviewed additions (`create_app(claim_authorities=, default_claim_authorities=, claim_trust_mode=)`, `create_access_token(claims=)`, `create_service_token`, `CoreRuntime.collect_claims -> Mapping`, `sdk.testing.mint_test_token`/`sealed_claims`). First run before the migrations: 297 failed / 122 errors (§4) |
| T-18 | TA-0 control | — | — | §4 |

## 4. TA-0 vs TA-1 (same patched kernel, `claim_trust_mode="ta0"`, `out/ta0.txt`)

| row | TA-0 | TA-1 |
|---|---|---|
| T-1, T-2, T-6 (mint side), T-13 (merged tokens) | pass (M2 is shared) | pass |
| T-9 legacy | **fail** (`out/t9-base-token.txt`, `out/ta0.txt`): the real base-wheel token: `role-admin` **200**, `perm-write` **200**, `require_platform_admin` **200**, kernel platform API **200**; L1/L2/L3 `Role` 200 | pass (401 `token_policy_outdated`, `Or` still allows, `~` never) |
| T-10 skew | **fail**: P3 with map B answers `Role`/`Perm`/`Role & Perm` **200** and even the platform gate **200** (map B makes `grants` the `roles` authority, and `grants` is in `claims_sources`): skew is undetectable and can mis-trust | pass |
| T-11 hand mint | **fail**: `create_access_token({"roles":["admin"],"claims_sources":["test_identity"],...})` minted → `Role` 200, `require_platform_admin` 200, kernel platform API 200; `create_service_token({"roles":["admin"]})` → `Role` 200; `({"tenant_id":T_A})` → `TenantScope` **200** | pass |

So TA-0 fails exactly T-9, T-10, T-11, the three rows §7.1 point 1 predicted; each failure is the
missing M4 (read-side view), M5a (`cpv`) or M5b (sealed mint) respectively.

**What the full suite says about migration** (`out/full-suite.txt`). Run 1 (TA-1 as first
written, no test changes): 297 failed / 122 errors, from five causes: (1) 312 × the kernel test
authority not enabled (the framework's `conftest.py` imports `core.testing.fixtures`, not the
plugin — fixed by enabling it on harness import); (2) tests and modules that hand-mint protected
claims with `create_access_token` (≈ 40 call sites in 15 files; migrated to `mint_test_token` /
`sealed_claims`); (3) PR #70's tests asserting "any foreign key → 503" (rewritten to the violator
rule: 52 cases); (4) `as_user(roles=["admin"], claims_sources=[])` meant "roles without identity
provenance" (honoured by letting a test that states `claims_sources` get exactly that provenance
checked); (5) billing's binding `critical: false` in the deploy profile, the split conformance and
e2e fixtures (answer #14 refuses it; profile regenerated with `sandloom add deploy --refresh`).
Plus one kernel bug the suite found: **R1's fixed consumer group collides across services** (§6).

## 5. Cost (T-15)

Base = `ceb3d66` + PR #72, candidate = TA-1, same session, same machine (shared with other
agents: load average 7–44 during the runs). Raw: `out/perf.txt`, `out/latency/`.

**Instructions, the changed path alone** (`bench/cg_micro.sh`, cachegrind, K = 20,000, 3 reps,
spread ±45 Ir): one verified-token cache hit + `UserContext` + `Role & Perm` decided, no I/O.

| kernel | Ir/op | Δ |
|---|---|---|
| base | 152,581 · 152,593 · 152,626 | — |
| TA-1 as first built (empty `sandloom.untrusted` written into every payload) | 162,258 · 162,336 · 162,340 | +9,711 |
| TA-1 final (marker written only when non-empty) | 159,687 | **+7,087** |

A whole `Role & Perm` request is ≈ 1.25 M Ir (below), so the request rung grows by **+0.57 %**
(+0.78 % before the one-line optimisation): **within +1 %**. The cost is the per-request `cpv`
string in the cached payload and `UserContext`'s extra field; the view itself runs once per
token (cache miss), not per request.

**Instructions, whole request and mint** (`bench/cg_trust.sh`, monolith with identity, tenancy,
billing and the stand, K = 200, mint K = 100, **1 rep**, pre-optimisation TA-1):

| case | base Ir/op | TA-1 Ir/op | Δ |
|---|---|---|---|
| control (authenticated, no policy) | 1,146,927 | 1,125,421 | −1.9 % |
| `Role("admin") & Perm(...)` | 1,250,519 | 1,276,742 | +2.1 % |
| `TenantScope()` route | 1,279,324 | 1,262,824 | −1.3 % |
| mint: `collect_claims` (3 contributors, real batteries) + `create_access_token` | 4,220,001 | 4,248,724 | **+0.68 %** |

The whole-request numbers move both ways by about 2 %: that is this stand's run-to-run noise
(perf-dx-baseline §1.2: ±4 %), so the +1 % verdict rests on the micro rung. The mint is within
**+2 %** (one rep; the merge adds one dict lookup per contributed key and the `cpv` stamp).

**Latency** (`bench/trust_stand.py run`, in-process ASGI, concurrency 1, 2,000 requests per case,
200 mints, 3 runs alternating base / TA-1; medians of the runs):

| case | base p50 / p95 / p99 µs | TA-1 p50 / p95 / p99 µs |
|---|---|---|
| control | 1093 / 1466 / 1898 | 1097 / 1413 / 1790 |
| `Role & Perm` | 1188 / 1682 / 2469 | 1166 / 1625 / 2062 |
| `TenantScope` | 1209 / 1670 / 2323 | 1189 / 1644 / 2096 |
| mint | 4971 / 6740 / 8530 | 4891 / 6969 / 8226 |

No latency difference above the machine's noise (p50 per run varies by up to ±20 % for the
mint). **Calls** (T-14): 0 DB, 1 Redis `GET`, 0 RPC per `Role`/`Perm`/`TenantScope` request,
mono and split, as the baseline. Mint: no extra call (the check runs in the existing merge pass).

## 6. Findings the ADR must carry

1. **TA-1 holds its rows; TA-0 fails T-9/T-10/T-11** (§3, §4). Nothing smaller passes.
2. **Answers #10 + #14 make billing critical.** Assigning the access-granting billing claims
   (`subscription_status`, `plan_code`) requires billing's contributor and every
   `billing.claims.v1` binding to be critical, so a billing outage now refuses every login and
   refresh; today it degrades to live checks (`claims_sources_failed` → claim-mode facts escalate).
   The shipped deploy profile and the split conformance bind billing `critical: false`
   deliberately. The owner must choose: (a) billing critical (as built), (b) leave billing claims
   unassigned (the view strips them; `Subscribed` escalates to live — changes I-23 call counts), or
   (c) refine #14: an authority may be non-critical when every reader of its keys treats
   `claims_sources_failed` as indeterminate/escalation (claim-mode facts do; `Role`/`Perm`/`TenantScope`
   do not, so identity/tenancy stay critical).
3. **The bundled default map is a kernel edge.** Putting it in `sandloom.main` fails
   `test_the_kernel_names_no_module` (run 5). It must live in the generated `asgi.py` (or wherever
   decision A puts bundled defaults). A default filtered per process would break `cpv` parity in a
   split; the PoC filters by *installed* modules, and an app's explicit map is strict.
4. **R1 needs a per-deployment group.** A kernel-registered durable consumer with one fixed group
   (`sandloom:auth@tokens_revoked`) is refused by the consumer runtime in a split (one group, two
   databases' inboxes; split conformance and e2e failed with `ConsumerConfigurationError`). Fixed
   by `@<SANDLOOM_SERVICE_NAME or database identity>`: every service applies every revocation
   (idempotent). The kernel can own the channel's applier, but "a kernel `@on`" is not free.
5. **The trusted view belongs in `decode_token`**, computed once per verified token and cached with
   it (policy marker in the cache key). Every bearer reader then sees the same view (incl.
   `TenantScope` and `platform_operator`, which read raw claims), and the request rung pays only a
   larger cached payload.
6. **`token_policy_outdated` reaches gates outside the algebra too**: `TenantScope` and both
   platform gates needed their own 401 mapping; a service token must carry the current `cpv`, or
   its (absent) protected claims read as "refresh" instead of a definite 403.
7. **The test authority needs provenance semantics.** "No stated `claims_sources`" → vouches for
   every key; stated → exactly that provenance is checked. Without the second rule the platform
   tests that model "roles without identity" (23 + 17 cases) would pass wrongly.
8. **Sealed mint scope.** Refusing `email`/`email_verified` at the mint broke identity's no-core
   path; the view alone protects them (dropped without the sealed `cpv`). ADR wording: protected
   keys, provenance and `cpv` are refused at the mint; issuer fields are protected at the read.
9. **Open:** M3 widened to every contract prefix (not built; T5 in-process for non-reserved names
   stays first-registrant); M6 (declared keys, answer #15); `owner=` should leave the facade (it
   is only reachable by bypassing `ModuleAPI`).
10. **Bookkeeping the PoC raised** (would need review in a real PR): lint-suppression ratchet
    C901 81→84, PLC0415 122→127, S105 8→9; two new SANDLOOM codes (B035 boot, R017 runtime);
    `public-source.json` classification of the three new files; the public-API migration map
    entry for `create_service_token`.

## 7. What ADR-C (trust part) must state

1. The composition root assigns each protected claim key to one authority module
   (`create_app(claim_authorities=...)`), written by `sandloom new` into the app's `asgi.py`; the
   kernel holds no default naming a module. Protected = `roles`, `permissions`, `tenant_ids`
   always; `tenant_id`/`org_id` with the authority of `tenant_ids`; every key a kernel atom reads
   and every key the map names; `email`/`email_verified` are issuer fields trusted only under the
   current `cpv`.
2. Mint: a protected key only from its authority's step (local = the module loaded from the
   assigned path; remote = the pinned `<m>.claims.v1` binding); an optional violator loses its whole
   result (failed provenance, ERROR log, counter); a critical violator refuses the mint naming
   itself; startup refuses a typo, an authority absent from the issuing process and unbound, and a
   non-critical authority (with the owner's decision on billing, finding 2).
3. Read: one trusted view per verified token, computed where the token is verified and cached with
   it; every gate (algebra atoms, `TenantScope`, the tenant allowlist, claim-mode facts, the one
   platform admission) reads only the view. A key untrusted because the token predates the policy
   is `PolicyIndeterminate(401, token_policy_outdated)` inside the algebra (`Or` may still allow,
   `~` never), and 401 `token_policy_outdated` at `TenantScope` and the platform gates; an
   unassigned key or an absent authority is a definite deny.
4. `cpv` = policy version + digest of the map, stamped by the merge and by `create_service_token`;
   a different `cpv` is untrusted and logged once per value (split skew detection at zero I/O).
5. Sealed mint: `create_access_token` takes protected keys, provenance and `cpv` only from
   `collect_claims`' sealed result; `create_service_token` grants no user right; the kernel test
   authority exists only in a test composition and is refused in a production one; a test that
   states `claims_sources` gets that provenance checked.
6. Revocation: the kernel registers the durable `core.auth.tokens_revoked.v1` applier with a
   per-deployment group; an `issues_tokens` module without a durable
   `core.auth.user_blocked.v1` consumer refuses startup.
7. Budget: request rung +0.57 % Ir, mint +0.68 % Ir, no extra call; migration = tokens lose
   protected claims until refresh (one 401 with a refresh-aware client), hand-minting tests move to
   the test kit, deployments bind every authority critical.
8. Rejected: TA-0 (fails T-9, T-10, T-11 on running code), TA-2 (answer #9), TA-3 (H-12).
