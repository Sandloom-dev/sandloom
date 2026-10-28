---
title: Local CI runner
---

# Local CI runner (`tools/ci/ci_local.sh`)

When GitHub Actions is unavailable (billing block, outage), verify a branch or
PR **before merge** with the local Docker CI runner. It reproduces the PR gates
of `.github/workflows/ci.yml`, `docs.yml` and `architecture-check.yml` against
an exported git ref — your working tree is never used or modified.

Fallback workspaces get a fresh Git index of the exported files before any
dependency install or build. Checks using `git ls-files` therefore see the
checkout's tracked sources, including committed files matched by `.gitignore`,
and exclude later build artifacts. The original history and remotes are absent.

```bash
tools/ci/ci_local.sh <git-ref>                       # every job, in parallel (see below)
tools/ci/ci_local.sh origin/feat/x --fetch           # fetch first, test the remote head
tools/ci/ci_local.sh pr:55                           # a PR head (fetched from origin)
tools/ci/ci_local.sh HEAD --jobs backend-static,docs # some jobs (or job groups)
tools/ci/ci_local.sh HEAD --jobs backend-tests --python 3.12
tools/ci/ci_local.sh HEAD --parallel 1               # sequential
tools/ci/ci_local.sh HEAD --rerun-failed             # only what failed last time
tools/ci/ci_local.sh --list                          # jobs, engine, why
tools/ci/ci_local.sh --check                         # workflow drift check only
```

Requirements: Docker Desktop, `uv` (the script is a uv inline-metadata script,
`tools/ci/ci_local.py`), and [act](https://github.com/nektos/act)
(`brew install act`) — without act, `--engine fallback` runs every job with
the built-in runner.

Exit code `0` = every selected job passed. Logs, `summary.md` and
`summary.json` stay in `.ci-local/runs/<run-id>/` (git-ignored).
`--report-pr <N>` posts `summary.md` as a PR comment via `gh` (off by default).

## Parallel jobs (the default)

`--parallel` defaults to what the Docker engine (the Docker Desktop VM on
macOS) affords: `min(3, CPUs // 3, memory // 2.5 GB)`, at least 1 — three at
once on the default 10-CPU / 8 GB VM, which takes a full run from ~16 min
(sequential) to ~7 min. The run prints the value it chose; `--parallel 1`
runs the jobs one by one in workflow order.

- **Order.** Timing-sensitive jobs (`benchmarks-security`: wall-clock
  cancellations, the load harness's latency checks) start first, then the
  rest longest-first, by the previous run's durations
  (`.ci-local/runs/*/summary.json`, with built-in estimates as a fallback),
  so the run does not end waiting on one long job. The summary still lists
  the jobs in workflow order.
- **CPUs.** A container job is pinned with `--cpuset-cpus` (`--cpus`, default
  4, as a 4-vCPU GitHub runner). Slot 0 gets CPUs `0..3` and, on an engine
  with room for it, no other container job shares them — the timing-sensitive
  job starts first and takes that slot, so its neighbours cannot starve it.
  The other slots share the remaining CPUs. act jobs are capped with `--cpus`
  but not pinned.
- **Isolation.** Parallel jobs never share a service: each container job has
  its own `postgres` / `redis` containers named `sandloom-ci-<run-id>-<job>-…`,
  in the job container's own network namespace (ci.yml's `localhost:5433` /
  `localhost:6380` are per job, nothing is published on the host); the
  microservices stack has its own compose project, `/24` subnet, image tag
  and an ephemeral gateway port; act containers are named per job; the
  docker-build image tag carries the run id. act is never given an artifact
  server (see [Artifact steps](#artifact-steps)); act's cache server binds
  a free port itself and each act job has its own cache directory.
- **act container names.** act names a job's container after the workflow's
  `name:` and the job only, so every act job that `needs: changes` used to
  run its own `changes` job in the *same* container name — two at once
  created and removed one container under each other (`Error response from
  daemon: RWLayer of container … is unexpectedly nil`). Such a job now runs
  a private, untracked copy of its workflow
  (`.github/workflows/.ci-local-<job>.yml` in the act checkout) whose only
  difference is a top-level `name:` carrying the local job name.
- **Docker daemon hiccups.** Should act's container setup still fail on a
  daemon error, an act job whose attempt died that way —
  a daemon error in *Set up job*, before any `Pre` / `Main` / `Post` step of
  any job ran — is retried **once**, and its note says so. A failure after a
  workflow step started is a result and is never retried.

## Artifact steps

No artifact storage exists locally. act's own artifact server binds a fixed
port and does not serve the results API that `actions/upload-artifact@v4`
calls, so the step timed out on `CreateArtifact` and failed the `docs` job.
The runner therefore emulates artifact actions instead of running them:

- **act.** A job using `actions/upload-artifact` or
  `actions/download-artifact` (`docs`, `supply-chain`) runs a private,
  untracked copy of its workflow (`.github/workflows/.ci-local-<job>.yml`, the
  same copy as for `needs`). Each artifact step there is a `run:` step; its
  `with:` inputs become `INPUT_<NAME>` variables and every other key (`if:`,
  `continue-on-error`, `name`) is kept. An upload lists the files it would
  upload and honours `if-no-files-found` (`warn` by default, `error` fails the
  step); nothing leaves the container. A download fails with an explanation:
  no workflow downloads artifacts today, and a job that does needs
  `--engine fallback` or GitHub.
- **Fallback.** An upload copies its path into the run directory
  (`<job>-artifact-<name>`), as before.

`tests/architecture/test_ci_local_runner.py` checks that the copy differs from
the workflow only in these steps and that the emulated upload behaves as above.

## Fail-fast loop: `--rerun-failed`

`tools/ci/ci_local.sh <ref> --rerun-failed` reruns only the jobs that did not
pass (`fail`, `error`, `cancelled`) in the newest run of the **same ref
string** under `.ci-local/runs/` (`--out` moves it) — on whatever commit the
ref names now, so the loop is: fix, commit, `--rerun-failed`, until it prints
that nothing failed. It cannot be combined with `--jobs` / `--python`, and,
like any subset, it never writes the full-pass marker: **the merge gate stays
a full run.**

## Two engines

| job | engine | why |
|---|---|---|
| `backend-static`, `supply-chain`, `architecture-check`, `docs`, `deploy-profiles`, `docker-build` | **act** — the real workflow job | — |
| `backend-tests-3.11/3.12/3.13`, `benchmarks-security`, `examples-*` | fallback (container) | ci.yml reaches its services at `localhost:5433` / `localhost:6380`. Under act that only works with host networking, where those ports are the shared dev containers `sandloom_postgres_test` / `sandloom_redis_test` (tests would truncate them); `backend-tests` also starts `docker run --name redis -p 6380:6379`, which collides by name and port |
| `quickstart` | fallback (container) | the documented quickstart (`tools/smoke/quickstart_check.py`) against the job's own Postgres `:5433` / Redis `:6380`, which under act would be the shared dev containers |
| `app-recipes` | fallback (container) | the `sandloom docs` recipes run in a generated app (`tools/smoke/recipes_check.py`), against the job's own Postgres / Redis like `quickstart` |
| `microservices-smoke` | fallback (host docker) | the step hard-codes the gateway at `localhost:8080` and the stack's default subnet; two stacks would collide |

**act** runs with `catthehacker/ubuntu:act-latest` (native arm64), a
`pull_request` event whose base is the merge base with `origin/main` (an empty
base when the ref *is* main, so every path filter is true), the run's own
docker network, and an **empty** `GITHUB_TOKEN` (act would otherwise pick up
`gh auth token`; no credentials are ever passed). The `changes` job and
`dorny/paths-filter` therefore run as on GitHub: a job the filter skips is
reported `skipped`.

The **fallback** never copies the job commands: it reads the exported ref's
workflow and runs each `run:` step verbatim — job/step `env`,
`working-directory`, `if:`, `continue-on-error`, `timeout-minutes`,
`$GITHUB_ENV`/`$GITHUB_PATH`, the matrix and its `${{ }}` expressions. It
owns only the runner environment:

- a job container `python:<setup-python>-slim-bookworm` + the `uv` binary
  pinned by `astral-sh/setup-uv` (`UV_PYTHON` set like the action), 4 CPUs /
  4 GB (`--cpus`, `--memory`; `-n auto` is pinned to the CPU budget through
  `PYTEST_XDIST_AUTO_NUM_WORKERS`, like a 4-vCPU GitHub runner);
- its **own fresh** `postgres:16` / `redis:7` containers in the job
  container's network namespace, each listening on the host-side port of its
  ci.yml mapping — so `localhost:5433` / `localhost:6380` work unchanged
  (backend-tests' Redis gets its `--databases 256 --save "" --appendonly no`).
  Each Postgres runs with `fsync=off` and `full_page_writes=off`: it lives
  for one job and no test crashes the server, so flushes only cost time
  (benchmarks-security's S5 step −18 %). `synchronous_commit` keeps its
  default — a suite test pins `SHOW synchronous_commit` = `on`;
- the microservices stack gets its own compose project, free `/24` subnet
  (gateway `.10`, dynamic pool `SANDLOOM_IP_RANGE` = the upper `/25`, as in the
  profile's defaults) and image tag, and its gateway publishes on an **ephemeral** loopback port:
  `GATEWAY_PORT=127.0.0.1:0` turns the profile's `${GATEWAY_PORT:-8080}:8080`
  into `127.0.0.1:0:8080`, so Docker picks a free port atomically when it
  binds (the runner never picks one itself — pick-then-bind races other
  workloads on a shared host with "Address already in use"). After
  `compose up` the runner reads it back with `docker compose port gateway
  8080` and rewrites the smoke step's `localhost:8080` to it — the only
  rewrite. The profile's default (`8080` on all interfaces) is unchanged.

Fallback jobs ignore the `changes` path filter (they always run: stricter than
CI).

### backend-tests matrix: what each Python runs

| job | suite |
|---|---|
| `backend-tests-3.11` | full (`-n auto --dist loadfile`) |
| `backend-tests-3.12` | full, with coverage (`COVERAGE_CORE=sysmon`) |
| `backend-tests-3.13`, `backend-tests-3.14` | `-m "not slow"` |

The `slow` marker covers the multi-process suites — the split conformance
matrix, module extraction, the generated project, the boot configuration
checks (`uv run pytest --collect-only -qq -m slow` lists them; about 45 % of
a job's work). They run **only on 3.11 and 3.12**; 3.13 and 3.14 run every
other test. Owner decision of 2026-10-25 (about 12 % of a full run). The
runner takes this from ci.yml's matrix (`slow: true|false`) like any other
step, so local and GitHub runs select the same tests.

### Overloaded host: `SANDLOOM_CI_LOCAL_TIMEOUT_SCALE`

On a shared machine under heavy load a job can hit its workflow
`timeout-minutes` although it would pass on a GitHub runner. Opt in to a
multiplier (`1`–`10`, default `1`) for the fallback runner's job/step
timeouts and act's outer kill timer:

```bash
SANDLOOM_CI_LOCAL_TIMEOUT_SCALE=3 tools/ci/ci_local.sh HEAD --jobs benchmarks-security
```

The workflows' `timeout-minutes` are never edited. A scale other than `1` is
printed as a warning in the summary (`summary.md`, `summary.json`, terminal)
and in every job's note — such a pass does not prove the job fits its CI
timeout. act enforces a job's own `timeout-minutes` itself, so the scale only
stretches fallback jobs.

## Isolation, lock and cleanup

- Each run has an id (`YYYYmmdd-HHMMSS-xxxx`); containers, the docker network
  and the compose project are named/labelled with it. The shared dev
  containers (`sandloom_postgres_test`, `sandloom_redis_test`, `sandloom_redis_bench`) are
  never used.
- The fresh services also satisfy the test harness's guards
  ([Testing → Isolation guards](../guides/testing.md#isolation-guards)): their
  databases / Redis indexes are unmarked, so the run claims them (no
  `SANDLOOM_TEST_*` needed), and a stock `max_connections` of 100 affords the
  4 pinned xdist workers (`4 × 13`) plus the 40 the split conformance matrix
  may hold. Outside the runner, the `backend-full-tests` pre-push hook needs
  `SANDLOOM_TEST_DATABASE_URL` (the shared `sandloom_test` is refused).
- While running, the runner holds `/tmp/sandloom-ci-local.lock` — an
  `fcntl.flock` on the file, never the file's mere existence
  (`tools/ci/ci_lock.py`, shared with the CPU benchmarks). The file names the
  run id, pid, ref and start time for humans and is removed on release. A
  second run waits for the flock (`--no-wait` fails instead). A crashed holder
  releases it automatically (the kernel drops the flock), so a leftover file —
  empty, unparseable or naming a dead pid — is stale and taken over; never
  delete the file by hand while a holder is alive. CPU benchmarks take the
  same flock (`benchmarks/ladder/http_pipeline.py` refuses to run beside CI,
  and CI waits for it) or probe it with `ci_lock.is_held()`
  (`rpc_guard.py`, `run_rpctx.py`) — never `os.path.exists`.
- On exit — including Ctrl-C / SIGTERM — containers, the network, the compose
  stack (`down -v`), run-scoped images and the temp export are removed and
  the lock released; logs are kept. The uv cache (`sandloom-ci-local-uv-cache`
  volume), the runner images (`sandloom-ci-local/runner:*`) and act's cache
  (`~/.cache/sandloom-ci-local/act-cache`) persist for speed.

## A full pass skips the duplicate pre-push checks

When every job of a run passes (no `--jobs` / `--python` subset), the runner
writes a marker named after the tested commit's **tree** to
`~/.cache/sandloom-ci-local/passed/` (`SANDLOOM_CI_PASS_DIR` overrides it). The pre-push
hooks that mirror CI jobs — `backend-full-tests`, `docs-build`,
`production-image` — run through `tools/ci/unless_ci_passed.sh` and are skipped
when the pushed commit's tree has such a marker, so a CI-green push does not
run the same suite a second time. The key is the tree, not the commit: any
content change needs a new run. `package-distributions` is not a CI job and
always runs; `SANDLOOM_PREPUSH_IGNORE_CI_PASS=1 git push` forces the full hooks.

## Which runner version runs

`tools/ci/ci_local.sh <ref>` executes the ref's **own** `tools/ci/ci_local.py`
(extracted to a temp file), not the working tree's copy, so a PR that changes
a workflow together with the runner's mapping for it (a new CI step, new
fingerprints) is verified by its own runner. It prints a note when the two
differ. Set `SANDLOOM_CI_LOCAL_USE_WORKTREE_RUNNER=1` to force the working-tree
copy (e.g. while developing the runner itself).

## When ci.yml changes

`tests/architecture/test_ci_local_runner.py` (run by the
`backend-static` job) fails when a workflow uses something the runner cannot
map (a new job, `uses:` action, expression, service image) or when a mapped
job's body changed since the mapping was reviewed. Review `CI_JOBS` /
`KNOWN_USES` in `tools/ci/ci_local.py`, then update `FINGERPRINTS` with the
values `tools/ci/ci_local.sh --check` prints.

## Not reproduced

- `dependency-review.yml` (needs the GitHub dependency graph; see [Supply-chain gates](supply-chain.md)).
- The non-PR workflows
  (`release.yml`, `packages.yml`, `benchmarks.yml`, `extraction-e2e.yml`).
- Uploading artifacts: act jobs list what they would upload; fallback jobs
  (the coverage HTML of `backend-tests-3.12`) copy it into the run directory
  ([Artifact steps](#artifact-steps)). `actions/cache` (mypy cache) is not
  restored in the fallback.
- Docker Desktop on macOS is an arm64 Linux VM; GitHub runners are x86_64.
  Native wheels and the production image are therefore built for arm64.
