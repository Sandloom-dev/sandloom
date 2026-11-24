### Fixed

- **A fresh app with billing answered `/health` 503 under `sandloom dev run`** (dx-dog-health-reconciler-dev):
  `sandloom dev run` starts no worker and no beat, so the `billing_reconciler`
  check read `never_ran` and failed. In `ENVIRONMENT=dev` only, a reconciler
  that never ran now reports `ok` with `state: never_ran` and a note naming
  `uv run sandloom worker` and `uv run sandloom beat`. A run that went silent
  and open compensation still fail in every environment; `test` and `prod`
  behave as before. `sandloom dev run` prints one line naming the two
  commands when a composed module requires periodic jobs (read from
  `.sandloom/inspection.json`), and the generated README's quick start says the
  same for an app with billing. Checkout and portal still answer 503 until a
  reconciler run completes, in development too.
