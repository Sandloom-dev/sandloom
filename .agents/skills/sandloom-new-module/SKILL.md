---
name: sandloom-new-module
description: Create a new Sandloom business module (sandloom add module, migrate, test). Use when asked to add a module/feature domain to the Sandloom framework or a Sandloom app — e.g. "add a notifications module", "создай модуль аналитики".
---

# Create a new Sandloom module

1. **Read the module-author contract first.**
   - In the framework tree, that is `src/sandloom/AGENTS.md`.
   - In an `sandloom new` app, it is the project's `AGENTS.md`.
   - Its "Rules no check enforces yet" list matters most: per-org rows are
     `TenantScoped`, effects outside a durable consumer need their own
     dedupe, a breaking event change is a dual-published `.v2`, a command is
     a saga step. No gate catches those.
2. **Generate** from the repository root (framework tree) or from the app root:

   ```bash
   uv run sandloom add module <snake_case_name>
   ```

   - It writes the new package: `__init__.py`
     (`register = define_module(__name__)`), `models/` with an example
     `TenantScoped` table, `schemas.py`, `services.py`, `api.py`
     (`router = module_router()`), `events.py`, `migrations/` with a baseline
     revision (RLS included) and `tests/`.
   - It also wires the module, and prints every file it edited. In the
     framework tree, the package lands at `src/<name>/` and joins
     `LOCAL_MODULES` (`src/sandloom/main.py`), `.env.example` and the monolith
     profile (installed module packages are discovered by their
     `sandloom.modules` entry point). Its `tests/conftest.py` brings
     the kit's fixtures (`client`, `published` recording). An `sandloom new` app
     discovers every module package and lists nothing. Do not wire by hand.
   - `define_module` collects what `models`, `api`, `events`, `consumers`,
     `contracts`, `sagas`, `projections` and `tasks` declare. `SANDLOOM_MODULES`
     only selects a subset.
3. **Replace the example domain.**
   - **Tables:** prefix them `<name>_`. A row of one organization is
     `TenantScoped`. Regenerate the baseline from your model, or add a
     revision with `uv run sandloom migrate new <name> "<message>"` (it compares
     with a scratch database at the chain head); it writes the RLS policy of each new
     `TenantScoped` table.
   - **Routes:** `module_router()` authenticates every route and binds the
     tenant; take the caller as `user: CurrentUser` and the session as
     `db()`. Only an open route declares `dependencies=[public(reason="...")]`.
     Idempotency, session checks and risk are derived: declare none of them.
   - **Gates:** `dependencies=[Depends(require(OrgRole("admin")))]`; a billing
     atom names its account: `Subscribed(of=Account.user())`,
     `Credits(1, of=Account.org(param="org_id"))`.
   - **Events:** `uv run sandloom add event <name> <CamelName>` appends a producer
     `class X(Event, channel="<name>.<x>.v1")`; publish it with
     `await X(...).publish()` inside the unit.
4. **Other modules:** reach them through events, consumers, contracts or a
   saga, never imports.
   - `uv run sandloom add consumer <name> <channel>`: a view of the foreign event
     and an `@on` handler.
   - `uv run sandloom add contract <name> <contract>`: a `@contract` provider,
     its DTO in the module's `schemas.py` (the provider authors its DTOs), the
     generated stub and DTO copy; a caller declares `x = Contract(REF)` at
     module level and receives the copy.
   - `uv run sandloom add saga <name> <saga>`, `uv run sandloom add projection <name> <projection>`.
   - Exact signatures: the cheat-sheet in the root `AGENTS.md`, or
     `uv run sandloom docs <name>`.
5. **Tests** use `sandloom.sdk.testing`: the `client` fixture,
   `as_user(tenant_id=...)`, `published(X)`, `await deliver(X(...), times=2)`
   for a consumer, `fake_contract(REF, impl)` for a contract you call.
6. **Reachable in the microservices profile (do not skip when the project
   has `deploy/compose/microservices.yml`):**
   `uv run sandloom add deploy <name> --service <svc>` writes the service (or
   adds the module to an existing one), the gateway `location`s, the
   bindings and, for periodic tasks, the worker and its one beat (skill
   `sandloom-split-deploy`). Without it the module's routes answer nowhere in
   that profile.
7. **Gates:** skill `sandloom-quality-gates`. `uv run sandloom generate` writes
   the generated files (`module.snapshot.json`, route and contract snapshots,
   contract stubs, DTO copies, exported event schemas; `sandloom add` runs it
   last): review that diff. `sandloom test` never writes them. Every gate is
   green before you call the module done.

**Reference code:**

- `src/sandloom_tenancy/`: roles, guards, invitations, contracts.
- `src/sandloom_billing/`: PSP port, durable idempotency, command contracts.
- `examples/helpdesk`: an app module consuming `tenancy.membership.v1`.
- `examples/projecthub`: a projection and a consumer.

When extending an existing module or assessing its external impact, use
`sandloom-module-context` to read scoped rules/interfaces and declared connections.
