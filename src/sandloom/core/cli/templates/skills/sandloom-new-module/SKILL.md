---
name: sandloom-new-module
description: Create a business module in this Sandloom app (sandloom add module, migration, tests, gates). Use when asked to add a module or a feature domain, e.g. "add a notifications module".
---

# Create a module in <app>

1. **Read `AGENTS.md` first** (the map). The rules no gate catches:
   - a row of one organization is `TenantScoped`;
   - an effect outside a durable consumer needs its own dedupe;
   - a breaking event change is a dual-published `.v2` (skill
     `sandloom-event-version-roll`);
   - a contract command is not atomic with your unit (a saga step).
2. **Generate** from the app root:

   ```bash
   uv run sandloom add module <name>
   ```

   It writes `<app>/modules/<name>/`: `__init__.py`
   (`register = define_module(__name__)`), `models/` with an example
   `TenantScoped` table, `schemas.py`, `services.py`, `api.py`
   (`router = module_router()`), `events.py`, `migrations/` (a baseline with
   RLS) and `tests/`. The app discovers every module package: there is no
   list to edit.
3. **Replace the example domain.**
   - **Tables** are named `<name>_*`. After a model change:
     `uv run sandloom migrate new <name> "<message>"` (it writes the RLS policy
     of each new `TenantScoped` table).
   - **Routes:** `module_router()` authenticates every route and binds the
     tenant. Take the caller as `user: CurrentUser`, the session as `db()`.
     Only an open route declares `dependencies=[public(reason="...")]`.
   - **Gates:** `dependencies=[Depends(require(OrgRole("admin")))]`; a
     billing atom names its account (`sandloom docs recipe org-gate`).
   - **Events:** `uv run sandloom add event <name> <CamelName>`, then
     `await X(...).publish()` inside the unit.
4. **Other modules** only through events, contracts, projections or sagas:
   - `uv run sandloom add consumer <name> <channel>`, `uv run sandloom add contract
     <name> <contract>`, `uv run sandloom add projection <name> <projection>`,
     `uv run sandloom add saga <name> <saga>`, `uv run sandloom add task <name> <task>`;
   - what you can call: `uv run sandloom docs contracts`, `uv run sandloom docs
     events`, then `uv run sandloom docs <contract or channel>`;
   - how-tos with tests: `uv run sandloom docs recipes` (`call-contract`,
     `projection-from-platform-events`, `test-service-or-task`,
     `periodic-task`, `event-v2`).
5. **Tests** use `sandloom.sdk.testing`: the `client` fixture,
   `as_user(tenant_id=...)`, `published(X)`, `await deliver(X(...))`,
   `fake_contract(REF, impl)`. Code outside a request (a service, a task):
   `sandloom docs recipe test-service-or-task`.
6. **Split profile.** When `deploy/compose/microservices.yml`
   exists, run `uv run sandloom add deploy <name> --service <svc>` (skill
   `sandloom-split-deploy`): without it the module answers nowhere there.
7. **Gates:** skill `sandloom-quality-gates`. `uv run sandloom generate` (run
   last by `sandloom add`) writes the generated files (`module.snapshot.json`,
   `routes.snapshot.json`, contract stubs and DTO copies in
   `<app>/shared/contracts/`, event schemas in `<app>/shared/events/`):
   review that diff, never edit them by hand. `sandloom test` only checks them.
