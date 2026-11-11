# Performance and DX baseline for authorization

What moving a domain atom out of the kernel must not change (goals.md, "Moving a domain
atom out of the kernel must not by itself change the number of network calls, the number
of database queries, the evaluation order or the policy syntax"). Task: tm
`kb-perf-dx-baseline` (parent `kb-assess`). This file measures today; it proposes nothing.

Measured on 2026-11-08 at `47c31de` (branch `arch/kernel-boundary`). The authorization
evaluation code is identical to the fixed evidence commit `b8d8506`: `git diff --stat
b8d8506 47c31de -- src/` touches only `core/facade/_claims.py` and its test (token
minting, not evaluation). Scripts, raw results and the DX log: `_archive/_evidence/perf-dx/`.

## 1. Method

### 1.1 The stand

`_archive/_evidence/perf-dx/stand/authzstand/` is a throwaway module with one authenticated `GET`
per policy (`Depends(require(policy))`, handler does nothing) plus two controls without a
policy (`/azs/control`, and `/azs/bound/control` with `TenantScope()`). It is loaded into
the real `sandloom.main:app` with the real batteries; nothing in `src/` was changed.
`_archive/_evidence/perf-dx/authz_stand.py` drives it.

| policy (route) | as written |
|---|---|
| `role` | `Role("admin")` |
| `perm` | `Perm("perm.stand.read")` |
| `subscribed` | `Subscribed(of=Account.user())` |
| `subactive` | `SubscriptionActive(of=Account.user())` |
| `credits` | `Credits(10, of=Account.user())` |
| `role_or_credits` | `Role("admin") \| Credits(10, of=Account.user())` |
| `orgrole/{org_id}` | `OrgRole("admin")` |
| `orgrole_and_subactive/{org_id}` | `OrgRole("admin") & SubscriptionActive(of=Account.org(param="org_id"))` |
| `bound/orgrole_and_subactive` (+ `TenantScope()`, `X-Tenant-Id`) | `OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant())` (the owner's decisive PoC shape) |

Data: `benchsuite.platform seed` (one user, owner of one organization) plus
`_archive/_evidence/perf-dx/seed_billing.sql` (an active subscription with a catalog price and
100 credits for the user and for the organization). Subjects: `admin` (`roles=[admin]`,
`permissions=[perm.stand.read]`), `user` (neither), `outsider` (not a member). Every
token carries `subscription_status=active`, `tenant_ids=[org]`, `claims_sources=[identity,
tenancy, billing]`, `claims_sources_failed=[]` (authoritative provenance). Every case
asserts its status (200, or 403 for the deny cases) on every request.

**Topologies.**

- **mono**: one process, `SANDLOOM_MODULES = identity, tenancy, billing, authzstand`.
- **split**: the stand + identity in the measuring process; tenancy + billing in a
  `uvicorn` provider process (uvloop, httptools, 1 worker) bound by `SERVICE_BINDINGS`
  for `tenancy.membership.v1`, `billing.entitlements.v1`, `billing.facts.v1`, internal-RPC
  keys from `benchsuite.rpckeys`, `RPC_TRANSPORT=aiohttp` (default). Both processes use
  one PostgreSQL database under two database roles, so statements are attributed per
  process.

Both: `ENVIRONMENT=dev`, `OUTBOX_RELAY_ENABLED=off`, HS256 tokens, PostgreSQL 16 and
Redis 7 in Docker on the same machine (Apple M-series, 10 cores, macOS 26.6, host Python
3.12.10; the cachegrind container runs Python 3.13 with the locked benchmark
requirements).

### 1.2 Three measurements

| what | how | runs | noise tolerance |
|---|---|---|---|
| **Counts**: DB round trips, Redis commands, RPC requests per request | PostgreSQL `log_statement=all` between marker statements, split by database role (one log entry = one simple-query message or one extended-protocol execute; the merged `BEGIN …; SELECT set_config(…)` is one); Redis `MONITOR` between `ECHO` markers, split by db index; RPC = the provider's access-log lines for `/_sandloom/rpc/`. A "first" window (the case's first request) and a "steady" window (20 requests after 3 warm-ups) per case. No instrumentation inside the framework. | 1 run × 20 requests per case and topology, plus an earlier 10-request smoke run | **exact**: every steady window divided to an integer, and the smoke run gave the same integers |
| **Instructions (cachegrind)**, mono | `cg_authz.sh`: the ladder method of `benchmarks/README.md` / `ladder/cg/cgdriver.py`: `Ir/req = (Ir(K requests) − Ir(0 requests)) / K`, K = 200, both runs with the same boot, 20 warm-ups and shutdown, `PYTHONHASHSEED=0`, image `sandloom-ladder-cg:3.13` built from `benchmarks/ladder/cg/` | 1–3 runs per case (section 2.3) | the spread of repeated runs: ±4 % (section 2.3); the I-14 budget of +1 % is below this stand's noise, so a +1 % comparison needs more repeats |
| **Latency and instructions (rusage)**, mono and split | in-process ASGI calls at concurrency 1 (no client socket), `perf_counter` per request; retired instructions of the measuring process and of the provider process (`proc_pid_rusage`, `benchmarks/ladder/darwin_rusage.py`) over the window | 2 runs × 3 repetitions × 2,000 requests per case (200 warm-ups), cases interleaved and rotated per repetition | instructions: ±3 % between repetitions; latency: the machine was shared with other agents (load average ≈ 8 on 10 cores), so medians of repetitions are reported; p50 moved by up to 40 % between the two runs, so only differences above 50 % (p50) or 2× (p99) are signal |

Reproduce (from `_archive/_evidence/perf-dx/`, with an owner DSN of a seeded database):

```bash
export DATABASE_URL=postgresql+asyncpg://…/kbperf CACHE_URL=redis://…/3
(cd ../../../../benchmarks && python -m benchsuite.platform seed)
psql … < seed_billing.sql
python authz_stand.py counts --topology mono  --m 20 --out results/counts_mono.json
python authz_stand.py counts --topology split --m 20 --out results/counts_split.json
python authz_stand.py run --topology mono  --n 2000 --reps 3 --out results/run2_mono.json
python authz_stand.py run --topology split --n 2000 --reps 3 --out results/run2_split.json
CG_K=200 ./cg_authz.sh control role.admin …        # >> results/cg.txt
python render.py results                           # the tables below
```

`counts` needs a superuser DSN (it toggles `log_statement` and creates the two roles) and
the PostgreSQL container name in `KB_PG_CONTAINER` (default `kb-perfdx-pg`).

## 2. Results

### 2.1 Evaluation order, calls and queries (exact)

Per request, steady state. "DB" = database round trips; in the monolith they run in the
provider's own unit on the contract pool, in the split in the provider process (the
consumer issues none). Redis: 1 `GET` (the subject's revocation mark) in every case and
topology, policy or not; the split provider issued no Redis command.

| policy | evaluation order and short-circuit (code) | contract calls | mono DB | split RPC | split DB (provider) | request DB session opened |
|---|---|---|---|---|---|---|
| control | — | 0 | 0 | 0 | 0 | no |
| `Role("admin")` | one claim check: `role in subject.roles` (`atoms_claims.py:66`) | 0 | 0 | 0 | 0 | no (`deps.py:166`) |
| `Perm(...)` | one claim check (`atoms_claims.py:47`) | 0 | 0 | 0 | 0 | no |
| `Subscribed(of=Account.user())` | claim tier; looks up fact `subscription.active` (local, or remote via `billing.facts.v1`, cached 300 s, `facts.py:64`, `:544`), trusts the claim because billing is in `claims_sources` (`atoms_claims.py:156-198`) | 0 | 0 | 0 (first use: +2, see 2.2) | 0 | no |
| `SubscriptionActive(of=Account.user())` | live: account → `billing.entitlements.v1.check_subscription_active` (`atoms_live.py:130`) | 1 | 3 (`BEGIN`, one joined `SELECT`, `ROLLBACK`) | 1 | 3 | yes, no statement |
| `Credits(10, of=Account.user())` | live: `get_credit_balance` (`atoms_live.py:156`) | 1 | 4 (`BEGIN`, customer, balance, `ROLLBACK`) | 1 | 4 | yes, no statement |
| `Role("admin") \| Credits(10)` — admin | `Or` sorts by tier (`algebra.py:209`): `Role` allows, `Credits` never evaluated | 0 | 0 | 0 | 0 | **yes**: the policy's tier is its maximum (3), so `require` depends on `get_db` (`deps.py:121-123`, `:179`) |
| `Role("admin") \| Credits(10)` — user | `Role` denies, then `Credits` | 1 | 4 | 1 | 4 | yes |
| `OrgRole("admin")` | live: org from the path, `validate_against_token=False` (`atoms_live.py:207-214`) → `tenancy.membership.v1.has_role_at_least` | 1 | 3 (merged `BEGIN … READ ONLY; set_config`, `SELECT`, `COMMIT`) | 1 | 3 | yes |
| `OrgRole("admin") & SubscriptionActive(of=Account.org(param="org_id"))` — member | `And`, both tier 3, declaration order (`algebra.py:165`): membership, then the token allowlist check of the org (`account.py:218`, no I/O), then entitlements | 2 | 6 | 2 | 6 | yes |
| same — outsider | `OrgRole` denies: `SubscriptionActive` not evaluated | 1 | 3 | 1 | 3 | yes |
| `TenantScope()` alone (bound control) | — | 0 | 0 | 0 | 0 | no |
| `OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant())` | membership (`admin`), then `tenant_kind(bound)` asks membership **again** with `minimum="member"` (`account.py:153-168`, `:239`; the per-request memo is keyed by tenant and holds only `tenant_kind` results, and the contract read memo only dedupes identical calls), then entitlements | **3** | 9 | **3** | 9 | yes |

Two facts a new design inherits:

- The cost tier of a combinator is its most expensive operand, decided at wiring time, so
  `Role("admin") | Credits(10)` opens a request database session for an admin who never
  reaches `Credits` (no statement, but ≈ +0.5 M instructions, section 2.3).
- `Account.bound_tenant()` behind `OrgRole` costs a second membership call: 3 contract
  calls (3 RPCs in a split) for the decisive PoC shape, not 2.

### 2.2 Remote metadata on the request path (split, first requests)

| first use in a process of | extra requests | when again |
|---|---|---|
| any bound contract (`tenancy.membership.v1`, `billing.entitlements.v1`) | 1 `GET /_sandloom/rpc/<contract>` (the proxy's describe, `core/rpc/proxy.py:656`), although the provider log shows one `GET` of each of the three bound contracts while the consumer started, before its first request | not within the run |
| `Subscribed` (fact `subscription.active`) | `GET /_sandloom/rpc/billing.facts.v1` + `POST …/billing.facts.v1/describe` | every 300 s per owner (`REMOTE_FACT_TTL_SECONDS`, `facts.py:64`) |

The steady state has no discovery call; the first request after start (and a `Subscribed`
request every 5 minutes) does.

### 2.3 Instructions

#### cachegrind, monolith (instructions per request; K = 200)

Whole request through `sandloom.main:app` (middleware, token decode, revocation `GET`,
the guard, the provider's own unit in the monolith, the 200/403 response). Δ is against
the control without a policy (`bound.*` against the `TenantScope()` control). Raw:
`_archive/_evidence/perf-dx/results/cg.txt`.

| case | Ir/req | runs | Δ vs control |
|---|---|---|---|
| control | 1,126,330 | 1,079,333 · 1,126,330 · 1,174,644 | 0 |
| `Role("admin")` | 1,182,904 | 1,179,047 · 1,186,760 | +56,574 |
| `Role("admin")`, denied (403) | 1,795,870 | 1 | +669,540 |
| `Perm(...)` | 1,176,362 | 1 | +50,032 |
| `Subscribed(of=user)` | 1,215,627 | 1 | +89,297 |
| `SubscriptionActive(of=user)` | 4,140,788 | 4,140,316 · 4,141,260 | +3,014,458 |
| `Credits(10, of=user)` | 4,523,383 | 1 | +3,397,053 |
| `Role \| Credits`, admin | 1,746,413 | 1 | +620,083 |
| `Role \| Credits`, user | 4,552,723 | 1 | +3,426,393 |
| `OrgRole("admin")` | 2,624,547 | 1 | +1,498,217 |
| `OrgRole & SubscriptionActive(of=org)` | 5,122,587 | 1 | +3,996,257 |
| same, outsider (403) | 3,110,784 | 1 | +1,984,454 |
| `TenantScope()` control | 1,292,623 | 1 | 0 (+166,293 vs control) |
| `OrgRole & SubscriptionActive(of=bound_tenant)` | 5,827,384 | 1 | +4,534,761 |

Noise: the control's three runs spread ±4.3 % (≈ ±48 k), `Role("admin")` ±0.3 %,
`SubscriptionActive` ±0.01 %. The control and the claims cases sit inside one noise band:
a claims atom costs ≈ 50–90 k, below what one run resolves; the live atoms, the
`Role | Credits` session (+≈ 0.56 M over `Role` alone) and the deny path (+≈ 0.6 M: the
deny log, the metrics and the error response, `enforce.py:63`) are well outside it. The
rusage counts (below) agree with these within 4 %.

#### rusage (macOS retired instructions), thousands per request, median of 3 repetitions

Run 2 (`results/run2_*.json`), run 1 in the last column. The measuring process is the
whole monolith, or the split consumer; the provider column is the split provider
process over the same window.

| case | mono | split: consumer | split: provider | split total | run 1 (mono / consumer / provider) |
|---|---|---|---|---|---|
| control | 1,091 | 1,086 | 0 | 1,087 | 1,087 / 1,074 / 0 |
| `Role("admin")` | 1,163 | 1,156 | 0 | 1,156 | 1,156 / 1,146 / 0 |
| `Role("admin")`, denied | 1,826 | 1,812 | 0 | 1,813 | 1,860 / 1,826 / 1 |
| `Perm(...)` | 1,164 | 1,147 | 0 | 1,147 | 1,178 / 1,147 / 0 |
| `Subscribed(of=user)` | 1,200 | 1,182 | 0 | 1,182 | 1,206 / 1,181 / 0 |
| `SubscriptionActive(of=user)` | 4,000 | 2,592 | 3,044 | 5,636 | 3,982 / 2,580 / 3,062 |
| `Credits(10, of=user)` | 4,363 | 2,606 | 3,380 | 5,986 | 4,325 / 2,589 / 3,399 |
| `Role \| Credits`, admin | 1,691 | 1,667 | 0 | 1,668 | 1,672 / 1,665 / 0 |
| `Role \| Credits`, user | 4,407 | 2,642 | 3,376 | 6,019 | 4,348 / 2,631 / 3,404 |
| `OrgRole("admin")` | 2,600 | 2,761 | 1,659 | 4,420 | 2,566 / 2,746 / 1,654 |
| `OrgRole & SubscriptionActive(of=org)` | 5,019 | 3,792 | 4,704 | 8,495 | 4,976 / 3,766 / 4,709 |
| same, outsider | 3,187 | 3,366 | 1,645 | 5,011 | 3,179 / 3,368 / 1,639 |
| `TenantScope()` control | 1,264 | 1,251 | 0 | 1,252 | 1,252 / 1,242 / 0 |
| `OrgRole & SubscriptionActive(of=bound_tenant)` | 5,808 | 4,823 | 6,463 | 11,286 | 5,775 / 4,779 / 6,450 |

Repetitions within a run spread ≤ 3 %; the two runs agree within 2 %.

The split moves the provider's work out of the measuring process and adds the RPC cost on
both sides: for `SubscriptionActive`, mono ≈ 4.0 M in one process; split ≈ 2.6 M
(consumer) + 3.0 M (provider) ≈ 5.6 M. For `OrgRole` (a planned read, cheap in-process) the
split consumer alone (2.8 M) costs more than the whole monolith request (2.6 M).

### 2.4 Latency (µs per request, concurrency 1, in-process ASGI; median of 3 repetitions)

Run 2; run 1's p50 in the last column. Docker PostgreSQL on the same laptop: one
database round trip is ≈ 0.3–1 ms, which dominates every live atom; the RPC adds little
on top in this setup and would add a network hop in a real deployment.

| case | mono p50 / p95 / p99 | split p50 / p95 / p99 | run 1 p50 (mono / split) |
|---|---|---|---|
| control | 752 / 1,265 / 1,862 | 864 / 1,423 / 2,383 | 672 / 600 |
| `Role("admin")` | 680 / 1,226 / 1,923 | 606 / 1,569 / 4,367 | 616 / 623 |
| `Role("admin")`, denied | 763 / 1,683 / 3,687 | 735 / 1,516 / 2,831 | 886 / 759 |
| `Perm(...)` | 841 / 1,427 / 2,461 | 576 / 1,206 / 2,306 | 1,728 / 804 |
| `Subscribed(of=user)` | 858 / 1,467 / 2,496 | 613 / 1,378 / 2,683 | 833 / 660 |
| `SubscriptionActive(of=user)` | 3,934 / 6,431 / 8,039 | 3,736 / 6,788 / 9,396 | 2,863 / 3,693 |
| `Credits(10, of=user)` | 4,813 / 7,867 / 11,604 | 4,587 / 7,710 / 10,404 | 3,537 / 4,102 |
| `Role \| Credits`, admin | 884 / 1,672 / 2,780 | 705 / 1,601 / 2,715 | 740 / 746 |
| `Role \| Credits`, user | 4,969 / 7,541 / 9,213 | 4,507 / 7,825 / 11,185 | 3,419 / 4,289 |
| `OrgRole("admin")` | 2,895 / 4,727 / 6,628 | 3,349 / 6,173 / 10,274 | 2,283 / 3,182 |
| `OrgRole & SubscriptionActive(of=org)` | 6,384 / 9,458 / 11,852 | 6,441 / 11,062 / 14,932 | 4,599 / 6,047 |
| same, outsider | 3,424 / 5,211 / 6,691 | 3,897 / 6,779 / 9,434 | 2,648 / 3,631 |
| `TenantScope()` control | 880 / 1,463 / 2,648 | 824 / 1,437 / 2,602 | 654 / 686 |
| `OrgRole & SubscriptionActive(of=bound_tenant)` | 8,411 / 11,886 / 14,975 | 9,626 / 15,176 / 19,750 | 6,155 / 8,505 |

Between the two runs p50 moved by up to 40 % (other agents' load), so the latency
tables give the order of magnitude and the ranking: claims atoms are inside the
control's noise; each live atom adds ≈ 2–4 ms here, roughly in proportion to its database
round trips (3–4 per call); the bound-tenant PoC shape is the slowest (3 calls, 9 round
trips).

## 3. DX baseline

One developer task, end to end, by following only the shipped docs and CLI, in a
throwaway app outside the repository: a provider module `quota` with a contract, a
consumer module `reports` with a typed `QuotaAvailable(units=1)` access check on a route,
both tested, then provider and consumer split into two processes. Full log:
`_archive/_evidence/perf-dx/dx/dx-log.md`; the hand-written files: `_archive/_evidence/perf-dx/dx/files/`.

| measure | today |
|---|---|
| framework commands | 19 run (13 without error recovery) |
| files edited or created by hand | 7 (+2 generated files deleted by hand, +2 temporary workaround edits) |
| framework or doc errors met | 6 (SANDLOOM-T004/T007 on changing the generator's stand-in contract; `ModuleLoadError` after deleting the stub as the recipe says; T004 again from the snapshot; T009 advanced-tier markers for `Requirement`, `TIER_LIVE`, `AuthzContext`, `Category`, `bind_tenant`; a provider-side contract test under `bind_tenant` that saw no rows; test-database ownership refusal when running `pytest` directly) |
| doc lookups | 16 |
| kernel changes | 0 |
| manual split wiring | 0 (`sandloom add deploy` derives the binding and allowlist from `Contract(REF)` and the atom's `contracts()`); the documented split run is Docker-only, and a local two-process run needed an off-docs launcher |
| result | monolith: 201 then 402 `QuotaAvailable`; split: the same over RPC; provider down: 503 `remote_service_unavailable` |

What the run shows:

- The only typed, parameterised form is a `Requirement` subclass the **consumer** writes
  over the provider's stub (31 lines, four advanced-tier kernel names). No doc or recipe
  shows it; the provider cannot ship it to consumers.
- `AccessFact` + `Entitled` is provider-owned and travels to other processes, but it is a
  string with no parameter.
- A provider outage in the hand-written atom surfaces as the proxy's 503, not as an
  indeterminate decision: no deny log or `access_denied_*` metric, unlike the kernel's
  live atoms (`atoms_live.py:57-73`).

## 4. What could not be measured

- **cachegrind of the split**: the stand counts instructions in one container process;
  the split is measured with the macOS per-process counter (rusage) on both processes
  instead, which is noisier (±3 %).
- **Production settings**: `ENVIRONMENT=dev`. Production adds signed commands, a
  fail-closed RPC nonce guard (may add Redis commands in the provider), JSON logs and the
  outbox relay; counts of RPC and SQL per policy should not change, instructions and
  latency will.
- **Separate databases per service** in the split: one database, two roles. Does not
  change the counts; changes connection pools and latency.
- **A quiet, dedicated machine**: latency was taken on a laptop shared with other agents.
  Use the tables for shape and order of magnitude, and re-run both sides of any
  comparison in one session.
- **Concurrent / open-loop load**: concurrency 1 only.
- **Organization variants of `Credits` and `Subscribed`** (`of=Account.org(...)`) and
  `Feature` / `WithinLimit`: not in the stand.
- **In-process contract calls in the monolith** are counted through their database round
  trips and through the split's RPC requests, not with a counter inside the framework.
- **DX, the documented split run** (`docker compose … up --build`): the image build needs
  the framework from a registry; with a checkout-path framework it fails (ERR 7 in the
  log). The two-process behaviour was checked with local processes instead.
