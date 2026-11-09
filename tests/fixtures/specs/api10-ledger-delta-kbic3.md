# KB-IC-3: readiness for transient trouble, broker probe, billing reconciler (2026-11-09)

Owner decision on module infrastructure dependencies
(`design/kernel-boundary/owner-decision-infra-deps.md`), answers 1, 3 and 4.

## Changed entries

- G-OPS-23 (amended, enforced): `/ready`'s taskqueue term follows the loaded
  modules' required taskqueue requirements, not a `tasks.py`; a request-path
  use gates on the broker's reachability, a periodic-only use does not
  (answer 3), an optional one is never probed; the `celery` driver's
  `health()` is one bounded broker connection (answer 4). The KB-02 tests it
  listed were rewritten in the same file under new names.

## New entries

- G-OPS-24 (new, enforced): billing's intent reconciler is watched by its
  own run record; the `billing_reconciler` health check fails (`never_ran`,
  `overdue`) after `RECONCILE_MAX_SILENCE_SECONDS` without a completed run,
  after one interval of startup grace. This is the recorded "separate,
  testable criterion" of answer 3.
- C-OPS-15 (new, enforced): `sandloom doctor`'s `periodic` section and
  `/diagnostics/core`'s `periodic` block say whether a beat schedules each
  module's periodic jobs; required fails, optional warns (answer 1:
  identity's cleanup).
