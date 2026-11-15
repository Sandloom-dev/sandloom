# idk-worker-port: the worker host behind the taskqueue driver (2026-11-14)

Spec 8.2 and owner decisions 5 and 9 of the infra-drivers design: the worker
process, the scheduler and their Celery signals leave the kernel and run as the
taskqueue driver's host (`TaskQueueProvider.worker_host()`, a `WorkerHost` over
the kernel's `WorkerBoot`). `raw_backend` is gone from `TaskQueueProvider`, with
no `native()` and no alias.

## Changed entries

- C-WORKER-02 (wording and test): the worker takes its host
  (`worker_host()`) from the taskqueue instance named by
  `SANDLOOM_WORKER_TASKQUEUE` instead of a Celery app from `raw_backend()`. A
  driver without a host is rejected (`test_a_taskqueue_without_a_worker_host_is_rejected`
  replaces `test_non_celery_taskqueue_is_rejected`), and importing
  `sandloom.core.worker` imports no task-queue backend.
- C-OBS-07 (test): the parent freezes the startup heap before the pool forks,
  through `WorkerBoot.before_fork` (`gc_tuning.freeze_before_fork`) instead of a
  Celery `worker_init` receiver in the kernel. The behaviour is unchanged;
  `test_celery_worker_init_freezes_in_the_parent` is replaced by
  `test_the_worker_freezes_in_the_parent_before_the_fork`.
- G-OPS-05, G-OPS-23, C-WORKER-01 (tests only): the heartbeat tests moved to
  `test_worker_port.py` with the Celery host; the broker probe names no broker
  address or credential in its details (security review S7, PR #200).
