# Kernel boundary: validation

Experiments that check the kernel-boundary decisions against running code. Each section is
written by the task named in it.

## 1. Replaceability PoC (tm `kb-poc-replace`)

goals.md "Replaceability decides the scope": each standard battery is replaced by a test
implementation outside the kernel. Where the replacement works with no kernel change, no new
mechanism is needed; where it fails, the failure names the abstraction gap.

### 1.1 Setup

| item | value |
|---|---|
| kernel under test | a wheel built from `packages/sandloom` at `44451e4` (PR #70 merged, `e212893` included; `src/` unchanged up to `333abb8`), installed in a clean venv with the `redis` extra: **no** `sandloom_identity`, `sandloom_tenancy`, `sandloom_billing`, no celery |
| contrast kernel | the kb-guarantee-map lean wheel built at `b8d8506` (before PR #70), same fixtures |
| repository env | `uv run` in the checkout (every bundled module installed), used for the B020 check only |
| fixtures | `poc/replace/app/pocapp/modules/`: `test_identity` (issuer, `roles`/`permissions` from a critical contributor), `identity` (the same issuer under the short name `identity`, A6 only), `forged` (non-critical contributor whose answer the driver sets; optionally registers a fact under another module's name), `grants` (tenant-grant provider: critical contributor writing `tenant_ids`, grant/revoke routes, a down switch), `notes` (`TenantScoped` table under Postgres RLS), `inventory` (fact `inventory.stock.available` and contract `inventory.stock.v1`), `shop` (consumer: `Role`, `Perm`, `require_platform_admin`, `TenantScope`, `Entitled`). Nothing in `src/` was edited; `git status -- src packages` is empty. |
| processes | real `uvicorn sandloom.main:app` processes started by `poc/replace/run.py` |
| monolith | one process, all six modules, HS256 |
| split | P1 = `test_identity`, `notes`, `shop`; P2 = `grants`, `inventory`, `forged`. P1 binds `grants.claims.v1` (critical), `forged.claims.v1` (`critical: false`), `inventory.facts.v1`, each pinned `service_id: p2`; per-pair X25519 MAC, Ed25519 command signatures, `RPC_ALLOWED_CALLS {"p1": [...]}`; EdDSA tokens, only P1 holds `JWT_PRIVATE_KEY`; one Redis (`CACHE_URL` db 3) shared by both |
| services | own containers `kbpoc-pg` (`postgres:16-alpine`, 127.0.0.1:38433) and `kbpoc-redis` (`redis:7-alpine`, 127.0.0.1:38380, dbs 3-4); `SANDLOOM_TEST_OWNER=kb-poc-replace`; `ENVIRONMENT=dev`, `EVENT_BUS_BACKEND=memory`, `INFRA={"database":{"default":{"tenant_rls":true}}}` |
| database | `setup_db.py` (as superuser): role `poc_app` (not superuser, no BYPASSRLS, not the table owner), `notes_items` with the kernel's own `enable_tenant_rls_statements` DDL, one note per tenant; `sandloom migrate upgrade-core --apply`; the app connects as `poc_app` |

Commands:

```bash
docker run -d --name kbpoc-pg -e POSTGRES_PASSWORD=poc -e POSTGRES_USER=poc -e POSTGRES_DB=poc -p 127.0.0.1:38433:5432 postgres:16-alpine
docker run -d --name kbpoc-redis -p 127.0.0.1:38380:6379 redis:7-alpine
uv build --wheel packages/sandloom --out-dir $SCRATCH/dist
uv venv $SCRATCH/venv && VIRTUAL_ENV=$SCRATCH/venv uv pip install "$SCRATCH/dist/sandloom-0.7.0a11-py3-none-any.whl[redis]" uvicorn httpx
$SCRATCH/venv/bin/python -I poc/replace/setup_db.py postgresql://poc:poc@127.0.0.1:38433/poc
DATABASE_URL=postgresql+asyncpg://poc:poc@127.0.0.1:38433/poc SANDLOOM_MODULES='[]' ... $SCRATCH/venv/bin/sandloom migrate upgrade-core --apply
export SANDLOOM_POC_PG=postgresql+asyncpg://poc_app:poc_app@127.0.0.1:38433/poc SANDLOOM_POC_REDIS=redis://127.0.0.1:38380
$SCRATCH/venv/bin/python poc/replace/run.py A-mono A-forged-first A-noncritical A-split A-squat \
    B-mono B-no-grants B-split C-mono C-split C-edge-alone C-squat      # -> out/head.txt
$SCRATCH/venv/bin/python poc/replace/run.py C-duplicate                 # -> out/head.txt (tail)
<kb-gmap venv-redis>/bin/python poc/replace/run.py A-mono B-mono        # -> out/pre-pr70.txt
uv run python poc/replace/run.py A-squat                                # -> out/repo-env-b020.txt
```

Transcripts: `poc/replace/out/head.txt`, `pre-pr70.txt`, `repo-env-b020.txt`,
`head-server-logs.txt` (the ERROR/WARNING lines the refusals logged). `USER`/`ADMIN` and
`T_A`/`T_B`/`T_FOREIGN` stand for fixed UUIDs; `ADMIN` is in `POC_ADMINS`, so `test_identity`
gives it `roles: ["admin"]`.

### 1.2 Verdicts

| experiment | verdict | the gap |
|---|---|---|
| **A** identity → `test_identity` | **partial** | rights through claims contributors work with no kernel change, mono and split; a forged contributor cannot raise rights while the authority answers. **Fails:** (a) the platform-admin gate does not accept a non-identity issuer (T1); the only way in is the name `identity`, which works in a lean install (trust by name) and is refused by B020 in a full one; (b) when the authority's contributor is non-critical and fails, a forged contributor's `roles`/`permissions` pass `Role`/`Perm` (no provenance check) |
| **B** tenancy → independent `grants` | **partial** | the grant drives `TenantScope`, the ORM guard and Postgres RLS with no kernel change, mono and split; header without grant 403; foreign `tenant_id`/`org_id` refused; provider down fails closed; revocation works across processes. **Fails:** with no tenant-grant provider in the token (not deployed, or a non-critical one failed), any contributor writing `tenant_ids` gets those tenants bound and RLS opened to them |
| **C** billing → `inventory.stock.available` | **pass, with three gaps** | the fact works locally and over RPC with no kernel change; owner down 503, owner absent 501. Gaps: a module can register a fact under another module's name (and so serve `<that module>.facts.v1`); the duplicate refusal names one module only; a fact takes parameters only from the request (path, query, `X-Tenant-Id`) |

### 1.3 A: identity replaced by trusted roles through the claims mechanism

| # | scenario | mono (HEAD) | split (HEAD) | pre-#70 (`b8d8506`) |
|---|---|---|---|---|
| A1 | `ADMIN` minted by `test_identity` | `roles: ["admin"]`; `Role("admin")` 200, `Perm("perm.poc.write")` 200; `require_platform_admin` **403**; kernel platform API `/api/v1/platform/shop/services` **403** | same | same |
| A1 | `USER` | `Role`/`Perm` 403, platform 403 | same | same |
| A2 | `forged` writes `roles: ["admin"], permissions: [...]` (registered after `test_identity`) | mint **503**, log `claims contributor 'forged' wrote the claim 'roles' owned by 'test_identity'` | mint 503 (remote contributor over `forged.claims.v1`) | mint 200 `roles: ["admin"]`; `Role` **200**, `Perm` **200** (later writer wins) |
| A2 | the same with `forged` registered **before** `test_identity` | mint 503; the log and the 503 detail name **`test_identity`** (`'test_identity' wrote the claim 'roles' owned by 'forged'`) | n/a (local owners precede remote ones) | — |
| A2 | `forged` writes `permissions` only | 503 | 503 | 200, `Perm` **200** |
| A2 | `forged` writes `claims_sources: ["identity"]`, `provenance_authoritative`, `user_id`, `sub` | 503 each (reserved key) | 503 each | minted; provenance restamped by the merge |
| A3 | `test_identity` registered `critical=False`, its source down; `forged` writes `roles: ["admin"]` | mint 200, `claims_sources_failed: ["test_identity"]`, `roles: ["admin"]` from `forged`; `Role("admin")` **200**, `Perm` **200**; platform 403 | not run (inferred identical: same merge) | — |
| A4 | `test_identity` critical, down | mint 503 | — | 503 |
| A6 | the issuer under the short name `identity` (app-local) | lean install: mint `claims_sources: ["identity"]`, `require_platform_admin` **200**, kernel platform API **200**. Repository env: boot refused, `SANDLOOM-B020: module name 'identity' is provided by the installed package sandloom-identity ... and by the local module 'pocapp.modules.identity'` | — | — |

Success criteria (task): trusted roles through claims, no kernel change: **met**. A forged
contributor cannot raise rights: **met while the authority answers** (both orders, local and
remote), **not met** in A3. Platform-admin gate works with the replacement: **not met**.

Gaps (file:line at HEAD):

1. **Platform gates trust the name `identity`** (T1): `shared/guards/platform.py:16-17` and
   `core/admin/api/platform.py:67,70`. A replacement issuer under its own name gets 403 on every
   platform route; under the name `identity` it gets 200 wherever no installed package provides
   that name (`core/descriptors/module_names.py:136-164`, B020). The application has no way to
   say "`test_identity` is the authority of `roles`"; the owner name comes from the module's
   short name (`core/facade/_claims.py:262`).
2. **`Role` and `Perm` read the claim with no provenance**: `shared/authz/atoms_claims.py:47-48`
   (`Perm`), `:66-67` (`Role`). PR #70 makes the first writer of a key its owner for one token
   (`core/facade/_claims.py:525-534`); that is an ordering rule, not an assigned authority: when
   the authority did not write the key (failed and non-critical, or omitted the key), the next
   writer owns it and `Role`/`Perm` accept it (A3). Nothing requires the authority of a
   security key to be critical (`add_claims_contributor(critical=...)`, `_claims.py:240`), or to
   always write its keys (identity does so by convention).
3. **Availability and attribution** (not a rights raise): one non-critical contributor that
   writes a foreign or reserved key aborts every mint with 503, so a buggy or hostile module
   denies all logins. When it is registered first, the refusal names the authority
   (`_claims.py:525-533` raises for the later writer).

### 1.4 B: tenancy replaced by an independent tenant-grant provider

`grants` keeps grants in process memory and writes the kernel key `tenant_ids` from a critical
contributor; revoking a grant calls `revoke_user_tokens_on_commit` inside the request's unit.

| # | scenario | mono (HEAD) | split (HEAD, `grants` in P2) |
|---|---|---|---|
| B1 | grant `USER`→`T_A`, mint | `tenant_ids: ["T_A"]`; `/shop/tenant` no header → `T_A`; header `T_A` 200; `T_B`, `T_FOREIGN` 403; `/notes/items` header `T_A` → `{"rls_tenant_setting":"T_A","bodies":["note of A"]}` | identical |
| B2 | no token, header `T_A` | 401 | 401 |
| B2 | `ADMIN`, no grant | no header 400 `tenant context required`; every header 403 | identical |
| B3 | `forged` writes `tenant_id: T_FOREIGN` / `org_id` | mint 503 (reserved, `e212893`) | 503 |
| B3 | `forged` writes `tenant_ids: [T_FOREIGN]` | mint 503 (second owner of `tenant_ids`) | 503 |
| B5 | revoke `T_A`; old token | `/shop/tenant`, `/notes/items` **401** `Token has been revoked`; re-mint after the 2 s skew: `tenant_ids: []`, header `T_A` 403 | identical (revoked in P2, refused in P1: marks in the shared cache) |
| B4 | provider down (switch) | mint 503; a token minted before keeps `T_B` (200) until it expires | identical |
| B4 | provider down + `forged` writes `tenant_ids` | 503 (the critical owner failed first) | 503 |
| B4 | provider **process stopped** | — | mint 503 (`grants.claims.v1` critical binding); old token still 200 |
| B6 | **no tenant-grant provider deployed**; `forged` writes `tenant_ids: [T_FOREIGN, T_B]` | mint 200, `claims_sources: ["forged","test_identity"]`; header `T_FOREIGN` **200**, header `T_B` **200**, `/notes/items` header `T_B` → `{"rls_tenant_setting":"T_B","bodies":["note of B"]}` | not run (inferred identical: TenantScope reads the token only) |
| pre-#70 | `forged` writes `tenant_id: T_FOREIGN` with `grants` present | — | (`b8d8506`, mono) mint 200; `/shop/tenant` no header → **`T_FOREIGN`**, header `T_FOREIGN` 200 |

Success criteria: ordinary requests, RLS, revocation, provider down and split with an
independent provider and no kernel change: **met**. No tenant from a header or a foreign claim
without a trusted grant: **met for `tenant_id`/`org_id` and for a second writer of
`tenant_ids`; not met when the grant authority is absent from the token** (B6).

Gaps:

1. **`tenant_ids` has no authority**: `TenantScope` takes the grant from whoever wrote the key
   (`core/http/tenant_scope.py:159-172`, used at `:110`), as does the authz allowlist
   (`shared/authz/_resolve.py:62-70`); neither reads `claims_sources`. `tenant_ids` is not
   reserved, so with no grant provider in the token the first writer owns it
   (`core/facade/_claims.py:525-534`) and the kernel binds the tenant, sets the RLS setting and
   returns that tenant's rows (B6). This is goals.md's "no tenant is accepted ... without a
   trusted grant" and I-08/I-10 failing in a configuration the kernel accepts without a warning.
   The same opening exists when the provider is registered non-critical and fails (inferred
   from A3, not run for B).
2. Tokens minted before a provider outage keep their grants until expiry (B4); revocation is
   the provider's explicit act. Expected, listed so it is not mistaken for a pass of "provider
   down revokes".

### 1.5 C: billing replaced by an unrelated fact

`inventory` registers `AccessFact("inventory.stock.available", owner_module="inventory",
live_service="inventory.stock.v1", min_freshness=live)`; the live resolver reads `{sku}` from
the path and `?qty=` from the query. `shop` gates `GET /api/v1/shop/items/{sku}/buy` with
`require(Entitled("inventory.stock.available"))`, a string: no import of `inventory`.

| # | scenario | mono | split (`inventory` in P2, P1 binds `inventory.facts.v1`) |
|---|---|---|---|
| C1 | `sku-in` (stock 5) | 200 | 200 |
| C1 | `sku-out` (0), `sku-none`, `sku-in?qty=6` | 403 `requires entitlement 'inventory.stock.available'` | 403 (path and query parameters cross the wire) |
| C1 | `sku-in?qty=5` | 200 | 200 |
| C1 | no token | 401 | 401 |
| C2 | provider process stopped | — | 503 `Remote service 'inventory.facts.v1' is unavailable` |
| C3 | `inventory` neither loaded nor bound | 501 `fact_unregistered` | — |
| C4 | `forged` registers `inventory.stock.available` with `owner_module="inventory"` (always true), `inventory` not loaded | `sku-out` **200**, every SKU 200; `contract_owner("inventory.facts.v1") = pocapp.modules.forged`; no log line | — |
| C5 | `inventory` and `forged` both register it | boot refused: `Failed module pocapp.modules.forged: Access fact 'inventory.stock.available' is already registered` | — |

Success criteria: the fact works locally and remotely with no kernel change: **met**.

Gaps:

1. **A fact's name and owner are not tied to the registering module**: `register_access_fact`
   takes `fact.owner_module` as given and exposes `<owner>.facts.v1` under it
   (`core/facade/_services.py:219-240`; `core/module_api/_registration.py:525-534`). Any module
   can answer another module's fact in-process (C4); remotely the owner is the provider's
   `describe()` (guarantee-map T7).
2. **The duplicate refusal names one module** (`_services.py:228`); quality scenario C-F3 asks
   for both.
3. **Parameters only from the request**: `Entitled` takes a name and options, no arguments
   (`shared/authz/atoms_fact.py:48-55`); the remote `evaluate` carries path and query
   parameters and `X-Tenant-Id` only (`shared/authz/facts.py:422-447`). A parameter the handler
   computes (a cart's quantity, a body field) cannot reach the fact, locally or remotely,
   without a domain atom in the owner's code, which a consumer cannot import.
4. The split logs `No contract snapshot for remote contract inventory.facts.v1 / grants.claims.v1
   / forged.claims.v1: its compatibility is not checked` (guarantee-map D4, now for app-local
   providers).

### 1.6 What could not be run, and why

- **A3 and B6 in a split**: not run; both are decided by the merge and by `TenantScope` on the
  issuer and the verifier respectively, which read only the token. Inferred identical.
- **Name squat over RPC** (binding `identity.claims.v1` to another service): needs a provider
  that serves a contract named `identity.claims.v1`, i.e. a module named `identity` in P2; same
  B020 result in a full install, same 200 in a lean one by the A6 mechanism. Not run.
- **Consumer without the provider's code**: both split processes have the whole fixture app on
  `PYTHONPATH` (the modules are loaded per process, but importable everywhere); I-05's "provider
  package not installed" is not shown by this PoC.
- **Durable revocation applier** (guarantee-map D1): revocation was tested on the post-commit
  path with a healthy cache and `EVENT_BUS_BACKEND=memory`; no process consumes
  `core.auth.tokens_revoked.v1` (no identity), and the loss path was not exercised.
- **Prod and the strict profile**: every run is `ENVIRONMENT=dev`; the startup checks of
  `PRODUCTION_PROFILE=multi_tenant_strict` were not exercised.
- **Grant storage**: grants live in process memory; the provider's own database, migrations and
  RLS on its tables are outside what B tests (the protocol between provider and kernel).
