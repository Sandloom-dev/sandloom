---
name: sandloom-new-module
description: Use when asked to add a module or a feature domain to this Sandloom app, e.g. "add a notifications module", or to add a table, a route or a test to a module. It is the router for every module task here: which `sandloom add` command, which recipe, which skill.
---

# Work on a module of <app>

Read `AGENTS.md` first (the map). This skill says where to go; the recipes hold
the tested code: `uv run sandloom docs recipes`, one with `uv run sandloom docs
recipe <name>`.

## What you need, where it is

| You need | Command | Recipe or skill |
|---|---|---|
| a new module | `uv run sandloom add module <name>` | steps below |
| a table or a column | `uv run sandloom migrate new <name> "<message>"` | steps below |
| a route only paid admins reach | | recipe `org-gate` |
| a list in a stable order, ids, timestamps | | recipe `list-order` |
| a retried write that must not repeat | | recipe `retries` |
| to tell, ask or react to another module | `sandloom add event`, `consumer`, `contract`, `contract-use`, `projection`, `saga` | skill `sandloom-cross-module` |
| a job that runs later or on a schedule | `uv run sandloom add task <name> <task>` | skill `sandloom-background-tasks` |
| to test a service or a task body | | recipe `test-service-or-task` |
| a breaking change of an event | | skill `sandloom-event-version-roll` |
| the module in its own service | `uv run sandloom add deploy <name>` | skill `sandloom-split-deploy` |
| to know what a module exposes | `uv run sandloom context <name>` | skill `sandloom-module-context` |
| to finish | `uv run sandloom test <name>` | skill `sandloom-quality-gates` |

## Create the module

```bash
uv run sandloom add module <name>
```

It writes `<app>/modules/<name>/`: `__init__.py`
(`register = define_module(__name__)`), `models/` with an example
`TenantScoped` table, `schemas.py`, `services.py`, `api.py`
(`router = module_router()`), `events.py`, `migrations/` and `tests/`. The app
finds every module package: there is no list to edit. Then replace the example
domain:

- **Tables** are named `<name>_*`. After a model change run `migrate new`; it
  writes the row-level security policy of each new `TenantScoped` table.
- **Routes:** `module_router()` authenticates every route and binds the tenant.
  Take the caller as `user: CurrentUser` and the session as `db()`. Only an open
  route declares `dependencies=[public(reason="...")]`.
- **Role and plan gates** use `require(...)`. `OrgRole` and the billing atoms
  are not `sandloom.sdk` names: `sandloom generate` writes their stubs, import
  them from `<app>.shared.contracts.*`. A billing atom names its account:

  ```python
  ORG = Account.org(param="org_id")
  PAID_ADMIN = require(OrgRole("admin") & SubscriptionActive(of=ORG))


  @router.get("/orgs/{org_id}/report", dependencies=[Depends(PAID_ADMIN)])
  async def report(org_id: UUID, user: CurrentUser) -> ReportOut: ...
  ```

## Rules

1. **Imports.** A module imports only `sandloom.sdk`, the generated contract
   stubs and its own package. 29 names are free in any file: the 24 code names
   of `sandloom.sdk` and the test helpers `as_user`, `deliver`, `fake_contract`,
   `published` plus the `client` fixture. 12 more are free only in the module's
   own top-level file: `Saga`, `SagaContext`, `step`, `local_step`, `start_saga`
   in `sagas.py`; `Projection`, `SourceGroup`, `full_state`, `tombstone` in
   `projections.py`; `task`, `periodic`, `open_uow` in `tasks.py`. Any other
   import needs `# sandloom: advanced <reason>` on its line (SANDLOOM-T009).
2. **Never `commit()` or `rollback()`** on `db()`: the unit owns the
   transaction.
3. **A row owned through a tenant row is `TenantScoped` too**, even with no
   `organization_id` column.
4. **An effect outside a durable consumer needs its own dedupe**: a UNIQUE
   operation key on the effect's row.
5. **No import of another module, no foreign key across modules.** Reference
   another module's rows by plain UUID.
6. **A contract command is not atomic with your unit**: make it a saga step.
7. **Never edit a generated file** (`module.snapshot.json`,
   `routes.snapshot.json`, `<app>/shared/contracts/*`, `<app>/shared/events/*`).
   Run `uv run sandloom generate` and review the diff.

## Tests

`from sandloom.sdk.testing import as_user, deliver, published, fake_contract`.
The `client` fixture sends requests as `as_user(tenant_id=...)`;
`published(X)` lists the events a test published; `await deliver(X(...))` runs a
real consumer; `with fake_contract(REF, impl):` stands in for a provider.
Code outside a request (a service, a task body) is tested as in recipe
`test-service-or-task`.

When `deploy/compose/microservices.yml` exists, a new module answers nowhere in
that profile until `uv run sandloom add deploy <name> --service <svc>` runs.
When you extend an existing module or change its interface, read it first with
skill `sandloom-module-context`.
