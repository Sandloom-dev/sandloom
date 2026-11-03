# Dev services without collisions: research

Status: research, 2026-11-03 (tm: `dev-services-research`, under `compose-collision`). Question:
how should `sandloom new` and the `sandloom` CLI start local Postgres and Redis in Docker so that a
first-time user never hits a name or port collision, and never gets a raw traceback when something
is unreachable?

Sources are primary (official docs, source code, GitHub issues) unless marked. "Unverified" marks
claims that could not be checked against a source.

## What happened (the five failures) and their root cause

| # | failure | root cause in today's template |
|---|---|---|
| 1 | `up` failed: 5432/6379 held by `demo-dev`, 5433/6380 by another project | every app ships the same fixed host ports (`project.py` `_DEV_COMPOSE`, `${SANDLOOM_DEV_POSTGRES_PORT:-5432}` …) |
| 2 | two apps named `myapp` share `name: myapp-dev`; `up` in one recreated the other | project name is derived from the app name only, and it is committed in the compose file |
| 3 | containers healthy, ports not published (Docker Desktop), "connection refused" | `--wait` only checks the in-container healthcheck; nothing checks the host side |
| 4 | port 8000 held by a Homebrew service; uvicorn said only "Address already in use" | the golden path calls uvicorn directly; nothing checks or reports the port |
| 5 | `sandloom security grant-admin` printed a 200-line asyncpg traceback | the CLI has no top-level handler for connection errors |

A sixth trap found while researching: with `docker compose -f deploy/docker-compose.dev.yml`, Compose
reads the `.env` in the **compose file's directory** (`deploy/.env`), not the repository root, for
`${…}` interpolation. So `SANDLOOM_DEV_POSTGRES_PORT=15432` placed in the root `.env` is silently
ignored; only exported shell variables work (the README says `export`, but an agent that edits
`.env` will be wrong).
[Compose interpolation docs](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/).
The predefined-variables page instead says the `.env` is "located in the working directory"
([envvars](https://docs.docker.com/compose/how-tos/environment-variables/envvars/)); the two pages
disagree, which is itself a reason to pass `--env-file` explicitly.

A seventh: `.env.test` is committed (only `.env` is in the generated `.gitignore`), so per-checkout
test ports cannot be written into it.

## Recommendations for Sandloom

### 1. Identity: one compose project per checkout, not per app name

- Project name = `<app-slug>-<6 hex of sha256(realpath(project root))>`, e.g. `myapp-3fa91c`.
  Two apps both called `myapp`, a second clone and a git worktree all get different names.
  Supabase's new stack identity is the same idea (project root + git branch + optional `--stack`
  name; "Two local projects with different identities never share containers, processes, ports"),
  [Supabase: running multiple local projects](https://supabase.com/docs/guides/local-development/running-multiple-local-projects).
- Computed and persisted on the first `sandloom dev up`, never committed: store it in a gitignored
  state file together with the root path it was computed for. If the stored path differs from the
  current realpath (the directory was copied with `cp -r`), treat it as a new checkout and compute a
  fresh name; if it was moved (old path gone), say so and offer to keep the old name, because the
  named volume `dev_postgres` belongs to the old project name.
- Remove the hard-coded `name:` from the generated compose file, or make it
  `name: ${SANDLOOM_DEV_PROJECT:?run "sandloom dev up" once}` so a raw `docker compose up` fails
  with a pointer instead of silently sharing a name. The CLI always passes `-p` (highest precedence,
  [Compose project name](https://docs.docker.com/compose/how-tos/project-name/)).
- Guard against what still slips through (a hand-edited name): before `up`, list containers with
  label `com.docker.compose.project=<name>` and compare their
  `com.docker.compose.project.working_dir` label to our root
  ([labels.go](https://github.com/docker/compose/blob/main/pkg/api/labels.go)). A mismatch means
  another checkout owns the name: refuse, name that directory, never recreate its containers.

### 2. Ports: sticky per-checkout ports chosen by the CLI, published explicitly

Three models exist (details per tool below):

| model | used by | GUI client / agent reading `.env` | Docker Desktop risk |
|---|---|---|---|
| fixed defaults (5432/6379) | Laravel Sail, Supabase classic, Sandloom today | stable, but the second app collides | none |
| ephemeral, discovered on each start (`127.0.0.1::5432`, `docker compose port`) | Testcontainers, Lando `portforward: true`, DDEV default, Quarkus | port changes on every recreate; TablePlus and `.env` go stale | inspect may report empty bindings for ephemeral ports (docker/for-mac#7787) |
| sticky: CLI picks a free port once, saves it, publishes it explicitly | Supabase experimental stack (20000–32767, start derived from identity, probe, persist) | stable per checkout | explicit `host:container` mappings are the case #7787 says still works |

Recommend the **sticky** model:

- On the first `sandloom dev up`, for each of the four services pick a port from a high range
  (e.g. 20000–32767), starting at an offset derived from the project-name hash, probing with a real
  `bind()` on 127.0.0.1 and skipping ports already recorded by other Sandloom checkouts (a small
  per-user registry, e.g. under `~/.sandloom/`). Supabase's ADR 0017 specifies this: start
  "derived from the stack's project root, identifier, and listener key", stride 257, up to 64
  attempts, sticky values that "do not migrate on their own"
  ([ADR 0017](https://github.com/supabase/cli/blob/develop/docs/adr/0017-simplified-managed-stack-architecture.md)).
- Save the ports in the gitignored state and publish them as explicit
  `127.0.0.1:${SANDLOOM_DEV_POSTGRES_PORT:?}:5432` mappings (keep the loopback binding).
- On later runs reuse the saved port; if something else now holds it, fail naming the holder
  (see §5) and offer `sandloom dev up --reassign-ports`, rather than silently moving (moving breaks
  the user's saved TablePlus connection).
- Allow pinning (`sandloom dev up --postgres-port 5432`), like Quarkus `devservices.port` and DDEV
  `host_db_port`.

### 3. How the app, tests, GUI clients and agents learn the ports

- **App:** the CLI rewrites only the connection lines it owns in `.env` (`DATABASE_URL`,
  `CACHE_URL`, `BROKER_URL`) inside a marked block (`# >>> sandloom dev (managed)` … `# <<<`), and
  only when a value changed. `.env` is already gitignored. `uv run uvicorn …` and every `sandloom`
  command keep working without a wrapper, and an agent reading `.env` sees the truth.
  Laravel Sail edits `.env` at install time (`sail:install` will "modify your `.env` file with the
  required environment variables", [Sail docs](https://laravel.com/docs/12.x/sail)); Supabase emits
  an env file on demand (`supabase status --env --output-format text > .env.local`, multiple-projects
  guide above). Pure env injection by a wrapper (Aspire `WithReference`, Spring service connections)
  is invisible to an agent and to a user who runs uvicorn directly; not recommended as the only path.
- **Tests:** because `.env.test` is committed, the CLI writes the test URLs as
  `SANDLOOM_TEST_DATABASE_URL` / `_CACHE_URL` / `_BROKER_URL` (overrides that already exist) into a
  gitignored `.env.test.local` that the pytest plugin loads after `.env.test`. `sandloom test`
  preflights the test services (§4) before collecting, with the §6 message on failure.
- **GUI clients:** `sandloom dev up` and `sandloom dev status` print a ready-to-paste URL per service
  (`postgresql://sandloom:sandloom@127.0.0.1:24817/myapp`), like `supabase start`
  ([getting started](https://supabase.com/docs/guides/local-development/cli/getting-started)),
  `ddev describe` and `lando info`
  ([DDEV DB management](https://docs.ddev.com/en/stable/users/usage/database-management/),
  [Lando postgres](https://docs.lando.dev/plugins/postgres/config.html)). Later, optionally:
  `sandloom dev psql`, `sandloom dev open tableplus` (DDEV has `ddev tableplus`).
- **Single source of truth:** the state file. `.env`, `.env.test.local` and the compose
  `--env-file` are renderings of it; `sandloom dev status` reports drift (e.g. `.env` points at a
  port no container publishes).

### 4. Readiness: check from the host, and repair the Docker Desktop stale binding

`docker compose up --wait` waits for "running|healthy"
([compose up](https://docs.docker.com/reference/cli/docker/compose/up/)), and a compose healthcheck
runs inside the container
([services: healthcheck](https://docs.docker.com/reference/compose-file/services/#healthcheck)).
Neither proves the host port works. After `up --wait`, `sandloom dev up` should:

1. Inspect each container's `NetworkSettings.Ports` and require a `127.0.0.1` binding equal to the
   saved port, polling briefly. Supabase polls every 250 ms because "The engine can report a
   container started before its loopback publish lands"
   ([Container.ts](https://github.com/supabase/cli/blob/develop/packages/stack/src/runtime/Container.ts)).
2. Open a real client connection from the host: asyncpg `SELECT 1`, Redis `PING`. Spring Boot's
   default readiness is "a container is considered ready when a TCP/IP connection can be
   established to its mapped port"
   ([Spring dev services](https://docs.spring.io/spring-boot/reference/features/dev-services.html)).
3. If the binding is missing or the connect is refused while the container is healthy, run
   `up -d --force-recreate --wait <service>` once and re-check; if it still fails, stop with a
   one-line diagnosis (service, port, what inspect shows, the likely holder of the port).

Evidence that this happens on Docker Desktop for Mac: when a Mac process already holds the host
port, "Docker Desktop then drops every forward for the container, but dockerd still reports the
container as running", leaving empty bindings
([spate-etl/spate#1031](https://github.com/spate-etl/spate/issues/1031));
ephemeral ports reported with empty HostIp/HostPort since ~4.40, still open
([docker/for-mac#7787](https://github.com/docker/for-mac/issues/7787));
empty bindings after a Docker Desktop restart
([docker/for-mac#5521](https://github.com/docker/for-mac/issues/5521), old).
The owner's exact symptom (explicit binding, `PortBindings` set, `Ports` empty after freeing the port
and re-running `up`) fits the #1031 mechanism (containers created while the port was held keep no
forward, and `up` does not recreate unchanged containers), but no issue reports it verbatim:
**unverified as a filed Docker bug**. Pre-checking ports before `up` (§2) avoids most of it.

### 5. The app port (8000)

- Add `sandloom dev run` (or `serve`): uvicorn `--reload` after a preflight bind test on
  127.0.0.1:8000; on failure, print who holds it when `lsof -nP -iTCP:8000 -sTCP:LISTEN` is
  available (a suggestion, not from a source) and a free alternative. Encore is the model:
  "Failed to run on … - port is already in use", then "Note: port N is available; specify
  --port=N to use it" ([encore daemon/run.go](https://github.com/encoredev/encore/blob/main/cli/daemon/run.go)).
- Do not silently switch by default: Vite does ("will automatically try the next available port")
  and needs `strictPort` to stop it ([Vite server options](https://vite.dev/config/server-options));
  a silent switch breaks agents and the admin console URL. Print the final URL on start.
- Uvicorn itself only logs the `OSError` and exits (`logger.error(exc)` in
  [uvicorn server.py](https://github.com/encode/uvicorn/blob/master/uvicorn/server.py)), so the
  preflight belongs in the Sandloom command.

### 6. Error messages

- A top-level handler in the CLI entry point maps connection failures (`ConnectionRefusedError`,
  `OSError` ECONNREFUSED/EADDRINUSE, asyncpg `InvalidPasswordError` / `InvalidCatalogNameError`,
  redis `ConnectionError`, Docker daemon not running) to one line plus the fix, non-zero exit,
  traceback only with `--debug` / `SANDLOOM_DEBUG=1`. Django's runserver does this, commented "Use
  helpful error messages instead of ugly tracebacks"
  ([runserver.py](https://github.com/django/django/blob/main/django/core/management/commands/runserver.py));
  Prisma P1001: "Can't reach database server at `{database_location}`. Please make sure your
  database server is running at `{database_location}`."
  ([prisma-engines common.rs](https://github.com/prisma/prisma-engines/blob/main/libs/user-facing-errors/src/common.rs)).
- Proposed shape, with a code so `sandloom explain` works:

  ```
  error[SANDLOOM-D001]: cannot reach Postgres at 127.0.0.1:24817 (DATABASE_URL in .env): connection refused
    fix: sandloom dev up        check: sandloom dev status
  ```

### 7. Commands, and wrapper vs raw compose

| command | does |
|---|---|
| `sandloom dev up` | resolve identity and ports (§1–2), render the `.env` blocks, `docker compose -p … --env-file … -f deploy/docker-compose.dev.yml up -d --wait`, host readiness + repair (§4), print the URL table; idempotent |
| `sandloom dev status [--json]` | containers, ports, host reachability, `.env` drift; `--json` for agents |
| `sandloom dev down` | stop, keep volumes |
| `sandloom dev reset [--yes]` | `down -v` for this project only, then `up` |
| `sandloom dev logs [service]` | `docker compose logs -f` with the right `-p`/`-f` |
| `sandloom dev run` | preflight + uvicorn `--reload` (§5) |
| `sandloom dev env` | print the env lines (like `supabase status --env`) |

A wrapper is the better golden path for an agent-driven workflow: one idempotent command,
deterministic output, machine-readable status, and the project name / env file are never forgotten.
Precedent: Sail ("At its heart, Sail is the `compose.yaml` file and the `sail` script"),
cookiecutter-django's `just up/down/logs/prune`
([cookiecutter-django docker](https://cookiecutter-django.readthedocs.io/en/latest/2-local-development/developing-locally-docker.html)),
`supabase start/status/stop`, `ddev start/describe`. Keep the compose file plain and documented
(`sandloom dev up --print` shows the exact compose command) so nothing is hidden. The README's golden
path becomes `sandloom dev up` → `sandloom migrate upgrade-all --apply` → `sandloom dev run`.

**What `sandloom new` writes:** the compose file without a fixed `name:` and with `${VAR:?}` host
ports; `.env` with the managed block (placeholder values until the first `dev up`); `.gitignore`
entries for the state file and `.env.test.local`; a README that points at `sandloom dev`.

## Per-tool findings

### Docker Compose (the substrate)

- Project name precedence: `-p` > `COMPOSE_PROJECT_NAME` > top-level `name:` (the last one with
  several `-f`) > basename of the project directory; allowed: lowercase letters, digits, `-`, `_`.
  [project-name](https://docs.docker.com/compose/how-tos/project-name/)
- `.env` for interpolation comes from the project directory = `--project-directory`, else the first
  `-f` file's directory; `--env-file` replaces it and is relative to the CWD; shell variables win;
  `docker compose config --environment` shows the result.
  [variable-interpolation](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)
- Ports: `[HOST:]CONTAINER`, host = `[IP:](port|range)`; with only the container port "the container
  runtime automatically allocates any unassigned port of the host". `127.0.0.1::5432` follows from
  the grammar but has no example in the spec (Supabase's CLI uses it in code, below).
  [services: ports](https://docs.docker.com/reference/compose-file/services/#ports)
- `docker compose port SERVICE [PRIVATE_PORT]` prints the published port
  ([compose port](https://docs.docker.com/reference/cli/docker/compose/port/)). Whether it is hit by
  docker/for-mac#7787 (it reads container inspect data): **unverified**.
- `--wait` waits for running|healthy; `--force-recreate` recreates unchanged containers.
  [compose up](https://docs.docker.com/reference/cli/docker/compose/up/)
- Every container carries project, `working_dir` and `config_files` labels.
  [labels.go](https://github.com/docker/compose/blob/main/pkg/api/labels.go)

### Supabase CLI

- Classic: `config.toml` has `project_id` (to "distinguish different Supabase projects on the same
  host", set from the directory name by `supabase init`) and fixed ports: api 54321, db 54322,
  shadow 54320, studio 54323. [config](https://supabase.com/docs/guides/local-development/cli/config)
  With defaults, one project per machine, "because every project's `config.toml` uses the same
  ports". [getting started](https://supabase.com/docs/guides/local-development/cli/getting-started)
- Experimental stack: drop the fixed ports and the CLI "assigns ports for each local project from
  the range 20000 to 32767 and keeps them stable across restarts"; identity = project root + git
  branch + `--stack`; `supabase status --env --output-format text > .env.local`.
  [multiple projects](https://supabase.com/docs/guides/local-development/running-multiple-local-projects)
- Design: hash-derived start, stride 257, ≤64 `EADDRINUSE` attempts, sticky saved ports; a taken
  fixed port "fails the start with the usual port conflict"; `state.json` stores identity.
  [ADR 0017](https://github.com/supabase/cli/blob/develop/docs/adr/0017-simplified-managed-stack-architecture.md)
- Runtime: `--publish 127.0.0.1::<port>`, up to 3 launch retries on "port is already allocated",
  polls `docker inspect` for the loopback binding with a timeout and a named error ("No loopback
  publication for …").
  [Container.ts](https://github.com/supabase/cli/blob/develop/packages/stack/src/runtime/Container.ts)
  How that ephemeral publish relates to the sticky public ports (the ADR says public sockets are
  held and "adopted directly", suggesting a host-side listener in front): **not verified in code**.

### Laravel Sail

- `compose.yaml` + the `sail` script; `sail:install` edits `.env`; the app container reaches the DB
  by service name (`DB_HOST=mysql`); host access on `localhost:3306` / `6379` for TablePlus; users
  are told to "ensure that no other web servers or databases are running".
  [Sail docs](https://laravel.com/docs/12.x/sail)
- Fixed defaults overridable by env: `'${FORWARD_DB_PORT:-5432}:5432'`
  ([pgsql.stub](https://github.com/laravel/sail/blob/1.x/stubs/pgsql.stub)). No collision detection
  found in the docs.

### DDEV

- Project name defaults to the directory name and "Must be unique". `host_db_port` and friends
  default to automatic: "assigned automatically and change each time `ddev start` is run".
  [config](https://docs.ddev.com/en/stable/users/configuration/config/)
- `ddev describe` shows host connection details; `ddev tableplus` etc. launch GUI clients; a stable
  IDE data source needs a static `host_db_port`.
  [database management](https://docs.ddev.com/en/stable/users/usage/database-management/)
- Checks host ports itself before starting the router (`port %s is already in use`) and can
  substitute router ports. [router.go](https://github.com/ddev/ddev/blob/main/pkg/ddevapp/router.go)

### Lando

- `portforward: true` (recommended): "Docker will assign a different port every time you restart
  your application"; a number pins it; `lando info` prints connection details.
  [postgres config](https://docs.lando.dev/plugins/postgres/config.html)

### Rails

- `rails new` Postgres `database.yml`: Unix socket by default, `host`/`port` commented out.
  [postgresql.yml.tt](https://github.com/rails/rails/blob/main/railties/lib/rails/generators/rails/app/templates/config/databases/postgresql.yml.tt)
- `bin/dev` is `exec "./bin/rails", "server", *ARGV`.
  [bin/dev.tt](https://github.com/rails/rails/blob/main/railties/lib/rails/generators/rails/app/templates/bin/dev.tt)
- `--devcontainer` compose: `name: "<app_name>"` (the same per-app-name collision class as
  Sandloom's), the app runs inside the container, DB/Redis publish no host ports; the app port is
  forwarded by `devcontainer.json`.
  [compose.yaml.tt](https://github.com/rails/rails/blob/main/railties/lib/rails/generators/rails/devcontainer/templates/devcontainer/compose.yaml.tt)

### Django cookiecutter

- `docker-compose.local.yml`: no top-level `name:`; Postgres/Redis publish no host ports (reached by
  service name); Django publishes fixed `8000:8000`; `container_name` is prefixed with the slug.
  [docker-compose.local.yml](https://github.com/cookiecutter/cookiecutter-django/blob/main/%7B%7Bcookiecutter.project_slug%7D%7D/docker-compose.local.yml),
  [docs](https://cookiecutter-django.readthedocs.io/en/latest/2-local-development/developing-locally-docker.html)
- A fixed `container_name` makes two same-slug projects conflict outright (container names are
  global per daemon): inference, not tested.

### Encore

- `encore run` creates databases in Docker automatically; access via `encore db shell`,
  `encore db conn-uri`, `encore db proxy`; local ports not documented.
  [databases](https://encore.dev/docs/go/primitives/databases)
- Infra namespaces isolate data (`encore namespace switch`, `--namespace`).
  [namespaces](https://encore.dev/docs/go/cli/infra-namespaces)
- App port: default 4000 on 127.0.0.1; on conflict prints the failure plus an available port and
  the flag to use. [cmd run.go](https://github.com/encoredev/encore/blob/main/cli/cmd/encore/run.go),
  [daemon run.go](https://github.com/encoredev/encore/blob/main/cli/daemon/run.go)

### Testcontainers

- Random host ports "by design, to avoid port collisions that may arise with locally running
  software or in between parallel test runs"; ask `getMappedPort`/`getHost` at runtime.
  [networking](https://java.testcontainers.org/features/networking/)

### .NET Aspire

- Proxied endpoints: omitted host port → random ports; a fixed `port` pins the public one; values
  reach the app as environment variables; `WithReference` "Injects a connection string as an
  environment variable". Proxyless dynamic ports from 10000–32767 (13.5+).
  [networking overview](https://aspire.dev/fundamentals/networking-overview/) Exact variable names
  are not on that page: **unverified here**.

### Spring Boot Docker Compose support

- Finds `compose.yml` in the working directory, runs `docker compose up`, creates connection beans
  from image names, "will always discover and use the locally mapped port"; connection details
  override configured properties; lifecycle `none|start-only|start-and-stop`; if already running it
  only connects. Readiness: TCP connect to the mapped port (timeouts configurable, per-container
  opt-out label). Off in tests by default.
  [dev services](https://docs.spring.io/spring-boot/reference/features/dev-services.html)

### Quarkus Dev Services

- Random port unless `quarkus.datasource.devservices.port` is set; "The application is configured
  automatically"; containers found by label and shared in dev mode; GUI users read the port from
  `docker ps`. [dev services](https://quarkus.io/guides/dev-services),
  [database dev services](https://quarkus.io/guides/databases-dev-services)

### Tilt / Skaffold

Not examined: both target Kubernetes-style dev loops; Compose, Aspire and Spring already cover the
host-port question.

## Open questions for the owner

1. Identity: path hash only, or also the git branch like Supabase (a branch switch would then give a
   fresh, empty database)? Recommendation: path only.
2. Where the state lives: `.sandloom/dev.json` (a new gitignored directory) vs a gitignored
   `deploy/.env` that Compose may pick up natively (but the two Compose pages disagree on which
   `.env` is read).
3. Tests: `.env.test.local` overlay loaded by the pytest plugin, or the plugin reads the state file?
   Should `.env.test` stop carrying the framework's 5433/6380 defaults?
4. Keep dev and test services in one compose project (one `up` starts four containers), or split so
   `sandloom test` starts only the test pair?
5. App port: fail with a suggestion (Encore), or a sticky `APP_PORT` chosen like the service ports?
6. Should commands that need the DB start services implicitly (Spring `start-only`), or only point at
   `sandloom dev up`? Implicit start helps humans but surprises an agent that did not ask for
   containers.
7. Migration: existing apps with `name: myapp-dev` keep their data in that project's volume; adopt
   that name on the first `sandloom dev up`, or start fresh?
8. Targets: Docker Desktop only, or also OrbStack/Colima/Podman (Supabase's code has Podman-specific
   port races)? §4's repair should be tested on each.
