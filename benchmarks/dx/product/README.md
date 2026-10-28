# DX eval, north-star suite: build and run a whole product

The small tasks in `../tasks/` are diagnostics: one framework mechanism at a
time. This suite measures what Sandloom is for. An agent builds and grows a
multi-module SaaS product quickly, and the framework handles the footguns:
modules stay isolated, and the app scales from monolith to microservices by
configuration.

## The product: Academy

`spec-base.md` plus one file per module in `modules/`. The spec fixes the
public API (paths, bodies, status codes), so the hidden tests can drive the
product; the implementation is up to the agent.

| # | module | exercises |
|---|---|---|
| 1 | `catalog` | tenant-scoped CRUD, validation |
| 2 | `enrollment` | paid with billing credits (a command contract), 402/409 rules, an event |
| 3 | `progress` | an exactly-once consumer, idempotent completion, a follow-up event |
| 4 | `certificates` | an exactly-once effect plus an email through the notifier |
| 5 | `reporting` | a cross-module projection, rebuildable |
| 6 | `reminders` | a nightly Celery task, at most once per day |
| x | `announcements` | the module added at N=2 and at N=7 (it also reacts to `catalog`) |

`catalog` always announces `catalog.course.created.v1`, also in the
1-module product (p2): the module added at N=2 (p3) and at N=7 (p4) reacts to
it without touching `catalog`, so both measure the same work. (Before this
was in the spec, p2's catalog published no such event and p3 could not pass
the course hook without breaking "do not change the existing modules"; p3
results from before it are not comparable on that test.)

The spec is parameterised: a task lists the module sections it includes, so
a product of any prefix (`["1-catalog", "2-enrollment"]`) or a different
product file set can be composed without new code.

## Tasks (`tasks/`): the five north-star measurements

| task | starts from | measures |
|---|---|---|
| `p1_build_full` | the ref under test | **product build:** the 6-module product end to end (acceptance pass rate, tokens, wall time, interventions) |
| `p2_build_one` | the ref under test | the 1-module product, the starting point for N=2 |
| `p3_add_module_n2` | `p2_build_one` | **marginal module cost** at N=2: tokens, turns, files outside `announcements/` |
| `p4_add_module_n7` | `p1_build_full` | **marginal module cost** at N=7 (same module, same prompt). Flat is the target; growth means isolation is leaking |
| `p5_scale_switch` | `p1_build_full` | **scale switch:** monolith to microservices. Code changes (target 0), config changes, and whether the product still passes. App mode: the app's own `deploy/compose/microservices.yml`, scored by `checks/check_p5_scale_switch.py` (split, gateway, bindings, a database and migrate per service, one beat, a boot of each service's modules); framework mode is a kernel-contributor task, run only when named (`../README.md`, 'Modes') |
| `p6_change_waitlist` | `p1_build_full` | **change blast radius:** a feature change in `enrollment`. Modules touched other than `enrollment` (target 0) |

**Footgun escapes** are scored on every task: the hidden probes run against
the product each task leaves behind.

## Hidden tests (`hidden/`, copied into the checkout only while scoring)

- **`test_acc_*` (acceptance).** The spec's behaviour through the public API.
  A test is skipped when its modules are not part of the product.
- **`test_fg_*` (footgun probes).** One class of production incident each;
  the target is 0 failures.
  - cross-tenant reads, enrolment into another organization's course and
    progress leaks;
  - per-organization tables that are not `TenantScoped`;
  - a double charge on a retried POST, and a charge for a rejected
    enrollment;
  - mutating routes without an idempotency guard;
  - consumers without a durable dedupe;
  - a duplicated certificate;
  - routes reachable without authentication;
  - a revoked session still working.

The probes use the framework harness (`real_uow_client`: real commits and
real post-commit dispatch). They seed organizations through tenancy's API
and credits through billing's service, and poll for eventually consistent
effects (15 s).

## Running it

```bash
uv run python ../benchmarks/dx/run_eval.py run --suite product --label <label> --ref <ref> \
    --parallel 4 --keep --budget-usd 40 --timeout-min 150
uv run python ../benchmarks/dx/run_eval.py run --suite product --label <label> --tasks p2,p3   # a subset
uv run python ../benchmarks/dx/run_eval.py rescore --suite product --label <label> [--tasks p1]
uv run python ../benchmarks/dx/run_eval.py report --label <label> [--compare <other>]
```

- **Order.** Tasks run in dependency waves: the builds first, then the
  follow-ups. After scoring, each task's product is committed as
  `refs/dx-eval/<label>/<task>`, the starting point of its follow-ups. Delete
  the refs with `git for-each-ref refs/dx-eval --format='%(refname)' | xargs
  -n1 git update-ref -d` when done.
- **Result fields.** `acceptance` and `footguns` (test → passed),
  `code_changes` and `config_changes` (paths), and `modules_touched`
  (product module directories changed, excluding a change task's `target`).
- **Interventions.** The agent runs headless, so human interventions are 0 by
  construction. A run where the agent stopped to ask shows up as a failed or
  partial build and in its `*.report.md`.
- **Cost.** A full run (six agents, two of them building whole products)
  costs roughly $40–80 and 2–3 hours at `--parallel 4` on a shared machine.
  Run it per release and whenever the module-author contract, the `sandloom add`
  generators or the public API changes. Use `--tasks p2,p3` (the cheapest pair) for a
  quick signal.

A footgun probe whose own setup fails (`SetupFailed` in `hidden/_academy.py`:
the product feature the probe needs, such as enrolling into a course, is
broken) is reported as **not evaluated** (`probes-not-evaluated.txt` in the
task's artefacts), never as an escape; the broken feature already fails its
acceptance test.
