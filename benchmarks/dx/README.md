# DX eval: fresh agents build features on Sandloom

A repeatable version of the agent experiment in
`docs/design/dx-cognitive-load-2026-10-25.md` (section 2), and the yardstick
for every DX change: is the agent context, the `sandloom add` generators or the
public API (`sandloom.sdk`) easier to build on than before?

## Two suites

- **North star:** `--suite product` (`product/README.md`). An agent builds a
  whole 6-module product from a spec, adds the 7th module, splits it into
  microservices and changes one feature. Hidden acceptance tests and
  footgun probes score it. This is what Sandloom is for; report it first.
- **Diagnostics:** the 10 small tasks below (the default suite). One framework
  mechanism each, cheap enough to run on every change.

## Modes

`--mode app` (the default) measures what a framework **user** gets;
`--mode framework` measures an agent working inside the framework repository
(the method below, unchanged). Every result JSON records its `mode`, the
summary prints it, and `report --compare` / `run --compare` refuse to compare
runs of different modes (`--force-compare` turns that into a warning).

**App mode, how it works:**

1. **Wheels, once per commit.** The runner builds every
   `packages/*` distribution of `--ref` (the set
   `tools/packaging/check_package_distributions.py` builds) from a clean
   worktree of that commit into `$SANDLOOM_DX_EVAL_HOME/_dist/<sha>/wheels`, installs
   them into a CLI venv, and exposes only its `sandloom` entry point in
   `_dist/<sha>/bin`. Uncommitted edits never reach the agent. Every wheel
   carries the commit in its version (`0.7.0+g<sha12>`), so uv can never
   install another commit's cached build of `0.7.0`; scoring refuses an app
   whose installed Sandloom versions are not the built ones (`outcome: error`).
2. **An empty task directory outside the repository**
   (`$SANDLOOM_DX_APP_HOME`, default `~/.cache/sandloom-dx-apps/<label>/<task>/`). The
   agent's PATH starts with that `bin/` and has no path into the framework
   repository and no venv; `UV_FIND_LINKS` points `uv` at the wheels, so the
   app's `uv sync` resolves `sandloom`, `sandloom-identity`, … from them (the
   benchmark-specific builds must take priority over PyPI releases). The framework source is nowhere in the
   agent's tree, only the installed package in the app's `.venv`.
3. **The agent runs `sandloom new <app>` itself** (the footer, `footer.app.md`,
   names the app, `teamhub` for the tasks and `academy` for the product, and
   says the product has accounts, organizations and billing). Its context is
   what `sandloom new` generates (`AGENTS.md`, `CLAUDE.md`) plus the task text.
   Claude Code loads a nested `CLAUDE.md` only when files under it are read,
   so the footer points at the app's `AGENTS.md`, the file a user starting a
   session in the app would get loaded. `ALLOWED` adds `Bash(sandloom:*)` and
   `Bash(uv lock:*)`.
4. **Scoring:**
   - **gates**: the app's own `sandloom test --ci` (each step a gate, `ci: <step>`),
     then `sandloom test` (one gate), then the task module's tests
     (`module-tests`). There is no framework suite.
   - **diff base**: the runner regenerates the app the agent started from by
     re-running the agent's last `sandloom new <app> …` (same options, `--dir`
     dropped, from the transcript; else options read back from the app's
     README). A follow-up task's base is its start task's checkpoint.
   - **hidden checks**: the same checkers. `checks/_dx.py` locates modules
     through the project's `architecture_rules.yml` (`modules_path`), so
     `module_dir("notes")` is `src/sandloom/notes` or `<app>/modules/notes`;
     changed paths come from the runner (`DX_CHANGED`). A check that cannot
     apply in an app is skipped and left out (t2's gateway route: an `sandloom new`
     app has no microservices profile).
   - **hidden product tests**: copied into `<app>/tests/_dx_product` and run
     with the app's pytest config.
   - **kernel files read**: the same paths, now in the installed package
     (`.venv/…/site-packages/sandloom/core/**`, `sandloom/sdk/_*/**`,
     `sandloom/shared/{authz,guards}/**`), plus any read under the framework
     repository, recorded as `repo:<path>`.
   - **edited outside**: paths outside the task's `<app>/modules/<name>/`,
     except the app's expected wiring, which is recorded in `expected_wiring`
     and does not count. Expected wiring is `pyproject.toml`, `.env*`,
     `deploy/`, `architecture_rules.yml`, CI, pre-commit and Docker files, the
     composition root (`<app>/asgi.py`, package `__init__`s), and the shared
     DTO package `<app>/shared/`. Generated files (`<app>/shared/events/*.json`,
     contract stubs, module/route/contract snapshots, `uv.lock`) are
     `generated_files`. Edited installed framework files (a RECORD hash
     mismatch in the app's venv) count as `framework` edits. Another app
     module (the sample `example` included), root `tests/` and docs count.
   - `created_env_file` is always false: `sandloom new` writes the app's `.env`.

**The owner's rule: the eval measures how developers USE the framework.**
Every task runs in an `sandloom new` app (`--mode app`, the default); no app-mode
task edits the framework or its bundled modules (identity, tenancy, billing
arrive as installed batteries).

**Which task runs in which mode** (`modes = [...]` in each task's metadata;
a task text can hold `<!-- mode: app -->` / `<!-- mode: framework -->`
blocks). `kernel_modes = ["framework"]` marks a task whose framework-mode
variant is a **kernel-contributor task** (it edits a bundled module or the
repository's deploy profile): a whole-suite run skips it, naming it with
`--tasks` runs it.

| task | app mode starts from | framework mode |
|---|---|---|
| `t1_notes`, `t2_welcome`, `t6_team_overview` | `sandloom new` (the agent runs it) | same task |
| `t3_quotas` | `sandloom new`; the caller is the app's own `invites` module (same mechanism: a required command contract, consumed once per keyed request) | same task (caller: tenancy) |
| `t10_org_banner` | `sandloom new`; the cheapest task, the app-mode smoke task | same task |
| `t4_resend_invitation` | the **teamhub seed**: an admin-only resend in its `invites` module (org-admin gate on its tenant table; audited or evented on the route's own path, the automatic sensitive-write audit counts) | kernel-contributor (tenancy) |
| `t5_billing_contacts` | the **teamhub seed**: a `TenantScoped` contacts table with RLS in its `workspace` module, a new revision of the module's chain (`sandloom migrate new`; the applied baseline untouched) | kernel-contributor (billing) |
| `t7_credits_event_v2` | the **teamhub seed**: roll `invites.invitation.sent` v1→v2 (dual publish, `activity`'s consumer moved to v2 and not doubled, v1 kept for unknown readers) | kernel-contributor (billing channel) |
| `t8_purge_invitations` | the **teamhub seed**: a nightly periodic task in `invites` (`sandloom add task --periodic`, beat wired: the app's `sandloom test` checks SANDLOOM-T010; at-least-once, explicitly cross-tenant) | kernel-contributor (tenancy) |
| `t9_member_export` | the **teamhub seed**: an admin-only CSV export of `invites`' rows behind a LIVE subscription gate on the organization's account; billing's contract declared or derived (the module snapshot's `consumes`) | kernel-contributor (tenancy) |
| `p1`, `p2` (build), `p3`, `p4`, `p6` (follow-ups) | `sandloom new`; follow-ups start from the start task's app checkpoint (`<eval home>/<label>/<task>/checkpoint/`) | same tasks |
| `p5_scale_switch` | p1's app checkpoint: a `deploy/compose/microservices.yml` next to the monolith, config only (`checks/check_p5_scale_switch.py`, below) | kernel-contributor (the repository's profile) |

`run` without `--tasks` skips the tasks that do not support the mode and the
kernel-contributor variants (and says so); naming an unsupported task with
`--tasks` is an error. The task ids are the same in both modes; every result
records its `mode` (and app-mode results their diff base, `pristine`:
`transcript`, `inferred`, `checkpoint` or `seed:<name>`), and runs of
different modes are never compared (above).

**Seeds** (`seeds/<name>/`). A task that extends an EXISTING product module
starts from a seed app (`seed = "<name>"`), not from an empty directory. The
harness builds it once per label and dist (`<eval home>/<label>/_seed/<name>/`,
rebuilt when the seed's files or the ref change): `seed.toml`'s `sandloom new`,
`sandloom add module` per module, its `remove` list, `files/` copied over the app,
then `uv sync` and `sandloom test`, which must pass. The agent gets a copy (no
venv: it runs `uv sync` itself, as in a follow-up); the same build is the diff
base, so only the agent's edits count. `seeds/teamhub` holds three modules:
`invites` (members invite people by email: a tenant table with a hashed link
token and an expiry; publishes `invites.invitation.sent.v1`), `activity` (the
organization's feed; consumes that event; "another team's module") and
`workspace` (the organization's settings; admin-only write).

**p5 in app mode.** An `sandloom new` app ships only the monolith profile; `sandloom
add deploy <modules> --service <name>` creates the microservices profile and
its gateway from the package's templates, then writes each service (the
recipe `sandloom docs recipe split`). The task measures whether the agent finds it
instead of hand-writing the profile. The checker parses the app's profile
and checks what the framework's own deploy-profile test pins for the
repository's: the split the task names, each module in exactly one service,
the gateway routing each product prefix to its service, no `.py` changed, a
database and a migrate one-shot per service, `SERVICE_BINDINGS` exactly the
contracts each service consumes from another one (+ the issuer's remote
claims), one beat for `reminders`, the durable event bus — and a boot check:
each service's `SANDLOOM_MODULES` loads alone in the app's own interpreter
(`checks/_boot_services.py`, registration only). The hidden acceptance tests
and footgun probes run in-process as before (the code did not change).

**`run --dry-run --mode app`** spends nothing on agents. It builds the wheels,
makes a task directory, runs `sandloom new` and `uv sync` through the agent's PATH
and find-links, and checks that the framework resolved from the wheels and
matches the ref (`framework_matches_ref`: every Sandloom version is its wheel's
`+g<sha12>`, every installed `sandloom/` file is byte-identical to the commit;
a mismatch stops the dry run). It
then scores that untouched app through the real scoring path, once per
selected task: a synthetic transcript, the pristine regeneration, the diff,
the gates and every checker, plus the hidden product tests for the product
suite. The report goes to `<eval home>/<label>/_dryrun/dry-run.json`.
A seeded task is scored on a copy of its built seed app (`pristine:
seed:<name>`); a follow-up, whose start checkpoint a dry run does not have,
on a stand-in: the untouched `sandloom new` app (`pristine: stand-in`; its checks
fail, nothing was built — the point is that setup, scoring and every checker
run). `--mode framework --dry-run` only lists the waves, as before.

## When to run it

The owner's rule: run the eval on **every change to AGENTS.md (or any nested
contract, rule or `sandloom-*` skill), the generators / `sandloom new` output, or the
public API**. To check whether a diff needs a run:

```bash
uv run python benchmarks/dx/run_eval.py triggers --base origin/main   # exit 1 = run the eval
```

## Method (framework mode; app mode differs as described above)

1. **Fresh session, throwaway worktree.** For each task the runner checks
   out a throwaway `git worktree` of the ref under test, outside the repo, and
   starts a fresh headless `claude -p` session in it.
   - The session loads exactly that ref's CLAUDE.md, AGENTS.md, nested files,
     rules and skills. That always-loaded context is what is being measured.
   - User settings and MCP servers are off (`--setting-sources project
     --strict-mcp-config`), so runs on different machines are comparable.
2. **Hidden from the agent:** `benchmarks/dx/` (these checkers) and the DX
   research note, which describes the deviations. Both are removed in a
   commit before the agent starts.
3. **The prompt** is the task text, written the way a product owner would
   phrase it, with no hints about framework mechanisms, plus `footer.md`.
   - The footer says: work in this checkout; your own test database and Redis
     are in `SANDLOOM_TEST_*`; `sandloom test` (with its parallel steps) is allowed,
     raw `pytest -n` is not; no `pkill`.
   - It ends with a self-report: what you built, what you read, your
     mistakes and what caught them, where you got stuck.
4. **Permissions.** The agent runs in `--permission-mode auto`. The
   framework's own workflow is pre-approved (`ALLOWED` in `run_eval.py`):
   read-only shell, the `uv run` gates, `sandloom add` and `sandloom migrate`.
   Anything else goes through Claude Code's safety classifier. It never has
   an unrestricted shell, and it cannot push.
5. **Scoring**, after the agent stops, by the runner, not the agent:

| measure | how |
|---|---|
| **gates** | `ruff check`, `ruff format --check`, `mypy src/`, `sandloom check`, `tools/checks/check_architecture.py`, `sandloom contracts snapshot --check`, the fast suite (`-m "not slow"`), and the tests of every touched module, run on the task's own resources (`.env.test` + `SANDLOOM_TEST_*`) |
| **fn checks** | hidden `checks/check_<task>.py::test_fn_*`: the feature exists (routes, contract, consumer, …) |
| **dev checks failed** (headline) | hidden `test_dev_*`: a rule the framework states but no gate enforces (a write route that opts out of the model E defaults, `TenantScoped` per-org rows, a durable dedupe, a dual-publish, a live billing gate on the organization's account, a required contract, a gateway route, …). Every failure counts, whatever the gates said |
| **uncaught deviations** (secondary) | the same failures, counted only when every gate is green ("a deviation no check caught"). It moves with gate noise: a red gate hides a task's deviations, so compare `dev_checks_failed` across runs |
| **outcome** | `pass` = all gates green and all fn checks pass; `error` = the task never ran to a score (harness error, or 0 tokens) |
| **tokens** | the sum of input, output, cache-read and cache-write tokens over every model call (`modelUsage`). Cache reads dominate, so this is ≈ turns × context size. `cost_usd` and `tokens_output` are recorded too |
| **turns / tools / wall** | `num_turns`, tool_use blocks, `duration_ms` |
| **kernel files read** | distinct `sandloom/core/**`, `sandloom/sdk/_*/**` and `sandloom/shared/{authz,guards}/**` `.py` paths (spec §2.5; tests excluded) opened with Read or with `cat`/`sed`/`head`/… in Bash. Other `sandloom/shared` and `docs/` reads are recorded separately; the public `sandloom/sdk/*.py` facades are not kernel reads |
| **edited outside** | changed or untracked paths outside the task's `allowed` prefixes. This is the framework-file tax: route tables, chain lists, pins, deploy files. Files `sandloom generate` writes (`shared/events/*.json`, contract stubs and DTO copies in `shared/contracts/`, `module`/`routes`/`contracts.snapshot.json`) are recorded as `generated_files` and do not count |
| **permission denials** | denials in the session (a too-narrow `ALLOWED` shows here) |

The checkers are static (AST and text), so they run in a second and never
import the agent's code. They reproduce the experiment's uncaught deviations
on its preserved worktrees:

- agent A's unguarded POST (since Public API 1.0 the framework deduplicates
  keyed writes itself, so the check now catches a route that opts out);
- agent B's missing gateway route and missing end-to-end consumer test;
- agent C's per-org tables without `TenantScoped`.

## Tasks

| id | what the product owner asks for | from |
|---|---|---|
| `t1_notes` | a tenant CRUD module, an event on create, a paid gate | experiment A |
| `t2_welcome` | an exactly-once welcome email on registration, plus a status endpoint | experiment B |
| `t3_quotas` | a quotas module with a command contract, consumed by tenancy's invitations | experiment C |
| `t4_resend_invitation` | an admin-only write endpoint in an existing module (app: resend an invitation of the seed's `invites`) | new |
| `t5_billing_contacts` | a tenant-owned table in an existing module (app: billing contacts in the seed's `workspace`) | new |
| `t6_team_overview` | a cross-module read model (projection) | new |
| `t7_credits_event_v2` | roll an event from v1 to v2 without breaking consumers (app: the seed's `invites.invitation.sent`, consumed by `activity`; framework: `billing.credits.spent`) | new |
| `t8_purge_invitations` | a nightly Celery task, safe to run twice (app: the seed's `invites`) | new |
| `t9_member_export` | an admin-only export behind a live subscription gate (app: the seed's invitations; framework: tenancy's members) | new |
| `t10_org_banner` | an admin-only write on a tenant row, in a small new module (the cheapest task; both modes) | new |

A saga task is to be added once the sagas work lands.

## Running it

Run from the repository root. The runner uses the backend venv, which has `redis` and
`python-dotenv`.

```bash
uv run python benchmarks/dx/run_eval.py run --label <label> --ref <git-ref> [--tasks t1,t10] \
    [--mode app|framework] [--model claude-sonnet-5-5] [--parallel 3] [--keep] \
    [--compare <other-label> [--force-compare]] [--dry-run]
uv run python benchmarks/dx/run_eval.py report  --label <label> [--compare <other>]
uv run python benchmarks/dx/run_eval.py rescore --label <label> [--tasks …] [--checks-only]  # needs --keep
uv run python benchmarks/dx/run_eval.py promote --label <label>   # writes baseline.json
uv run python benchmarks/dx/run_eval.py prune-slots [--dry-run]   # free finished runs' Redis indexes
```

**Where the output goes:**

- Raw artefacts go to `$SANDLOOM_DX_EVAL_HOME` (default `~/.cache/sandloom-dx-eval/<label>/<task>/`):
  the transcript, `changes.patch` (untracked files included, so a new module
  is in it), gate logs, `checks.xml` and, with `--keep`, the worktree.
- Committed results go to `results/<label>/`: one JSON per task, the agent's
  `*.report.md`, and `summary.md` / `summary.json`.
- `baseline.json` is the promoted reference run.

**On a shared machine** point the runner at your own resources:
`SANDLOOM_DX_REDIS=redis://localhost:6399` (the test Redis),
`SANDLOOM_DX_REDIS_INDEXES=200-239` (where the per-task pairs come from) and
`SANDLOOM_DX_DB_PREFIX=sandloom_test_dxapp_` (the per-task database names). In app mode
`--keep` keeps the app directory (`rescore` needs it); without it only the
artefacts and, for the product suite, the app checkpoint stay.

**Each task gets its own resources:** an `SANDLOOM_TEST_OWNER`, a test database
`sandloom_test_dxeval_<label>_<task>`, and two Redis indexes that are empty and
unused by any other eval run. A rerun keeps its indexes. Drop the databases
afterwards with `tools/checks/test_resources.py prune`.

**Redis indexes of finished runs.** Each agent's `sandloom test` claims about 12
more indexes for its xdist workers, marked with the task's owner
(`dx-eval-<label>-<task>`). They outlive the run, and when too few are free,
`sandloom test`'s parallel steps exit 4 and the agent works without the fast suite.
So `run` first frees the indexes of finished eval runs (`prune-slots` does it
on demand):

- only indexes marked with a `dx-eval-*` owner whose run directory is
  finished: a `FINISHED` marker, a `RUNNING` marker whose pid is gone, or (a
  run from before the markers) a stored result newer than everything in the
  run directory, or nothing changed in it for 6 hours;
- never the per-task pairs, an index anyone is connected to, any other owner
  or a still-running run (`RUNNING` with a live pid, also during `rescore`).

It then warns when fewer indexes are free than `--parallel` agents need. When
a task finds no free pair, its error names who holds the indexes and how to
free them.

**Cost.** With Sonnet 5.5, one task costs roughly $0.5–3 and 3–15 minutes,
plus about 6 minutes of gates. A 10-task run costs about $10–20 and about an
hour at `--parallel 3`. `--budget-usd` (default 25) and `--timeout-min`
(default 60) cap each task. Run a subset (`--tasks t1,t3,t4`) for a quick
check, and the full set before promoting a baseline.

## Reading the numbers

- **Headline: `dev_checks_failed`**, then `passed`. `uncaught_deviations` is
  secondary (it depends on the gates being green).
- **Means are over the tasks that ran.** An errored task (harness error or
  0 tokens) is listed under "Errored" in the summary and left out of every
  mean and per-task total; its cost still counts.

- **One sample per task is noisy.** Model sampling moves tokens and turns by
  ±30% between identical runs. Compare sums over the task set, and treat a
  single task's delta as a hint, not a finding. Use repeated runs
  (`--label x-2`) for a decision that hinges on a small delta.
- **The hidden checks encode rules.** When a change to the contract names a
  rule that a hidden check tests, a lower deviation count partly measures the
  prose. That is the point of the "rules no check enforces yet" list. A rule
  that matters should still become a check (DX plan R3/R4).
- **Model choice.** The default model is `claude-sonnet-5-5`, chosen for cost.
  A weaker model is more sensitive to the quality of the context. Compare
  runs only within one model.
