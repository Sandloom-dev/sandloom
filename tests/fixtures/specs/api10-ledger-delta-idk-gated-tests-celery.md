# idk-gated-tests-celery: the Celery-driver tests leave the kernel suite (2026-11-16)

Follow-up of `idk-gated-tests-redis` (ADR-0016, `tools/ci/no_redis.py`): the
eleven kernel test files that imported `sandloom_celery`, `celery` or `redis`
at module level, and so were not collected by the kernel-without-Redis step,
are split. What exercised the `celery` driver moved to
`packages/sandloom-celery/tests` (the `celery-drivers` lane); what is the
kernel's own rule stays in `src/sandloom/core/tests` on the `memory` drivers or
on the recording stub driver of `test_taskqueue_enqueue_layer`. The behaviour is
unchanged; the pending list of files in `tools/ci/no_redis_pending.json` is
empty and `CEILING_FILES` is 0.

## Changed entries (tests only)

- G-TASK-01: `test_successful_retries_publish_one_real_message`,
  `test_parallel_retries_never_publish_twice` and
  `test_replay_window_expires_on_real_redis` moved to
  `packages/sandloom-celery/tests/test_celery_enqueue_deduplication.py` (real
  Celery messages, a real Redis window). The same two layer rules run in the
  kernel on the recording driver (`test_retries_with_the_key_send_one_message`,
  `test_parallel_retries_send_one_message`), listed beside them.
- G-TASK-03: `test_uncertain_broker_ack_is_not_republished`,
  `test_ambiguous_send_error_still_keeps_the_key_pending` and
  `test_deterministic_send_error_releases_the_reservation` moved with them (the
  errors are Celery's); the kernel's `test_an_ambiguous_driver_error_keeps_the_key_pending`
  holds the layer's side and is listed.
- G-INFRA-11: `test_periodic_is_a_taskqueue_feature_only_celery_advertises` is
  split: the kernel keeps `test_periodic_is_a_taskqueue_capability_feature` (the
  feature is a capability feature of the port), the package holds
  `test_celery_features.py::test_celery_advertises_periodic_and_only_capability_features`.
- G-OPS-01, C-WORKER-06: the beat lease tests that drive Celery's scheduler
  (`test_second_beat_stands_by_and_takes_over_after_release`,
  `test_a_holder_that_lost_its_token_stops_scheduling_at_once`,
  `test_cache_outage_keeps_scheduling_only_until_the_local_deadline`,
  `test_beat_init_wraps_the_scheduler_of_this_app_only`,
  `test_two_leases_on_real_redis`) moved to
  `packages/sandloom-celery/tests/test_celery_beat_lease.py`;
  `test_two_schedulers_share_one_slot_on_real_redis` moved from
  `test_periodic_dedupe.py` to `test_celery_periodic_dedupe.py`. The kernel's
  resolution of the lease and the production refusal stay in `test_beat_lease.py`.
- G-OPS-05: the heartbeat tests moved from `test_worker_port.py` to
  `packages/sandloom-celery/tests/test_celery_worker_host.py`; the kernel keeps
  the port's own two tests in `test_worker_port.py`.
- G-OPS-23: the tests that reach a broker moved to
  `packages/sandloom-celery/tests/test_celery_ready_taskqueue.py`; the kernel keeps
  the driver-cannot-open, not-probed and probe-cache rows in
  `test_ready_taskqueue.py`.

## Rows that did not change

G-EFFECT-05 (`test_replay_limits.py`) and the kernel rows of
`test_module_capability_derivation.py` stay where they are: only their Celery
assertions moved (the dedupe TTL default to `test_celery_replay_limits.py`, the
`idempotency_key` feature line to `test_celery_features.py`).

## Not a ledger row

`test_driver_options_from_settings.py` is split in three: the SettingsFill engine
(driver-free, on a probe driver) to `src/sandloom/core/tests/test_driver_options_fill.py`,
the `redis` / `streams` fills to
`packages/sandloom-redis/tests/test_redis_options_from_settings.py`, the `celery`
fill to `packages/sandloom-celery/tests/test_celery_options_from_settings.py`.
No guarantee names it.
