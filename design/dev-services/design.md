# `sandloom dev`: local services without collisions

Status: design, 2026-11-03 (tm: `dev-admin-design`, under `compose-collision`). Built on
[research.md](research.md) and the owner's decisions of 2026-11-03: several agents work on one
project at once, in one checkout or in git worktrees, and none may break another's run.

## The model: servers per repository, databases per checkout and per run

| Thing | Scope | Name |
| --- | --- | --- |
| Compose project (dev Postgres + Redis, test Postgres + Redis) | one per **repository** (`git rev-parse --git-common-dir`, realpath; the project root outside git) | `<app>-<6 hex of sha256(repository id)>` |
| Host ports of those four services | one set per repository, sticky | chosen once from 20000-32767, kept in the state |
| Dev database | one per **checkout** (the main checkout or a worktree) | `<app>` for the main checkout, `<app>_<6 hex of checkout path>` for a worktree |
| Dev Redis indexes (cache, broker) | one pair per checkout | allocated from the dev Redis, recorded in the state |
| Test database | one per **test run** | `sandloom_test_<checkout hash>` while free, else `…_2`, `…_3` (see "Test runs") |
| Test Redis indexes | one set per test run | `SANDLOOM_TEST_REDIS_DB_STRIDE=auto` (exists today) |

Worktrees share the containers, so ten agents in ten worktrees still run four containers, and each
has its own database to migrate on its own branch. Agents in one checkout share that checkout's
dev database (they work on one app) but never a test database.

Both Redis servers start with `--databases 1024`, so per-checkout and per-run indexes do not run
out at 16.

## State

- **Repository state**, `<main checkout>/.sandloom/dev.json` (gitignored): repository id and its
  realpath, the compose project name, the four host ports, and one row per checkout using the
  servers: path, database name, Redis indexes, last `up`.
- **Machine registry**, `~/.sandloom/ports.json`: port -> repository path, for every port any
  repository on the machine reserved. Read and written only under the machine lock.
- **Checkout `.env`**: the CLI owns one marked block (`# >>> sandloom dev` ... `# <<< sandloom dev`)
  with `DATABASE_URL`, `CACHE_URL`, `BROKER_URL`; the rest of the file is the user's. Written
  atomically (temp file + `os.replace`).
- **Checkout `.env.test.local`** (gitignored): `SANDLOOM_TEST_DATABASE_URL`,
  `SANDLOOM_TEST_CACHE_URL`, `SANDLOOM_TEST_BROKER_URL` pointing at the repository's test servers.
  The test plugin loads it after `.env.test`. `.env.test` stays committed and keeps CI's values.

## Locks

| Lock | File | Held while |
| --- | --- | --- |
| machine | `~/.sandloom/ports.lock` (`fcntl.flock`; `msvcrt.locking` on Windows) | choosing and registering ports |
| repository | `<main checkout>/.sandloom/dev.lock` | `up`, `down`, `reset`, `prune`, writing `dev.json` |
| checkout | `<checkout>/.sandloom/dev.lock` | writing that checkout's `.env` / `.env.test.local`, creating its database |
| migrations | PostgreSQL advisory lock (exists: `migrations.py`) | `migrate upgrade-all` |
| test run | PostgreSQL advisory lock on the test database, session-scoped | the whole test session |

Locks are taken in that order (machine, repository, checkout) and never the other way, so two
commands cannot deadlock. A second `dev up` waits for the first (up to 120 s, printing "waiting for
another `sandloom dev up` in <path>") and then finds everything already up.

## Commands

- `sandloom dev up`: under the locks, ensure the repository's services run and are reachable from
  the host, ensure this checkout's databases and Redis indexes exist, write its `.env` block and
  `.env.test.local`, print the URLs. Idempotent; safe to run from every agent first thing.
- `sandloom dev status [--json]`: the services, their host ports, reachability, this checkout's
  database and URLs (ready to paste into TablePlus), the other checkouts using the servers.
- `sandloom dev run [--port N|auto]`: start uvicorn for this checkout. Port 8000 by default; when it
  is taken, say what holds it (`lsof` / `psutil` where available) and suggest a free one; `--port
  auto` picks one and prints the URL (agents use this). A pid file per checkout makes a second `dev
  run` in the same checkout answer "already running at http://127.0.0.1:N" instead of failing.
- `sandloom dev reset`: drop and recreate this checkout's database and flush its Redis indexes. The
  servers and every other checkout are untouched.
- `sandloom dev down [--all]`: stop the repository's containers. Without `--all` it refuses while
  another checkout used the servers in the last 24 h or a test database is locked, and names them.
  `--volumes` (with `--all`) also deletes the data, after a typed confirmation at a terminal.
- `sandloom dev prune`: drop the databases and free the Redis indexes of checkouts whose path no
  longer exists (worktrees removed by an agent harness), and stale test databases (no lock, unused
  for 3 days; the resource marker records last use already).
- `sandloom dev logs [service]`: compose logs.

Commands that need a database never start services themselves. When they cannot connect they say
so in one line and name `sandloom dev up` (see "Errors"). The one exception is `dev run`, the
person's "run my app" (decided 2026-11-03, `first-run-polish`): stopped services are started first,
the way `dev up` starts them (same locks and checks, one line), so the app never boots without its
database.

## Readiness from the host

After `docker compose up -d --wait`, for each service: the published binding exists
(`docker compose port`), a TCP connect succeeds, then `SELECT 1` / `PING`. A container that is
healthy but unreachable from the host (the Docker Desktop case the owner hit) is recreated once with
`--force-recreate --no-deps <service>`; if it is still unreachable, `up` fails with one line naming
the service, the port and what to try. With `DOCKER_HOST` set to a remote or a devcontainer, the
checks use that host instead of 127.0.0.1.

## Ports

Chosen once per repository under the machine lock: start at an offset derived from the repository
hash, skip ports in the machine registry, test-bind each candidate on 127.0.0.1, write the four into
`dev.json` and the registry. Published as explicit `127.0.0.1:<port>:5432` mappings through
`--env-file <state env>`, never through `${…:-default}` (Compose reads `deploy/.env`, not the root
`.env`). If a sticky port is taken by something else at the next `up` (the services were down), pick
a new one, rewrite the state and every registered checkout's `.env` block and `.env.test.local`,
and warn that running app servers must restart.

## Test runs

The pytest plugin, at session start:

1. reads `SANDLOOM_TEST_DATABASE_URL` (from `.env.test.local` or the environment);
2. tries `pg_try_advisory_lock` on that database for the session; if another run holds it, uses
   `<name>_2`, `<name>_3`, ... (created on first use, stamped with the owner marker) and says which;
3. xdist workers derive `<name>_gw<N>` from the database the controller locked (as today);
4. Redis indexes come from the existing `auto` stride, now with 1024 indexes available.

This closes a gap in today's harness: the owner is the checkout root, so two runs in one checkout
are the same owner and can reset each other's database. The lock, not the owner, now decides.

## Errors

A top-level handler in the `sandloom` CLI turns connection failures (`OSError` / `asyncpg` /
`psycopg2` / `redis` connection errors, unwrapped from SQLAlchemy) into one line and exit code 1:

```text
error: cannot reach PostgreSQL at 127.0.0.1:5432 (connection refused). Start the dev services: uv run sandloom dev up   [SANDLOOM-E001]
```

The traceback appears with `--debug` or `SANDLOOM_DEBUG=1`. `sandloom explain SANDLOOM-E001` gives
the causes (services down, wrong `.env`, port not published).

## Edge cases

| Case | Behaviour |
| --- | --- |
| Two agents run `dev up` at once in one checkout | the second waits on the repository lock, then finds everything up |
| Two repositories choose ports at once | the machine lock serialises the choice; the registry prevents reuse |
| An agent runs `dev down` while others work | refused without `--all`, naming the active checkouts |
| An agent runs `dev reset` | only its checkout's database and indexes |
| Two `sandloom test` in one checkout | the second gets `sandloom_test_<hash>_2` |
| Two `migrate upgrade-all` in one checkout | the existing advisory lock serialises them |
| Two worktrees on different branches migrate | different databases; no conflict |
| Worktree removed without cleanup | its database stays until `dev prune` |
| Directory moved | stored path missing: `up` offers to adopt the old project (volumes keep the data) |
| Directory copied (`cp -r`) | stored path differs from realpath: a new repository identity, new project |
| Two clones of one repository | different realpaths of the git common dir: different projects |
| A sticky port taken while services were down | new port, all registered checkouts rewritten, a warning |
| Container healthy, port not published | recreated once, then a one-line failure |
| An existing app with `name: myapp-dev` and its volumes | first `up` finds the old project's containers by label and working dir, offers to adopt its name |
| User edits `.env` while an agent runs `dev up` | only the marked block is replaced, atomically, under the checkout lock |
| `.env` has no marked block (old app, hand-written) | the block is appended; existing `DATABASE_URL` lines outside it are commented out with a note |
| CI (`CI=true`) | `dev` refuses with a pointer: CI provides services and `SANDLOOM_TEST_*` |
| No Docker (cloud agent sandbox) | one line: Docker not reachable; set `SANDLOOM_TEST_DATABASE_URL` to an existing server |
| `DOCKER_HOST` remote / devcontainer | reachability checked against that host |
| Docker Desktop, OrbStack | supported and tested; Podman, Colima: expected to work, not tested |
| Windows | locks through `msvcrt`; paths hashed after `realpath` and case folding |
| Raw `docker compose -f deploy/docker-compose.dev.yml up` | fails fast: the compose file uses `${SANDLOOM_DEV_PROJECT:?run "uv run sandloom dev up"}` |

## What `sandloom new` writes

- `deploy/docker-compose.dev.yml` without `name:`, ports from required variables, Redis with
  `--databases 1024`.
- `.gitignore`: `.sandloom/dev.json`, `.sandloom/*.lock`, `.sandloom/run/`, `.env.test.local`.
  `.sandloom/agent-files.json` stays committed.
- `.env` with an empty marked block, filled by the first `dev up`.
- README golden path: `uv sync` -> `uv run sandloom dev up` -> `uv run sandloom migrate upgrade-all
  --apply` -> `uv run sandloom dev run`.
- `QUICKSTART` (run verbatim by CI) keeps working: CI sets `CI=true` and its own services, so the
  generated quickstart test uses the service URLs CI provides.

## Out of scope

Branch-scoped databases inside one checkout (Supabase's branch identity), Podman support,
implicit service start (except `dev run`, above). The app port is sticky since `first-run-polish`:
the first `dev up` gives each checkout 8000 or the next free port, kept in the state, `dev run`'s
default.

## Revisions after the edge-case review (2026-11-03)

An adversarial review against the code found six blockers and twelve gaps. These rules replace the
sections above where they disagree.

**Where state lives.** Repository state goes to
`$(git rev-parse --path-format=absolute --git-common-dir)/sandloom/` (`dev.json`, `dev.lock`, the
CLI's own copy of the compose file). That works for the main checkout, worktrees, a bare repository
with worktrees and submodules, and keeps it out of iCloud/Dropbox-synced trees. Without git: the
project root's `.sandloom/`. `GIT_DIR` / `GIT_COMMON_DIR` from a git hook are ignored.

**Identity of a run.** Owners and derived names include a machine id (generated once into
`~/.sandloom/machine-id`), because cloud sandboxes share paths like `/workspace/repo`.

**Test runs (replaces "Test runs").** A run takes a *slot* before anything touches a database:

1. In the process that starts the run (the `sandloom test` parent, or the pytest controller for
   plain `pytest`), before `load_test_env`: lock slot 1, 2, 3 ... with
   `pg_try_advisory_lock(<hash of machine id + checkout>, <slot>)` on the test server's `postgres`
   database, held on a connection the process keeps open for the whole run.
2. Slot N sets `SANDLOOM_TEST_DATABASE_URL` to `sandloom_test_<checkout hash>` (slot 1) or
   `…_<N>`, and `SANDLOOM_TEST_OWNER` to `<machine id>:<checkout>#<N>`, in `os.environ`, so
   `assert_disposable_test_database`, xdist's `_gw<N>` copies and the ownership markers all see
   this run's own name and owner. Redis indexes come from the `auto` stride under that owner, so a
   second run in one checkout never gets the first run's indexes.
3. `sandloom test` holds the slot across all its pytest steps and passes it down
   (`SANDLOOM_TEST_SLOT_HELD=<pid>`), so the steps do not re-lock.

**Compose: never recreate under someone else.** `up` runs compose from the CLI's own copy of the
compose file in the state directory. When the checkout's `deploy/docker-compose.dev.yml` differs
from the running config (another branch bumped an image, added a service), `up` does not apply it:
it says so and applies it only with `dev up --recreate`, which refuses while other checkouts have
sessions (`pg_stat_activity`, Redis `CLIENT LIST`) unless `--force`. The "unreachable from the
host" repair recreates automatically only when nobody else is connected; otherwise it asks.
`--remove-orphans` is never passed.

**Locks.** Images are pulled before the repository lock is taken. A lock file records its holder's
pid, host and a heartbeat; a waiter waits while the holder is alive (no fixed 120 s), and takes over
a lock whose holder is dead. Order is always machine -> repository -> checkout(s), several
checkouts sorted by path. Port reassignment releases the repository lock, takes machine then
repository again, and re-reads the state.

**A new worktree has no `.env`.** `dev up` seeds a missing `.env` from `.env.example` with a fresh
`JWT_SECRET`, then writes the block. The file keeps its mode (0600 when it was) and a symlinked
`.env` is written through the link.

**Which value wins.** Process environment beats `.env` (pydantic-settings), so `dev run` passes the
checkout's URLs explicitly to uvicorn, and `dev status` and every CLI command that connects warn
when an exported `DATABASE_URL` / `CACHE_URL` / `BROKER_URL` differs from the block.

**`.env.test.local`** is loaded with `override=False` and only from the directory of the
`.env.test` that was found, never by walking up (a worktree nested in the main checkout must not
pick up the main checkout's file). An exported `SANDLOOM_TEST_*` still wins.

**Same-checkout agents.** `dev reset` and `dev down` refuse while other sessions use the database
or a live `dev run` pid file exists, unless `--force`, and name them.

**Ports.** URLs always use `127.0.0.1`, never `localhost` (which may resolve to `::1` and reach a
different server with the same credentials). Candidates are test-bound on `127.0.0.1` and `::1`;
with a remote `DOCKER_HOST` or Colima the published port is checked on the Docker host after `up`.
`prune` frees registry entries of repositories whose path is gone.

**CI.** With `CI=true` and `SANDLOOM_TEST_DATABASE_URL` set, `dev up` exits 0 and prints that CI
provides the services. `tools/.../quickstart_check.py` learns the `sandloom dev up` line the way it
handles `docker compose` lines today.

**Errors.** The connection-failure code joins an existing family, a runtime `SANDLOOM-R0xx` code,
registered in `error_codes` and `docs/reference/error-codes.md`.
