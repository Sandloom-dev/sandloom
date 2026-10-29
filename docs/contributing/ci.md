---
title: CI profiles and minute budget
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
| Backend source/tests, migrations, package/build code or dependencies | Static gates, fast backend suite on Python 3.12, production image build; package checks for their declared paths |
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
The Python 3.11/3.13/3.14 compatibility runs, coverage, serial in-process isolation suite,
generated-app quickstart/recipes/split checks and live microservices smoke run
in full validation. Those omissions from the PR profile are intentional; its
green result is not evidence that full release validation passed.

Checks run on PRs rather than repeating the same work after a merge to `main`.
New commits cancel older runs of the same PR. Closing or merging a PR also
cancels its older Monorepo CI run without starting new test jobs. Short-lived
workflow artifacts expire after three days (the SBOM after seven).

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

## Generated application profiles

Full validation creates applications through a standalone kernel-only CLI,
then executes its printed instructions: dependency installation, generation,
fresh-app CI, module addition, gates, migrations and an authenticated HTTP round trip.
Bare, identity, tenancy, billing and all-battery profiles run as five parallel
jobs, each with its own PostgreSQL and Redis services. This covers the
`uvx --from sandloom` dependency handoff without serializing the five applications.
