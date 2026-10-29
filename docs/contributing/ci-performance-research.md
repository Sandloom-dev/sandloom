---
title: CI performance research
---

# CI performance research

Related: [CI profiles](ci.md) · [Testing guide](../guides/testing.md).

Research date: **2026-10-29**. This is an observed sample, not a ranking of
framework quality or a universal target for test duration.

Public CI from mature Python projects does run for several minutes. In the
sample below, five test-executing PR runs per project had median elapsed times
between **5:43 and 9:55**. Broad compatibility/build workflows could take much
longer. This does not establish that Sandloom's current duration is optimal;
it establishes that a nine-minute full validation is not, by itself, evidence
of an unusual testing practice. The useful comparison is the work performed,
the time to actionable feedback, and the resources spent.

## Measurement method

The source is the first-party GitHub Actions REST API: list recent successful
workflow runs, then enumerate their jobs, including all pages. Select the
five newest successful **first-attempt** PR runs that actually execute the
project's test jobs. Do not select the fastest five. Samples were collected
on October 29; several FastAPI/SQLAlchemy samples are from October 26–28.

- **Elapsed time:** latest non-skipped job completion minus `run_started_at`.
  It includes setup, queues between jobs, tests, downstream jobs and cleanup.
- **Sum of job minutes:** sum of non-skipped jobs' `completed_at - started_at`.
  Parallel jobs overlap in elapsed time. This is an execution-time sum, not
  GitHub's billed amount; it does not apply OS or runner pricing multipliers.
- Workflow `timeout-minutes` is a failure limit, not a measured duration.
- Canceled/failed runs and second attempts were excluded. Successful runs alone
  do not describe a project's failure rate or typical time to find a defect.
- These are whole workflow samples, not pytest-only benchmarks. They are not
  normalized for test count, native code, runner hardware, platform, database,
  coverage, benchmarks or the number of compatibility combinations.

The API fields and endpoints are documented in GitHub's
[workflow-run API](https://docs.github.com/en/rest/actions/workflow-runs)
and [workflow-job API](https://docs.github.com/en/rest/actions/workflow-jobs).
Each sample below links to the originating run so its scope and job breakdown
can be inspected.

## Observed durations

| Project and workflow | Sample | Median elapsed | Elapsed range | Median sum of job minutes | Active jobs per run |
| --- | --- | ---: | ---: | ---: | ---: |
| FastAPI, `Test`, PR | 5 | 6:17 | 6:01–6:59 | 41.73 | 20 |
| Pydantic, `CI`, PR | 5 | 9:55 | 8:48–12:16 | 149.67 | 56 |
| Django, `Tests`, PR | 5 | 6:40 | 5:40–7:42 | 11.63 | 4 |
| SQLAlchemy, `Run tests on a pr` | 5 | 5:43 | 4:24–8:24 | 30.60 | 9 |
| SQLAlchemy, `Run tests`, push to `main` | 5 | 33:07 | 27:06–76:34 | 512.88 | 104 |

The SQLAlchemy push workflow is a broad cross-platform/build matrix, not a
single serial test process. Its long elapsed time includes scheduling: in the
76:34 sample, the longest individual job was 9:00. A workflow with many queued
jobs can take much longer than its slowest test command. These are observations
from the [sample's job timestamps](https://api.github.com/repos/sqlalchemy/sqlalchemy/actions/runs/40403425914/jobs?per_page=100),
not a claim that SQLAlchemy's tests themselves need 76 minutes in one process.

FastAPI had two newer successful `Test` runs lasting only 17 and 22 seconds;
the test matrix was skipped. They are deliberately excluded from the table:
[run 40883353243](https://github.com/fastapi/fastapi/actions/runs/40883353243),
[run 40686442620](https://github.com/fastapi/fastapi/actions/runs/40686442620).
Comparing those runs with Sandloom's full release validation would be misleading.

Pydantic's three latest sampled successful pushes to `main` took **21:53,
27:23 and 26:22**, with 116 active jobs each. Their workflow enables additional
native-wheel builds on main/tags or an explicit full-build label; this is
additional work beyond its PR profile:
[40933290063](https://github.com/pydantic/pydantic/actions/runs/40933290063),
[40929898819](https://github.com/pydantic/pydantic/actions/runs/40929898819),
[40929802413](https://github.com/pydantic/pydantic/actions/runs/40929802413),
[pinned workflow](https://github.com/pydantic/pydantic/blob/57b0a4e8854e3525c208d3a058fab8da0dcd90a5/.github/workflows/ci.yml).

### Sample ledger

Elapsed seconds are unrounded; the duration table formats them as minutes and
seconds. Run configuration was read at the first listed run's head SHA. A PR
checkout may execute GitHub's synthesized merge commit; the head SHA identifies
the inspected source snapshot rather than asserting a universal checkout policy.

| Project/profile | Successful first-attempt runs, newest first | Elapsed seconds, same order |
| --- | --- | --- |
| FastAPI PR | [40824424135](https://github.com/fastapi/fastapi/actions/runs/40824424135), [40452495831](https://github.com/fastapi/fastapi/actions/runs/40452495831), [40404874858](https://github.com/fastapi/fastapi/actions/runs/40404874858), [40309280097](https://github.com/fastapi/fastapi/actions/runs/40309280097), [40306634413](https://github.com/fastapi/fastapi/actions/runs/40306634413) | 361, 377, 419, 417, 376 |
| Pydantic PR | [40935460964](https://github.com/pydantic/pydantic/actions/runs/40935460964), [40935080894](https://github.com/pydantic/pydantic/actions/runs/40935080894), [40933040908](https://github.com/pydantic/pydantic/actions/runs/40933040908), [40931941721](https://github.com/pydantic/pydantic/actions/runs/40931941721), [40931672290](https://github.com/pydantic/pydantic/actions/runs/40931672290) | 595, 617, 581, 528, 736 |
| Django PR | [40919286882](https://github.com/django/django/actions/runs/40919286882), [40916641478](https://github.com/django/django/actions/runs/40916641478), [40898439088](https://github.com/django/django/actions/runs/40898439088), [40896988016](https://github.com/django/django/actions/runs/40896988016), [40890939907](https://github.com/django/django/actions/runs/40890939907) | 462, 340, 355, 435, 400 |
| SQLAlchemy PR | [40931720405](https://github.com/sqlalchemy/sqlalchemy/actions/runs/40931720405), [40899311500](https://github.com/sqlalchemy/sqlalchemy/actions/runs/40899311500), [40713650316](https://github.com/sqlalchemy/sqlalchemy/actions/runs/40713650316), [40528700592](https://github.com/sqlalchemy/sqlalchemy/actions/runs/40528700592), [40362844271](https://github.com/sqlalchemy/sqlalchemy/actions/runs/40362844271) | 280, 504, 264, 343, 353 |
| SQLAlchemy main | [40907415637](https://github.com/sqlalchemy/sqlalchemy/actions/runs/40907415637), [40403425914](https://github.com/sqlalchemy/sqlalchemy/actions/runs/40403425914), [40403368725](https://github.com/sqlalchemy/sqlalchemy/actions/runs/40403368725), [40348744063](https://github.com/sqlalchemy/sqlalchemy/actions/runs/40348744063), [39969892362](https://github.com/sqlalchemy/sqlalchemy/actions/runs/39969892362) | 1626, 4594, 2085, 1987, 1716 |

## What these projects actually do

### FastAPI: select changes, parallelize, then combine coverage

The inspected workflow selects source-related changes, runs a compatibility
matrix across Python/OS/dependency combinations and combines coverage after
its test jobs. Its test script uses `pytest -n auto --dist loadgroup`, so
related groups stay on one worker. Regression checks for changed tests and
benchmarks are separate jobs. The ten-minute job timeout is not its observed
runtime. Sources:
[pinned workflow](https://github.com/fastapi/fastapi/blob/8bf381108c8b3336ae17957421a916394ddff4a1/.github/workflows/test.yml),
[pinned test script](https://github.com/fastapi/fastapi/blob/8bf381108c8b3336ae17957421a916394ddff4a1/scripts/test.sh).

### Django: local test clients and different database isolation modes

The sampled GitHub workflow runs SQLite on Windows and free-threaded Ubuntu,
plus JavaScript and script tests. It excludes docs-only changes and caches
package downloads. It is not a measurement of all supported production
database configurations. Django's own runner defaults to parallel processes
and has shuffle/bisect tools for order-dependent failures. Sources:
[pinned workflow](https://github.com/django/django/blob/f4e1545d8a517bdb8a32126ce511627427b65408/.github/workflows/tests.yml),
[contributor testing documentation](https://docs.djangoproject.com/en/dev/internals/contributing/writing-code/unit-tests/).

Django's in-process client avoids real HTTP. `TestCase` uses nested transactions
and rollback; class-level `setUpTestData` avoids recreating seed data per method.
`TransactionTestCase` provides the more expensive mode needed to observe real
commit/rollback behavior. This is a test-semantics choice: replacing a real
commit test with rollback-only isolation would change what it proves. Source:
[testing tools](https://docs.djangoproject.com/en/dev/topics/testing/tools/).

### SQLAlchemy: a deliberately smaller PR matrix

The PR workflow tests two Python versions on Linux with three build types,
plus static/type checks; its `TOX_WORKERS=-n4` is passed into pytest through nox.
The inspected GitHub nox sessions use SQLite, with an asyncio driver in the
greenlet variant. The push workflow expands the OS/architecture/Python matrix.
The difference between the two profiles is explicit in source:
[PR workflow](https://github.com/sqlalchemy/sqlalchemy/blob/e44750a6bbfe6b6672e4661613663efdf8a182b2/.github/workflows/run-on-pr.yaml),
[nox sessions](https://github.com/sqlalchemy/sqlalchemy/blob/e44750a6bbfe6b6672e4661613663efdf8a182b2/noxfile.py),
[push workflow](https://github.com/sqlalchemy/sqlalchemy/blob/2d8e64920e61872d40ccf8430d289ab1cb4688b5/.github/workflows/run-test.yaml).

### Pydantic: parallel jobs, targeted coverage and separate build scope

Pydantic's PR workflow includes Python/platform tests, Rust/core tests, typing,
memory and documentation jobs. Broad native wheel builds are conditional on
main/tags or an explicit full-build label. It caches dependencies in test jobs;
its PyPy test command omits coverage because coverage is expensive there.
The ordinary test matrix, free-threaded cases and native builds have different
purposes; its total workflow time is not a benchmark of one Python unit-test
command. Source:
[pinned workflow](https://github.com/pydantic/pydantic/blob/57b0a4e8854e3525c208d3a058fab8da0dcd90a5/.github/workflows/ci.yml).

## What is applicable to Sandloom

These are recommendations inferred from the source and observations above,
not claims that copying another project's exact worker count will improve
Sandloom.

1. **Keep fast feedback distinct from release confidence.** Sandloom already
   has changed-area PR checks and an opt-in full profile. Compare its PR profile
   with PR profiles, and full validation with full validation. Track both.
2. **Optimize repeated preparation before adding workers.** Measure aggregate
   fixture setup/teardown, database reset, schema compilation, imports and child
   process startup. Preserve disposable-resource ownership, RLS checks, sequence
   reset and fallback behavior while removing redundant work.
3. **Use the cheapest faithful test mode.** Pure policy/serialization tests need
   no live servers. In-process ASGI tests can cover request behavior. Keep real
   PostgreSQL, Redis, committed UoW and multi-service tests wherever those are
   the guarantees under test. Do not substitute SQLite for PostgreSQL/RLS proof.
4. **Reuse expensive immutable preparation and retain local state isolation.**
   Transactions/savepoints are useful for tests that can operate within them;
   tests of independent commits, reconnects, migrations or multiple services
   need their existing real boundaries. Broadening fixture scope is not a
   safe automatic optimization.
5. **Balance parallel work without duplicating module-scoped setup.** Sandloom's
   `loadfile` choice keeps a test file together. Other xdist scheduling modes
   change grouping guarantees; measure the longest-worker tail before changing
   one. Group scheduling is documented by
   [pytest-xdist](https://pytest-xdist.readthedocs.io/en/stable/distribution.html).
6. **Preserve the serial isolation gate's purpose.** Sharding that gate changes
   which tests share a process and can hide cross-file leaks. Parallel matrix
   coverage does not replace that evidence. Improve fixture/engine/SQL/import
   overhead inside the serial pass; any reduced gate would need an explicit
   change to the assurance contract.
7. **Measure elapsed time and execution cost separately.** Extra jobs can reduce
   wait time while increasing total job minutes and queues. Check the critical
   path and retain before/after runs with the same selection and seed.

A reasonable engineering objective is faster actionable PR feedback and a
measured reduction in full validation, while keeping the proof each suite
provides. This research does not establish a universal two- or three-minute
standard for mature frameworks.
