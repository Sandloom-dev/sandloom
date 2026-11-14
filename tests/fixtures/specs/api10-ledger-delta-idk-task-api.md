# idk-task-api: `@task` and `@periodic` replace `shared_task`, `cron` and the beat hook (2026-11-14)

Owner question 4 of the infra-drivers design (spec section 8): a module declares
background work with Sandloom's own `sandloom.sdk.tasks.task` and `periodic`, and
the `taskqueue` driver door is `TaskQueueProvider.send` (major 2).

## Changed entries

- G-BILLING-09 (test renamed): the reconciler is a `@periodic` task declared
  `at_least_once`. The enforcing test `test_reconcile_task_is_scheduled_and_acks_late`
  (which read Celery's task attributes) is replaced by
  `test_reconcile_task_is_scheduled_every_five_minutes` (its cron, id and
  `max_interval`). The guarantee itself is unchanged.
- G-INFRA-11 (wording and tests): the `periodic` feature is derived from a
  `@periodic` task (not from the beat hook), and an `at_least_once` task derives
  the new feature `redelivery`. The tests
  `test_tasks_derive_taskqueue_and_a_beat_hook_derives_periodic` and
  `test_an_enqueue_only_driver_cannot_serve_a_beat_hook` are renamed
  `test_tasks_derive_taskqueue_and_a_periodic_task_derives_periodic` and
  `test_a_send_only_driver_cannot_serve_a_periodic_task`.
