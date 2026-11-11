---
title: CI profiles and minute budget
type: reference
---

# CI profiles and minute budget

Automatic PR validation uses checks selected by changed area. Every
change still needs the [owner's final review and manual merge](branches.md).
The required `CI required` check blocks merging until the selected suites pass.
The final merge remains manual and restricted to the owner.

## Required merge check

`ci.yml` is the single PR entry point. It selects suites by changed paths and
calls the documentation, package, architecture, admin and dependency-review
workflows as reusable jobs, so those suites do not run twice.

The final **CI required** job runs even when an earlier job fails or is skipped.
It requires the classifier and policy checks to succeed, then verifies that every
selected suite succeeded. A failure, cancellation, missing result or unexpected
skip blocks the merge. Unrelated suites can be skipped. Heavy full-validation
jobs remain opt-in; selecting this check in branch protection does not launch
extra test runs. The result is accepted only from the GitHub Actions app.

`main` does not require a fresh rebuild merely because another PR merged first
(`strict: false`). It still requires a successful check for the PR's current
commit, and the owner reviews compatibility with the current `main`.

## Automatic PR checks

| Changed area | Checks |
| --- | --- |
| Every PR | Workflow-policy regressions, reviewed source allowlist, credential-pattern scan; change classification |
| Backend source/tests, migrations, package/build code or dependencies | Static gates, fast backend suite on Python 3.12, the kernel suites without module packages (`kernel-without-batteries`), the SQLite lane (`second-db-test-lane`) and the skip ledger, production image build; package checks for their declared paths |
| Admin or backend source/dependencies | Admin contract drift and the admin workflow |
| Documentation, website, tutorial examples, SDK or documentation tooling | Strict site build, stale-symbol lint and website acceptance tests |
| Example applications | All five example applications' gates and migrations |
| Deployment profiles | Compose/observability validation and production image build |
| Benchmark code | Benchmark security tests and load-harness negative controls |

An update to `tools/packaging/public-source.json` alone does not run the full
backend or rebuild packages. The always-running workflow-policy job checks the
source selection. CI configuration changes have dedicated regression tests and exercise the
regular PR suites. Manual full-validation jobs remain opt-in.

Normal PR backend tests use Python 3.12, without coverage or `slow` tests.
They run in two parts of one seed: `--sandloom-shard=1/2` and `2/2`, with the
run id as the seed. Each part runs half the test files on its own runner.
Together they run the whole fast suite, split by file as xdist already does.
Rerunning a failed part keeps the run id, and so the split.
The Python 3.11/3.13/3.14 compatibility runs, coverage, serial in-process isolation suite,
generated-app quickstart/recipes/split checks and live microservices smoke run
in full validation. Those omissions from the PR profile are intentional; its
green result is not evidence that full release validation passed.

PostgreSQL is the mandatory database lane: `backend-tests` runs everything
on it. `second-db-test-lane` runs on SQLite files: the standard modules'
suites, the examples' tests and an application from `sandloom new`. A test
may skip there for a feature SQLite lacks (`skip_unless_db_feature`). Each
lane uploads a skip ledger: `backend-tests` and `examples` for PostgreSQL,
`second-db-test-lane` for SQLite. The lane runs its modules step with backend
changes and its examples step with example changes: each ledger has its
PostgreSQL counterpart. The `skip-ledger` job fails when a test was skipped on
every lane ([the skip ledger](../reference/testing.md#the-skip-ledger)). Only
`tools/ci/skip_ledger_allowlist.json` excuses one, with a reason. Full
validation collects every suite; there an allowlist entry that matches no
test fails too (`--strict-allowlist`).

`kernel-without-batteries` runs the kernel's own test suites in a venv with
the `sandloom` package alone (ADR-0012). It also runs
the replaceability fixtures in `tests/integration/replaceability`. There,
test modules replace the bundled modules, in one process and split.
`tools/ci/no_batteries.py` hides the bundled modules from every import, the
checkout's `src/` included. A kernel test that needs a bundled module belongs
in `tests/integration/with_batteries` or in the module's own tests.
`tools/ci/no_batteries_pending.json` lists the tests still waiting to move.
It is empty and stays empty: an entry outside the ceiling in
`tools/ci/no_batteries.py` fails the job. A listed test that passes without
the bundled modules fails it too, as does an entry whose test is gone.
Locally:

```bash
uv venv .venv-kernel
uv export --frozen --only-group dev --no-hashes --no-emit-project > /tmp/dev-req.txt
uv pip install --python .venv-kernel/bin/python -e "packages/sandloom[redis,celery,check,fast,sqlite,sentry]" -r /tmp/dev-req.txt
.venv-kernel/bin/python tools/ci/no_batteries.py install
.venv-kernel/bin/python -m pytest -n auto --dist loadfile -p tools.ci.no_batteries src/sandloom/core/tests tests/sdk tests/architecture tests/integration/replaceability
```

Checks run on PRs rather than repeating the same work after a merge to `main`.
New commits cancel older runs of the same PR. Closing or merging a PR also
cancels its older Monorepo CI run without starting new test jobs. Short-lived
workflow artifacts expire after three days (the SBOM after seven, the
`ci-evidence` file of the release reuse after fourteen).

## Actions and dependency caches

JavaScript actions use Node.js 24 releases, pinned to reviewed commit SHAs
with their release versions in comments. Python and application Node.js
versions are selected separately by the setup actions.

Root dependency caches use `uv.lock` and setup-uv's Python/platform key.
Only `backend-tests` saves them, once per Python version in its matrix;
other jobs restore the same cache with `save-cache: false`. Cache misses
still install the locked dependencies normally. Concurrent full runs on the
same ref can still race to create a new key, but parallel readers within a
run do not compete to upload it. Comparative benchmarks use their separate
lock and Python selection and do not use this shared uv cache.

## Serial isolation performance

The serial pass retains every in-process test and its shuffled file order.
It runs as two shards of one seed. Each shard is one serial process over
half the files: the seed deals each file to the shard with fewer tests so
far. The two shards together are the whole selection. One serial process
took about 10.7 minutes on a hosted runner; a shard takes about half.

The gate is weaker in one respect, and that is the owner's call. Under one
seed, a leak from file A shows in file B only when both share a shard. Every
seed deals the files anew. So a given pair shares a process in about half of
the release runs, not in all of them. More shards would not shorten the
release, because the compatibility matrix (about 5 minutes) is then longest.

`subprocess_probe` marks only checks whose tested runtime is entirely in a
fresh child interpreter (package build/install and CLI/plugin isolation probes).
They remain in the unfiltered full matrix on Python 3.11 and 3.12; repeating
them in the parent-process isolation pass cannot expose parent runtime leaks.
The serial job prints its 25 slowest phases so later regressions are visible.
The split-conformance matrix is module-scoped: its child servers and database
connections are closed before the next test file, rather than running through
the rest of the session.

Readiness polls use a direct loopback HTTP connection rather than constructing
an HTTPX/TLS context for every poll. Child identity/nonce, early-exit, timeout
and foreign-server checks remain enforced; no production transport is changed.

## Full release validation

From GitHub Actions, choose **Full validation (manual)**, select the exact branch
or tag, and click **Run workflow**. From an authenticated terminal:

```bash
gh workflow run full-validation.yml --ref main
```

This calls the original full backend matrix (3.11, 3.12 with coverage, 3.13,
3.14), serial isolation, all example and security suites, generated-app and
deployment smoke checks, plus package, documentation, admin and data-extraction
workflows. It does not publish or deploy anything. Review every called job's
result on the exact release commit before releasing; fix failures, do not
interpret skips or an older green PR run as a completed release gate.

The full workflow remains opt-in. Data extraction has no nightly schedule;
it can also be requested manually or with the `extraction-e2e` PR label.
Comparative load benchmarks retain their explicit manual/label opt-in.
The PyPI release workflow requests full backend validation. Dry runs retain
validated artifacts without uploading; real uploads additionally require owner
approval in the `pypi` environment.

### Reusing the PR run on the same tree

A release usually follows a PR merge. Its `validate` stage used to repeat
the jobs that PR had passed on the same tree. The release's `plan` job runs
`tools/ci/pr_evidence.py select`. It names the jobs whose green PR result
covers the release commit, and `validate` skips exactly those. A job is
reused only when all of these hold:

- the release commit is the merge commit of a PR merged into `main`;
- a `pull_request` run of `ci.yml` for that PR's head succeeded, so its
  `CI required` gate passed;
- that run's `ci-evidence` artifact records the release commit's exact git
  tree (the PR's `CI required` job writes it). If `main` moved after the PR
  run, the tree differs and nothing is reused;
- GitHub's job list for that run shows the job succeeded, every matrix entry
  included. The results come from GitHub, never from the artifact.

Only a job that runs the same commands in both profiles can be reused. The
list is `REUSABLE_JOBS` in `tools/ci/required_ci.py`. The backend matrix
always runs, with its other Pythons, coverage and slow suites. So do the
isolation shards and every full-only job.

If any condition fails, nothing is reused and every job runs: a tag on a
commit without a PR, say, or an expired artifact.
The full-validation gate accepts a skip only for the jobs the release named,
and only outside a PR. The manual umbrella (`full-validation.yml`) never
reuses.

The [local CI runner](local-ci.md) retains the full four-version matrix and
heavy backend jobs by default. It deliberately overrides `full_validation` in
its private act workflow copies and local expression context. Local job
selection (`--jobs`, `--python`) is still available. The local runner does not
cover every workflow in the manual umbrella (notably the full admin workflow
and extraction scenario); validate those separately or use the umbrella.

## Reliability regressions

The extraction probe explicitly declares its synthetic, loopback-only test
routes as public with a reason. A fast regression runs the real route-policy
startup check against the actual probe router; the full extraction scenario
also exercises it in running monolith and split services.

Hosted test-service URLs and `.env.test` use numeric loopback addresses.
Resolving `localhost` runs through the event loop's thread executor and can
exhaust the bounded Redis connect deadline when that executor is busy, even
with a healthy Redis server. This reproduced the CI connection timeout;
changing only the address to `127.0.0.1` passed with the executor still blocked.
A regression checks the service addresses. The root test bootstrap also seeds
missing `.env.test` settings when CI exports `DATABASE_URL`; previously its
early return silently omitted the test cache-connect profile. Explicit shell
values retain priority, verified by importing the real bootstrap in fresh
processes. Production cache deadlines, retry
policy and security behavior are unchanged.

The focused Redis/billing regressions and full data-extraction scenario are
verified locally. Full validation must still pass on the exact selected commit
before a release; an economical PR result does not replace that gate.

## Tests on shared test servers

Several runs share one PostgreSQL and one Redis server: xdist workers, the CI
shards, and developers or agents on one machine. A test that passes alone and
fails next to another run is a bug in the test or in the harness, never a
reason to rerun. The rules:

- **Use your own resources.** A test uses this worker's database
  (`DATABASE_URL` after the harness rewrote it) or a scratch database or Redis
  index with a unique name. It never uses the run's base
  `SANDLOOM_TEST_DATABASE_URL`: every worker of the run shares that one.
- **Assert on your own resources, not on server totals.** "How many indexes
  are free" changes while the test runs; "my index counts for me and not for a
  stranger" does not.
- **Feed a snapshot for global logic.** Code that reads the whole server
  (`CLIENT LIST`, `pg_stat_activity`) is tested with a fixed answer; the live
  server only proves the wiring.
- **Hand back what you claim.** A test or a test slot that claims an index or a
  database releases it, so runs do not leak them until `prune`.
- **No real signals across threads.** Under xdist the main thread need not be
  the one running the test: raise the interrupt where the code waits.

Before a change to the harness or a test of shared services, run the fast suite
twice at once against the same servers. Give each run its own
`SANDLOOM_TEST_OWNER`, `SANDLOOM_TEST_DATABASE_URL` and Redis indexes; both
must pass. This check found five such tests during the 0.7.0a9 release, and two
harness bugs. A scanner counted as a client of every index it read, and test
slots never handed their Redis indexes back.

## Generated application profiles

Full validation creates applications through a standalone kernel-only CLI,
then executes its printed instructions: dependency installation, generation,
fresh-app CI, module addition, gates, migrations and an authenticated HTTP round trip.
Bare, identity, tenancy, billing and all-battery profiles run as five parallel
jobs, each with its own PostgreSQL and Redis services. This covers the
`uvx --from sandloom` dependency handoff without serializing the five applications.

## Performance evidence

[CI performance research](ci-performance-research.md) compares measured public
workflow runs, their selected suites and their execution cost. A workflow
limit is not a timing result, and a docs-only skip is not a framework test run.

The serial isolation job depends only on the full-validation input, so it can
start alongside change classification. Its result remains mandatory in the
full-validation gate; publication still waits for all required checks.

A fixture profile on the PR #6 source (pytest shuffle seed 1234, Python 3.12) measured 429.54s:
978 database-engine preparations consumed 43.68s, subprocess calls 64.39s,
conformance setup 37.54s and full GC 19.35s. Runtime snapshot/restore overhead
was below a second. Rewriting the reset probe in raw SQL improved a small
100-reset sample only from 3.925s to 3.575s median; that experiment was not
retained because the suite-level gain did not justify extra SQL handling.

The child-only audit extends `subprocess_probe` to boot-entry-point checks,
child GC/shuffle/import tests, L1 example registration and isolated metric
probes. Mixed parent/child generator, database, networking and runtime tests
remain serial. Every marked case still runs in the unfiltered full matrix.

The repeated local serial pass completed in 393.18s (5004 passed, six skipped,
52 child-only cases deselected and one expected failure), against 429.54s
before (5037 passed, seven skipped, 18 deselected and one expected failure).
The 34-case difference is exactly the audited child-only set; all remain
in the unfiltered full matrix. Recorded subprocess calls fell from 127 to 84
and their aggregate wall time from 64.39s to 44.29s. Both runs used the same
fixture profiler and pytest shuffle seed; Hypothesis inputs and OS scheduling
were not fixed, so the whole-suite difference is an observation rather than
a guaranteed 8.5% saving on every run.
