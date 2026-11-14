# idk-worker-cli: `sandloom worker`, `sandloom beat`, `sandloom worker check` (2026-11-14)

Owner decision 9 of the infra-drivers design: the worker commands are
`sandloom worker`, `sandloom beat` and `sandloom worker check`; the old
`celery -A sandloom.core.worker worker|beat` and
`python -m sandloom.core.worker healthcheck` are gone, with no shim.

## Changed entries

- G-OPS-05 (wording and test): workers are health-checked by
  `sandloom worker check`, which fails when the heartbeat written on the
  worker timer is stale. The behaviour is unchanged. The test
  `test_healthcheck_cli` (which called the removed `worker.main`) is replaced
  by `test_cli_worker.py::test_check_follows_the_heartbeat`.
