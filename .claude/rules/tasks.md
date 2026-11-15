---
paths:
  - "**/tasks.py"
---
# Background tasks (`<module>/tasks.py`)

- **Generate a task:** `sandloom add task <module> <name> [--periodic "0 3 * * *"]`.
  It writes a `@task` (or, with `--periodic`, a `@periodic(cron=..., id=...)`)
  and the module's `taskqueue` declaration. With `--periodic` it also adds the
  module's worker/beat pair in the microservices profile (or prints the step
  that adds it).
- **Declare tasks with `@task` / `@periodic` from `sandloom.sdk.tasks`** (typed: no
  mypy override). Never create a broker app. The framework worker
  (`sandloom worker`) imports `tasks.py` of every loaded module; the scheduler
  is `sandloom beat`.
- **A task is a module-level function taking one pydantic model of wire types**
  (or nothing). Its name is its dotted path (`<package>.tasks.<function>`): no `name=`. Send it with
  `await task.enqueue(payload, delay=..., idempotency_key=...)`; `await
  task(payload)` runs the body in your process.
- **Bodies are `async def`.** The worker runs them on its persistent loop: no
  `asyncio.run`, no `run_async` in new code. Open a unit with
  `async with open_uow():` (`sandloom.sdk.data`); in it `db()`,
  `await E(...).publish()` and `record_audit` work as in a request.
- **A run can repeat.** An idempotent task may opt into redelivery:
  `@task(at_least_once=True)` (the module declares the taskqueue feature
  `redelivery`, SANDLOOM-C033). Its effects still need a DB-atomic dedupe
  (`durable_inbox` or a UNIQUE operation key).
- **Periodic tasks:** `@periodic(cron="0 3 * * *", id="<module>.purge")` on a
  function without a payload. `cron` is five fields, in UTC; `id` is unique in
  the deployment. The module declares the taskqueue feature `periodic`
  (SANDLOOM-C033). Exactly one beat runs per broker (fenced lease). **A module's
  first periodic task needs a beat in every deploy profile that serves it**: in
  the microservices profile `sandloom add deploy <module>` (or `sandloom add task
  --periodic`, or `sandloom add deploy --refresh`) adds `<service>-worker` /
  `<service>-beat`. `sandloom test`'s static `schema & deploy` step checks it
  (SANDLOOM-T010).
- **Tenants.** Cross-tenant maintenance runs inside `with unscoped():`.
  Per-tenant work binds its tenant with `bind_tenant(id)` (`sandloom.sdk.tenant`).
- **Enqueue by name** (a task of another module's queue, no import):
  `taskqueue().enqueue("<package>.tasks.<fn>", {...}, idempotency_key=...)`
  (`sandloom.sdk.infra`).
- Concept: `docs/modules/database/units.md` (task units); reference:
  `docs/reference/sdk/tasks.md`.
