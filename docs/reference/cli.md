---
title: CLI reference
type: reference
---

# CLI reference

Every `sandloom` command, the configuration tiers, accepted risks, the key settings
and the local CI runner. Run the commands from the application root (or
the framework repository root) with `uv run sandloom ...`. Every command
has `--help`.

`sandloom --help` lists the commands in three groups, by what you are doing: `start`,
`build` and `operate`.

A command that runs once logs warnings and errors only, so its own output stays readable.
That is a default of the command, not a setting. It writes no `LOG_LEVEL`, and
`sandloom config dump` still reports the configured one. The level is the first that applies:

1. `LOG_LEVEL` exported in the shell.
2. `--debug`: DEBUG, with tracebacks.
3. `LOG_LEVEL` in `.env`.
4. `sandloom -v <command>`: INFO. Without `-v`: WARNING.

`dev`, `worker` and `beat` keep the configured level, because their log is their output.
Put `-v` before the command: after it, the flag belongs to the command.

`sandloom migrate upgrade-all`, `upgrade-core`, `upgrade-module` and `ensure-outbox` print
their result themselves, whatever the log level is. That is one line per database and
module, and without `--apply` what they would do.

## Context and declared architecture

Available in `sandloom>=0.7.0a16,<0.8`. <!-- sandloom-release -->

| Command | What it does |
| --- | --- |
| `sandloom context MODULE` | Compact Markdown module map; JSON with `--format json`. |
| `sandloom graph [MODULE]` | Compact project map or a detailed module neighborhood. |
| `sandloom graph impact ITEM` | Direct witnesses and potential transitive module impact. |
| `sandloom graph diff REV_OR_FILE` | Compare the committed or saved inspection index. |
| `sandloom graph sequence [ID]` | List readable process names or inspect saga/route phases; names and stable IDs are accepted. |
| `sandloom graph state ID` | Conditional saga state transitions. |
| `sandloom graph [MODULE] --runtime-risks` | Add the synchronous contract coupling table (dependencies, depth, fan-in) to the text map. |
| `sandloom graph --format html > architecture.html` | Save the offline viewer. |
| `sandloom explore [MODULE]` | Experimental: generate, save and open the offline viewer. |

Both inspection commands accept `--root PATH`, `--format` and
`--require-verified`. Context formats are `markdown|json`. Graph formats are
`text|json|mermaid|html`; impact/diff exclude Mermaid. Graph defaults to text and
context to Markdown in terminals and pipes. JSON requires `--format json`.
The project text map includes module purposes and compact connection/count facts;
JSON includes stable module IDs and summaries. Results go to stdout; save them
with shell redirection.

Graph JSON accepts `--detail compact|full` and `--max-bytes N` (default 131072).
Full topology adds provenance/policy; full process queries add verification
basis evidence. `graph MODULE --hops N --direction incoming|outgoing|both` scopes
a neighborhood (default one hop, both directions). These flags apply only to
`graph [MODULE]`. Use `module:NAME` to disambiguate modules named `impact`, `diff`,
`sequence` or `state`.

`graph [MODULE] --runtime-risks` appends a table to the text map. Each row is a
module, what it calls synchronously, and two counts: depth and fan-in. The numbers
are an upper bound from declared requires. JSON always carries them as
`data.sync_coupling`, and the HTML viewer has a Coupling tab. So the flag is a
usage error (exit 2) with any format but `text`, and with any action
other than `graph [MODULE]`. How to read the table:
[synchronous coupling](../ai/architecture-graph.md#synchronous-coupling).

A diff baseline is a project-relative saved file when it exists, ends in `.json`
or starts with `./`; otherwise it is a local Git revision. Existing files win
over revision names. Revision comparison reads this project's committed index
without fetch, checkout or execution of old code. File comparison needs no Git.

Exit codes: 0 usable (check uncertainty), 1 invalid/missing/unsafe input, budget or
I/O failure, 2 usage (including an unknown graph module selector), 3 strict
verification unavailable. JSON emits one versioned
success/error envelope. Text errors leave stdout empty and explain on stderr.
Inspection performs no app imports, prompts, network access, `.env` loading or
implicit generation. See [module maps](../ai/module-context.md) and
[graph semantics](../ai/architecture-graph.md).

`explore` runs `generate`, writes `.sandloom/cache/explore.html` atomically, prints
its URL and opens the browser. `--no-generate` reads the saved index;
`--no-open` only saves. Module selectors, `--root`, `--hops` and `--direction`
work there too. Failed generation leaves the previous HTML intact and unopened.

## Project and code

| Command | What it does |
| --- | --- |
| `sandloom new <app> [--with identity,tenancy,billing \| --no-batteries] [--module <name>] [--dir <path>] [--agents <a>,<b>]` | Generate a standalone, runnable application. `--agents` picks the coding agents it writes skills for ([Agent tooling](../ai/agent-tooling.md)); with `--yes` (or without a terminal) and no `--agents` only the shared `.agents/skills` and `AGENTS.md` are written. The app depends on `sandloom[postgres,redis,celery]>=0.7.0a16,<0.8` plus `sandloom-<module>>=0.7.0a16,<0.8` per `--with` module. `--framework-req`, `--framework-source {pypi,git,path}` and `--framework-path` choose where the framework comes from (`path`: editable sources for exactly those distributions). They are for working on the framework itself, so `sandloom new --help` does not list them: it ends with a line that points to [Framework dependency sources](#framework-dependency-sources) and the [contributing setup](../contributing/setup.md). |
| `sandloom new` (at a terminal) | The wizard. It asks what the flags leave open: the name, the standard modules, the coding agents, how to run PostgreSQL and Redis (Docker, servers you already run, or later) and whether to set the app up now. The setup runs `uv sync`, `generate`, `dev up` and the migrations; its output goes to `.sandloom/run/new-setup.log`. With the console it then asks the administrator's email and runs `admin create` on the terminal, which asks the password itself: the wizard never handles it. The summary says how to start the app and what to open, with help apart, and ends with a pinned `uvx` command that makes the same app without questions. Without a terminal, in CI or under a coding agent, nothing is asked. |
| `sandloom new … [--stack standard]` | The stack the app runs on: the backing servers and the packages installed for them. Today there is one, `standard` (PostgreSQL and Redis; it installs `sandloom-redis` and `sandloom-celery`): the wizard asks only when there is a choice, and the flag answers it. The kernel's list holds a name, a summary and the extras; the stack's package writes the compose files, `.env` and the CI service. With `sandloom-redis` importable (`sandloom[standard]`, `sandloom[redis]`) `new` writes them at once. From a bare `sandloom` it writes the rest of the app with markers where they go, then runs `uv sync`, `uv run sandloom add stack` and `uv run sandloom generate` in the new app; without `uv`, or when a step fails, it prints the commands to run later. The files are byte for byte the same on both paths. `--services external` needs the package at once. |
| `sandloom new … [--no-git]` | A git repository with a first commit is the default; `--no-git` skips it. Inside an existing repository none is created and nothing is committed. The first commit runs no hook and is not signed (`--no-verify`, `-c commit.gpgsign=false`); without a git identity the repository stays uncommitted and the output says how to commit. |
| `sandloom new … --services docker\|external\|none [--database-url URL] [--kv-url URL]` | How `--setup` runs PostgreSQL and Redis. `external` uses servers you already run (default URLs: `postgresql://$USER@127.0.0.1:5432/postgres`, `redis://127.0.0.1:6379`), checks them, creates the app's database and writes `.env` like `dev up --services external`. `none` creates the files only. |
| `sandloom new … --yes` (alias `--no-input`) | Ask nothing: the flags and the defaults decide, and the app name is required. The output ends with the "Next steps" block. Scripts and coding agents pass it. |
| `sandloom new … --setup [--admin-email <e>]` | Also run the setup. With `--with admin` and `--admin-email`, the last step creates the administrator; without a terminal it prints a set-password link (`admin create --invite`). CI refuses `--setup`: CI provides its own services. |
| `sandloom new … --plain` | No colour, no redrawn lines and typed answers, for screen readers and logs. `NO_COLOR` and `FORCE_COLOR` work too. |
| `sandloom add module <name> [--entity NAME \| --empty] [--settings] [--no-tenant]` | Creates a module under the app's `modules` package with one example entity: a model, a route, an event, a baseline migration and tests. `--entity NAME` (snake_case) names the example in place of `item` (`--entity order`: `<Module>Order`, `<module>_orders`, `/orders`, `<module>.order.created.v1`). `--empty` writes no entity, only the package and its migration chain (an empty `migrations/versions/`); write your first model, then `sandloom migrate new <name> "<message>"`. `--entity` and `--empty` exclude each other, and `--empty` has no table for `--no-tenant` to change. An entity named `tenant` or `owner` is refused, because the example already has the columns `tenant_id` and `owner_id` (with `--no-tenant`, `tenant` is allowed). `--settings` also writes `settings.py` (`<MODULE>_DEFAULT_PAGE_SIZE`, read with `settings_of`); without it a module has none. The example table is `TenantScoped` with row-level security; `--no-tenant`, for an app without tenants, writes a plain table ([the scaffold is tenant-scoped](#the-scaffold-is-tenant-scoped)). |
| `sandloom add event <module> <name> [--field NAME:TYPE ...] [--no-tenant]` | A typed `Event` in the module's `events.py`; its channel is `<module>.<entity>.<verb>.v1`, taken from the class name. `--field` repeats, once per payload field, and `TYPE` is one of `str`, `int`, `float`, `bool`, `UUID`, `datetime`, `date`, `Decimal`; any other type is refused. Without `--field` the event carries `<entity>_id: UUID` and `tenant_id: UUID`: a `--field` list replaces `<entity>_id`, and `tenant_id` is added to it unless you name it. With `--no-tenant` no `tenant_id` is added. |
| `sandloom add consumer <module> <channel> [--field NAME:TYPE ...]` | A view of another module's event, an `@on` consumer of its channel and its test. `--field` is as for `add event`; without it the view takes the producer's own fields, read from its class or its exported schema. With a producer found, the view always keeps the producer's required fields (`tenant_id` included, so the test event passes the channel's schema) and `--field` narrows the optional ones or adds its own. With no producer found, the view is the `--field`s plus `tenant_id: UUID`, as `add event` writes it; `--no-tenant` (an app without tenants) leaves it out. The consumer carries an explicit `name=` (the function name, written out): the consumer group is that name, so renaming the function without it replays the stream ([Events](../modules/between/events.md), "Do not rename a consumer"). The test, `tests/test_<handler>.py`, delivers one event twice with `deliver(event, times=2, session=db)`; add the assertion on the handler's effect. |
| `sandloom add contract <module> <name>` | A `@contract` class and its DTO in the module's `schemas.py`; the stub and the DTO copy are generated. |
| `sandloom add contract-use <module> <contract>` | Declares `consumes=[ContractRequirement("<contract>")]` in the module's `define_module` and writes the stub (and the DTO copies it names) from the importable provider at once. |
| `sandloom add saga <module> <name>` | A saga with a typed command step, plus tests. |
| `sandloom add projection <module> <name>` | A projection skeleton. |
| `sandloom add task <module> <name> [--periodic CRON]` | An idempotent task skeleton; `--periodic` adds its five-field cron beat schedule. |
| `sandloom add skills [--agents <a>,<b>] [--choose] [--force]` | Write the application's agent files for its coding agents: the skills in each agent's folder and the module-author contract. It updates a file it wrote earlier and keeps a file you edited unless `--force`. `--agents` or `--choose` changes the recorded choice in `.sandloom/agent-files.json`. |
| `sandloom add stack` | Write the stack's files that `sandloom new` left marked (it ran without the stack's package): `.env`, `.env.example`, `.env.test`, the CI workflow, the README and the compose files, from the package installed in the app's environment, for the stack the app records in `[tool.sandloom] stack` (the default one where none is). `sandloom new` runs it for you; it is the command `new` prints when a step failed. Until it has run, `check`, `generate`, `test` and `dev up` stop with one line. Nothing changes when the files are written already. |
| `sandloom add deploy <module>... [--service S] [--force]` / `--refresh` / `--check [--env-file F]` | Split into services: create (first run) or update the microservices profile and its gateway, derived from what the modules provide and consume; `--check` runs the split invariants (SANDLOOM-T014) and boots every service alone. [Split an app into services](../operate/split-into-services.md). |
| `sandloom dev up \| status [--json] \| run [--port N\|auto] \| reset \| down [--all] [--volumes] [--force] \| prune \| logs [service] \| token [--tenant UUID] [--role NAME] [--user UUID] [--ttl-minutes N] [--quiet]` | Local Postgres and Redis for the app: one set per repository on ports it keeps, a database and Redis indexes per app checkout or git worktree, written to a marked block of `.env` and to `.env.test.local` (`127.0.0.1` URLs). Safe for several agents at once: commands wait for each other, `reset` and `down` refuse while others use the services. `run` starts the app server and, once it answers, prints what runs: the app, the console, the API docs and the health check. `token` prints a development bearer token the app accepts on standard output, never in production ([first access token](../modules/batteries/first-access-token.md)). |
| `sandloom dev up --services external [--database-url URL] [--kv-url URL]` | Use PostgreSQL and Redis you already run (Homebrew, Postgres.app) instead of Docker. The role must be allowed to create databases. It uses only a database it created (marked with a `sandloom dev: <checkout>` comment): an existing database with the app's name is refused, and `dev reset` / `dev prune` drop only marked ones. Each checkout gets two Redis indexes that were empty on your server, and `dev reset` / `dev prune` flush them. Naming another server with `--kv-url` checks every checkout's pair again there: a pair that holds keys is replaced, and `up` is refused, saving nothing, when the server has no two empty indexes left. TLS is not supported yet: `rediss://`, a Redis ACL user and URL options (`?sslmode=…`) are refused. The choice is kept: `up`, `run` and `status` never call Docker after it, `down` and `logs` have nothing to do, and `run` names the server that does not answer and how to start it. `--services docker` switches back. |
| `sandloom docs [<name>] [--write \| --check]` | Print a public `sandloom.sdk` name's exact signature, tier and docstring; `--write` / `--check` refresh or verify the generated reference blocks. |
| `sandloom explain <code> [--list \| --markdown]` | Explain an `SANDLOOM-xxxx` error code or a check rule: why, and the fix. |
| `sandloom version` | Print the framework version. |

Code generators run `sandloom generate` last, so snapshots and stubs are current
after scaffolding. `add deploy` validates deployment configuration; `add skills`
refreshes the agent files and does not run application generation.
`sandloom add --root <path>` runs a generator against another project root. Each
generator has a how-to: [Add a module](../modules/create.md),
[Add an event](../modules/between/add-event.md),
[Add a consumer](../modules/between/add-consumer.md),
[Add a contract](../modules/between/add-contract.md),
[Add a saga](../modules/between/add-saga.md),
[Add a projection](../modules/between/add-projection.md).
`sandloom add --help` lists the generators and `sandloom add <generator> --help`
says what each argument is.

### The scaffold is tenant-scoped

`add module`, `add event` and `add consumer` always scaffold tenant-scoped, and read
no setting. A gitignored `.env` is per machine. A scaffold that followed it would
write a table without row-level security into an app that is multi-tenant in
production. `--no-tenant` is the explicit choice for an app without tenants.

| | `add module` | `add event` | `add consumer` |
|---|---|---|---|
| default | The example table is `TenantScoped`: a `tenant_id` column, its index and row-level security in the baseline migration. The event, the service and the tests carry the tenant. | Adds `tenant_id: UUID` to the fields. | A view built from `--field` alone, with no producer schema found, gets `tenant_id: UUID`. A view of a found producer keeps that producer's required fields, `tenant_id` among them. |
| `--no-tenant` | A plain table (`Base`): no `tenant_id`, no row-level security, and tests that use `as_user(tenant=False)`. | Adds no `tenant_id`: the default is `<entity>_id` alone, and a `--field tenant_id:UUID` you name is written as given. | The view built from `--field` alone gets no `tenant_id`. A found producer's view is unchanged. |

A tenant-less scaffold is for an app without tenants. In that app
(`TENANCY_MODE=single`) one canonical tenant, `SINGLE_TENANT_ID`, is bound and
still filters a `TenantScoped` model you add yourself. A wrong default costs an extra `tenant_id`. A wrong
`--no-tenant` in a multi-tenant app costs the isolation, so pass it only where the
app has no tenants. `add projection` has no flag, because a projection row always
carries `tenant_id`.

The tenant-scoped tests (`add module` and the sample module `sandloom new` writes)
pass under both modes without reading the setting when they are written. Each test
asks the app's own settings which tenant to bind: a fresh one under
`TENANCY_MODE=multi`, `SINGLE_TENANT_ID` under `single`. In `single` the
isolation test asserts the `403` `tenant_mismatch` that a token naming another
tenant gets, where in `multi` it asserts an empty list.

## Tests and gates

| Command | What it does |
| --- | --- |
| `sandloom release [<module> ...] [--root ROOT]` | Marks every contract and exported event the project declares as released ([ADR-0017](../adr/0017-released-marks.md)): from then on `sandloom generate` refuses an incompatible change to it (SANDLOOM-T004, T005) and a change moves its revision, which `sandloom docs <name>` prints. A draft is explicit: what `sandloom new` (the sample) and `sandloom add module|contract|event` create, a new entry in an existing snapshot file, or a name given to `sandloom generate --draft`; `generate` rewrites a draft freely. A snapshot file that does not exist is written with every entry released. It writes `released` into the snapshots and `x-sandloom-released` into the exported event files, and reads no git, tag or remote. Snapshots written before the marks already count as released. |
| `sandloom generate [<module> ...] [--check] [--new-major NAME] [--draft NAME ...] [--root ROOT]` | Writes every generated file: each module's `module.snapshot.json`, `routes.snapshot.json` and `contracts.snapshot.json`, the contract stubs and the DTO copies (`<shared>/contracts/<module>_dto.py`, a wire-shape copy of each provider DTO a contract signature names) and the exported event schemas. Every file is compared with the committed one first: an incompatible change to a released contract or event (SANDLOOM-T004, T005), or to a route, stub or DTO copy (SANDLOOM-T001, T007, C012), fails and nothing is written. `--new-major NAME` copies the released contract or event `NAME` to the next major (`.v1` to `.v2`) beside the old one, then generates; it refuses a draft, an unknown name or `--check` (restore the released declaration first, run it, then edit the copy). `--draft NAME` (repeatable) writes the contract or event `NAME`, or every one of module `NAME`, as a draft; it refuses a released name, a name that is none of them, and `--check`. A weaker route policy is written only by `sandloom routes snapshot --write`. An exported event schema (`<shared>/events/<channel>.json`) that the composed module owning the channel no longer declares is an orphan: `generate` removes it and prints `removed <path>`, and `--check` lists it as stale. The schema of a module the process did not compose (a `SANDLOOM_MODULES` subset, an optional module that did not load, a deleted module) is never an orphan. Module names keep those modules' files only, and a run with names removes no orphan. After a write it also runs the event-view check and prints each consumer view that no longer matches its producer's event as `warning: ... (SANDLOOM-C025)`; the warning never changes the exit status, `sandloom check` is what fails on it. Loads the project's `.env.test` like `sandloom test`; generation does not apply migrations. Code generators run it last. `sandloom new` attempts it when dependencies are available; after `uv sync`, run it in the app environment. `--check` compares without writing; `--root` selects the application root. |
| `sandloom test [<module>] [--full] [--no-gates] [-n N] [--plan] [--ci] [--seed N \| --no-shuffle]` | The gate: the generated files and the schema checks, then the module's tests, the fast suite in parallel (`-m "not slow" -n auto --dist loadfile`, `SANDLOOM_TEST_REDIS_DB_STRIDE=auto`; `--full` includes slow tests), then `ruff check --no-fix`, ruff format, mypy, `sandloom check`, the architecture tests, `lint-imports` and the contract and route snapshots. A failing step names its code. It never writes a generated file: a stale one fails with "generated files are stale: run `sandloom generate`" and the list. `ruff check` never fixes, whatever `fix` your configuration sets (the gate writes nothing): a violation fails and `ruff check --fix` is yours to run. `--ci` differs only in not recording route effects. The test steps run in a seeded random order (printed; `--seed N` replays it, `--no-shuffle` keeps file order): [Test isolation](../modules/testing.md#test-isolation). When the hashes in `.sandloom/agent-files.json` differ from the installed sandloom's skill and contract templates, it prints one info line before the steps, ``agent files are from an older sandloom: run `sandloom add skills` (`--force` replaces a file you edited)``; that is no failure, it reads no network, an app without the record gets no line, and a record that does not parse gets one line naming it. |
| `sandloom check [--rules FILE] [--only rule,...] [--list-rules] [--boot]` | Static architecture analyzers against `./architecture_rules.yml` (exit `0` ok, `1` violations, `2` usage). `--boot` also builds the app through `create_app` and reports startup refusals and stale route snapshots. Outside this repository it needs `pip install "sandloom[check]"`. |
| `sandloom routes snapshot [--write \| --check] [--module M]` | Each module's route snapshot (`routes.snapshot.json`: method, path, authentication, resolved policy). `--check` fails on a stale file (SANDLOOM-T001); after a route change run `--write` and review the diff. `sandloom generate` never writes a weaker policy: `--write` is its explicit approval and names each weakening it approves. |
| `sandloom routes reference` | Print the generated route reference. |
| `sandloom contracts snapshot [--write \| --check] [--module M]` | Each module's contract snapshot (SANDLOOM-T004). |
| `sandloom contracts check --against <snapshot\|dir\|url> [--as provider\|consumer] [--service S] [--timeout T]` | Rolling-upgrade compatibility of this build's contracts against a snapshot or a running provider. |

Never re-run a serial `pytest -q` after each fix; `sandloom test` runs the suite in
parallel. The harness and its isolation guards are described in
[Testing Modules](../modules/testing.md); every fixture in the
[testing reference](testing.md).

```bash
docker compose -f deploy/compose/test.yml up -d --wait   # test PostgreSQL :5433, Redis :6380
uv run python tools/checks/test_resources.py list|prune [--days 7] [--apply]
                            # shared test DBs / Redis indexes: owner, last use,
                            # connections; prune is a dry run without --apply and
                            # never touches a connected or recently used one
SANDLOOM_TEST_DATABASE_URL=postgresql+asyncpg://…/sandloom_test_mine uv run sandloom test
                            # run on YOUR OWN database: SANDLOOM_TEST_DATABASE_URL /
                            # SANDLOOM_TEST_CACHE_URL / SANDLOOM_TEST_BROKER_URL win over
                            # .env.test on every load path (a plain shell
                            # DATABASE_URL does NOT — .env.test overrides it)
uv run pytest src/sandloom/<module>/tests/test_x.py::test_y --no-cov   # one test
```

Parallel workers get database `<db>_gw<N>` and their own Redis indexes, which
needs a Redis started with `--databases 256`. In tests, reload `.env.test`
with `reload_test_env` (from `sandloom.sdk.testing`), never a bare
`load_dotenv(override=True)`. CI runs the slow suites on Python 3.11 and 3.12
only (3.13 / 3.14: `-m "not slow"`); the pre-push hook runs everything.

## Migrations

Each module owns its own chain; the core chain holds the shared
infrastructure tables. See [Migrations](../modules/database/migrations.md) and
[Write a migration](../modules/database/write-migration.md).

| Command | What it does |
| --- | --- |
| `sandloom migrate new <module> "<message>" [--database D] [--rev-id R]` | Autogenerate a revision in ONE module's chain and add row-level security for each new `TenantScoped` table. When the models match the chain it refuses with "no changes detected" (exit 1) and writes no file. The revision is numbered after the chain's last one (`0002_<module>`, the file `0002_<module>_<slug>.py`) unless `--rev-id` gives an id. Every `sandloom migrate` command refuses a chain in which two files declare one revision id (two branches that each added `0002_<module>`) and names the file to renumber. A column added `NOT NULL` with no `server_default` (a Python `default=` is not one) prints a warning: it fails on a table that already has rows. |
| `sandloom migrate revision <module> -m "<message>" [--autogenerate] [--database D] [--rev-id R]` | A revision in one module's own chain (its `migrations/`, version table `alembic_version_<chain>`, its database), numbered like `migrate new`. With `--autogenerate` it refuses "no changes detected" when there is nothing to write; without it, it writes the empty template to fill in by hand. |
| `sandloom migrate upgrade-all [--apply] [--lock-timeout S] [--create-databases]` | Core chain → every hosting database, then each module's chain → its database (exit 1 on failure). Waits up to `S` (default `MIGRATION_LOCK_TIMEOUT_SECONDS`, 300) for another runner's advisory lock and logs the holder. `--create-databases` first creates a missing PostgreSQL database (role with `CREATEDB`; the microservices one-shots). |
| `sandloom migrate upgrade-core --database D --apply` | The core chain → a named database. |
| `sandloom migrate upgrade-module <module> --apply` | One module's chain → its database(s). |
| `sandloom migrate databases` | List the configured database instances. |
| `sandloom migrate plan` | Show the migration plan (dry run). |
| `sandloom migrate ensure-outbox --all-databases --apply` | DEV/REPAIR only: create the shared core tables (outbox, audit, consumer inbox, command log, HTTP idempotency log) per database. Refused in prod without `--force` (no drift detection) and the accepted risk `unversioned_ddl_repair`; production uses `upgrade-core`. |
| `sandloom migrate extract-module <module> --from <src> --to <dst> [--apply --i-have-stopped-writes] [--verify \| --drop-source \| --unfence [--force]]` | Move ONE module's existing data out of a shared database: fence, copy, verify, outbox/inbox hand-off, consumer-group owner transfer. Dry run by default; offline. Runbook: `deploy/README.md`. |

## Operations

| Command | What it does |
| --- | --- |
| `sandloom outbox list-dead \| requeue \| purge [--database D] [--channel C] [--id ID \| --all] [--limit N]` | Inspect dead-lettered outbox events, return them to pending, or delete delivered events older than the retention window. |
| `sandloom streams dlq-list \| dlq-replay <channel> [--id ID] [--group G]` | List or re-publish a channel's dead-letter entries. On a driver without the `dlq_ops` feature the command exits 1 with "not supported by this driver". |
| `sandloom streams groups <channel> \| consumers [--all] \| release-group <channel> <group>` | Consumer groups of a channel, the subscription inventory, or forget a group's owner fingerprint. `groups` needs the `inspection` feature and `consumers` needs `inventory`; without it the command exits 1. |
| `sandloom projection list \| rebuild <name> [--tenant <id>]` | List registered projections and consumers; rebuild one from its source. |
| `sandloom sagas list [--all] \| show <type> <id> \| retry <type> <id> \| park <type> <id> \| resolve <type> <id> --step S --as applied\|skipped` | Operate saga instances. Dry run unless `--apply --reason R` (prod: also `--confirm-production`). |
| `sandloom security revoke-sessions \| force-password-reset (--all \| --tenant ORG \| --user ID) [--operation-id ID]` | Incident response: end every session of the targets, or require a password reset. The kernel revokes the access tokens (any issuer); the claim map's issuer ends its refresh sessions; the output reports each kind of session. Asks the issuer's `<issuer>.security.v1` (`--tenant`: the `tenant_ids` owner's), confirmed before anything is done: a missing method refuses (`SANDLOOM-B039`, exit 1), e.g. `force-password-reset` with an issuer that has no passwords. Dry run unless `--apply --reason R` (prod: also `--confirm-production`); `--operation-id` resumes an interrupted run. |
| `sandloom security grant-admin --email ADDRESS [--apply --reason R] [--confirm-production]` | Make a registered user with a verified address a platform administrator. The issuer finds the account (`account_by_email`), the owner of `roles` grants the role (`appoint_platform_admin`); both are confirmed before anything is done, a dry run included, and may be different modules. An unverified address is refused: use `sandloom admin create --take-over`. Dry run by default. |
| `sandloom admin create --email ADDRESS [--name N] [--invite \| --password-stdin] [--if-missing] [--take-over] [--quiet] [--apply --confirm-production --reason R]` | Create a platform administrator for the [console](../admin-console/index.md). At a terminal it asks for the password twice; `--invite` prints a one-time set-password link (15 minutes) for agents and CI, built on the set-password page the composed application serves (the console's `set_password_page` route), else on `FRONTEND_URL`'s `/auth/reset-password`, else refused before anything is done; `--password-stdin` reads one line from a pipe. `--if-missing` succeeds when the address is already an administrator, without a terminal too. `--quiet` prints no success line. Acts at once outside production. Makes the account and its role in one unit, so the claim map's issuer and `roles` owner must be one module with `create_platform_admin` (and the password methods for a password): otherwise refused before anything is done (`SANDLOOM-B039`, exit 1); create the account at the issuer, then `grant-admin`. |
| `sandloom admin invite --email ADDRESS [--apply --confirm-production --reason R]` | A new set-password link for an administrator; the old link and the account's sessions end. Same owner rule as `admin create`. |
| `sandloom admin list [--confirm-production --reason R [--show-emails]]` | The platform administrators: the `roles` owner's holders (`platform_admin_ids`) with the issuer's address, whether a password is set (`at the provider` for an issuer that keeps none) and last sign-in (`accounts_by_id`). Read-only. In production it needs `--confirm-production --reason`, writes an audit record through the `roles` owner (identity: `identity.admin.listed`) and masks the addresses unless `--show-emails`. The command grants no access the server's CLI lacks; in production it adds the audit trail and keeps addresses out of shell history and CI logs. |
| `sandloom security audit-export --since T [--until T] [--action-prefix P] --output FILE` | Export an audit-log window as JSONL (sanitized metadata). |
| `sandloom worker [--queues Q[,Q...]] [--concurrency N] [--loglevel L]` | Run a task worker for the loaded modules (every deploy profile's `<service>-worker`). `--queues` defaults to the default queue, `--concurrency` to `WORKER_CONCURRENCY`, else the CPU quota; the process model is the `worker_pool` option of the celery instance in `INFRA` (on macOS set `solo` or `threads`: [the note](../modules/background/tasks.md#the-worker-and-beat)); `--loglevel` defaults to `INFO`. It boots the composition root like the API, runs the worker host of the taskqueue driver, and exits with the worker's exit code (non-zero when the worker dies). Replaces `celery -A sandloom.core.worker worker`. |
| `sandloom beat [--loglevel L] [--schedule PATH]` | Run the scheduler. It schedules only while it holds the singleton lease: a second one stands by, so run one per broker and module set. `--schedule` is the scheduler's state file (default `celerybeat-schedule` in the working directory; containers pass `/tmp/celerybeat-schedule`). Replaces `celery -A sandloom.core.worker beat`. |
| `sandloom worker check [--timeout SECONDS]` | The worker's container healthcheck: exit 0 when its heartbeat file is younger than `--timeout` (default 60, at most 86400), 1 when missing or stale. It asks the driver's host and boots nothing: no composition root, no broker round trip. It loads the full, valid settings of the worker (`DATABASE_URL`, `INFRA` and `JWT_SECRET`) to find the driver, and fails like any process on an invalid one. Replaces `python -m sandloom.core.worker healthcheck`. |
| `sandloom backup plan \| verify <dump> \| reconcile-security \| fence-leases` | What to back up per database and Redis endpoint; restore a dump into a scratch database and check it; re-derive blocks and revocation marks after a security-store restore; lift every fence of the `database` lease driver (`infra_lease`) after a restore of a PostgreSQL database, in one transaction with its audit row (dry-run by default; `fence-leases` and `verify` refuse a database that is not PostgreSQL). |
| `sandloom keys generate [--alg EdDSA\|RS256\|ES256] [--purpose jwt\|rpc\|rpc-dh] [--service S] [--kid K] [--out-dir DIR]` | Generate an access-token signing pair, or a service's internal-RPC keys. |
| `sandloom keys allowlist` | Derive `RPC_ALLOWED_CALLS` from `SERVICE_BINDINGS`. |
| `sandloom keys dev-env [--deploy-dir D] [--out F]` | DEVELOPMENT / CI ONLY: a compose `--env-file` with fresh keys for every variable the microservices profile requires. |
| `sandloom config dump [--json] [--only PREFIXES] [--non-default] [--show-secrets] [--kernel-only]` | The effective settings with their source (env, `.env`, `SECRETS_DIR`, default), the kernel's and each loaded module's; secrets redacted unless `--show-secrets` (refused in prod). A module's invalid setting is shown as a diagnosed entry (exit 0). It builds the composition without serving it (no lifespan, no connection); `--kernel-only` skips that and prints the kernel's settings only. |
| `sandloom doctor [--json] [--skip S] [--timeout T] [--lag-warn N]` | Read-only deployment preflight: config, modules, infrastructure reachability, migration heads, consumer lag, RLS, sagas (exit `0` ok, `1` warnings, `2` failures). |
| `sandloom admin export --output FILE [--check]` | Compile the static admin UI manifest; `--check` fails on drift. |

Procedures: the [runbook](../operate/runbook.md) and the
[operator reference](../operate/operator-reference.md).

```bash
uv run uvicorn <app>.asgi:app --reload                         # API on :8000 (needs .env)
uv run sandloom worker                                          # background tasks (and: sandloom beat)
```

## Framework dependency sources

Generate apps from an existing checkout with
`uv run sandloom new myapp --framework-source path --framework-path .`.
The path is the repository root containing `packages/`. Editable host paths
work locally; they are not portable Docker or CI dependencies.
`--framework-source pypi` installs published distributions and is the default
for the installed CLI. `--framework-source git` is disabled; use a local
checkout for source development. The canonical repository is
[Sandloom-dev/sandloom](https://github.com/Sandloom-dev/sandloom). See
[getting started](../getting-started/index.md).

## Local CI (GitHub Actions stand-in)

Run from the repository root. It exports the ref with `git archive` (never
your working tree), runs the real `ci.yml` / `docs.yml` /
`architecture-check.yml` jobs under `act` where safe and the service-backed
jobs (backend-tests ×4, benchmarks-security, examples, microservices-smoke)
in its own fallback runner, each with fresh run-id-named Postgres/Redis on an
isolated network (never the shared `sandloom_*_test` containers). backend-tests
runs the full suite on Python 3.11 and 3.12 (coverage on 3.12); 3.13 and 3.14
run `-m "not slow"`. It holds `/tmp/sandloom-ci-local.lock` while running (CPU
benchmarks wait for it); logs and summary in `.ci-local/runs/<run-id>/`;
exit 0 = green. Details: [Local CI runner](../contributing/local-ci.md).

```bash
tools/ci/ci_local.sh <git-ref> [--fetch] [--jobs a,b] [--python 3.11,3.13] [--parallel N]
tools/ci/ci_local.sh pr:<N>                      # a PR head from origin
tools/ci/ci_local.sh <git-ref> --rerun-failed    # only the jobs that failed last
                            # time for that ref (fail-fast loop; never a merge gate)
# jobs run in parallel by default (min(3, docker CPUs//3, mem//2.5GB));
# --parallel 1 = sequential
tools/ci/ci_local.sh --list | --check            # jobs + engines | workflow drift check
# --report-pr <N> posts the summary on the PR via gh (only when asked)
```

`uv sync` installs the `dev` dependency group (ruff, mypy, pytest — pinned
exactly in `pyproject.toml` and locked in `uv.lock`); CI runs
`uv sync --frozen` and the hooks `uv run --locked`, so a stale `uv.lock` fails
instead of re-resolving. Bump a tool by editing its pin, `uv lock`, and fixing
its new findings in the same change.

## Check database drift

`uv run sandloom migrate drift` builds a scratch database from the loaded
module migration chains and compares it with the models. It reports missing
or extra schema operations and does not repair the application database.
`sandloom test` includes this check. See [migrations](../modules/database/run-migrations.md).

For incident tools (`security`, `backup`, `streams`, `outbox`, `sagas`, keys and
projections), including scope, dry-run, JSON, batching and production-confirmation
flags, use the [operator reference](../operate/operator-reference.md).
Publishing the website is a separate [manual workflow](../contributing/website.md).

## Configuration tiers and accepted risks

Every `Settings` field is classified in the kernel's tier table
(`src/sandloom/core/config/tiers.py`): *semantic* (changes a guarantee —
its value is chosen per profile dev / test / prod / prod +
`multi_tenant_strict` in one table, see
[Configuration Tiers](configuration-tiers.md)) or *ops* (capacity /
operations). A new setting must be classified
(`src/sandloom/core/tests/test_config_tiers.py`); prefer an ops knob or a
profile-chosen value over a new semantic flag — the semantic count is
ratcheted (ceiling 15) and a new one needs an ADR. Overrides of the profile
are logged at startup and listed by `/diagnostics/core` and `sandloom doctor`.

**Every production weakening is an accepted risk, never a new boolean**
([ADR-0006](../adr/0006-accepted-risks.md)): `SANDLOOM_ACCEPTED_RISKS` (JSON or
comma list) names stable ids (`log_notifier_in_prod`,
`local_storage_in_prod`, `volatile_event_bus`, `unversioned_ddl_repair`,
`weak_password_hash`, `shared_hs_secret_split`, `relay_disabled`,
`command_dedupe_below_retry_horizon`, `saga_horizon_beyond_command_dedupe`).
An unknown id fails startup, and each accepted risk is logged at every start,
exported as `sandloom_accepted_risk_active{risk}` and listed by
`/diagnostics/core` / `sandloom doctor`. Removed settings (among them the former
`*_ALLOW_*` booleans, `RPC_AUTH_MODE`, `RPC_RESPONSE_BINDING` and the former
per-route revocation settings) fail startup unless set to their inert value.

## Key settings

Every knob is a `Settings` field (validated at startup, `.env` /
`SECRETS_DIR`-aware); the full list with defaults is the
[Configuration Reference](configuration.md). Framework code never reads
configuration with `os.getenv` (`SANDLOOM_COMPOSITION_ROOT` is read from the
process env first, then from `Settings` / `.env`; only `SANDLOOM_WORKER_TASKQUEUE`
is raw env).

- **Modules.** `SANDLOOM_MODULES` (JSON list) selects the modules a process loads;
  without it the composition root loads its defaults (an `sandloom new` app: every
  module of its `modules` package plus the chosen module packages;
  `sandloom.main:app`: every installed `sandloom.modules` entry point). An entry
  is a module name (`identity`) or an import path (`sandloom_identity`).
  `MODULE_REQUIRED` (load failure aborts startup; when empty, every module
  listed EXPLICITLY in `SANDLOOM_MODULES` is required — list the required subset in
  `MODULE_REQUIRED` instead). See
  [Loader Configuration](../operate/loader-configuration.md).
- **Services.** `SERVICE_BINDINGS` (strictly validated; `"required": true`
  makes `/ready` — never `/health` — fail while that peer is unreachable;
  `SERVICE_PROBE_TIMEOUT_SECONDS` caps each concurrent probe).
- **Events.** `EVENT_BUS_BACKEND` (memory/broadcast/streams; unset = the <!-- prose-ok: S4 dense settings list, only the unset clause changed -->
  composition's selection, `streams` under `sandloom_standard`, never
  `memory`; the volatile `memory` / `broadcast`
  (Pub/Sub) drivers are refused in prod unless the risk `volatile_event_bus`
  is accepted, and even then never as the default bus; required/durable
  consumers on Pub/Sub also need it outside prod), `OUTBOX_RELAY_ENABLED` (auto/on/off/delegated; auto = prod only;
  under gunicorn only one elected worker runs it, the others get `delegated`;
  `off` in a prod app process is refused unless the risk `relay_disabled` is
  accepted), `OUTBOX_RELAY_MAX_ATTEMPTS`,
  `OUTBOX_RELAY_BACKOFF_BASE_SECONDS` / `_MAX_SECONDS` (per-row retry backoff),
  `OUTBOX_RELAY_CIRCUIT_BASE_SECONDS` / `_MAX_SECONDS` (broker-down cool-down
  that does not burn attempts), `OUTBOX_RETENTION_HOURS` (the relay purges
  delivered rows; `sandloom outbox purge` on demand),
  `CONSUMER_STARTUP_TIMEOUT_SECONDS`, `CONSUMER_RETRY_BASE_SECONDS` and
  `CONSUMER_RETRY_MAX_SECONDS` (required-consumer startup/reconnect policy),
  `EVENT_STREAM_READ_COUNT` (Streams entries per consumed batch, default 64).
- **Contracts.** `CONTRACT_READ_SCOPE_ENABLED` / `CONTRACT_READ_SCOPE_IDLE_MS`
  (request-scoped read connection + memo for local read contracts; opt out of
  the memo per scope with `contract_read_scope(memo=False)` from
  `sandloom.sdk.contracts`), `CONTRACT_DB_POOL_SIZE` / `CONTRACT_DB_MAX_OVERFLOW` /
  `CONTRACT_DB_LOCK_TIMEOUT_MS` (dedicated pool of in-process contract calls),
  `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS` (command replay window, at most 7
  days).
- **Internal endpoints.** `INTERNAL_API_TOKEN` (unlocks `/metrics`,
  `/diagnostics/*`, `/contracts/*` and `/health` details — never `/_sandloom/rpc`;
  without it they are closed, unless `INTERNAL_ENDPOINTS_OPEN_IN_DEV=true`
  outside prod), `METRICS_TOKEN` (scrape-only `/metrics` credential; must
  differ from the internal token), `HEALTH_CACHE_TTL_SECONDS`.
- **Edge.** `RATE_LIMIT_ENABLED` (per-route `rate_limiter` limits; always on in
  prod), `RATE_LIMIT_GLOBAL_REQUESTS` / `RATE_LIMIT_GLOBAL_PERIOD_SECONDS` /
  `RATE_LIMIT_GLOBAL_EXEMPT_PATH_PREFIXES` (opt-in per-IP floor over every
  route incl. 404s, 0 = off everywhere, fail-open; the primary floor is the
  edge gateway), `TRUST_PROXY_HEADERS` + `TRUSTED_PROXY_IPS` (CIDR list;
  `X-Forwarded-For` is walked right-to-left from a trusted peer only —
  `get_client_ip` in `sandloom.sdk.web`), `CORS_ORIGINS`, `MAX_UPLOAD_SIZE_BYTES`,
  `MAX_REQUEST_BODY_BYTES` (global body cap → 413), `DOCS_ENABLED` (gate
  `/docs` etc.; off in prod by default).
- **Tokens and sessions.** `JWT_ALGORITHM` (HS256/384/512 shared secret —
  monolith/dev default — or EdDSA/RS256/ES256 asymmetric: the issuer — the
  process loading a module declared `issues_tokens=True`, i.e. identity —
  holds `JWT_PRIVATE_KEY`, every service verifies with `JWT_PUBLIC_KEYS`
  `{"<kid>": "<PEM>"}` and cannot mint; a critical startup check refuses a
  private key or `JWT_SECRET` on a verify-only process and, in prod, a shared
  HS secret in a split process unless the risk `shared_hs_secret_split` is
  accepted; `sandloom keys generate` makes a pair), `JWT_PREVIOUS_SECRETS` +
  `JWT_KID` (key rotation; asymmetric: rotate kids in `JWT_PUBLIC_KEYS`),
  `AUTH_REVOCATION_FAIL_CLOSED`, `AUTH_REVOCATION_CLOCK_SKEW_SECONDS` /
  `AUTH_REVOCATION_WRITE_ATTEMPTS` / `AUTH_REVOCATION_RETRY_BASE_SECONDS`
  (revocation mark margin and retries; identical in every service),
  `TWOFA_ENCRYPTION_KEY` (required in prod with identity; comma list =
  rotation), `INFRA.atomic_store.security` (the store of the subject-state
  records and — by default — idempotency records) and
  `INFRA.atomic_store.idempotency` (opt-in separate store for idempotency
  records; absent = the security store). Every authenticated route reads the
  session record live; there is no per-route revocation setting
  ([Route policy](../modules/http/route-policy.md)).
- **Passwords.** `PASSWORD_HASH_CONCURRENCY` (argon2 pool; default
  min(4, CPUs)), `PASSWORD_HASH_PROFILE` (`strong` default = m=65536 KiB/t=3/p=4
  | `owasp_min` = m=19456/t=2/p=1 | `custom`) + `PASSWORD_HASH_TIME_COST` /
  `PASSWORD_HASH_MEMORY_KIB` / `PASSWORD_HASH_PARALLELISM` (explicit values
  override the preset; build hashers with `password_hasher()` from
  `sandloom.sdk.security`, never hand-pinned parameters; a successful login
  re-hashes a hash with other parameters; prod refuses parameters below the
  OWASP argon2id minimum unless the risk `weak_password_hash` is accepted).
- **Infrastructure.** <!-- prose-ok: S4 dense settings list, only the Redis bounds clause changed --> `INFRA` (JSON declaring additional named infrastructure
  instances / driver overrides — see
  [Capabilities & Infrastructure](../infrastructure/index.md)),
  `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` / `DB_POOL_TIMEOUT` (default-engine pool
  sizing), `DB_POOL_PING_IDLE_SECONDS` (checkout ping only after that much
  idle time; no `pool_pre_ping`), `DB_POOL_OVERFLOW_LINGER_SECONDS` (overflow
  connections linger instead of churning connect/close; 0 = stock
  QueuePool), `DB_POOL_FAIR_BARGE_MS` (fair checkout: a waiter older than
  this is served before newcomers; 0 = strict FIFO handoff),
  `DB_STATEMENT_TIMEOUT_MS` / `DB_IDLE_IN_TX_TIMEOUT_MS` (server-side guards,
  0 = off), the Redis client bounds (options of each Redis instance in `INFRA`:
  `socket_timeout_seconds`, `socket_connect_timeout_seconds`, `max_connections`,
  `pool_timeout_seconds`, `retry_attempts`, `health_check_interval_seconds`; a
  dead Redis raises fast so fail-open/closed policies run), `GC_FREEZE_AFTER_STARTUP`
  / `GC_THRESHOLDS` / `GC_METRICS_ENABLED` (cyclic GC of gunicorn / Celery
  workers: freeze the startup heap, thresholds `2000,10,10`, pause histogram;
  applied by the process entry points, never by `create_app`).
- **Observability.** `LOG_FORMAT` (auto/json/text; auto = JSON in prod),
  `LOG_LEVEL`, `SENTRY_DSN` (+ `SENTRY_TRACES_SAMPLE_RATE`; no-op when
  unset), `TRACING_URL` (OTLP endpoint).
- **Billing and notifications.** `BILLING_PROVIDER=stripe` fails fast at
  startup unless `STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET` are set. Prod
  refuses `fake` unless the module risk `billing.fake_psp_in_prod` is accepted.
  Both are billing's own settings check: a process without billing skips them.
  `NOTIFIER_BACKEND` (log/smtp; default log) + the `notification` instance in `INFRA`
  (host/port/username/password/from_addr/use_tls options for the email driver).
- **Secrets.** In prod, secrets and tokens must be at least 32 characters and
  not placeholders. `SECRETS_DIR` (Docker/K8s file-based secrets — a setting
  may be supplied as a file named after its field).

Secrets, backups, retention, audit and observability: the
[operator reference](../operate/operator-reference.md).
