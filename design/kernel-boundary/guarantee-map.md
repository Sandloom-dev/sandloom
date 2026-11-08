# Kernel boundary: guarantee map

Who provides each platform guarantee, who supplies its data, and what happens without the
battery, with a third-party replacement and in a split deployment. Task: tm `kb-guarantee-map`
(parent `kb-assess`), from [goals.md](goals.md) "Who owns what". This file describes; it
proposes nothing.

Commit: `arch/kernel-boundary` HEAD `f5fa478`. The kernel source at HEAD is byte-identical to
`b8d8506` ([baseline.md](baseline.md)): no commit since touches `src/`. Every `file:line` is at
HEAD; paths are under `src/sandloom/` unless they start with `sandloom_<m>/` (`src/`) or name a
repository file. PR #70 (`claude/roles-claim-trust-794d62`, `044c198..4bc45ba`, in review) is
not merged; where it changes a cell, the cell says so (**PR #70:**).

## 1. Method and terms

The five questions, one column each:

1. **Provider**: the kernel code that gives the guarantee, and its ledger ids (`guarantees.yml`).
2. **Data supplier**: the battery, contract, claim or setting the guarantee reads.
3. **Without the battery**: fail closed, refuse to start, or degrade (goals.md forbids the last).
4. **Third-party replacement**: does the guarantee hold with no kernel change, and what must the
   replacement provide.
5. **Split deployment**: who evaluates, what crosses the wire, what is lost.

Status of each cell: **verified** (read at the cited line and, where a probe is named, run),
**inferred** (follows from verified lines, not observed), **unknown**.

Probes (scripts and trimmed transcripts in `_evidence/guarantee-map/`):

| environment | what | used by |
|---|---|---|
| E1 lean | the `sandloom` wheel built at `b8d8506` alone (`kb-close/venv-lean`, baseline §13.1), no module installed | `claims-trust-probe.txt` kernel A |
| E2 PR #70 overlay | E1 with only `core/facade/_claims.py` replaced by PR #70's (`4bc45ba`) | `claims-trust-probe.txt` kernel B |
| E3 lean + redis | the same wheel with the `redis` extra (no celery, no module), throwaway `postgres:16-alpine` and `redis:7-alpine` containers; app-local stand-in modules `shop.modules.{identity,accounts,orgs,inventory}` | `boot-probe.txt`, `compat-probe.txt`, `cli-without-identity.txt`, `settings-and-tasks-probe.txt` |
| E4 repository | `uv run` in the checkout (every bundled module installed) | contrast lines in `boot-probe.txt`, `compat-probe.txt` |

The stand-ins: `identity` is a third-party token issuer that happens to be named `identity`;
`accounts` is the same code under its own name; `orgs` replaces tenancy and serves
`tenancy.membership.v1`; `inventory` is a business module whose claims contributor writes
`tenant_id`. No split deployment (two processes over RPC) was run in this pass: column 5 is
read from code and from the ledger's split tests.

## 2. Summary: one line per mechanism

| mechanism | provider | data supplier | without the battery | replacement | split |
|---|---|---|---|---|---|
| JWT | kernel mints, verifies, revokes, blocks (`core/auth/*`) | identity: claims, `sub`, mint call, durable revocation applier, operator contract | no issuer, no tokens: fail closed; **the durable revocation applier disappears silently** | works; the kernel cannot see whether the issuer collects claims, checks the block or applies revocations; any code holding the key mints anything | key placement refused at startup when wrong; marks shared through `CACHE_URL` and the bus |
| claims | kernel merge, provenance, `<owner>.claims.v1` | identity `roles`/`permissions`, tenancy `tenant_ids`, billing `subscription_status` | first writer owns a key (PR #70) or last writer wins (HEAD); no authority | works; trust follows the owner's **name** (`identity`) | remote owner name = binding key text |
| authorization | kernel algebra, atoms, facts, live adapters, platform gate | identity roles; billing fact and `billing.entitlements.v1`; tenancy `tenancy.membership.v1` | live atoms 501, platform gate 403 (fail closed); **`Subscribed` and `Role`/`Perm` trust any writer** | must squat `billing.*`/`tenancy.*` contract names or the process-wide adapter slot; platform gate only by the name `identity` | evaluated in the caller; facts' owner is the provider's own word |
| tenant isolation | kernel ORM guard, RLS, `TenantScope`, RPC context | tenancy `tenant_ids`; nobody writes `tenant_id`/`org_id` | no claim: 400/403, fail closed | must write the kernel's key `tenant_ids`; **any contributor can grant any tenant through `tenant_id`** (HEAD and PR #70) | tenant bound only from the MAC-covered context |
| contracts | kernel registry, RPC, compat check | provider modules; stubs/DTOs in the kernel wheel | required: refuse to start; optional: `None` | serve under the original module's name; no name ownership; **compat check off with a warning** | SERVICE_BINDINGS + `service_id` + RPC_ALLOWED_CALLS: the one explicit app trust assignment |
| admin | kernel platform API per module, gate, CLI | identity (gate source, `identity.security.v1`), admin (console, manifest) | API 403, CLI refuses with a message | a replacement issuer passes the gate only if named `identity`; no manifest for another console | each service mounts its own platform API; gate from the bearer |
| tasks | kernel worker, enqueue dedupe, outbox relay | celery extra, modules' `tasks.py` | taskqueue unusable, `/ready` 200 with a warning; a module importing `sdk.tasks` fails to load | taskqueue is a capability driver | per-service worker and beat lease |
| settings | kernel `Settings`, tiers, validators, risk registry | 22 module settings live in the kernel | module settings stay, inert | cannot add a setting, a tier entry or an accepted risk | `SHARED_KEYS` names one billing setting |

## 3. JWT: minting, verification, keys, revocation

| aspect | 1 provider | 2 data supplier | 3 without the battery | 4 third-party replacement | 5 split |
|---|---|---|---|---|---|
| minting | `core/auth/tokens.py:31-102` `create_access_token`: `exp` always, `iat_us` (revocation instant), `iat`/`nbf`/`jti`/`iss`/`aud` by `setdefault`, `kid` header; public as `sdk.security.create_access_token`. G-AUTH-02, -09, G-REVOKE-01, -02. [verified] | identity's mint: `sandloom_identity/services/token_service.py:115-153` (live block check, `collect_claims(issuer="identity")`, then `sub`, `email`, `email_verified`, `token_type`, `auth_time` written after the merged claims). Issuer declared by `define_module(issues_tokens=True)` (`sandloom_identity/__init__.py:106`). [verified] | Nothing mints: the only callers of `create_access_token` are `token_service.py:153` and the test helper `core/testing/auth.py:55` (rg). Every authenticated route answers 401. Fail closed. Boot probe run 1: app starts, `/ready` 200, no issuer. [verified] | Works without a kernel change: declare `issues_tokens=True`, call `collect_claims` and `create_access_token` (boot probe run 3, `accounts`: real `collect_claims` → token). The kernel does **not** enforce that an issuer collects claims, checks the block, stamps `token_type` or provenance: `create_access_token` takes any dict and has no caller check. Claims probe S5: code in a process that declares no issuer mints `{"roles":["admin"],"claims_sources":["identity"]}` and `require_platform_admin` allows it (HS256). G-BLOCK-04 covers "every bundled token issuer" only and is a ledger gap. [verified] | Asymmetric: only the issuer process holds `JWT_PRIVATE_KEY`; startup refuses an issuer without it and a verify-only process with a private key or `JWT_SECRET` (`tokens.py:123-176`, called at `core/app_factory/lifecycle.py:370-373`; G-AUTH-05, -06). HS256 split in prod refused unless `shared_hs_secret_split` (G-AUTH-05, G-RISK-10). Inside the issuer process, and in any HS256 process, every module can mint any claims. [verified: read + ledger tests; the in-process point inferred from the missing check] |
| verification, keys | `decode_token` (`tokens.py:289-379`): required `exp`/`iat`, one algorithm, key by `kid`, issuer/audience when set, verified-token cache; `core/auth/bearer.py` (access tokens only); key material check `tokens.py:179-208`. G-AUTH-01..10. [verified] | Kernel settings `JWT_*` (`core/config/auth.py`). No battery data. [verified] | Unaffected: kernel only. [verified] | A replacement that mints outside `create_access_token` must produce the same JWT shape (algorithm, `kid`, `exp`, `iat`, `token_type: "access"`); guards reject anything else (G-AUTH-01, -02). [inferred] | Each process verifies locally; `JWT_ALGORITHM`, `JWT_PUBLIC_KEYS`, `JWT_KID`, `JWT_ISSUER`, `JWT_AUDIENCE` must be equal everywhere: `SHARED_KEYS` (`core/testing/deploy_profile.py:66-95`, SANDLOOM-T014) checks the shipped compose profile at test time, not a running deployment. [verified: read] |
| revocation marks, block | `core/auth/marks.py`, `revocation.py`, `blocks.py`: subject-state record read live on every authenticated request; `revoke_user_tokens_on_commit`, `require_immediate`, post-commit retry, `block_user`/`unblock_user` (public in `sdk.security`). G-REVOKE-01..24, G-BLOCK-01..07. [verified: read] | Callers: identity (password change, role revocation, deactivation) and tenancy (member removal, role change, ownership transfer, org deletion), G-REVOKE-06 (ledger gap). **The durable applier of `core.auth.tokens_revoked.v1` is identity's consumer** `sessions@tokens_revoked` (`sandloom_identity/__init__.py:73-84`); the refresh-session end on block is identity's `sessions@user_blocked` (`:63-71`). [verified: read; boot probe contrast line `["sandloom_identity"]`] | Marks and blocks still work (kernel). **The durable applier is absent**: no consumer of `core.auth.*` with `SANDLOOM_MODULES=[]`, with the stand-ins, and with `["sandloom_tenancy"]` or `["sandloom_tenancy","sandloom_billing"]` in the repository (boot probe). Then G-REVOKE-07's "identity's durable consumer applies it" and G-REVOKE-08's `/ready` degraded signal do not exist: `_stuck_revocations` is filled only inside `apply_pending_revocation` (`revocation.py:502-511`), which `/ready` reads (`core/app_factory/ops.py:206`). A mark that still fails after the post-commit retries is counted (`auth_revocation_deferred_total`) and never applied; nothing refuses to start. **Silent degradation (metric only).** [verified: consumer absence; inferred: the loss path, not run with a failing cache] | The replacement issuer must itself register consumers for `core.auth.tokens_revoked.v1` (calling `apply_pending_revocation`, public) and `core.auth.user_blocked.v1` (ending its refresh sessions); no check, no warning tells it so (rg: no kernel code tests for a consumer of these channels). It need not call `ensure_user_not_blocked` for safety: a blocked user's tokens are rejected live anyway (G-BLOCK-01). [inferred] | Marks live in `SECURITY_CACHE_INSTANCE`; the shipped profile shares `CACHE_URL` (`SHARED_KEYS`); the event crosses the bus to identity's service; RPC carries the commit-time re-check in the MAC-covered context (G-REVOKE-18, -24). A tenancy service relies on an identity service elsewhere consuming its event. [inferred] |
| operator revocation | `sandloom security revoke-sessions / force-password-reset / grant-admin` (`core/cli/security.py`). [verified] | `identity.security.v1` (`security.py:60-63`) and `tenancy.membership.v1` for `--tenant`. [verified] | Refused with a message, exit 1 (cli probe: `contract 'identity.security.v1' is not available in this process`; `--tenant` names `tenancy.membership.v1`). Fail closed. [verified] | Must serve a contract named `identity.security.v1` with identity's method set (`revoke_sessions`, `appoint_platform_admin`, …): another module's name. [inferred] | Works where the contract is local or bound. [inferred] |

## 4. Claims: contributors, provenance, authority

| aspect | 1 provider | 2 data supplier | 3 without the battery | 4 third-party replacement | 5 split |
|---|---|---|---|---|---|
| contributors and merge | `CoreFacade.add_claims_contributor` (`core/facade/_claims.py:203-238`), `collect_claims` (`:278-`), merge `_merge_claim_outcomes` (`:414-453`): HEAD applies each result with `claims.update` (`:442`), later wins, no reserved key. A critical failure aborts with `ClaimsUnavailable` 503. G-AUTH-11, -12; G-AUTH-13 is enforced by identity's mint order (`token_service.py:145-150`), not by the kernel merge. **PR #70:** one owner per key per token (first writer), reserved keys (`claims_sources*`, `user_id`, `provenance_authoritative`, `claims`, `sub`, `iss`, `aud`, `exp`, `nbf`, `iat`, `jti`, `iat_us`, `auth_time`, `token_type`); a violation aborts minting 503. [verified] | identity `roles`, `permissions` (`sandloom_identity/claims.py:61-69`, critical, always both keys); tenancy `tenant_ids` (`sandloom_tenancy/claims.py:20-34`, critical); billing `subscription_status`, `plan_code` (`sandloom_billing/claims.py`, non-critical). [verified] | Without identity nothing calls `collect_claims` (only caller `token_service.py:127`). Without tenancy no `tenant_ids` (tenant isolation fails closed, §6). Without billing no `subscription_status` (but see `Subscribed`, §5). [verified] | A replacement contributor works (boot probe run 3). HEAD: claims probe S2, identity writes `roles: []`, a later `inventory` writes `["admin"]` → token `roles: ["admin"]`, platform gate allowed; S7, a contributor writes `user_id` → `UserContext.user_id` is another user while `sub` stays (`shared/subject.py:71`). **PR #70:** S2 and S7 refused 503. Both kernels: without identity the first writer owns `roles` (S1: `inventory` → `roles: ["admin"]`, `Role("admin")` allows). [verified] | Remote contributors are the `SERVICE_BINDINGS` keys `<owner>.claims.v1`, critical unless `"critical": false` (`_claims.py:259-276`; C-SPLIT-07). PR #70's check runs in the same merge, so it covers remote results. [verified: read] |
| provenance | Merge stamps `claims_sources` / `claims_sources_failed` (`_claims.py:452-453`); `UserContext` normalises them; a token is "authoritative" iff it carries the `claims_sources_failed` key (`shared/subject.py:117`). [verified] | Owner names: `owner=` of the facade call, else the registering module's short name (`_claims.py:225`), else a remote binding key; `ModuleAPI.add_claims_contributor` has no `owner=` (baseline §4.3). [verified] | Kernel only. [verified] | A replacement issuer that skips `collect_claims` stamps what it likes (S5). A token without `claims_sources_failed` is "legacy": claim-mode facts then trust `claims_sources` as written (`shared/authz/facts.py:299-307`). [verified] | Stamped once at the issuer; every verifier trusts the token. [verified: read] |
| authority (who may set a key) | None at HEAD: no key is bound to a source. PR #70 adds an ordering rule (first writer per token), not an assignment. Gates trust a **name**: `"identity"` in `claims_sources` (`shared/guards/platform.py:16`, `core/admin/api/platform.py:67`). [verified] | Nothing the application declares: the trust model config (`core/config/trust.py`, `TrustConfig.access`) sets a fact's resolution mode, not its authority; no setting or declaration names the authority of `roles`, `tenant_ids`, `tenant_id`, `subscription_status`, `features`, `limits` (read of `core/config/*`). [verified: read] | Without identity the platform gates never pass (boot probe run 3, `accounts` → 403): fail closed. `Role`/`Perm` trust whoever wrote `roles`/`permissions` first (S1): silent (§5). [verified] | A replacement issuer passes the platform gate only under the name `identity`: an app-local module named `identity` (boot probe run 2 → 200) or `owner="identity"` (S0, S4). SANDLOOM-B020 refuses the name only when an installed package also provides it (`core/descriptors/module_names.py:136-164`). [verified] | The owner name of a remote contributor is the binding key the operator writes; binding `identity.claims.v1` to any pinned service makes it `identity`. [inferred from `_claims.py:270-275`] |

## 5. Authorization: algebra, facts, atoms, live adapters, platform-admin gate

| aspect | 1 provider | 2 data supplier | 3 without the battery | 4 third-party replacement | 5 split |
|---|---|---|---|---|---|
| algebra | `shared/authz/algebra.py`, three-valued decisions. G-AUTHZ-01, -02, -13. [verified: read] | None. | Unaffected. [verified] | A `Requirement` subclass composes with `& \| ~` (baseline A19 probe step 3). [verified] | Evaluated in the caller's process. [verified: read] |
| claim atoms `Role`, `Perm` | `atoms_claims.py:35-67`: membership in `subject.roles` / `.permissions`, **no provenance check**. G-SENS-01 (risk tag only). [verified] | identity's `roles`/`permissions`. [verified] | Any module's `roles` passes (S1, both kernels): no refusal, no warning. **Silent degradation.** At HEAD also with identity present (S2). [verified] | A replacement writes the same keys; nothing marks it as the authority. [verified] | Read from the token. [verified: read] |
| claim atom `Subscribed(of=Account.user())` | `atoms_claims.py:80-217`: the claim read through the `subscription.active` fact with provenance; **with no fact anywhere** it reads the claim verbatim against `{"active","trialing"}` (`:184-188`, constant `:32`), no provenance. G-AUTH-12, G-AUTHZ-02. [verified] | billing's claim `subscription_status` and fact `subscription.active` (local, or `billing.facts.v1` via `WELL_KNOWN_FACT_OWNERS`, `facts.py:526-541`). [verified] | Billing absent (or not bound): `subscription_status` from any contributor allows (S1, S6, both kernels). With billing's fact present and billing failed or absent from provenance: 503 `subscription_unverifiable` (S1, S6). **Silent degradation without billing.** [verified] | The replacement must write the claim key `subscription_status` with billing's status words, or register a fact named `subscription.active`, which the kernel maps to owner `billing` for remote lookup. [verified: read] | A process that does not bind `billing.facts.v1` finds no fact (`lookup_access_fact` returns `None` when no owner is bound, `facts.py:561-564`) and takes the verbatim path. [inferred] |
| claim atoms `Feature`, `WithinLimit` (`Account.user()`) | `atoms_claims.py:304-363`: read claims `features` / `limits`, no provenance. G-AUTHZ-12. [verified] | No bundled module writes these keys (baseline §5.4). [verified] | Always "any writer": S1 `Feature("beta")` allows on `inventory`'s claim. Not a battery degradation (no battery ever supplied it); a trust gap (§11). [verified] | Any module may write them. [verified] | Read from the token. [verified: read] |
| access facts, `Entitled` | `facts.py`; `ModuleAPI.register_access_fact`; auto `<owner>.facts.v1`; claim mode trusted only when the owner is in `claims_sources` (`facts.py:280-307`). G-AUTHZ-01, -04. [verified] | The owning module (owner = first dotted segment, except `WELL_KNOWN_FACT_OWNERS`). [verified] | Unregistered fact: 501 indeterminate (G-AUTHZ-01). Fail closed. [verified: read + ledger] | Works for a fact named `<owner>.<x>` (baseline A19 probe step 4). [verified] | The consumer accepts the provider's own `owner_module` from `describe()` (`facts.py:486`, `:503`) and asks every bound facts provider for a fact by name (`:572-592`): the remote owner is self-declared. [verified: read; not run] |
| live atoms `SubscriptionActive`, `Credits`, `OrgRole`; `Account.org` / `bound_tenant` | `atoms_live.py`; default adapters `billing.entitlements.v1`, `tenancy.membership.v1` (`live_contracts.py:74-114`); absent service → 501 (`atoms_live.py:57-73`). G-AUTHZ-01, -03, -07, -10, -11. [verified] | billing, tenancy contracts. [verified] | 501 indeterminate; organization accounts indeterminate. Fail closed. [verified: read + ledger tests] | Must serve the contract under billing's/tenancy's **name** (boot probe run 2: `orgs` serves `tenancy.membership.v1`, `OrgRole("member")` allow, `"admin"` deny) or replace the process-wide adapter slot (baseline A19 step 2). `register_service` records the registrant, checks no name (`core/facade/_services.py:53-69`). `OrgRole` roles fixed `member/admin/owner` (`atoms_live.py:187`). [verified] | Called over the RPC proxy (MAC, `RPC_ALLOWED_CALLS`, `service_id` pin). G-AUTHZ-03. [verified: read + ledger] |
| platform-admin gate | `require_platform_admin` (`shared/guards/platform.py:10-19`), `platform_operator` (`core/admin/api/platform.py:54-71`). G-OPCLASS-07, G-SENS-01. [verified] | `roles` + `"identity"` in `claims_sources`. [verified] | 403 on every platform route (boot probe run 3). Fail closed. [verified] | Only under the name `identity` (§4 authority row). [verified] | Evaluated per service from the bearer. [verified: read] |

## 6. Tenant isolation: TenantScope, TenantScoped/RLS, tenant claim

| aspect | 1 provider | 2 data supplier | 3 without the battery | 4 third-party replacement | 5 split |
|---|---|---|---|---|---|
| ORM guard, RLS | `core/orm` `TenantScoped`, `core/tenant`, `core/sql_alchemy/tenant_rls`. G-TENANT-01..09, -15..18, G-RLS-01..15. [verified: read + ledger] | The bound tenant only. [verified] | Unaffected; `TENANCY_MODE=single` binds the canonical tenant (G-TENANT-15). [verified: read] | n/a | Per process and database; RPC binds the tenant only from the MAC-covered context (G-RPC-10). [verified: read + ledger] |
| `TenantScope`, tenant claim | `core/http/tenant_scope.py`: revoked token 401 first, then `X-Tenant-Id` must be in the token's `tenant_id`/`org_id` claim or `tenant_ids` list; **a `tenant_id`/`org_id` claim is preferred** when no header is sent (`:28`, `:111-119`). The authz allowlist reads the same three keys (`shared/authz/_resolve.py:61-75`). G-TENANT-10..14. [verified] | tenancy's `tenant_ids` (critical). **Nobody writes `tenant_id` or `org_id`** (rg over the modules). [verified] | No claim: required scope 400 `tenant context required`, header 403 (S2, S4). Fail closed. [verified] | Must write the kernel's key `tenant_ids` (UUID strings). **Any contributor writing `tenant_id` grants that tenant**: S3, identity + tenancy (`tenant_ids=[T_OWN]`) + `inventory` writing `tenant_id=T_FOREIGN` → `TenantScope` binds `T_FOREIGN` with no header and accepts `X-Tenant-Id: T_FOREIGN`, on HEAD **and with PR #70** (an unowned key goes to its first writer); boot probe run 2 shows the real `collect_claims` putting `inventory`'s `tenant_id` into the token. Not a battery degradation: a trust gap present in a full standard deployment. [verified] | `tenant_ids` collected over `tenancy.claims.v1` (critical binding). [verified: read] |

## 7. Contracts: stubs, DTO copies, RPC, compatibility

| aspect | 1 provider | 2 data supplier | 3 without the battery | 4 third-party replacement | 5 split |
|---|---|---|---|---|---|
| stubs, DTO copies | `sandloom generate` (`core/cli/generated.py`); the bundled modules' copies ship in the kernel wheel (baseline C1-C3). G-CONTRACT-19. [verified: baseline] | Provider modules' signatures. [verified] | The stub imports without the provider (baseline §13.1). [verified] | The replacement must match the copied DTO shape and the contract name. [inferred] | The consumer decodes into its copy (baseline §13.2 phase B). [verified] |
| registry, consumed contracts | `service_registry`, `register_service` (`core/facade/_services.py:53-69`); startup check of `consumes` (G-CONTRACT-12). [verified] | Provider modules. [verified] | Required: `ContractBindingError` at startup; optional: `None` (baseline §13.2). Fail closed. [verified] | No name ownership: any module registers any contract name (boot probe run 2: `tenancy.membership.v1` owner `shop.modules.orgs`). [verified] | Local first, else the bound proxy (C-SPLIT-02). [verified: ledger] |
| RPC | `core/rpc`: pair-MAC, audience, freshness, nonce, allowlist, `service_id` pin, response MAC. G-RPC-01..33. [verified: ledger] | `SERVICE_BINDINGS`, `RPC_ALLOWED_CALLS`, RPC keys: written by the application. [verified] | Unaffected. | Unaffected. | This is the one place where the application assigns trust explicitly: which service answers which contract, who may call what. [verified: read] |
| compatibility check | `core/contract_compat.py`, `core/rpc/readiness.py:305-345`. G-COMPAT-04..06. [verified] | The provider package's `contracts.snapshot.json`, located by the contract's first segment (`contract_compat.py:139-153`) or `CONTRACT_SNAPSHOT_PATHS`. [verified] | Consumer process without the provider package: no expectation, a WARNING "its compatibility is not checked", nothing compared, and `CONTRACT_COMPAT_CHECK=strict` still starts (compat probe: `expected_contracts -> {}`, `probed: []`; with the packages: both found, both probed). **Degradation with a warning only**, in exactly the consumer-only install of I-05. [verified] | A provider under another package name (`orgs` serving `tenancy.membership.v1`) has its snapshot looked up in a package named `tenancy`: the same warning, unless the application sets `CONTRACT_SNAPSHOT_PATHS`. [inferred] | Compares the remote `describe()` with the local expectation; G-PROFILE-09 refuses `CONTRACT_COMPAT_CHECK=off` under the strict profile but not this path. [verified: read] |

## 8. Admin: console, platform API, manifest, CLI

| aspect | 1 provider | 2 data supplier | 3 without the battery | 4 third-party replacement | 5 split |
|---|---|---|---|---|---|
| platform API | `core/admin/api/platform.py:262-280`: one router per loaded module at `/api/v1/platform/<name>`, gated by `platform_operator`. G-OPCLASS-07. [verified] | identity (gate source), each module's database. [verified] | Without the admin module the API is still mounted (boot probe: routers for every stand-in); without identity it answers 403. [verified] | Gate as §5. [verified] | Each service mounts its own; the gate reads the bearer. [verified: read] |
| console | `sandloom_admin` (C-CONSOLE-01, -02: off in prod unless `ADMIN_CONSOLE_ENABLED`, a kernel setting read by the module). [verified: baseline] | admin module. | Not served. [inferred] | A different console gets no manifest: `sandloom generate` writes it only when `"sandloom_admin"` is composed (`core/cli/generated.py:551`). [verified: read] | Served by whichever service loads it. [inferred] |
| CLI `admin create/list`, `security grant-admin` | `core/cli/admin.py`, `security.py`. [verified] | `identity.security.v1`. [verified] | Refused with a message, exit 1 (cli probe). Fail closed. [verified] | Must serve `identity.security.v1`; the password policy falls back to 8/100 when `sandloom_identity` is not importable (`core/cli/password_check.py:40-44`, baseline B-I1), silently for a replacement. [inferred] | Works where the contract is local or bound. [inferred] |

## 9. Tasks: celery, outbox dispatch

| aspect | 1 provider | 2 data supplier | 3 without the battery | 4 third-party replacement | 5 split |
|---|---|---|---|---|---|
| taskqueue, worker | `core/drivers/celery_taskqueue.py`, `core/worker.py:126-137`, enqueue dedupe `core/tasks/enqueue.py`. G-TASK-01..05, C-WORKER-01..06. [verified: read] | The `celery` extra; modules' `tasks.py`. [verified] | Without celery the default taskqueue fails to open: WARNING at startup, `/ready` 200, `taskqueue()` raises `ModuleNotFoundError` at use, `import sandloom.sdk.tasks` fails (tasks probe); a module whose `tasks.py` imports it fails to load (baseline §13.2). Not a guarantee loss (enqueue cannot run, so dedupe cannot weaken), but readiness reports ok. [verified] | A taskqueue driver registered as a capability. [inferred] | Worker and beat lease per service (C-WORKER-06). [verified: ledger] |
| outbox dispatch | Relay in the app process (`core/tasks/relay.py`), not celery; `OUTBOX_RELAY_ENABLED=off` refused in prod (G-RISK-05). [verified: read] | None. | Unaffected. [verified: read] | n/a | Per database. [inferred] |
| periodic tasks | Beat imports `setup_periodic_tasks` of loaded modules (`core/tasks/periodic.py`). [verified: read] | modules (billing reconciler, C-AUTH-08). | No runtime check that a beat or worker runs: the deploy check requires a beat service in the shipped profile (`deploy_profile.py:387`, test time). [inferred: rg found no runtime check] | n/a | Per service. [verified: ledger] |

## 10. Settings: tiers, validators, accepted risks

| aspect | 1 provider | 2 data supplier | 3 without the battery | 4 third-party replacement | 5 split |
|---|---|---|---|---|---|
| tiers, validation, risks | One `Settings` (`core/config/settings.py:98`), `core/config/tiers.py`, closed registry `accepted_risks.RISKS` (13 ids). G-CONFIG-01..08, G-RISK-01..13. [verified] | The 22 module settings live in the kernel's `ModulesConfig` (`core/config/modules.py:10`, baseline B-C1) plus `TENANT_CLAIMS_MAX_ORGANIZATIONS`. [verified] | The settings stay, inert (settings probe 4). Prod with billing absent and `BILLING_PROVIDER` unset starts (probe 5); an explicit `BILLING_PROVIDER=fake` is refused by the kernel even with billing absent (`settings.py:624-645`). `TWOFA_ENCRYPTION_KEY` is mandatory only through identity's own startup task (`sandloom_identity/__init__.py:28-35`). [verified] | Cannot add a setting (probe 3: `INVENTORY_API_KEY` is no field), cannot register an accepted risk (probe 2: an unknown id fails `Settings`), cannot classify a tier. It reads the environment itself, so tiers, overrides, risk logging, `config dump` and the profile-equality check do not cover it. [verified] | `SHARED_KEYS` names `BILLING_DENY_ON_PAST_DUE` (a billing setting); a replacement's shared settings are not checked. [verified: read] |

## 11. Silent degradations

A guarantee that weakens without the battery and neither fails closed nor refuses to start.

| # | guarantee | trigger | what weakens | signal | status |
|---|---|---|---|---|---|
| D1 | G-REVOKE-07, G-REVOKE-08 | identity not loaded anywhere (tenancy alone, or a replacement issuer) | the durable at-least-once applier of `core.auth.tokens_revoked.v1` and the `/ready` "revocation stuck" signal are identity's consumer; a mark lost after the post-commit retries is never applied | a counter (`auth_revocation_deferred_total`) only | consumer absence verified (boot probe); loss path inferred |
| D2 | G-AUTH-12 for `Subscribed` | billing not loaded and `billing.facts.v1` not bound | `Subscribed(of=Account.user())` reads `subscription_status` from any contributor, no provenance | none | verified (S1, S6) |
| D3 | I-09 for `Role`, `Perm` | identity not loaded | `roles` / `permissions` from whichever module writes them first are trusted by `Role`/`Perm` (and at HEAD, the last writer even with identity present) | none | verified (S1; S2 at HEAD) |
| D4 | G-COMPAT-04, G-COMPAT-06 | the provider's package is not installed in the consumer process (consumer-only install, third-party provider) | no compatibility comparison, also under `strict` | a startup WARNING | verified (compat probe) |

Weakenings found that are not tied to a battery's absence, listed so they are not lost:

- **Tenant grant through an unowned claim key** (§6): any claims contributor writing
  `tenant_id` or `org_id` gets that tenant bound, next to tenancy's `tenant_ids`; HEAD and
  PR #70. Verified (claims probe S3, boot probe run 2).
- **`user_id` impersonation at HEAD** (§4): a contributor writing `user_id` changes
  `UserContext.user_id`; PR #70 reserves the key. Verified (S7).
- **Mint authority is the key, not the issuer declaration** (§3): any code in a process holding
  the signing key mints any claims and provenance. Verified (S5).
- **Readiness with an unusable taskqueue** (§9): `/ready` 200 while the default taskqueue cannot
  open. Verified. Enqueue still fails closed, so no guarantee weakens.
- **Silent fallback password policy** for a replacement serving `identity.security.v1` (§8).
  Inferred.

## 12. Where the application should assign trust but cannot

| # | trust decision | today | evidence |
|---|---|---|---|
| T1 | which module is the authority of `roles` / `permissions` | the platform gates hard-code the name `identity`; `Role`/`Perm` trust any writer; PR #70 makes it the first writer | §4, §5; S1, S2, S4; boot probe runs 2, 3 |
| T2 | which module is the tenant authority (`tenant_ids`, `tenant_id`, `org_id`) | kernel key names, no authority; `tenant_id`/`org_id` unowned | §6; S3 |
| T3 | which module is the authority of plan claims (`subscription_status`, `features`, `limits`) and of `subscription.active` | claim keys and status words are kernel defaults; `WELL_KNOWN_FACT_OWNERS` maps the fact to `billing`; `features`/`limits` have no authority; `TrustConfig.access` sets a mode, not an owner | §5 |
| T4 | which module may mint tokens | `issues_tokens=True` only drives key placement; `create_access_token` has no caller check | §3; S5 |
| T5 | which provider answers a domain contract (`tenancy.membership.v1`, `billing.entitlements.v1`, `identity.security.v1`) in-process | first registrant of the name; the live-adapter slot is process-wide; no app declaration (remote: SERVICE_BINDINGS does assign) | §5, §7; boot probe run 2; baseline A19 |
| T6 | the name a claims source carries in provenance | module short name, facade `owner=`, or binding key text; B020 guards installed-package collisions only | §4; S0 |
| T7 | the owner of a remote access fact | the provider's own `describe()` says it | §5 |
| T8 | who applies durable revocations and ends refresh sessions on block | identity registers both consumers; nothing names or checks the duty | §3; D1 |
| T9 | the operator contract for revocation and admin bootstrap | bound to `identity.security.v1` by name | §3, §8 |
| T10 | accepting a third-party module's production risk | the risk registry is a closed kernel tuple | §10; settings probe 2 |

## 13. What stayed unverified

- No split deployment was run: every column-5 cell is read from code and from the ledger's
  split tests (`tests/split_conformance/`, C-SPLIT-*), not observed in this pass.
- D1's loss path (a mark whose post-commit write fails with no applier anywhere) was not run
  with a failing cache; only the applier's absence was observed.
- A remote facts provider declaring a foreign `owner_module` (T7): read, not run.
- A replacement issuer's refresh sessions after `block_user` / `unblock_user`: not run.
- A replacement serving `identity.security.v1` to the operator CLI, and the fallback password
  policy on that path: not run.
- Boot of the stand-ins with `ENVIRONMENT=prod`: not run (dev only); the startup checks in
  prod for a module named `identity` that is not `sandloom_identity` are not observed.
- Whether a worker or beat is running is checked anywhere at run time: searched, not found;
  absence of evidence only.
- G-AUTH-13 at the kernel level: the four identity keys are protected by identity's mint
  order; with PR #70 `sub` and `token_type` become reserved in the merge, `email` and
  `email_verified` do not (they are identity's own contributed-after keys).
