# kb-poc-authz: results

tm task `kb-poc-authz` (parent `kb-assess`). Scope: [alternatives-c-authz.md](../../_archive/alternatives-c-authz.md)
§8.2, build steps 1–5, rows Z-1…Z-16. This file records what was built and what each row
measured; it decides nothing (ADR-C decides). Run on 2026-11-08, branch head `ceb3d66`.

## 0. Verdict in one table

| row | criterion (short) | result |
|---|---|---|
| Z-1 | decisions equal to today, mono and split | **pass** (20/20 cases mono, 20/20 split, incl. the five decisive subjects) |
| Z-2 | counts exact | **pass** (every case equal; decisive 3 calls / 9 DB mono, 3 RPC / 9 provider DB / 0 consumer DB split; outsider 1/3; `Credits` 1/4). PA-3: equal too. Witness fix: 2/6 as its own row |
| Z-3 | order and laws | **pass** (provider call order equal in every split case; the six algebra laws plus five generated-atom laws pass, incl. a raising atom and an absent owner) |
| Z-4 | tier pins | **pass** (claims-only policies with the generated `Subscribed` depend on no `get_db`; 0 DB, 0 RPC steady) |
| Z-5 | metadata on the request path | **pass with K6** (0 describe `GET`s on a first request; without K6 equal to today). The fact describe of `Subscribed` (`POST billing.facts.v1/describe`) remains: K7, not in scope |
| Z-6 | instructions / latency | **pass** (cachegrind mono: decisive ≤ 5,865,621 Ir/req on every run, budget 5,885,658; `OrgRole`, `SubscriptionActive` inside budget; split rusage +0.1 %; no p50 > +50 %, no p99 > 2×). K6 −9 %, witness −12 % on the decisive shape; PA-3 +20 % consumer / +29 % provider instructions in the split |
| Z-7 | typing | **pass** (mypy: 3 errors on the generated atoms where today's atoms give 1) |
| Z-8 | archcheck from metadata | **pass** (third-party fixture atom found by gate-order, C027 and the sensitive-route rule with no class name in a list; a hand-written `Requirement` calling a declared check is recognised and its tier and risk checked; hand edit of the literal fails `generate --check` T007; lowering the provider's risk fails `generate` T004) |
| Z-9 | fail-closed | **pass** (absent 501; down 503, never allow, under `Not` too; with K5 `OrgRole \| SubscriptionActive` with tenancy down allows only on the subscription, deny-log line and metric per indeterminate branch; an RPC authorization refusal stays an error). One intended decision change: `Subscribed(of=Account.user())` with billing deployed nowhere is 501 instead of today's 200 |
| Z-10 | owner absent; stale consumer | **partial** (absent: 501 and the startup report; a provider newer than the consumer's stub is refused at startup by `CONTRACT_COMPAT_CHECK=strict` and never allows without it. **Fail:** a shape mismatch answers **422** `contract_argument_invalid` and raises out of an `Or`, not 501/503) |
| Z-11 | generation | **pass** (body change writes nothing; declaration change rewrites stub + atom + snapshot; byte-identical under two `PYTHONHASHSEED`s) |
| Z-12 | kernel boundary | **partial** (adding `QuotaAvailable` needs no kernel change: pass. The move itself was not done: today's kernel atoms stay beside the generated ones, 105 lines in `shared/authz` still name tenancy/billing vocabulary; 51 code files and 13 doc pages use the kernel atoms) |
| Z-13 | `Feature` / `WithinLimit` moved | **not run** (outside the assigned build scope) |
| Z-14 | `past_due` table | **recorded** (decisions equal base vs candidate, mono and split, both settings; the generated `Subscribed` reads the same fact path, so staleness is today's) |
| Z-15 | DX task | **recorded** (consumer atom lines 31 → 0, consumer advanced names 4 → 0, hand-edited files 7 → 5, framework errors 6 → 1; PA-1 control: 14 consumer lines + a 33-line base, invisible to every static rule) |
| Z-16 | full suite | **pass** (`sandloom test --full`: 6458 passed, 14 gates PASS; split conformance 27 passed) on the final candidate, after gate fixes the first run named |

Exit rule of §8.2 (Z-1…Z-5, Z-7…Z-12, Z-16 pass; Z-6 within budget or reported): **not
met as written**, on two rows: Z-10 (a contract-shape mismatch answers 422, not an
indeterminate 501/503) and Z-12 (the kernel atoms were not removed in this build). Neither
is a failure of the mechanism's equality claims (Z-1…Z-6 pass); both are listed in §4 as what
the ADR must require.

## 1. What was built

All framework code lives in a throwaway worktree outside the repository
(`$S/wt`, `$S` = the session scratch directory); the change is kept as
[patches/kb-poc-authz.patch](patches/kb-poc-authz.patch) (against `ceb3d66`, file list in
[patches/README.txt](patches/README.txt)). Nothing under `src/` or `packages/` of this checkout
changed.

| piece | where (in the patch) | what |
|---|---|---|
| `access_check(name, bind=, category=, values=, risk=, describe=, witnesses_membership=)`, `From.subject_id()/org()/account_type()/account_id()` | `shared/authz/contract_check.py`, exported by `sdk.authz` | a declaration on one boolean contract read method; a JSON value table on the function |
| describe / snapshot | `core/module_api/_schema.py` | the declaration is part of the method's describe document and `contracts.snapshot.json` |
| compat rule | `core/contracts/compatibility.py` | removing, re-binding or lowering the risk of a declared check is incompatible (T004) |
| generator | `core/cli/generated.py` (`render_stub`) | writes, after the Protocol and the `ContractRef`, one typed `ContractCheck` subclass per declared method with `__sandloom_atom__` as a literal, and one `ClaimFactCheck` subclass per owner `AccessFact(atom=...)`; formatted by the project's ruff; stub compatibility check unchanged |
| evaluator | `ContractCheck` (`contract_check.py`) | the one generic, domain-free evaluator: tier/category/`contracts()`/`org_params()`/`risks()`/`describe()` from the literal; binds subject, organization (`resolve_org_id`) or account (`resolve_account`), calls `resolve_contract(...)` and the one method with the arguments in signature order; absent → 501 as today |
| claim class | `ClaimFactCheck` | `Subscribed`: claim tier, today's fact path (`lookup_access_fact(..., owner=)`), delegation to the owner's declared `SubscriptionActive` for an organization account |
| providers | `sandloom_tenancy/contracts.py`, `sandloom_billing/contracts.py`, `sandloom_billing/access.py` | `has_role_at_least` → `OrgRole` (org bind, `values`, admin risk table, membership witness); `check_subscription_active` → `SubscriptionActive`; new `has_credits_at_least` → `Credits` (answer #24: the same read as `get_credit_balance`); `SUBSCRIPTION_ACTIVE_FACT` gains `atom="Subscribed"` |
| generated | `shared/contracts/tenancy_membership_v1.py`, `billing_entitlements_v1.py` | the four atoms, next to the existing stubs (decision B decides where bundled stubs live) |
| K1 | `witness.py`, `account.py` `_is_member` → `witness_is_member` | tenant kind asks the declared membership witness (`has_role_at_least(org, user, "member")`, today's call) when one is imported, else the legacy adapter slot |
| archcheck | `core/archcheck/atom_metadata.py`, `module_rules.py`, `api10_rules.py`, `error_codes.py` (C032), `runner.py` | rules read the literal (imported name → file on `sys.path`, AST, no import); new per-file rule `handwritten-access-check` (SANDLOOM-C032) |
| K5 (switch `KB_POC_K5=1`) | `ContractCheck.evaluate` | a bound provider that is down (`remote_service_unavailable`, `rpc_timeout`) → indeterminate 503 `owner_unavailable`, a warning per indeterminate branch and `access_indeterminate_branch_total`; any other RPC failure (authentication, allowlist) propagates |
| K6 (switch `KB_POC_K6=1`) | `algebra.py` `needs_request_session()`, `deps.py`, `rpc/readiness.py` | `require()` asks the policy (contract checks say False) instead of `tier > CLAIM`; the boot probe describes every remote binding and seeds the proxy's describe cache |
| witness 3→2 (switch `KB_POC_WITNESS=1`) | `ContractCheck._witness` | an allowing check with `witnesses_membership` records `organization` in the request's tenant-kind memo |
| PA-3 comparison | `facts_contract.py`, `facts.py`, `atoms_fact.py` | `facts.v1.evaluate(name, subject, request, params)`, `Entitled(name, params=)`; the stand's `pa3` provider module registers `pa3.org_role` / `pa3.subscription_active` |

The separate changes sit behind module constants (`contract_check._K5`, `._WITNESS`,
`deps._K6`, `readiness._K6_SEED`), all `False` in the patch, so the applied candidate is
PA-2 alone; the stand sets them from `KB_POC_K5` / `KB_POC_K6` / `KB_POC_WITNESS` before it
builds the app (`stand.py` `_poc_switches`), so each change runs as its own measurement on
one build. A real change would be unconditional. (The first full-suite run had the
switches as environment reads in the kernel and failed the raw-environment and
import-side-effect pins; they were moved to the stand, see Z-16.)

Example of a generated atom (`shared/contracts/tenancy_membership_v1.py`, after the stub):

```python
class OrgRole(ContractCheck):
    """True when the user's role ranks at least ``minimum`` ..."""

    __sandloom_atom__: ClassVar[dict[str, Any]] = {
        "contract": "tenancy.membership.v1",
        "method": "has_role_at_least",
        "args": ["organization_id", "user_id", "minimum"],
        "binds": {"organization_id": ["org", "org_id", False], "user_id": ["subject"]},
        "tier": 3,
        "category": "forbidden",
        "values": {"minimum": ["member", "admin", "owner"]},
        "risk": {"admin": {"minimum": ["admin", "owner"]}},
        "describe": "organization role '{minimum}'",
        "witness": {"organization_id": "org", "user_id": "subject", "minimum": "member"},
    }

    def __init__(
        self, minimum: Literal["member", "admin", "owner"], *, org_param: str = "org_id"
    ) -> None:
        super().__init__({"minimum": minimum}, org_param=org_param)
```

The route's expression is unchanged; only the import differs
([stand/authzpa2/api/__init__.py](stand/authzpa2/api/__init__.py) vs
[stand/authzstand/api/__init__.py](stand/authzstand/api/__init__.py)):

```python
from sandloom.shared.contracts.billing_entitlements_v1 import SubscriptionActive
from sandloom.shared.contracts.tenancy_membership_v1 import OrgRole
BOUND_ADMIN_AND_SUB = OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant())
```

## 2. Method

- **Stand**: [stand.py](stand.py) is perf-dx's `authz_stand.py` with the same method
  (counts by `log_statement=all` between markers per database role, Redis `MONITOR`, the
  provider's access log; cachegrind ladder `Ir/op = (Ir(K) − Ir(0)) / K`, K = 200; rusage and
  latency in-process at concurrency 1), extended with variants, subjects and topologies (its
  docstring). Modules: `authzstand` (base, kernel atoms; perf-dx's module plus two routes),
  `authzpa2` (generated atoms), `authzpa3` (+ provider module `pa3`).
- **Base and candidate in one session**: base = a second worktree at `ceb3d66` (`$S/base`),
  candidate = `$S/wt` with the patch. Same seeded database (`benchsuite.platform seed`,
  [seed_billing.sql](seed_billing.sql), [seed_poc.sql](seed_poc.sql): MEMBER is `member` of ORG;
  ORG2 has USER as `admin` and a canceled subscription).
- **Two processes, consumer without tenancy and billing**: the consumer runs in a fresh venv
  with only the `sandloom` and `sandloom-identity` wheels built from the side under test
  (`$S/venv-consumer-{base,wt}`), started with `python -I`; tenancy and billing run in a
  `uvicorn` provider process from the side's own venv, bound by `SERVICE_BINDINGS`:

  ```
  $ venv-consumer-wt/bin/python -I -c "import importlib.util as u; print(u.find_spec('sandloom_tenancy'), u.find_spec('sandloom_billing'))"
  None None
  $ venv-consumer-wt/bin/python -I -c "import sandloom_tenancy"
  ModuleNotFoundError: No module named 'sandloom_tenancy'
  $ venv-consumer-wt/bin/python -I -c "import sandloom_billing"
  ModuleNotFoundError: No module named 'sandloom_billing'
  $ uv pip list --python venv-consumer-wt/bin/python | grep sandloom
  sandloom                  0.7.0a11
  sandloom-identity         0.7.0a11
  ```
- **Own infrastructure**: PostgreSQL 16 `kb-pocauthz-pg` (127.0.0.1:47533, `log_line_prefix
  '%m [%p] %u@%d '`), Redis 7 `kb-pocauthz-redis` (47536, databases 3–6, 9, 10, 14, 15; db 0
  unused) and `kb-pocauthz-redis-test` (47537, 256 databases, for the suites);
  `SANDLOOM_TEST_OWNER=kb-poc-authz`. Removed after the runs.
- **Machine**: Apple M-series, 10 cores, shared with other agents; load average 16–21 during
  every run (perf-dx: ≈ 8). Counts and cachegrind are unaffected; latency is not comparable
  across sessions and is only compared base vs candidate inside this one.
- Reproduce: `. ./env.sh` (interpreters, URLs, `stand <side> <variant> <topology> <cmd>`,
  `rewheel`), then the commands quoted per row. `compare.py` and `render_counts.py` print the
  tables below from `results/`.

## 3. Rows

### Z-1 decisions

```
$ stand base authzstand split decisions --times 2 --out results/dec_base_split.json
$ stand wt   authzpa2   split decisions --times 2 --out results/dec_pa2_split.json
$ python3 compare.py results/dec_base_split.json results/dec_pa2_split.json
equal bound.orgrole_and_subactive (200, None)
equal bound.member (403, 'OrgRole')
equal bound.admin_inactive (402, 'SubscriptionActive')
equal bound.outsider (403, 'OrgRole')
equal bound.no_tenant (400, None)
... (15 more)
20/20 cases equal
$ python3 compare.py results/dec_base_mono.json results/dec_pa2_mono.json
20/20 cases equal
```

"Equal" = status, denial code and the provider calls in order. The outsider's evaluation
stops at `OrgRole` (one `has_role_at_least` call, `SubscriptionActive` not called) on both
sides. The no-tenant case is refused by `TenantScope()` (400, "X-Tenant-Id is required…")
before the policy, identically. Denial texts are equal except where a 501 names the
contract (`contract 'tenancy.membership.v1' is not configured` vs `Tenancy membership is not
configured`): the evaluator names no module.

### Z-2 counts (exact, steady state, per request)

`stand <side> <variant> {mono,split} counts --m 20`, then
`python3 render_counts.py results/counts_authzstand_<t>.json results/counts_authzpa2_<t>.json ...`.
Cell = DB round trips (database role) · Redis commands · RPC requests.

| case | base mono | PA-2 mono | base split | PA-2 split | PA-3 split | PA-2 + witness split |
|---|---|---|---|---|---|---|
| `bound.orgrole_and_subactive` (decisive) | 9 · r1 · 0 | 9 · r1 · 0 | 9 provider · r1 · 3 | 9 provider · r1 · 3 | 9 provider · r1 · 3 | **6 provider · r1 · 2** |
| `bound.admin_inactive` (402) | 9 · 0 | 9 · 0 | 9 · 3 | 9 · 3 | 9 · 3 | **6 · 2** |
| `bound.member` (403) | 3 · 0 | 3 · 0 | 3 · 1 | 3 · 1 | 3 · 1 | 3 · 1 |
| `bound.outsider` (403) | 3 · 0 | 3 · 0 | 3 · 1 | 3 · 1 | 3 · 1 | 3 · 1 |
| `orgrole` | 3 | 3 | 3 · 1 | 3 · 1 | — | 3 · 1 |
| `subactive` | 3 | 3 | 3 · 1 | 3 · 1 | — | 3 · 1 |
| `credits` (`get_credit_balance` → `has_credits_at_least`) | 4 | 4 | 4 · 1 | 4 · 1 | — | 4 · 1 |
| `subscribed`, `role`, `perm`, controls | 0 | 0 | 0 · 0 | 0 · 0 | — | 0 · 0 |

All 20 cases are equal base vs PA-2 in both topologies (`render_counts.py` prints `yes` on
every row), the first request included (split: `subscribed 2/2, subactive 2/2, orgrole
2/2, bound 3/3, …`). The consumer issues no DB statement in the split; Redis is the one
revocation `GET` everywhere.

**PA-3** (`Entitled(<fact>, params=...)` over the patched `pa3.facts.v1.evaluate`): steady
counts equal too. The inferred extra provider unit (alternatives §5.4) does not appear: the
provider's `FactsContract.evaluate` unit issues no statement of its own, the inner contract
call's lease is the only one (`results/counts_authzpa3_split.json`, sample:
`BEGIN … READ ONLY; set_config` · `SELECT e.role …` · `COMMIT` ×2, `BEGIN` · `SELECT billing_customers…` ·
`ROLLBACK`). Its first request costs 6 RPC instead of 3 (the fact describe). Its cost
difference is therefore in instructions (Z-6), the binding (`pa3.facts.v1` instead of the
domain contracts), the string-keyed parameters and the 403/402 texts
("requires entitlement 'pa3.org_role'"), not in calls.

**Witness fix (answer #23, its own run, `KB_POC_WITNESS=1`)**: 2 calls / 6 DB mono, 2 RPC / 6
provider DB split, decisions unchanged (`compare.py`: only the second `has_role_at_least`
call disappears from `bound.orgrole_and_subactive` and `bound.admin_inactive`). Freshness is
unchanged: the membership is the answer the same request received a moment before.

### Z-3 order and laws

- Order: the split decisions carry the provider's access log per case; the calls and their
  order are equal on all 20 cases (Z-1). Example, decisive shape, both sides:
  `has_role_at_least`, `has_role_at_least`, `check_subscription_active`.
- Laws: [tests/test_laws_generated_atoms.py](tests/test_laws_generated_atoms.py) runs the laws'
  own model with every tier-3 leaf replaced by a generated `ContractCheck` subclass (literal
  as `sandloom generate` writes it) over a fake core: T/F from the provider's answer, U from an
  absent owner (501).

  ```
  $ cd $S/wt && .venv/bin/python -m pytest -q -p no:cacheprovider -rA poc/authz/tests/test_laws_generated_atoms.py tests/laws/test_authz_algebra_laws.py
  PASSED test_generated_atoms_keep_value_and_order
  PASSED test_generated_atoms_not_keeps_an_indeterminate
  PASSED test_generated_atoms_fail_closed
  PASSED test_a_raising_generated_atom_never_allows
  PASSED test_absent_owner_is_indeterminate_501_even_negated
  PASSED test_value_and_evaluations_equal_the_documented_rule   (+5 more laws)
  11 passed
  ```
  `~(U & F)` stays indeterminate and `~(F & U)` allows (`test_the_known_order_dependence_is_kept`
  and the generated variant of the first law). (`results/z3_laws.txt`)

### Z-4 tier pins

```
$ .venv/bin/python poc/authz/tests/z4_tier_pins.py            (KB_POC_K6 off | =1)
tier=1 get_db=False Subscribed(of=Account.user())                         | False
tier=1 get_db=False Role('admin') & Subscribed(of=Account.user())         | False
tier=1 get_db=False Perm('x') | Subscribed(of=Account.user())             | False
tier=3 get_db=True  Subscribed(of=Account.bound_tenant())                 | True
tier=3 get_db=True  SubscriptionActive(of=Account.user())                 | False
tier=3 get_db=True  Role('admin') | Credits(10, of=Account.user())        | False
tier=3 get_db=True  OrgRole('admin') & SubscriptionActive(of=Account.bound_tenant()) | False
```

Claims-only policies with the generated `Subscribed` open no session and make no call
(Z-2: 0 DB, 0 RPC). Live policies depend on `get_db` exactly as today; with K6 only a policy
that may use `ctx.db` does (the claim class does not yet say False for an organization account).

### Z-5 metadata on the request path

```
$ KB_POC_K6=1 stand wt authzpa2 split counts --m 20 --out results/counts_authzpa2_k6_split.json
$ python3 render_counts.py results/counts_authzpa2_split.json results/counts_authzpa2_k6_split.json | tail -1
first request, RPC: subscribed 2/1, subactive 2/1, credits 1/1, orgrole 2/1, bound 3/3, ...
$ grep "GET /_sandloom" results/counts_authzpa2_k6_split.provider.log
"GET /_sandloom/rpc/billing.entitlements.v1" 200   <- boot probe, one per binding
"GET /_sandloom/rpc/billing.facts.v1" 200
"GET /_sandloom/rpc/tenancy.membership.v1" 200
```

Without K6 the first request per contract adds the proxy's describe `GET`, as today. With K6
the first request makes 0 describe `GET`s. Finding: in a consumer that does not ship the
provider's package, today's boot probe runs only for contracts with a known snapshot, so in
this PoC's consumer nothing was probed at boot and nothing could be seeded. K6 therefore
also probes every remote binding at boot (3 `GET`s at start, the same count perf-dx's
consumer made). The `Subscribed` fact describe (`POST billing.facts.v1/describe`, every 300 s)
remains: K7.

### Z-6 instructions and latency

**cachegrind, monolith** (`SIDE=<side> VARIANT=<variant> ./cg_poc.sh <case>` via
`cg_run.sh` over [cg_jobs2.txt](cg_jobs2.txt) and [cg_jobs3.txt](cg_jobs3.txt), 4 in
parallel, `python -B`, K = 200; raw lines `results/cg.txt`, `results/cg_final.txt`, run logs
kept in `$S/cglogs`). Ir per request:

| case | base (runs) | candidate PA-2 (runs; the last on the final build where present) | Δ means | perf-dx budget (+1 %) |
|---|---|---|---|---|
| `OrgRole & SubscriptionActive(of=bound_tenant)` | 5,838,040 · 5,771,173 | 5,865,621 · 5,841,503 · 5,804,676 | +0.6 % | ≤ 5,885,658: **pass** (max 5,865,621) |
| `OrgRole("admin")` | 2,521,037 · 2,605,972 | 2,539,677 · 2,570,931 | −0.3 % | ≤ 2,650,792: **pass** |
| `SubscriptionActive(of=user)` | 4,222,177 · 4,199,077 | 4,145,057 · 4,167,516 | −1.3 % | ≤ 4,182,196: **pass** |
| `Credits(10, of=user)` (`has_credits_at_least`) | 4,636,507 | 4,493,594 | −3.1 % | (4,523,383 + 1 %): pass |
| `Subscribed(of=user)` | 1,294,145 | 1,246,876 · 1,228,980 | −4 % | inside the control's band |
| control | 1,145,975 · 1,158,109 | 1,149,465 · 1,109,805 | −1.9 % | — |
| `TenantScope()` control | 1,319,070 · 1,366,188 | 1,325,378 · 1,312,844 | −1.8 % | — |
| decisive, **+K6** | | 5,281,253 · 5,297,506 | −8.9 % vs base | |
| decisive, **+witness** | | 5,115,830 · 5,132,642 | −11.7 % vs base | |
| decisive, **PA-3** (`Entitled(params=)`) | | 5,911,284 | +1.8 % vs base | |

Spread between repeats of one case on one side: up to 3.4 % (`OrgRole` base), 1.2 % (decisive
base), so every PA-2 difference is inside the repeat noise; the decisive shape stays within
perf-dx's +1 % budget on every run. A first batch (`results/cg_batch1.txt`) is not used: a
Python 3.13 run of the DX app had written `.pyc` files into the candidate's mounted tree midway,
so some N = 0 / N = K pairs straddled two boot costs (two negative results); its unaffected
pairs agree (decisive 5,854,383 base vs 5,875,752 / 5,836,699 candidate; `OrgRole` 2,581,640 vs
2,606,424).

**rusage and latency** (`./run_perf.sh`: 2 runs × 3 repetitions × 1,000 requests per case, base
and candidate alternating, mono and split; `python3 render_run.py results/run_*.json`). Medians;
instructions in thousands per request; latency µs, concurrency 1, machine load ≈ 20:

| case | topology | base instr (consumer / provider) | PA-2 instr | base p50 / p95 / p99 | PA-2 p50 / p95 / p99 |
|---|---|---|---|---|---|
| decisive | mono | 5,779 · 5,759 | 5,783 · 5,812 | 7,438 / 10,455 / 11,756 · 6,862 / 10,361 / 11,372 | 7,650 / 10,661 / 12,601 · 7,439 / 10,599 / 11,967 |
| decisive | split | 4,761 / 6,428 · 4,755 / 6,430 | 4,761 / 6,446 · 4,767 / 6,428 | 7,734 / 12,172 / 13,918 · 7,846 / 12,323 / 14,495 | 8,018 / 12,398 / 13,694 · 8,126 / 12,323 / 14,173 |
| `OrgRole` | split | 2,708 / 1,656 · 2,707 / 1,652 | 2,709 / 1,655 · 2,706 / 1,650 | 3,688 / 5,436 / 6,723 · 3,260 / 5,295 / 6,646 | 3,340 / 5,500 / 6,483 · 2,815 / 4,992 / 5,940 |
| `SubscriptionActive` | split | 2,540 / 3,034 · 2,539 / 3,021 | 2,540 / 3,055 · 2,541 / 3,037 | 3,319 / 6,092 / 7,112 · 3,193 / 5,847 / 6,855 | 3,277 / 5,828 / 6,641 · 3,285 / 5,731 / 6,693 |
| `SubscriptionActive` | mono | 3,968 · 3,952 | 3,965 · 3,983 | 3,256 / 5,643 / 6,796 · 2,786 / 5,573 / 6,995 | 3,849 / 5,737 / 6,996 · 3,751 / 5,733 / 6,620 |
| decisive +K6 | split | | 4,272 / 6,465 | | 7,432 / 11,909 / 13,482 |
| decisive +witness | split | | 3,790 / 4,746 | | 5,574 / 9,265 / 10,900 |
| decisive PA-3 | split | | **5,718 / 8,264** | | 7,941 / 11,916 / 14,111 |

- Split instructions: consumer +0.1 %, provider +0.1 % (budget +3 %): **pass**.
- Latency: no p50 increase above 50 %, no p99 above 2×: **pass**. One pattern recorded
  without a cause: `SubscriptionActive` in the monolith has a 25–40 % higher p50 on the
  candidate in every run (incl. a re-run with the candidate first, `results/run_*_mono_order_*`),
  with equal instructions, equal p95/p99 and equal split latency; wall time only.
- **PA-3** costs +20 % consumer and +29 % provider instructions on the decisive shape in the
  split against PA-2 (5,718 / 8,264 vs 4,761 / 6,446 k): the facts envelope (subject and
  request to wire, `evaluate` dispatch, the resolver's local contract call) is CPU, not round
  trips.

### Z-7 typing

```
$ .venv/bin/python -m mypy --strict --follow-imports=silent typing/z7_typing.py      (candidate)
z7_typing.py:13: error: Argument 1 to "OrgRole" has incompatible type "Literal['admn']"; expected "Literal['member', 'admin', 'owner']"  [arg-type]
z7_typing.py:14: error: Missing named argument "of" for "SubscriptionActive"  [call-arg]
z7_typing.py:15: error: Argument 1 to "Credits" has incompatible type "str"; expected "int"  [arg-type]
Found 3 errors in 1 file
$ (base, the same three lines against today's kernel atoms)
z7_typing_today.py:7: error: Argument 1 to "Credits" has incompatible type "str"; expected "int"  [arg-type]
Found 1 error in 1 file
```

At run time the generated `OrgRole("admn")` still raises `ValueError` (the `values` table)
and `SubscriptionActive()` raises `TypeError`.

### Z-8 archcheck from metadata

Fixture: a third-party provider [z8/z8provider.py](z8/z8provider.py) declaring
`ProjectRole` (org bind, admin risk for `lead`/`owner`) and `SeatsAvailable` (account bind);
its stub written by the generator's own `render_stub` ([z8/make_stub.py](z8/make_stub.py));
a consumer [z8/app/z8app/modules/reports/api.py](z8/app/z8app/modules/reports/api.py).
Neither name is in any list.

```
$ PYTHONPATH=app python run_rules.py        (candidate | base)
guard-order:  Line 23: a require(...) gate that needs an organisation is declared before TenantScope() (board)   | (nothing)
C027:         Line 31: route '/orgs/{org_id}/seats' carries an organization parameter but SeatsAvailable(of=Account.user()) checks the caller's personal account   | (nothing)
C022:         Line 53: POST /orgs/{org_id}/notes (viewer gate) ... declares no risk   | Line 42 settings (lead gate) AND Line 53 notes
C032:         Line 60: CheapLead calls the declared access check projects.roles.v1.has_project_role with tier 1 (declared 3)
              Line 60: CheapLead ... does not state its declared risk ['admin'] (risks())   | rule absent
              (HonestLead, same call with tier, category and risks() stated: no report)
```

```
$ (hand edit) "risk": {"admin": ...} -> "risk": {} in shared/contracts/tenancy_membership_v1.py
$ sandloom generate --check
generated files are stale: run `sandloom generate` (1 file(s)):
  src/sandloom/shared/contracts/tenancy_membership_v1.py [SANDLOOM-T007] (differs)
$ (provider) risk={"admin": {"minimum": ["owner"]}} in sandloom_tenancy/contracts.py
$ sandloom generate
src/sandloom_tenancy/contracts.snapshot.json: error[SANDLOOM-T004]: incompatible change: tenancy.membership.v1.has_role_at_least: access check risk 'admin' lowered for 'minimum'
sandloom generate: nothing was written
```

(`results/z8_rules.txt`, `results/z8_handedit.txt`). Limits found: the static gate-order rule
reads only an `APIRouter(...)`'s dependencies, not `module_router()` (pre-existing; the
fixture uses `APIRouter`); C032 sees a hand-written class that calls `<client>.<method>(...)`
directly, not one that calls through `getattr` (the PA-1 base below is invisible to it). A
module can still hand-roll an authority check the analyser does not see (answer #22: route
classification and runtime checks remain the backstop).

### Z-9 fail-closed

```
$ stand base authzstand split-tenancy-down decisions ...   (membership bound to a closed port)
orgrole 503 remote_service_unavailable | bound 503 | or_sub_user.admin 503 | not_member.admin 503
$ stand wt authzpa2 split-tenancy-down decisions ...       (no K5: identical, 4/4 equal)
$ KB_POC_K5=1 stand wt authzpa2 split-tenancy-down decisions ...
WARNING sandloom.access: access check indeterminate: owner_unavailable (organization role 'admin', tenancy.membership.v1)
WARNING sandloom.access: access denied: owner_unavailable (organization role 'admin')
orgrole 503 owner_unavailable
bound.orgrole_and_subactive 503 owner_unavailable
WARNING sandloom.access: access check indeterminate: owner_unavailable (organization role 'admin', tenancy.membership.v1)
or_sub_user.admin 200   (OrgRole("admin") | SubscriptionActive(of=Account.user()): the subscription allowed)
not_member.admin 503 owner_unavailable   (~OrgRole("member"): never allow)
metrics {"access_denied_total": 3, "access_indeterminate_total": 3, "access_indeterminate_branch_total": 4}
$ KB_PROVIDER_DENY=tenancy.membership.v1 KB_POC_K5=1 stand wt authzpa2 split decisions ...
orgrole | bound | or_sub_user.admin | not_member.admin: 503 rpc_caller_forbidden (an error, not an indeterminate; the Or does not allow)
$ stand {base,wt} {authzstand,authzpa2} split-nobind decisions ...   (no binding at all)
orgrole, bound, or_sub_user, not_member, credits: 501 service_unavailable on both sides
subscribed: base 200 | candidate 501 fact_unregistered
```

(`results/k5_runs.txt`, `results/nobind_runs.txt`). The one decision change: today's
`Subscribed(of=Account.user())` with billing deployed nowhere reads `subscription_status`
verbatim from whatever wrote it (200, `atoms_claims.py:184-188`); the generated class answers
501 (the C-F5 silent path closed). It needs the owner's approval as a behaviour change.

### Z-10 owner absent; stale consumer declaration

- Owner absent: 501 (Z-9) and reported at startup by the derived `consumes`:
  `authzpa2 consumes optional contract 'tenancy.membership.v1', which is neither provided
  locally nor bound in SERVICE_BINDINGS (route policy: (organization role 'admin' OR active
  subscription); …)` — the same line as for today's atoms.
- Provider newer than the consumer's generated stub (`has_role_at_least` gained a required
  `scope`), consumer compat check off: every gate answers **422**
  `tenancy.membership.v1.has_role_at_least: missing a required argument: 'scope'`; never an
  allow, but not an indeterminate either, so `OrgRole | SubscriptionActive` raises instead of
  allowing on the subscription.
- Same with `CONTRACT_COMPAT_CHECK=strict` and the consumer-side snapshot of
  `tenancy.membership.v1`: the consumer refuses to start —
  `Remote contract(s) incompatible with this release: … has_role_at_least(scope): new required
  parameter (callers do not pass it)` → `CriticalStartupError`. A consumer that does not ship
  the provider's snapshot is not checked (the warning "No contract snapshot for remote
  contract …").
- Consumer newer than the provider (C-B1): the generator refuses an added optional parameter
  on the same `.v1` (`SANDLOOM-T007: method 'has_role_at_least' changed`), so the skew cannot
  be generated within one version. Simulated by editing the consumer's installed stub: every
  gate answers **422** `too many positional arguments`.
- **Fail against the criterion** (501/503): a contract-shape mismatch at an access check is
  surfaced as the provider's 422. The fix is one mapping in the evaluator
  (`ContractArgumentError` from a declared check → indeterminate 501 `contract_mismatch`),
  not built. (`results/z10_runs.txt`)

### Z-11 generation

```
$ (provider body change only) sandloom generate
generated files current (51 file(s))
$ (access_check describe= changed) sandloom generate
wrote src/sandloom_tenancy/contracts.snapshot.json
wrote src/sandloom/shared/contracts/tenancy_membership_v1.py
$ PYTHONHASHSEED=1 sandloom generate; PYTHONHASHSEED=4242 sandloom generate --check
generated files up to date (51 file(s))
745e16dc…33db  tenancy_membership_v1.py   (identical sha256 after a regeneration under each seed)
44d2ecde…ae55  billing_entitlements_v1.py
```

(`results/z11_generation.txt`)

### Z-12 kernel boundary

- Adding `QuotaAvailable` (Z-15's DX app, framework = the candidate worktree): one decorator
  in the provider, zero files of the framework edited.
- The move itself was not part of this build. The kernel's `OrgRole`, `SubscriptionActive`,
  `Credits`, `Subscribed`, the adapter table and `WELL_KNOWN_FACT_OWNERS` remain, beside the
  generated atoms, so every existing caller works unchanged.
  `grep -rnE "billing|tenancy|OrgRole|SubscriptionActive|Credits\b|Subscribed|ACTIVE_SUBSCRIPTION|membership" src/sandloom/shared/authz --include="*.py" | wc -l`
  → 105 on the final candidate (in `contract_check.py` and `witness.py` only docstrings and
  the generic word "membership"; the rest is today's kernel atoms, adapters and fact owners).
  The migration it implies: 51 code files call a kernel atom outside `shared/`
  (`src`, `examples`, `tests`, `benchmarks`), 13 doc pages name them.

### Z-13 `Feature` / `WithinLimit`

Not run: the assigned build scope does not include it (answer #21 research stays open).

### Z-14 `past_due`

`BILLING_DENY_ON_PAST_DUE` set per process, a token with `subscription_status=past_due`,
`Subscribed(of=Account.user())`:

| setting | mono base | mono PA-2 | split base | split PA-2 |
|---|---|---|---|---|
| false (grace) | 200 | 200 | 200 | 200 |
| true | 402 | 402 | 402 | 402 |

Change during a token's life (code, both sides the same path `lookup_access_fact` →
`fact.interpret_claim`, `facts.py`): a status change is seen when the token is refreshed
(claim ≤ token TTL); a setting change is seen at once in the monolith (the rule factory runs
per evaluation) and within `REMOTE_FACT_TTL_SECONDS` = 300 s in a split consumer (cached
describe). The generated class keeps exactly this; K7 (refresh off the request path) would
change only where the 300 s describe happens. (`results/z14_past_due.txt`)

### Z-15 DX task

Re-run of perf-dx's task on the candidate (`sandloom new dxquota --framework-path $S/wt`),
by the agent that built the mechanism: there is no documentation of `access_check`, so doc
lookups are not comparable and are not counted. Files: [results/dx/](results/dx/).

| measure | today (perf-dx §3) | PA-2 | PA-1 control |
|---|---|---|---|
| consumer atom lines | 31 (`reports/access.py`) | **0** | 14 (+ a 33-line base the kernel would ship) |
| consumer advanced names | 4 (`Requirement`, `TIER_LIVE`, `AuthzContext`, `Category`) | **0** (imports `QuotaAvailable` from the app's generated stub) | 1 (`Category`) + 3 in the base |
| provider additions | 0 | 1 decorator (6 lines) + 3 advanced names (`Category`, `From`, `access_check`) | 0 |
| hand-edited files | 7 | 5 (`quota/contracts.py`, `quota/services.py`, `reports/api.py`, two tests) | 6 |
| framework errors met | 6 | 1: `sandloom generate` imports the app, so a consumer that already imports the atom before its first generation fails with `ModuleLoadError` (the class of perf-dx ERR 2); recovered by 2 temporary edits | 1 (stale `module.snapshot.json` until `generate`) |
| framework commands | 19 (13 without recovery) | 15 (12 without recovery) | +2 |
| split wiring | derived | derived (`SERVICE_BINDINGS {"quota.limits.v1": …}`, `RPC_ALLOWED_CALLS {"reports": ["quota.limits.v1"]}` from the atom's `contracts()`; `add deploy --check`: boot quota OK, boot reports OK, split check OK) | derived |
| static checks see the atom | no | yes (literal) | no (C032 and every other rule: nothing; `getattr` call) |
| result | 201 / 402 mono and split | `sandloom test`: OK (10 gates); 201 then 402 `QuotaAvailable` via the real provider; fake provider 201/402 | `sandloom test`: OK |

Not counted against the design: perf-dx ERR 1–3 (replacing the generator's stand-in contract
methods) were avoided by adding `available` beside them; one own mistake (a Core
`count()` on a `TenantScoped` table, `UnsafeTenantOperationError`); the two-process run of the
DX app itself was not repeated (the decisive scenario runs two processes).

### Z-16 full suite

On the final candidate (the patch as committed), own test PostgreSQL database and a
256-database Redis:

```
$ cd $S/wt && SANDLOOM_TEST_DATABASE_URL=…/kbpoc_test2 SANDLOOM_TEST_CACHE_URL=redis://127.0.0.1:47537/120 \
    SANDLOOM_TEST_BROKER_URL=redis://127.0.0.1:47537/121 SANDLOOM_TEST_OWNER=kb-poc-authz \
    .venv/bin/sandloom test --full -n 6
===== 6458 passed, 10 skipped, 1 xfailed, 20 warnings in 246.36s (0:04:06) =====
======================= 1654 passed, 1 skipped in 18.46s =======================   (architecture)
  PASS ruff check · generated files · schema & deploy · full suite · ruff format · mypy ·
  sandloom check · architecture tests · lint-imports · contract snapshots · route snapshots ·
  migration drift · public API baseline · typing tests
$ SANDLOOM_TEST_REDIS_DB_STRIDE=auto .venv/bin/python -m pytest -q -n 4 --dist loadfile tests/split_conformance
27 passed in 42.56s
```

The first run (before the gate fixes) failed, each for a reason the fix names:
`test_raw_environment_reads_only_go_down` and `test_sdk_import_is_side_effect_free` (the
PoC switches read the environment in the kernel → moved to the stand),
`test_noqa_counts_only_go_down` (three function-local imports and one `assert` → a
`witness.py` module and hoisted imports), `test_every_api10_rule_has_its_appendix_a_code`,
`test_cli_help_lists_every_rule`, `test_repository_selection_is_complete_and_safe` (the new
rule and files had to be registered: code pin, `sandloom check` help, `public-source.json`),
two mypy errors, and two tests refused by a Redis ownership marker another test of the suite
had left on the shared index 1 (environment; gone with their own indexes). The changes were
behaviour-neutral refactors; decisions (20/20 mono and split), counts (all cases), K5 and the
witness run were repeated on the final build with the same results
(`results/dec_pa2_*_final.json`, `results/counts_authzpa2_*final*.json`).

## 4. What the ADR-C (portability part) must state

What this PoC shows ADR-C has to decide or state, in order of weight:

1. **Mechanism.** A domain atom is declared by its owner on one boolean contract read
   method (`access_check`); `sandloom generate` writes the typed class next to the contract
   stub with the declaration as a literal; one generic kernel evaluator (`ContractCheck`)
   evaluates every such atom. Claim-tier atoms are generated from the owner's `AccessFact`
   (`ClaimFactCheck`). The kernel names no module, contract, method or role word; the
   evaluator's binding sources are the kernel's subject, organization and account. A
   consumer process needs neither the provider's package nor any kernel change (Z-12a, the
   two-process runs).
2. **Equality is by construction and measured.** Same method, same arguments in signature
   order, same provider unit: every counted case equal mono and split, the decisive shape 3
   calls / 9 DB, call order equal, algebra laws unchanged (Z-1…Z-4). State it as the
   invariant a generated atom must keep, and keep the stand as its regression check.
3. **The literal is the single metadata source**: `require()` (tier, `contracts()`,
   `org_params()`, `risks()` → `sensitive_effect`), the static rules (AST, no import) and the
   snapshot read it; lowering a risk is a T004 incompatibility, a hand edit is a stale T007
   file (Z-8). Name `handwritten-access-check` (C032 here) as the rule for hand-written
   `Requirement`s calling a declared check, and say what it cannot see (`getattr` calls, an
   authority checked in a body): route classification and runtime checks stay the backstop.
4. **Decision changes to approve explicitly** (none is hidden by the PoC):
   - `Subscribed(of=Account.user())` with billing deployed nowhere: 200 today (the claim read
     verbatim) → 501 (C-F5 closed);
   - 501 texts name the contract (`contract 'tenancy.membership.v1' is not configured`) instead of
     a module ("Tenancy membership is not configured");
   - the generated `Subscribed` takes only `of=`: today's `statuses=` and `claim_key=` overrides
     are not carried (decide: drop, or declare them on the fact);
   - four beginner names change import (`OrgRole`, `SubscriptionActive`, `Credits`,
     `Subscribed`) while the expression stays the same: the ADR-0010 amendment (answer #18).
5. **K1 must be tied to part (a).** The PoC finds the membership witness through a
   process-global table filled when a generated atom class is defined (import time); with no
   witness imported it falls back to the legacy adapter slot. ADR-C must say: tenant kind
   asks the application-assigned tenant authority's declared witness (TA-1), found from the
   authority's generated stub, never by import order, and is `unknown` (indeterminate) without
   one.
6. **Separate changes, each with its own number**:
   - K5 (outage → indeterminate 503 inside the algebra, a log line and a counter per
     indeterminate branch, RPC authentication/allowlist failures stay errors): passes Z-9;
   - K6 (`needs_request_session()` per atom; describe seeding): passes Z-5 **only if the boot
     probe describes every remote binding** — today it probes only contracts with a known
     snapshot, so a consumer without the provider's package has nothing to seed;
   - witness 3→2 (answer #23): 2 calls / 6 DB, decisions equal, freshness equal (same
     request); it depends on `OrgRole` being evaluated before the account resolution (both
     tier 3, declaration order).
   Instructions on the decisive shape (cachegrind mono): PA-2 +0.6 % (noise), +K6 −9 %,
   +witness −12 %.
7. **Contract mismatch at an access check must be an indeterminate.** Today a provider whose
   method shape differs answers 422 out of the gate (and out of an `Or`). Map
   `ContractArgumentError` from a declared check to indeterminate 501 `contract_mismatch`
   (Z-10 fail), and recommend `CONTRACT_COMPAT_CHECK=strict` with the provider's snapshot in
   split consumers (it refuses startup on the same skew).
8. **Generation order (DX).** `sandloom generate` imports the app, so a consumer that imports
   an atom before its first generation cannot be generated (`ModuleLoadError`). Either the
   generator writes stubs without importing consumers, or the recipe/CLI orders provider →
   generate → consumer (`sandloom add access-check` could write both).
9. **PA-3 is not worse in calls** (no extra provider statement in the split, Z-2); what
   separates it from PA-2 is the binding to `<owner>.facts.v1` instead of the domain contract,
   string-keyed parameters with a second schema, mode chosen by configuration behind a fixed
   tier, 403/402 texts and +20 % consumer / +29 % provider instructions on the decisive shape
   in a split (Z-6). The ADR should cite these, not an extra hop.
10. **Where bundled atoms live** is decision B's: the PoC generates tenancy's and billing's atoms
    into `sandloom/shared/contracts/`, so the kernel wheel ships (does not import) module
    artifacts.
11. **Static gate-order gap (pre-existing):** the rule reads only an `APIRouter(...)`'s
    dependencies, not `module_router()` routes; SANDLOOM-B007 at startup is the check that sees
    them.

## 5. Files

- [stand.py](stand.py), [env.sh](env.sh), [compare.py](compare.py), [render_counts.py](render_counts.py),
  [cg_poc.sh](cg_poc.sh), [cg_run.sh](cg_run.sh), `cg_jobs*.txt`, seeds, stand modules under
  [stand/](stand/), PA-1 control under [pa1/](pa1/), Z-8 fixture under [z8/](z8/), tests under
  [tests/](tests/).
- [patches/kb-poc-authz.patch](patches/kb-poc-authz.patch): the candidate against `ceb3d66`.
- `results/`: every JSON and transcript quoted above.
