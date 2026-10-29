---
title: CI profiles and minute budget
---

# CI profiles and minute budget

Private-repository development uses a smaller automatic PR profile. Every
change still needs the [owner's final review and manual merge](branches.md).
No automatic merge or new server-side required checks are introduced here.

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
source selection. CI configuration changes have dedicated regression tests.

Normal PR backend tests use Python 3.12, without coverage or `slow` tests.
The Python 3.11/3.13/3.14 compatibility runs, coverage, serial isolation suite,
generated-app quickstart/recipes/split checks and live microservices smoke run
in full validation. Those omissions from the PR profile are intentional; its
green result is not evidence that full pre-release validation passed.

Checks run on PRs rather than repeating the same work after a merge to `main`.
New commits cancel older runs of the same PR. Closing or merging a PR also
cancels its older Monorepo CI run without starting new test jobs. Short-lived
workflow artifacts expire after three days (the SBOM after seven).

## Full pre-release validation

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
The disabled PyPI workflow still requests full backend validation if run for
its build-only rehearsal.

The [local CI runner](local-ci.md) retains the full four-version matrix and
heavy backend jobs by default. It deliberately overrides `full_validation` in
its private act workflow copies and local expression context. Local job
selection (`--jobs`, `--python`) is still available. The local runner does not
cover every workflow in the manual umbrella (notably the full admin workflow
and extraction scenario); validate those separately or use the umbrella.

## Known validation debt

The initial source snapshot's extraction-e2e failed because its probe routes
lack a declared authentication policy. One initial Python 3.11 run also hit
a Redis connection timeout in the concurrent subject-state test; that test
passed in the next PR run. The post-merge Python 3.12 coverage run also hit
a Redis connection timeout during billing webhook-test setup. These service
timeouts are not confined to one Python version. This CI cost change does not
fix those problems.
The extraction failure must be resolved and the full selected commit verified
before release. Moving a check to explicit full validation does not close a bug.
