# idk-pkg-celery-lane: the Celery driver's tests move into its package (2026-11-15)

Spec 8.2 and item 46 of section 13 of the infra-drivers design: the tests of
the `celery` taskqueue driver itself live in `packages/sandloom-celery/tests`
and run in the CI job `celery-drivers` against the built wheel and a real Redis
broker (`SANDLOOM_CELERY_BROKER_REQUIRED=1`: an unreachable broker is an error).

## Changed entries

- G-OPS-01, G-OPS-23 (tests only): the evidence in `test_celery_slots` and
  `test_celery_taskqueue_health` moved to `packages/sandloom-celery/tests/`.
  The behaviour and the tests are unchanged.
- G-TASK-05, C-WORKER-05 (tests only): the three Celery-defaults tests moved
  from `test_cache_batch_and_celery_defaults.py` to
  `packages/sandloom-celery/tests/test_celery_defaults.py` (the cache batch
  tests stay in the kernel as `test_cache_batch_ops.py`). The behaviour and the
  tests are unchanged.
