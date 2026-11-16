# idk-gated-tests-3b: the last 51 kernel tests leave the no-redis pending list (2026-11-16)

Follow-up of `idk-gated-tests-3a` (ADR-0016, `tools/ci/no_redis.py`). The 51
entries of `tools/ci/no_redis_pending.json` `tests` are gone and
`CEILING_TESTS` is 0. A test that was only the kernel's rule now runs on the
`memory` drivers (or a stand-in for the Redis client) in
`src/sandloom/core/tests`; the part that exercises a Redis or Celery driver moved
to `packages/sandloom-redis/tests` (the `redis-drivers` lane) or
`packages/sandloom-celery/tests` (the `celery-drivers` lane). The behaviour is
unchanged.

## Changed entries (tests only)

- G-OUTBOX-11 (relay availability): the kernel test
  `test_batch_publish.py::test_redis_availability_replies_do_not_burn_relay_attempts`
  now feeds the relay a stand-in `ResponseError` with the same reply text; the
  real client's parsed replies are the package's
  `test_redis_relay_availability.py::test_redis_availability_replies_do_not_burn_relay_attempts`,
  listed beside it.
- G-WINDOW-02: `test_create_app_refuses_in_production` (it builds the Redis
  atomic store the production app selects) moved to
  `packages/sandloom-redis/tests/test_guarantee_windows_prod.py`. The three other
  tests of the row stay in the kernel.
- G-OPS-05: `test_check_follows_the_heartbeat` (it builds the Celery host)
  moved to `packages/sandloom-celery/tests/test_celery_cli_worker.py`; the
  kernel's `test_cli_worker.py::test_check_exits_with_the_hosts_verdict_and_prints_its_reason`
  holds the CLI's side (exit code, printed reason, the timeout it asks with),
  listed beside it.
- C-WORKER-01: `test_real_composition_root_resolves_driver_built_host` (the
  worker host is the Celery driver's) moved to
  `packages/sandloom-celery/tests/test_celery_worker_composition.py`.
