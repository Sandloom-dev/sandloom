# DX eval t9 (merged-main-2026-11-06): two findings

Run: `benchmarks/dx/results/merged-main-2026-11-06/t9_member_export.json`
(ref c270cbc, app mode, seed teamhub). Raw run directory:
`~/.cache/sandloom-dx-eval-portable/merged-main-2026-11-06/t9_member_export/`
(transcript, `changes.patch`, `gates/*.log`, `pristine/`).

## Finding 1: `Account.bound_tenant()` on a route without `org_id`

### What the agent wrote

`teamhub/modules/invites/api.py` (from `changes.patch`):

```python
PAID_ADMIN = Depends(
    require(OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant()))
)

@router.get("/invitations/export", dependencies=[PAID_ADMIN])
```

The invites routes have no `org_id` path or query parameter; `module_router()`
binds the tenant because the module has a `TenantScoped` table.

### What the agent could have written

- `Account.org(param="org_id")` (the recipe's form) is refused on this route:
  `find_account_route_mismatches` reports "Account.org(param='org_id') on route
  ..., which has no 'org_id' path or query parameter"
  (`src/sandloom/core/archcheck/api10_rules.py:550`, message at `:597`). So the
  recipe cannot be copied as is.
- The two accepted forms are `Account.org(param=None)` and
  `Account.bound_tenant()` (`docs/modules/http/authorization.md:137-138`).

### How each resolves on a tenant-bound route

The bound tenant is safe in both cases. `TenantScope` takes it from the signed
token; `X-Tenant-Id` only selects among the tenants the token grants (403
otherwise), and a revoked token is refused
(`src/sandloom/core/http/tenant_scope.py:49`, `:110-114`). A header that
disagrees with the bound tenant is `tenant_mismatch`
(`src/sandloom/shared/authz/_resolve.py:155-160`). So neither form can be
pointed at a foreign organization through the header.

| bound tenant is | `Account.org(param=None)` | `Account.bound_tenant()` |
|---|---|---|
| an organization the caller belongs to | that organization | that organization (after a membership-contract call, `account.py:153`, `:170`) |
| the caller's own user id (personal tenant) | indeterminate, 403 `not_an_organization` (`account.py:220`) | the caller's **personal** account (`account.py:240`) |
| an id the membership contract does not confirm | that id; billing's org atom then answers | indeterminate, 403 |
| nothing bound | 400 `tenant_context_missing` | 400 `tenant_context_missing` |

The only difference that matters: on a personal tenant, `bound_tenant()` checks
the caller's personal subscription, while `Account.org(...)` refuses. For this
route the difference does not reach the result. `OrgRole("admin")` reads the
same bound tenant (`atoms_live.py:207-224`, `org_param="org_id"` falls back to
the header or the bound tenant). It asks the membership contract for an admin
role in an organization whose id is the user's id. No such membership exists,
so the `&` denies. Under `OrgRole("admin") & ...`, both forms allow and deny the
same requests.

`bound_tenant()` also costs one more membership call per request (`tenant_kind`
is memoised, but `OrgRole` asks `has_role_at_least` separately).

### Verdict

**The checker is wrong (false negative).** The agent's gate checks the
organization's subscription for every request it allows, and it fails closed.
It is a documented account (`authorization.md:138`, G-AUTHZ-11). The check's
docstring calls `Account.user(reason=...)` the wrong account; `bound_tenant()`
is not that case.

`Account.org(param=None)` is the stricter, intent-matching form ("the
organization", never a personal account). Nothing tells an agent to use it: the
recipe (`src/sandloom/sdk/_prim/recipes.py:19-61`) shows only a `/orgs/{org_id}`
route, and `param=None` appears only in the account table of
`authorization.md:137`. The agent read the recipe, hit the route shape the recipe
does not cover, and chose the account whose docstring says "the bound tenant".

### Proposed changes

1. Check (`benchmarks/dx/checks/check_t9_member_export.py`,
   `test_dev_gate_checks_the_organization`): also accept `Account.bound_tenant()`
   when the same gate requires an organization role. That is always true here,
   because `test_dev_admin_only` already requires it.

   ```python
   def _org_account(blob: str) -> bool:
       """of=Account.org(...), the org fact, or Account.bound_tenant() next to an
       organization role (the role fails on a personal tenant, so the pair
       checks only an organization's account)."""
       if "Account.org(" in blob or _org_fact_gate(blob):
           return True
       return "Account.bound_tenant()" in blob and any(g in blob for g in ADMIN_GATES)

   def test_dev_gate_checks_the_organization() -> None:
       assert any(_org_account(_gate_text(r)) for r in _export())
   ```

   Update the docstring's "Account.org(...) or the org fact" to match.

2. Recipe `org-gate` (`recipes.py`, after the `/orgs/{org_id}` example):

   > A route without `org_id` in its path (the module binds the tenant from the
   > token): `ORG = Account.org(param=None)`, the organization the request is
   > bound to. `Account.org(param="org_id")` there fails `sandloom check`.
   > `Account.bound_tenant()` also accepts a personal tenant (it then checks the
   > caller's own subscription); use it only when either account is meant.

3. `docs/modules/http/authorization.md:137`: replace "the header or the bound
   tenant only" with "the header or the bound tenant only: the form for a
   tenant-bound route without an organization parameter". Add to `:138`: "a
   personal tenant checks the caller's own account; for an organization-only
   gate use `Account.org(...)`".

## Finding 2: `.sandloom/inspection.json` stale after a green `sandloom test`

### What changed

The agent's final `inspection.json` (from `changes.patch`) and the file a fresh
`sandloom generate` writes for the same source differ in six values, all
source **line numbers** of the invites routes:

```
.modules[5].routes[0].source.line   26 != 32   (invites, POST /invitations)
.modules[5].routes[1].source.line   31 != 37
.modules[5].routes[2].source.line   39 != 45   (GET /invitations/export)
.processes[81..83].source.line      the same three routes
```

They are written by `processes._location` (`code.co_firstlineno`,
`src/sandloom/core/cli/inspection/processes.py:26`) through `route_sources`
(`:29-39`) into `collect.collect` (`collect.py`, `card["routes"][...]["source"]`
and `"processes"`).

### Why it went stale

1. The agent edited `api.py` with one long import line:
   `from sandloom.sdk import CurrentUser, OrgRole, SubscriptionActive, module_router, require`
   (transcript, the `python3` edit before `sandloom generate`).
2. `sandloom generate` wrote `inspection.json` with route lines 26/31/39.
3. `sandloom test` (developer mode) ran **"generated files" first**
   (`src/sandloom/core/cli/run_tests.py:334-336`): PASS.
4. Later in the same run, `ruff check` ran **without `--no-fix`**
   (`run_tests.py:384-390`; `--no-fix` only under `--ci`). The app's
   `pyproject.toml` has `fix = true` (line 62). The isort rule (I001) rewrote the
   over-long import into a 7-line parenthesized import, which pushed every route
   down by 6 lines. Ruff reports a fixed file as a pass ("PASS ruff check 0.1s"),
   and `ruff format --check` then passes too.
5. The scorer's `sandloom test --ci` and `sandloom test` run the
   generated-files check against the reformatted `api.py`:
   `.sandloom/inspection.json [SANDLOOM-T015] (differs)` (`gates/sandloom-test-ci.log:7`,
   `gates/sandloom-test.log:7`).

Reproduced in a copy (pristine seed + `changes.patch`, wheels of c270cbc):
after putting the one-line import back, `sandloom generate` then
`generate --check` gives "up to date". `ruff check teamhub/modules/invites/api.py`
prints "1 fixed", and the next `generate --check` gives
`.sandloom/inspection.json [SANDLOOM-T015] (differs)`.

### Ruled out

- Hash seed: `generate --check` is "up to date" with `PYTHONHASHSEED=0`, `1` and `7`.
- Database / environment: the same result with `DATABASE_URL=sqlite+aiosqlite:...`
  and with the scorer's `SANDLOOM_TEST_DATABASE_URL` / `SANDLOOM_TEST_OWNER`. The
  collector reads no settings that differ between the two runs. Edges, features,
  reasons and methods are all sorted (`collect.py`, `_policy_requirements` in
  `sdk/_decl/module.py`).
- The gate-derived `requires_contract` edge (billing.entitlements.v1): unchanged
  between the two files. It has nothing to do with the staleness. t9 differs
  from the other tasks only because this agent wrote an import longer than the
  line length.

One field does depend on the environment, but it did not change here:
`modules[*].distributions` records installed versions. The DX wheels carry
`0.7.0a10+gc270cbc5f819`, so the file changes whenever the framework is rebuilt
or upgraded.

### The helpdesk one-line diff (commit 84e1c3f)

This one is not the same cause. Its only change is
`edges[1].features: [] -> ["list_aggregates", "transaction_locks"]` on
`requires_capability` of `module:identity`. The infra branch added
`features=frozenset({"transaction_locks", "list_aggregates"})` to identity's
database capability (`src/sandloom_identity/__init__.py:99`, commit bc99119).
The app's index records installed framework modules too, so a framework change
from the other branch makes the app's file stale. This was a real content change
that came in with the merge, not an unstable value.

### Relation to tm `inspection-merge-conflict`

The two are related, but the causes differ. That task is about two branches
conflicting on the tracked one-line `inspection.json`. The helpdesk diff is an
instance of it. The line numbers make that task worse: any edit that shifts a
route in any module changes the shared file. The t9 failure is a step-order bug
in `sandloom test`. Fix 2 below helps both.

### Proposed fixes

1. **`sandloom test` must not change source after the generated-files check.**
   In developer mode, run `ruff check` (with fixes) before "generated files"
   (it takes 0.1 s). Or take `collect.capture(rules)` before and after the
   steps, and fail with "ruff fixed <files>: run `sandloom generate`" when it
   changed. A green `sandloom test` must mean the scorer's `--ci` run is green
   on the same tree.
2. **Keep line numbers out of the tracked index.** Record the route source as
   path + qualified function name (`processes._location`, and the `line` in
   `inventory.py:56`). Let the reader (`sandloom context` / `graph`) find the
   line when it prints. Formatting and edits above a route then no longer make
   `inspection.json` stale. This also removes most merge conflicts from
   `inspection-merge-conflict`.
3. (Smaller) Leave the `+g<sha>` local version out of `distributions`, or
   record only the public version, so a framework rebuild does not change the
   app's index.

## Outcome (dx-eval-t9-followups)

- Fix 1: `check_t9_member_export.py` accepts `Account.bound_tenant()` next to
  an admin role (`_org_account`) and `Account.org(param=None)`; a gate on
  `Account.user(...)` and a bare `bound_tenant()` still fail. Re-run on the
  t9 run's app (pristine seed + `changes.patch`): before 5 passed, 1 failed
  (`test_dev_gate_checks_the_organization`); after 6 passed. The committed
  result (`results/merged-main-2026-11-06/t9_member_export.json`) is left as
  recorded. The `org-gate` recipe and `authorization.md` name
  `Account.org(param=None)` for a route without `org_id`.
- Fix 2: developer-mode `sandloom test` runs `ruff check` before the
  generated-files check (`--ci` unchanged); the inspection index (schema 2)
  stores a route's path and qualified function, and `sandloom context` /
  `graph` look the line up when printing; `distributions` drop the local
  `+g<sha>` tag.
