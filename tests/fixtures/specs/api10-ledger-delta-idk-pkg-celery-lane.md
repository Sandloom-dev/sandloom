# idk-pkg-celery-lane: the Celery driver's tests move into its package (2026-11-15)

Spec 8.2 and item 46 of section 13 of the infra-drivers design: the tests of
the `celery` taskqueue driver itself live in `packages/sandloom-celery/tests`
and run in the CI job `celery-drivers` against the built wheel and a real Redis
broker (`SANDLOOM_CELERY_REQUIRED=1`: an unreachable broker is an error).

## Changed entries

- G-OPS-01, G-OPS-23 (tests only): the evidence in `test_celery_slots` and
  `test_celery_taskqueue_health` moved to `packages/sandloom-celery/tests/`.
  The behaviour and the tests are unchanged.
- G-TASK-05, C-WORKER-05 (tests only): the three Celery-defaults tests moved
  from `test_cache_batch_and_celery_defaults.py` to
  `packages/sandloom-celery/tests/test_celery_defaults.py` (the cache batch
  tests stay in the kernel as `test_cache_batch_ops.py`). The behaviour and the
  tests are unchanged.

## Where the moved evidence runs

The ledger's `packages/<package>/tests` evidence is collected by the
`guarantees` marker too: `tools/checks/guarantees.py` (`backend_nodeids`) lists
those node ids with the `src/` and `tests/` ones, and `pytest_paths` (the
`paths` command, rendered into `docs/reference/guarantees.md`) adds each
package's `tests` directory to `src`, so the marker run covers the package
tests as well (they skip with their reason where no broker answers). In CI they
run in the required job `celery-drivers` (a red or skipped lane blocks merge:
`tools/ci/required_ci.py`), against the built wheel, and the job feeds the skip
ledger (lane `celery`).
