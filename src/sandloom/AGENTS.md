<!-- sandloom:module-contract v2 · source: sandloom/core/cli/templates/module-AGENTS.md -->
# Sandloom module-author contract (Public API 1.0)

Rules for writing or changing a Sandloom business module. A rule a check enforces
names its code: run the check rather than re-reading the rule, and `uv run sandloom
explain <code>` gives the why and the fix. Any signature: `uv run sandloom docs
<name>`. `docs:` paths are relative to `docs/` in the framework repository.

## The one rule: modules are isolated

A module and its tests import only public `sandloom.sdk` modules, the generated contract stubs
and DTO copies (`sandloom.shared.contracts.*`, the app's `<app>.shared` /
`CONTRACT_DTO_PACKAGES`) and their own package, all at module level (C001, C002, C005, C013, C021);
an installed module package (`sandloom_identity`) is another module. Generated code and examples
use the 29 beginner names; an advanced import carries `# sandloom: advanced <reason>` (SANDLOOM-T009).

To talk to another module:

- **Fire and forget:** a producer class in `events.py`,
  `class NoteCreated(Event, channel="notes.note.created.v1")`, and
  `await NoteCreated(...).publish()` inside the unit (`sandloom add event <m> <Name>`).
- **React to an event:** in `consumers.py`, a view of the foreign channel (the
  fields you read) and `@on(View) async def f(event: View) -> None`. Each
  delivery runs in its own unit, claimed once per envelope id
  (`sandloom add consumer <m> <channel>`).
- **Synchronous:** the provider `@contract("<m>.<name>.v1") class X:` in
  `contracts.py` (`sandloom add contract <m> <name>`); the caller, at module level,
  `x = Contract(M_NAME_V1)` from the generated `sandloom.shared.contracts` stub.
  `Contract(REF, optional=True).client()` is `None` only when the provider is
  not deployed: use it only where you have a correct answer without it.
- **Read across modules:** a projection (`sandloom add projection`; docs: `concepts/projections.md`).
- **A user's email or name:** `Contract(IDENTITY_CONTACT_V1).get_contact(user_id)`
  at send time (events carry no PII); never copy it into your tables.
- **A process with undo:** a saga in `sagas.py` (`sandloom add saga <m> <name>`; docs: `concepts/sagas.md`).
- Reference other modules' rows by plain UUID: no cross-module foreign keys.

## Layout (discovered: nothing to register)

- `__init__.py`: `register = define_module(__name__, version="0.1.0")`. It
  imports `models`, `api`, `events`, `consumers`, `contracts`, `sagas`,
  `projections` and `tasks` and collects what they declare. `setup=` is only
  for L2 calls (startup tasks, claims contributors).
- `models/`: tables named `<module>_*`, ids `default=new_id` (UUIDv7) and
  `created_at: Mapped[CreatedAt]` (`clock_timestamp()`), so a list ordered by
  `created_at, id` never ties; `schemas.py`: pydantic only (C011); `services.py`:
  SQLAlchemy through `db()`; `api.py`: `router = module_router()` and routes
  with pydantic DTOs (C009, C010).
- `migrations/`: the module's own chain. `sandloom migrate new <m> "<message>"`
  autogenerates a revision, with RLS for each new `TenantScoped` table
  (against a scratch database). `tasks.py`: `sandloom add task <m> <name>`.
- Snapshots, contract stubs, DTO copies and event schemas: written by
  `sandloom generate` (run last by `sandloom add`); review, never hand-edit.

## Non-negotiables (each names its check)

1. **The unit owns the transaction.** `db()` is the running unit's session:
   `add`, `flush`, `begin_nested()`; never `commit()` / `rollback()` (C006).
   `db()`, `.publish()` and `record_audit` outside a unit raise (SANDLOOM-R002);
   `required=False` needs `# sandloom: best-effort <reason>` (C007). A child task
   opens its own `open_uow()` (SANDLOOM-R013); one unit per request writes (SANDLOOM-R007).
2. **Routes are authenticated by default.** `module_router()` authenticates
   every route (the caller: `user: CurrentUser`) and binds the tenant when the
   module has a `TenantScoped` table. An open route declares
   `dependencies=[public(reason="...")]` (SANDLOOM-B008). Session checks,
   idempotency (status replay on `Idempotency-Key`) and risk are derived and
   recorded in the route snapshot (SANDLOOM-T001). Deviations are advanced
   (`sandloom.sdk.web`): `no_idempotency`, `replay_safe`, `sensitive`, `at_commit`.
3. **Tenant rows inherit `TenantScoped`** (C014) and their migration enables
   RLS. Raw SQL on tenant tables needs `unscoped()` (C015, C024).
4. **Events:** channel `<module>.<event>.vN`, wire-typed fields, one producer
   per channel (SANDLOOM-B015). The same `.vN` may only gain an optional field
   (SANDLOOM-T005); a view must match its producer (C025).
5. **Contracts:** a stateless class, public `async def` methods, wire types,
   DTOs in its own `schemas` (copied for callers; SANDLOOM-B018, C012); `@command(idempotency_key=...)`
   marks commands; stubs stay current (SANDLOOM-T004, SANDLOOM-T007). A required
   contract nobody provides fails startup (SANDLOOM-B005).
6. **Consumers** take exactly their event: `async def f(event: E) -> None` (SANDLOOM-B016).
7. **Billing atoms name their account:** `Subscribed(of=Account.user())`,
   `Credits(1, of=Account.org(param="org_id"))` (SANDLOOM-B017). An organization
   route gating with `of=Account.user()` needs `reason=` (C027).
8. **No backend SDK or raw client:** `cache()`, `notifier()` (`sandloom.sdk.infra`),
   `Database(name)` (C003, C004). `sandloom.driver` is for driver packages, not modules.

## Rules no check enforces yet (get these right yourself)

- **Per-organization data is tenant data:** `TenantScoped`, even when you
  would name the column `organization_id`. A deliberately global table goes
  under `tenant_scope_exempt` with a reason.
- **Effects outside a durable consumer need their own dedupe** in the same
  unit: a UNIQUE `operation_key(...)` on the effect's row, or
  `durable_inbox("<m>@<x>").claim(db(), id)` (`sandloom.sdk.effects`).
- **Events** are at-least-once and unordered: order by a per-aggregate
  `version` field. A breaking change is a new `.v2` class, dual-published with
  `publish_event_versions` until no `.v1` reader is left (skill `sandloom-event-version-roll`).
- **Contract commands** are not atomic with the caller: design each as a saga
  step. Cap list `limit`s (`Query(le=...)`).
- **Security events:** on a password, role or membership change, call
  `revoke_user_tokens_on_commit(user_id, reason=..., require_immediate=True)`
  (`sandloom.sdk.security`) as the last fallible step, and `record_audit(...)` the
  action. Keep PII out of the audit `data`.
- **Side effects outside the database** (email, cache) go through
  `await on_commit(cb)`; a check that must hold at commit, `before_commit(cb)`.
- **Celery tasks** (`sandloom add task`): `run_async(coro)` with `open_uow()`
  blocks, never `asyncio.run`; idempotent: `@shared_task(acks_late=True,
  reject_on_worker_lost=True)`; cross-tenant in `with unscoped():`. A periodic
  task needs one beat per deploy profile (SANDLOOM-T010; `--periodic` wires it).

## Testing and gates (copy-paste examples: `reference/testing.md`)

- `from sandloom.sdk.testing import as_user, deliver, published, fake_contract`.
  The `client` fixture shares the rolled-back `db`; `real_uow_client` commits.
  `_, headers = as_user(tenant_id=tid)`; `assert len(published(NoteCreated)) == 1`;
  `await deliver(NoteCreated(...), times=2)` drives the real consumer runtime;
  `with fake_contract(STATS_QUOTA_V1, Fake()):`; `fake_access_fact(name, value)`.
- `uv run sandloom generate` after a declaration change (`sandloom test` never writes);
  `sandloom test <module>` while iterating, `--full` once at the end; no serial `pytest -q`.

Docs: `concepts/modules.md`, `concepts/units.md`, `concepts/route-policy.md`,
`concepts/events.md`, `concepts/contracts.md`, `concepts/authorization.md`;
every name `reference/sdk/index.md`; every code `reference/error-codes.md`.
