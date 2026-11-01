---
paths:
  - "**/tasks.py"
---
# Celery tasks (`<module>/tasks.py`)

- **Generate a task:** `sandloom add task <module> <name> [--periodic "0 3 * * *"]`.
  It writes the idempotent at-least-once form below and, with `--periodic`,
  the beat schedule and the module's worker/beat pair in the microservices
  profile (or prints the step that adds it).
- **Declare tasks with `@shared_task` from `sandloom.sdk.tasks`** (typed: no mypy
  override). Never create a Celery app. The framework worker
  (`celery -A sandloom.core.worker worker|beat`) imports `tasks.py` of every
  loaded module.
- **Task bodies are sync.** Run async work with `run_async(coro)` from
  `sandloom.sdk.effects`, never `asyncio.run`. Inside the coroutine, open a unit
  with `async with open_uow():` (`sandloom.sdk.data`); in it `db()`,
  `await E(...).publish()` and `record_audit` work as in a request.
- **An idempotent task opts into at-least-once:**
  `@shared_task(acks_late=True, reject_on_worker_lost=True)`. Its effects
  still need a DB-atomic dedupe (`durable_inbox` or a UNIQUE operation key).
- **Results.** No result is stored by default (`task_ignore_result`). Opt in
  with `ignore_result=False`.
- **Beat schedules:** define `setup_periodic_tasks(sender, **kwargs)` in
  `tasks.py` (`sender.add_periodic_task(cron("0 3 * * *"), task.s(), name=...)`).
  Exactly one beat runs per broker (fenced lease). **A module's first periodic
  task needs a beat in every deploy profile that serves it**: in the
  microservices profile `sandloom add deploy <module>` (or `sandloom add task
  --periodic`, or `sandloom add deploy --refresh`) adds `<service>-worker` /
  `<service>-beat`. `sandloom test`'s
  static `schema & deploy` step checks it (SANDLOOM-T010).
- **Tenants.** Cross-tenant maintenance runs inside `with unscoped():`.
  Per-tenant work binds its tenant with `bind_tenant(id)` (`sandloom.sdk.tenant`).
- **Enqueue retries safely:**
  `taskqueue().enqueue(..., idempotency_key=...)` (`sandloom.sdk.infra`).
- Concept: `docs/modules/database/units.md` (task units).
