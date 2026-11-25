"""Project generator (`sandloom new`).

Scaffolds a *standalone* Sandloom application that consumes the framework as
installed distributions: ``sandloom`` (kernel, ``sandloom.sdk``, CLI) and the
standard module packages chosen with ``--with`` (``sandloom-identity``, ...,
named in ``SANDLOOM_MODULES`` by their entry-point names). The generated project
owns its Python namespace (``<app>``)
and puts business modules under ``<app>.modules.*``. Module code imports only
``sandloom.sdk``, its own package and generated contracts/DTOs. The composition
root uses ``sandloom.sdk.app.create_app``; a minimal app needs no platform package.

What you get is runnable and testable out of the box:

* ``<app>/asgi.py``         — ``app = create_app()`` (the kernel app factory)
* ``<app>/modules/<m>/``    — a sample module (architecture-compliant skeleton)
* ``tests/``                — a smoke test that boots the app
* ``<app>/modules/<m>/migrations/`` — the module's OWN Alembic chain (no
  project-level chain: the framework's core chain ships in the package and
  ``sandloom migrate upgrade-all`` applies core + every module chain per database)
* ``.env`` (random JWT secret, git-ignored) / ``.env.example`` / ``.env.test``
* ``architecture_rules.yml``— import boundaries, enforced by ``sandloom check``
* ``.github/workflows/ci.yml`` / ``.pre-commit-config.yaml`` — run ``sandloom check``,
  lint and tests on every change
* ``Dockerfile`` / ``.dockerignore`` — production image (non-root, gunicorn)
* ``deploy/docker-compose.yml`` — monolith profile (one-shot migrate + app +
  Celery worker + beat via ``sandloom worker`` and ``sandloom beat``)

Usage:
    sandloom new myapp                              # minimal app + sample module
    sandloom new helpdesk --with identity,tenancy,billing
    sandloom new linkly --module links --no-batteries
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import keyword
import re
import secrets
import sys
import tomllib
import uuid
from collections.abc import Sequence
from pathlib import Path
from string import Template

import sandloom.core.cli.add as _add
import sandloom.core.cli.image_secrets as _image_secrets
import sandloom.core.cli.skills as _skills
import sandloom.core.cli.stack_files as _stack_files
import sandloom.core.cli.stack_service as _stack

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_BUNDLED = ("identity", "tenancy", "billing", "admin")

#: Dev-tool pins for generated projects — the SAME versions the framework pins
#: in its own dev extra (test_project asserts they stay in sync), so `sandloom check`,
#: ruff and mypy give an app the verdicts the framework's CI gives.
DEV_TOOL_PINS: tuple[str, ...] = (
    "ruff==0.17.1",
    "mypy==2.4.0",
    "pytest==9.1.1",
    "pytest-asyncio==1.4.0",
    # `sandloom test` runs the suite in parallel (`-n auto --dist loadfile`).
    "pytest-xdist==3.8.0",
    "pre-commit==4.7.0",
)

#: Dev requirement that brings `sandloom check`'s optional deps (PyYAML), the
#: test kit's (pytest plugins) and SQLite's driver (aiosqlite: the app and its
#: tests run on `sqlite+aiosqlite://` without another install) — through the
#: framework's own `check` / `testing` / `sqlite` extras, so their ranges are
#: owned in ONE place.
CHECK_EXTRA_REQ = "sandloom[check,testing,sqlite]"

#: Runtime requirement for the framework's native accelerators (hiredis C
#: reply parser, jsonschema-rs) — pinned once, in the framework's `fast` extra.
FAST_EXTRA_REQ = "sandloom[fast]"

#: The quickstart: ``sandloom new`` prints these lines as its "Next steps" and the
#: generated README repeats them. This is the ONE source of the documented
#: quickstart: ``docs/getting-started/index.md`` is tested against
#: :func:`quickstart` and ``tools/smoke/quickstart_check.py`` (the CI job
#: ``quickstart``) executes the lines ``sandloom new`` prints, verbatim.
QUICKSTART: tuple[tuple[str, str], ...] = (
    ("cd {path}", ""),
    ("uv sync", "the app, the framework, the pinned dev tools"),
    (
        "uv run sandloom generate",
        "snapshots, stubs and DTOs from installed dependencies",
    ),
    ("uv run sandloom dev up", "Postgres + Redis (Docker); writes .env"),
    ("uv run sandloom add module {module}", "a module: discovered, nothing to wire"),
    ("uv run sandloom test", "tests + every gate (never writes)"),
    ("uv run sandloom migrate upgrade-all --apply", "core + module chains"),
    ("uv run sandloom dev run", "serves the app; prints its address"),
)


#: The README's first-administrator step, for an app generated ``--with admin``.
_ADMIN_STEP = """Create the first console administrator. It asks for the password; with
`--invite` it prints a one-time link instead (for agents and CI):

```bash
uv run sandloom admin create --email you@example.com
```

The console is at `/admin/` on the address `sandloom dev run` prints
([guide](https://sandloom.dev/docs/admin-console/)). Cannot sign in? See
which addresses are administrators, then give one a new password:

```bash
uv run sandloom admin list
uv run sandloom admin invite --email you@example.com
```

"""

#: The README's note for an app with identity: development prints email links.
_IDENTITY_NOTE = """
In development the emails identity sends (verification, password reset) are
not mailed: their links appear in the `sandloom dev run` output
(`NOTIFIER_LOG_LINKS=true` in `.env`). Tokens for API calls:
[your first access token](https://sandloom.dev/docs/modules/batteries/first-access-token/).
"""

#: The README's note for an app with no bundled module: the generated route
#: needs a tenant and a token, and ``.env`` already holds the tenant.
_BARE_APP_NOTE = """
This app composes no identity and no tenancy battery. `.env` binds one tenant
for every request (`TENANCY_MODE=single` with a generated `SINGLE_TENANT_ID`;
uncomment the two lines of `.env.example` when you recreate `.env`), so a
tenant-scoped route such as the generated module's needs only a token:
`uv run sandloom dev token` prints one for development
([guide](https://sandloom.dev/docs/modules/batteries/first-access-token/)).
"""

#: The README's note for an app with bundled modules but no tenancy.
_SINGLE_TENANT_NOTE = """
This app composes no tenancy battery, so no token names a tenant: `.env` binds
one tenant for every request (`TENANCY_MODE=single` with a generated
`SINGLE_TENANT_ID`; uncomment the two lines of `.env.example` when you
recreate `.env`). Add the tenancy battery and switch to `TENANCY_MODE=multi`
for several tenants.
"""

#: The tenant lines of an app with no tenancy module (``--no-batteries``, or
#: identity / billing / admin without tenancy): no module grants a tenant (a
#: login token carries no ``tenant_ids``), so under the default
#: ``TENANCY_MODE=multi`` a
#: tenant-scoped route (the generated module's) answers 400 ``tenant_required``
#: and, with ``X-Tenant-Id``, 403 ``tenant_not_authorized`` for every token.
#: Single mode binds one tenant for every request, so ``sandloom dev token``
#: reaches the route. ``$tenant_id`` is a random UUID per app. The local
#: ``.env`` writes the lines; ``.env.example`` (the file a production deploy
#: copies) keeps them commented, as production decides its own tenancy.
_SINGLE_TENANT_ENV = Template(
    """
# ==== Tenancy ====
# This app composes no tenancy module, so no token can name a tenant: one
# tenant is bound for every request (a tenant-scoped route needs it). Add the
# tenancy battery and switch to TENANCY_MODE=multi for several tenants.
${prefix}TENANCY_MODE=single
${prefix}SINGLE_TENANT_ID=$tenant_id
"""
)

#: The README's note for an app with billing: ``sandloom dev run`` serves the
#: web process only, and billing's reconciler is a periodic job.
_BILLING_NOTE = """
With billing, `sandloom dev run` serves the web process only: start
`uv run sandloom worker` and `uv run sandloom beat` in two more terminals to
run its reconciler (until a run completes, checkout and portal answer 503 and
`/health` reports `never_ran`).
"""

#: The ``.env`` line that prints email links (verification, password reset)
#: to the console in local development; production refuses it.
_LOG_LINKS = """# Local development only: print the links an email carries ([dev only]).
# NOTIFIER_LOG_LINKS=true
"""

#: The next step ``sandloom new --with admin`` prints after the quickstart.
ADMIN_NEXT_STEP = (
    "uv run sandloom admin create --email you@example.com"
    "   # the first console administrator (--invite: a one-time link)"
)


def _quickstart_section(lines: list[str], *, admin: bool) -> str:
    """The README's quick start: one block, or the admin step before the
    last line (serving the app) when the app has the console."""
    if not admin:
        return "```bash\n" + "\n".join(lines) + "\n```\n"
    return (
        "```bash\n"
        + "\n".join(lines[:-1])
        + "\n```\n\n"
        + _ADMIN_STEP
        + "```bash\n"
        + lines[-1]
        + "\n```\n"
    )


def quickstart_module(sample_module: str) -> str:
    """The module the quickstart adds (never the sample module's name)."""
    return "tasks" if sample_module == "notes" else "notes"


def quickstart(
    app: str, sample_module: str = "example", *, path: str | None = None
) -> list[str]:
    """The quickstart lines for ``app`` (``path``: the ``cd`` target)."""
    values = {
        "app": app,
        "path": path or app,
        "module": quickstart_module(sample_module),
    }
    commands = [command.format(**values) for command, _ in QUICKSTART]
    # Comments align on the commands that carry one: the `cd` line has none,
    # and its (absolute) path must not push every comment off to the right.
    width = (
        max(
            len(command)
            for command, (_, note) in zip(commands, QUICKSTART, strict=True)
            if note
        )
        + 2
    )
    return [
        f"{command:<{width}}# {note}" if note else command
        for command, (_, note) in zip(commands, QUICKSTART, strict=True)
    ]


# --------------------------------------------------------------------------- #
# File templates ($-substitution; generated code keeps its literal braces)
# --------------------------------------------------------------------------- #

_PYPROJECT = Template(
    """[project]
name = "$app"
version = "0.1.0"
description = "A Sandloom application."
requires-python = ">=3.11"
dependencies = [
$framework_deps
]

[project.optional-dependencies]
# Native accelerators (hiredis C reply parser, jsonschema-rs) — the engines the
# production image runs (the Dockerfile installs `--extra fast`); the framework
# falls back to pure Python without them.
fast = ["$fast_req"]
# Exact pins (matching the framework's) so every machine, pre-commit and CI
# lint / type-check / `sandloom check` with the same tool versions. Includes `fast`
# so tests exercise the production engines.
dev = [
$dev_pins
]

[dependency-groups]
# `uv sync` installs the dev group by default — it never strips the tools.
dev = ["$app[dev]"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["$app"]

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["$app", "tests"]
python_files = ["test_*.py"]
# `-p sandloom.sdk.testing.plugin`: the framework's test harness and the 1.0 kit —
# loads .env.test before anything imports the framework, gives every module's
# tests the client / db fixtures, records `published(...)` events and resolves
# `Contract(...)` through the test app (no conftest.py needed).
addopts = [
    "-v", "--tb=short", "--asyncio-mode=auto",
    "-p", "sandloom.sdk.testing.plugin",
]
asyncio_mode = "auto"
# A test still running after 300 s dumps every thread's stack.
faulthandler_timeout = 300
# A deprecated call (in the app or a library it uses) fails the suite.
filterwarnings = ["error::DeprecationWarning"]

[tool.ruff]
fix = true
force-exclude = true
# `_generated/` holds copies `sandloom generate` lays out itself (ADR-0013).
extend-exclude = ["$app/modules/*/migrations", "**/_generated"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "C4", "UP", "C901"]
ignore = ["B008"]

[tool.ruff.lint.mccabe]
max-complexity = 10

[tool.ruff.lint.isort]
# Framework imports are grouped like first-party (matching sandloom add output).
known-first-party = ["sandloom", "$app"]

[tool.ruff.lint.per-file-ignores]
"**/tests/**" = ["E501", "C901"]

[tool.mypy]
strict = true
plugins = ["pydantic.mypy"]
exclude = ["/migrations/", ".*/tests/.*"]
"""
)

_AGENTS_HEADER = Template(
    """<!-- $app: a Sandloom application (Public API 1.0). -->
# $app — agent map

`$app` is a Sandloom application: business modules live in `$app/modules/`
(sample: `$module`), `$app/asgi.py` is the composition root and loads every
module of `$app/modules` (no list to edit). From the project root:

- a new module: `uv run sandloom add module <name>` (also `sandloom add event|contract|
  consumer|saga|projection <module> ...`);
- a schema change: `uv run sandloom migrate new <module> "<message>"`;
- the generated files (snapshots, contract stubs and DTO copies, event schemas):
  `uv run sandloom generate` (`sandloom add` runs it last);
- the one gate: `uv run sandloom test` (tests, lint, types, `sandloom check`, generated
  files checked, never written; `sandloom explain <SANDLOOM-code>` says how to fix
  a failure);
- another app: `sandloom new <name> --yes`. An agent always passes `--yes`
  (or `--no-input`): at a terminal `sandloom new` asks questions otherwise.

A module imports only `sandloom.sdk` (plus generated contract stubs and DTO copies
in `$app.shared`) and its own package; a provider's contract DTOs live in its own
`schemas.py`. The beginner names, all you need for
CRUD, events and cross-module calls:

| need | names (`from sandloom.sdk import ...`) |
| --- | --- |
| a module | `define_module` |
| tables | `Base`, `TenantScoped`, `db`, `new_id` |
| times | `CreatedAt`, `UpdatedAt`, `UtcDateTime` |
| routes | `module_router`, `public`, `CurrentUser` |
| refusals | `NotFound`, `Conflict`, `Refused` |
| events | `Event`, `on` |
| contracts | `contract`, `command`, `Contract` |
| authorization | `require`, `Account` |
| effects | `record_audit`, `on_commit`, `bind_tenant` |
| tests (`sandloom.sdk.testing`) | `client` (fixture), `as_user`, `published`, |
| | `deliver`, `fake_contract` |

These 29 names need no marker in any file. Twelve more need none in the file
`sandloom add` writes them into: `Saga`, `SagaContext`, `step`, `local_step`,
`start_saga` (`sandloom.sdk.sagas`) in `sagas.py`; `Projection`, `SourceGroup`,
`full_state`, `tombstone` (`sandloom.sdk.projections`) in `projections.py`;
`task`, `periodic` (`sandloom.sdk.tasks`) and `open_uow` (`sandloom.sdk.data`)
in `tasks.py`. Any other SDK name, or one of these in another file, is advanced:
mark the import `# sandloom: advanced <reason>`.
The billing and tenancy gates (`Subscribed`, `SubscriptionActive`, `Credits`,
`OrgRole`) are not sdk names: `sandloom generate` writes them beside the
consumer's contract stub, and a route imports them from
`$app.shared.contracts.billing_entitlements_v1` or
`$app.shared.contracts.tenancy_membership_v1`.
Ids and times: `default=new_id` (UUIDv7) and `created_at: Mapped[CreatedAt]`
(`clock_timestamp()`), so a list ordered by `created_at, id` never ties; any
other time column is `UtcDateTime()` (SANDLOOM-C029). A refusal is
`raise NotFound("...")`, `Conflict("...")` or `Refused(422, "...")`, with no
FastAPI import.

Routes declare nothing but their exceptions: every route is authenticated
(`CurrentUser`, or a gate) unless it says `public(reason="...")`; writes get
the commit-time session check, automatic idempotency and the audit of
sensitive effects from the framework (there is no per-route class). A billing
gate names the account it checks, `require(Subscribed(of=Account.user()))` or
`of=Account.org(param="org_id")` on an organization route
(`from sandloom.sdk import Account`).

How-tos with tested code: `uv run sandloom docs recipes`. What you can call:
`uv run sandloom docs contracts`, `... events`, `... <contract or channel>`.
Skills ($skill_dirs): sandloom-module-context, sandloom-new-module,
sandloom-quality-gates, sandloom-cross-module, sandloom-background-tasks,
sandloom-module-rules, sandloom-event-version-roll, sandloom-split-deploy.
Every module follows the contract in `$app/modules/AGENTS.md`.
Before changing a module: `uv run sandloom context <name>`; discover connections
with `uv run sandloom graph <name> --hops 1`. Refresh: `sandloom add skills`.

"""
)

_README = Template(
    """# $app

An application built on **Sandloom**, the Python backend framework for
AI-driven development.

The framework is supplied by the `$framework_distribution` dependencies.
This project owns only
its business logic — its modules live under `$app/modules/*` and import the
framework's public API (`sandloom.sdk`). The ASGI app is the framework's
`create_app`; it loads every module of `$app/modules` (set `SANDLOOM_MODULES` to
select a subset).

## Quick start

From the project root:

$quickstart_section
`uv sync` installs the app, the framework and the pinned dev tools (ruff,
mypy, pytest, pytest-xdist); `pre-commit install` adds the commit hooks. The
API reads `.env` (generated with a random `JWT_SECRET`; recreate it from
`.env.example` if you delete it), the tests read `.env.test`.

The app answers on the address `sandloom dev run` prints: port 8000, or the
next free one when 8000 was taken at the first `dev up`. The port is kept
for this checkout, so the address does not change between runs.
$identity_note$billing_note
### The dev services

`uv run sandloom dev up` starts Postgres and Redis in Docker for this
repository and gives this checkout its own database. It picks free ports
once and keeps them. It writes the app's URLs into the marked block of
`.env` (`DATABASE_URL` and an `INFRA` line with one Redis url per instance:
the cache and broadcast on one index, the security store, beat lease, event
streams and Celery broker on another) and the test URLs into `.env.test.local`
(not committed). An `INFRA` line of your own in `.env` is merged into the
block's, instance by instance: your keys win. It is safe to run again: a
second run waits for the first and then finds everything up.

A git worktree of this repository uses the same containers with its own
database: run `uv run sandloom dev up` in the worktree too. Each test run
gets its own test database, so two `sandloom test` runs never clash.

```bash
uv run sandloom dev status     # ports, URLs (paste them into a database GUI)
uv run sandloom dev run --port auto   # a free port now, when the app's own is taken
uv run sandloom dev reset      # empty this checkout's database
uv run sandloom dev down       # stop the containers (the data stays)
uv run sandloom dev prune      # remove databases of deleted worktrees
```

### SQLite instead of PostgreSQL (development and tests)

The app also runs on a SQLite file; `uv sync` installs its driver. Set the
database in `.env`, outside the `sandloom dev` block, with a Redis of your own
(SQLite replaces PostgreSQL only), one explicit url per Redis instance of
`$app/__init__.py` (`DEFAULT_INFRA`), the persistent ones on another database
than the cache:

```bash
DATABASE_URL=sqlite+aiosqlite:///./data/app.db
INFRA='$infra_sqlite'
```

`uv run sandloom dev run` then serves the app on that file and starts no dev
services; `uv run sandloom dev up` starts nothing and says so. The app
builds its tables from the models at startup; there is no
`sandloom migrate` on SQLite (SANDLOOM-B028), and after a model change
`dev run` sets the old file aside and builds a fresh one: restart any
worker, beat or admin process on the file then (each refuses the replaced
file, SANDLOOM-B028). Tenant isolation
is the ORM guard only (no row security), and writers take turns. Production
needs PostgreSQL (SANDLOOM-B029), and CI runs the tests on PostgreSQL: keep it
so. On SQLite a contract command joins its caller's transaction, while on
PostgreSQL it commits on its own (the tests fail on `JoinedUnitRolledBack`
where code relies on the difference).

The tests run on SQLite with `SANDLOOM_TEST_DATABASE_URL`; a test that needs
a feature SQLite lacks skips (`skip_unless_db_feature`), and the migration
checks skip, each saying why: the chain coverage of `schema & deploy`
(SANDLOOM-T002), the migrations-match-models tests and `migration drift`
(SANDLOOM-T006). A new model or column passes there without a revision.
Before production, write its revision against PostgreSQL (`sandloom migrate
new <module> "<message>"`); those checks run there, and `sandloom migrate
upgrade-all` refuses a chain without it (SANDLOOM-T002):

```bash
SANDLOOM_TEST_DATABASE_URL=sqlite+aiosqlite:///./data/app_test.db uv run sandloom test
```

## Architecture checks

```bash
sandloom check            # module isolation rules against architecture_rules.yml
```

`sandloom check` runs the framework's own architecture analyzers over
`$app/modules/*`: no sibling-module imports, only the framework public API
(`sandloom.sdk`, `sandloom.shared.*`; never `sandloom.core.*`),
no ORM in endpoints, endpoints only in `api/`, no `db.commit()`, no raw
backend SDKs. CI (`.github/workflows/ci.yml`) and the pre-commit hook run it.

## Deploy (monolith)

```bash
export JWT_SECRET=$$(openssl rand -hex 32) INTERNAL_API_TOKEN=$$(openssl rand -hex 32)
docker compose -f deploy/docker-compose.yml up --build
```

The `migrate` service runs `sandloom migrate upgrade-all --apply` to completion
before the app starts; `worker` / `beat` run the framework task commands
(`sandloom worker`, `sandloom beat`), which register the `tasks.py` of
every loaded module.

## Deploy as microservices (no code change)

```bash
uv run sandloom add deploy <module>... --service <name>   # once per service
uv run sandloom add deploy --check                        # verify and boot services
uv run sandloom keys dev-env --out deploy/.env.microservices   # DEVELOPMENT keys only
docker compose --env-file deploy/.env.microservices \
  -f deploy/compose/microservices.yml up --build
```

The first `sandloom add deploy` creates `deploy/compose/microservices.yml`
and its nginx gateway from the framework's templates; every run derives the
services, their databases, bindings, RPC allowlist and gateway routes from
what the modules provide and consume. `uv run sandloom docs recipe split` has the
details.

## Add a module

```bash
sandloom add module <name>     # a new package under $app/modules (+ sandloom generate)
sandloom test                  # every gate (checks the generated files, never writes)
```

## Migrations

Per-module-per-database, exactly like the framework: there is NO project-level
Alembic chain. The framework's core chain (outbox / audit / consumer inbox)
ships inside the `sandloom` package, and each module owns its own chain in
`$app/modules/<m>/migrations/` (the scaffold includes a baseline revision).

```bash
sandloom migrate upgrade-all            # dry run: show the plan
sandloom migrate upgrade-all --apply    # core chain -> every hosting DB, then each
                                   # module's chain -> the DB it declares
```

Add a revision to a module's chain with `sandloom migrate new <module> "description"`
(autogenerated, with row-level security for new tenant tables; the owning
database must be at the chain's head).

## Framework dependency source

`uv sync` honors `[tool.uv.sources]` when generated from a source checkout.
Use `--framework-source path --framework-path /path/to/sandloom`, where the
path is the repository root containing `packages/`. These editable dependencies
use the live checkout; host paths are unavailable in Docker or remote CI.
The canonical source is [Sandloom-dev/sandloom](https://github.com/Sandloom-dev/sandloom).
`--framework-source pypi` uses the published distributions.
`--framework-source git` is disabled; use local editable paths for source
development, or a complete matching wheel set for a portable deployment.
Commit `uv.lock` after resolving dependencies and use
`uv sync --locked` in subsequent CI runs.

## Loaded modules

Every module of `$app/modules` (plus the bundled ones: `$bundled_json`);
`SANDLOOM_MODULES` selects a subset.
"""
)

#: One line per record in `.sandloom/inspection.json`: two branches that each add a
#: module add different lines, and `union` keeps both where they land together.
#: git applies it on a local merge (GitHub's merge button ignores merge drivers).
#: `text eol=lf` keeps the lines LF in a checkout with `core.autocrlf`.
_GITATTRIBUTES = """# One record per line (`sandloom generate`): branches merge.
.sandloom/inspection.json merge=union text eol=lf
"""

_GITIGNORE = """.sandloom/cache/
# `sandloom dev`: state, locks and run files (.sandloom/agent-files.json stays)
.sandloom/dev.json
.sandloom/dev.env
.sandloom/docker-compose.dev.yml
.sandloom/*.lock
.sandloom/run/
.env.test.local
__pycache__/
*.py[cod]
.venv/
.env
# `sandloom keys dev-env --out deploy/.env.microservices`: development keys
.env.microservices
data/
test_data/
*.egg-info/
.pytest_cache/
.ruff_cache/
.mypy_cache/
.observed_effects.json
.observed_effects.json.lock
"""


_ENV_EXAMPLE = Template(
    """# ==== Modules loaded by this process ====
# Default: every module of $app/modules (and the bundled ones). Set to select
# a subset: SANDLOOM_MODULES=["$app.modules.<name>", ...]
# SANDLOOM_MODULES=
# Generated contract stubs and DTO copies live here (importable by every process).
CONTRACT_DTO_PACKAGES=["$app.shared"]
# Composition root booted by sandloom migrate, the worker and the `sandloom` ops CLIs.
SANDLOOM_COMPOSITION_ROOT=$app.asgi

# ==== Environment ====
ENVIRONMENT=dev
$tenancy
# ==== Database and Redis instances ====
# `uv run sandloom dev up` writes DATABASE_URL and INFRA (one Redis url per
# instance) of this checkout's dev services into the block below; everything
# else is yours. Your own INFRA line (instances beyond the Redis ones, an
# option) is merged into the block's: your keys win.
$dev_block
# ==== Event bus ====
# The app selects Redis Streams ($app/__init__.py: DEFAULT_INFRA), which a
# deploy runs on. Development names the in-process bus here, explicitly: it
# reaches no other process and loses events on restart (never use it in prod).
EVENT_BUS_BACKEND=memory

# ==== Storage ====
# STORAGE_TYPE stays unset: the object store is local files unless the app's
# composition or INFRA selects another driver.
STORAGE_PATH=./data

# ==== Security ====
# Production startup rejects short / placeholder secrets. Generate one with
#   openssl rand -hex 32
JWT_SECRET=$jwt_secret
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=14

# ==== Ops (unlocks /metrics, /diagnostics, /health details; never /_sandloom/rpc) ====
# Internal endpoints are CLOSED unless a token is set (>= 32 chars in prod).
# INTERNAL_API_TOKEN=
# Local development only: open them without a token (ignored in prod).
$open_internal
$log_links"""
)

_ENV_TEST = Template(
    """# ==== Test environment (loaded by the sandloom.core.testing pytest plugin) ====
ENVIRONMENT=test
CONTRACT_DTO_PACKAGES=["$app.shared"]
SANDLOOM_COMPOSITION_ROOT=$app.asgi

# CI's test services (.github/workflows/ci.yml: Postgres :5433, Redis :6380).
# Locally, `uv run sandloom dev up` writes .env.test.local (not committed),
# which points the tests at this repository's test services instead.
DATABASE_URL=postgresql+asyncpg://test_user:test_password@127.0.0.1:5433/sandloom_test
INFRA='$infra_test'

# The tests run on the in-process bus, named explicitly (DEFAULT_INFRA selects Streams).
EVENT_BUS_BACKEND=memory

STORAGE_PATH=./test_data

JWT_SECRET=test_super_secret_key_for_testing_only
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=14
"""
)

_APP_INIT = Template(
    '''"""$app — a Sandloom application package."""

__version__ = "0.1.0"

#: The drivers this app runs on, ``{capability: {instance: {"driver": ...}}}``:
#: the kernel selects none, so the app does (``create_app(default_infra=...)``,
#: and ``sandloom worker check`` reads it here). ``INFRA`` overrides it per
#: instance. ``memory`` is never selected for you: development names it in
#: ``.env`` (``EVENT_BUS_BACKEND=memory``), production uses the Streams bus.
DEFAULT_INFRA = {
    "database": {"default": {"driver": "sqlalchemy"}},
    "cache": {"default": {"driver": "redis"}},
    "eventbus": {"default": {"driver": "streams"}},
    "broadcast": {"default": {"driver": "redis"}},
    "taskqueue": {"default": {"driver": "celery"}},
    "atomic_store": {"security": {"driver": "redis"}},
    "lease": {"beat": {"driver": "redis"}},
}
'''
)

_ASGI = Template(
    '''"""ASGI entry point (the composition root).

Loads every module of ``$app.modules`` (found statically: each subpackage
whose ``__init__.py`` defines ``register``) plus the bundled ones, so adding
a module edits nothing here. ``SANDLOOM_MODULES`` selects a subset.

    uvicorn $app.asgi:app --reload
"""

from sandloom.sdk.app import (  # sandloom: advanced app
    Authority,
    app_modules,
    create_app,
)

from . import DEFAULT_INFRA

BUNDLED: list[str] = $bundled_json
$billing_authority
#: Who may write each protected token claim, and the module that issues
#: tokens (ADR-0014): code, never a setting. A contributor writing a protected
#: claim it is not assigned loses its whole result; startup refuses a map the
#: composition cannot honour (`sandloom explain SANDLOOM-B035`).
CLAIM_AUTHORITIES: dict[str, str | Authority] = $claim_authorities

$create_app'''
)


def _dict_literal(mapping: dict[str, str]) -> str:
    """Render a dictionary of Python expressions in Ruff's multiline format."""
    if not mapping:
        return "{}"
    lines = "".join(f'    "{key}": {value},\n' for key, value in mapping.items())
    return "{\n" + lines + "}"


def _create_app_call(app: str, width: int = 88) -> str:
    """``app = create_app(...)`` as ``ruff format`` lays it out for this name
    (the app's own format gate checks it): one line when it fits, else
    wrapped, then one argument per line, then the list exploded too."""
    title = f'title="{app}"'
    modules = f'default_modules=[*BUNDLED, *app_modules("{app}.modules")]'
    authorities = "claim_authorities=CLAIM_AUTHORITIES"
    infra = "default_infra=DEFAULT_INFRA"
    forms = [
        f"app = create_app({title}, {modules}, {authorities}, {infra})\n",
        f"app = create_app(\n    {title}, {modules}, {authorities}, {infra}\n)\n",
        f"app = create_app(\n    {title},\n    {modules},\n    {authorities},\n"
        f"    {infra},\n)\n",
    ]
    exploded = (
        f"app = create_app(\n    {title},\n    default_modules=[\n        *BUNDLED,\n"
        f'        *app_modules("{app}.modules"),\n    ],\n    {authorities},\n'
        f"    {infra},\n)\n"
    )
    forms.append(exploded)
    for form in forms:
        if all(len(line) <= width for line in form.splitlines()):
            return form
    return exploded.replace(
        f'        *app_modules("{app}.modules"),\n',
        f'        *app_modules(\n            "{app}.modules"\n        ),\n',
    )


_MODULES_INIT = Template(
    '''"""Business modules for the $app application.

Each module is atomic and isolated: it imports only ``sandloom.sdk`` (plus the
generated contract stubs and DTO copies of ``$app.shared``) and its own package,
and talks to other modules through typed events and contracts.
"""
'''
)

_TEST_SMOKE = Template(
    '''"""Smoke test: the application boots and wires its modules.

Touches no database — it only proves the ASGI app assembles, the framework
ops endpoints respond, and the sample module's router is mounted behind auth.
"""

import httpx
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_app_boots_and_health_ok() -> None:
    from $app.asgi import app

    async with AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health/simple")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_sample_module_router_mounted() -> None:
    from $app.asgi import app

    async with AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # The sample module's create endpoint exists and authenticates (401),
        # which proves the module was discovered and its router mounted.
        resp = await client.post("/api/v1/$module/items", json={"title": "x"})
        assert resp.status_code == 401
'''
)

_ARCH_RULES = Template(
    """# Import-boundary manifest for the $app application — enforced by
#
#     sandloom check            (also in .github/workflows/ci.yml + pre-commit)
#
# Business modules under $app.modules.* are atomic: each may import only the
# framework public API below, sandloom.shared.* and its own package — never a
# sibling module or an installed module package (sandloom_identity, ...).
# Cross-module interaction goes through versioned events or contracts.
version: 1

# Directory holding the business-module packages, and its dotted package.
modules_path: $app/modules
modules_package: $app.modules
# sys.path root the app package is importable from (resolves relative imports).
source_root: .

# The framework's ENFORCED public API for business modules: sandloom.sdk (Public
# API 1.0). Never list a sandloom.core module: `sandloom check` refuses one.
public_api:
  - sandloom.sdk
  - sandloom.sdk.app
  - sandloom.sdk.authz
  - sandloom.sdk.contracts
  - sandloom.sdk.data
  - sandloom.sdk.effects
  - sandloom.sdk.errors
  - sandloom.sdk.events
  - sandloom.sdk.infra
  - sandloom.sdk.migrations
  - sandloom.sdk.module
  - sandloom.sdk.projections
  - sandloom.sdk.sagas
  - sandloom.sdk.security
  - sandloom.sdk.tasks
  - sandloom.sdk.tenant
  - sandloom.sdk.testing
  - sandloom.sdk.testing.fixtures
  - sandloom.sdk.tooling
  - sandloom.sdk.web
# Further framework packages a module may import.
shared_api:
  - sandloom.shared
  - $app.shared
# Generated contract stubs, DTO copies and exported event schemas
# (sandloom generate writes them).
shared_package: $app.shared

# Extra glob patterns to forbid (e.g. "$app.modules.*._internal*").
forbidden_patterns:
  # sandloom.sdk's private layers: import the public sandloom.sdk modules.
  - "sandloom.sdk._*"
  - "sandloom.sdk.*._*"

# Justified, file-scoped exceptions: {file: <glob relative to this file>,
# imports: [<glob>, ...]}. Keep it empty — every entry is a hole.
allowlist: []
"""
)

_DEV_COMPOSE = Template(
    """# Local-only development and disposable test services, started by
#   uv run sandloom dev up
# which names the compose project per repository and fills the host ports
# (chosen once, free on this machine). A plain `docker compose up` stops at
# the first missing port.
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: sandloom
      POSTGRES_PASSWORD: sandloom
      POSTGRES_DB: $app
    ports: ["127.0.0.1:$${SANDLOOM_DEV_POSTGRES_PORT:?$port_hint}:5432"]
    volumes: ["dev_postgres:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sandloom"]
      interval: 2s
      timeout: 2s
      retries: 20
$kv_dev
  postgres_test:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: test_user
      POSTGRES_PASSWORD: test_password
      POSTGRES_DB: sandloom_test
    ports: ["127.0.0.1:$${SANDLOOM_DEV_TEST_POSTGRES_PORT:?$port_hint}:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U test_user"]
      interval: 2s
      timeout: 2s
      retries: 20
$kv_dev_test
volumes:
  dev_postgres:
"""
)

_DOCKER_COMPOSE = Template(
    """# Monolith profile for $app: one process loads every module.
# The same code runs as microservices (no code change): `sandloom add deploy
# <module>... --service <name>` writes deploy/compose/microservices.yml
# and its gateway (`sandloom docs recipe split`).
#
#   docker compose -f deploy/docker-compose.yml up --build
#
# Requires JWT_SECRET and INTERNAL_API_TOKEN in the environment (or a .env next
# to this file): openssl rand -hex 32

x-app-env: &app-env
  CONTRACT_DTO_PACKAGES: '["$app.shared"]'
  # Explicit: ENVIRONMENT defaults to dev, which must never reach a deploy.
  ENVIRONMENT: prod
  DATABASE_URL: postgresql+asyncpg://sandloom:sandloom@db:5432/$app
  # One explicit url per Redis instance: the cache and broadcast on /1, what
  # must survive (security store, beat lease, event streams, Celery broker) on
  # /2, never on the cache's database.
  INFRA: '$infra_compose'
  SHUTDOWN_BUDGET_SECONDS: 30
  GUNICORN_GRACEFUL_TIMEOUT: 45
  # The weaker guarantees this stack accepts (logged at every start):
  # single-host local storage, persisted below — use a driver plugin for
  # replicas on hosts without a shared filesystem. A demo on the fake PSP adds
  # billing.fake_psp_in_prod (comma-separated).
  SANDLOOM_ACCEPTED_RISKS: $${SANDLOOM_ACCEPTED_RISKS:-local_storage_in_prod}
  JWT_SECRET: $${JWT_SECRET:?set JWT_SECRET (openssl rand -hex 32)}
  INTERNAL_API_TOKEN: $${INTERNAL_API_TOKEN:?set INTERNAL_API_TOKEN}

$billing_env
x-app-build: &app-build
  # The project root (one level up from deploy/) holds the Dockerfile.
  context: ..
  dockerfile: Dockerfile

x-app-runtime: &app-runtime
  read_only: true
  cap_drop: [ALL]
  security_opt: ["no-new-privileges:true"]
  tmpfs:
    - /tmp:rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=0700

services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: sandloom
      POSTGRES_PASSWORD: sandloom
      POSTGRES_DB: $app
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sandloom"]
      interval: 5s
      timeout: 3s
      retries: 10
$kv_prod

  # One-shot schema migration: runs to completion before the app starts.
  migrate:
    <<: *app-runtime
    build: *app-build
    command: ["sandloom", "migrate", "upgrade-all", "--apply"]
    environment:
      <<: *app-env
      SANDLOOM_COMPOSITION_ROOT: $app.asgi
    depends_on:
      db:
        condition: service_healthy
    restart: "no"

  app:
    <<: *app-runtime
    build: *app-build
    volumes: ["app_data:/app/data"]
    environment:
      <<: *app-env
      # Workers x (DB_POOL_SIZE + DB_MAX_OVERFLOW) must stay under Postgres
      # max_connections; the image caps the default at min(2*cpus+1, 4).
      WEB_CONCURRENCY: $${WEB_CONCURRENCY:-2}
      DB_POOL_SIZE: $${DB_POOL_SIZE:-5}
      DB_MAX_OVERFLOW: $${DB_MAX_OVERFLOW:-5}
    ports:
      - "8000:8000"
    depends_on:
      db:
        condition: service_healthy
      $kv_name:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
    restart: unless-stopped
    stop_grace_period: 60s
    deploy:
      resources:
        limits:
          cpus: "1.0"
          memory: 512m

  # Celery worker: the FRAMEWORK entrypoint boots $app.asgi (the same modules)
  # and registers each loaded module's tasks.py on the taskqueue's app.
  worker:
    <<: *app-runtime
    build: *app-build
    volumes: ["app_data:/app/data"]
    command: ["sandloom", "worker", "--loglevel=INFO"]
    environment:
      <<: *app-env
      SANDLOOM_COMPOSITION_ROOT: $app.asgi
      DB_POOL_SIZE: $${DB_POOL_SIZE:-5}
      DB_MAX_OVERFLOW: $${DB_MAX_OVERFLOW:-5}
    healthcheck:
      disable: true
    depends_on:
      $kv_name:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
    restart: unless-stopped
    stop_grace_period: 60s
    deploy:
      resources:
        limits:
          cpus: "0.5"
          memory: 384m

  # Exactly ONE beat per deployment (it schedules the modules' periodic tasks).
  beat:
    <<: *app-runtime
    build: *app-build
    volumes: ["app_data:/app/data"]
    command: ["sandloom", "beat", "--loglevel=INFO",
              "--schedule=/tmp/celerybeat-schedule"]
    environment:
      <<: *app-env
      SANDLOOM_COMPOSITION_ROOT: $app.asgi
    healthcheck:
      disable: true
    depends_on:
      $kv_name:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
    restart: unless-stopped
    stop_grace_period: 60s
    deploy:
      resources:
        limits:
          cpus: "0.25"
          memory: 256m

volumes:
  postgres_data:
  $kv_volume:
  app_data:
"""
)

# The starter environment of a bundled module ``sandloom new`` was asked for,
# written before the package is installed (edge row B-N6): a copy of the
# ``deploy`` lines its settings declare (``module_env``, what ``sandloom add
# deploy`` writes from the installed package), kept equal to them by
# ``tests/integration/with_batteries/test_starter_env_declared.py``.
# identity refuses to start in prod without a dedicated 2FA encryption key.
_IDENTITY_ENV: tuple[str, ...] = (
    "# identity encrypts TOTP secrets with a DEDICATED key (no JWT_SECRET",
    "# fallback in prod); comma-separated, newest first, for rotation.",
    "TWOFA_ENCRYPTION_KEY: ${TWOFA_ENCRYPTION_KEY:?set TWOFA_ENCRYPTION_KEY "
    "(openssl rand -hex 32) - required when identity is loaded}",
    "# Refresh-session lifetime: identity's setting (the issuer's sessions).",
    "REFRESH_TOKEN_EXPIRE_DAYS: ${REFRESH_TOKEN_EXPIRE_DAYS:-14}",
)

# identity and tenancy REQUIRE an email notifier; the default log driver
# delivers none, so in prod the app refuses to start on it.
_NOTIFIER_ENV = """\
  # identity / tenancy send mail (recovery, invitations) and REQUIRE an email
  # notifier: with ENVIRONMENT=prod the default `log` driver (no mail) is
  # refused at startup. Production: NOTIFIER_BACKEND=smtp with the options of the
  # `notification` instance in INFRA (host, from_addr, username / password over
  # use_tls, which stays on): INFRA: '{"notification": {"default": {"driver":
  # "smtp", "host": "...", "from_addr": "..."}}}'. A demo
  # without mail adds log_notifier_in_prod to SANDLOOM_ACCEPTED_RISKS (mail is
  # NOT delivered).
  NOTIFIER_BACKEND: ${NOTIFIER_BACKEND:-}
"""

# Billing refuses the fake PSP in prod, so a batteries app must choose.
_BILLING_ENV: tuple[str, ...] = (
    "# billing refuses the fake PSP in prod: the operator must choose.",
    "BILLING_PROVIDER: ${BILLING_PROVIDER:?set BILLING_PROVIDER (stripe, or "
    "fake with SANDLOOM_ACCEPTED_RISKS="
    "local_storage_in_prod,billing.fake_psp_in_prod for demos)}",
    "STRIPE_SECRET_KEY: ${STRIPE_SECRET_KEY:-}",
    "STRIPE_WEBHOOK_SECRET: ${STRIPE_WEBHOOK_SECRET:-}",
    "# Past-due policy: billing's setting (other services read it through the",
    "# access fact billing describes, never from their own environment).",
    "BILLING_DENY_ON_PAST_DUE: ${BILLING_DENY_ON_PAST_DUE:-false}",
)
# The console is off in production by default.
_ADMIN_ENV: tuple[str, ...] = (
    "# The admin console is off in production unless switched on here.",
    "ADMIN_CONSOLE_ENABLED: ${ADMIN_CONSOLE_ENABLED:-false}",
)


def _single_tenant_env(tenant_id: str, *, local: bool) -> str:
    """The tenancy block of an app without a tenancy module: live lines in the
    local ``.env``, commented ones in ``.env.example``."""
    return _SINGLE_TENANT_ENV.substitute(
        prefix="" if local else "# ", tenant_id=tenant_id
    )


def _env_text(lines: tuple[str, ...]) -> str:
    """Declared ``deploy`` lines as the monolith profile's ``x-app-env``
    entries (indented as ``sandloom add deploy`` indents them)."""
    return "".join(f"  {line}\n" for line in lines)


_CI_WORKFLOW = Template(
    """# CI for $app: architecture rules, lint and tests on every change.
name: CI

on:
  push:
    branches: ["main"]
  pull_request:

permissions:
  contents: read

jobs:
  checks:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_DB: sandloom_test
          POSTGRES_USER: test_user
          POSTGRES_PASSWORD: test_password
        ports:
          - 5433:5432
        options: >-
          --health-cmd "pg_isready -U test_user"
          --health-interval 5s
          --health-timeout 3s
          --health-retries 10
$kv_ci
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v10.2.0
        with:
          python-version: "3.12"
          version: "0.13.4"
      - name: Install
        run: |
          uv sync
      # --full: the slow tests run too (without it a suite of only slow tests
      # collects nothing and pytest exits 5).
      - name: The gate (tests, ruff, mypy, sandloom check, generated files)
        run: uv run sandloom test --ci --full
"""
)

_PRE_COMMIT = """# Local gates for this app (install with `pre-commit install`).
repos:
-   repo: local
    hooks:
    -   id: sandloom-check
        name: sandloom check (module isolation)
        entry: sandloom check
        language: system
        files: (\\.py|architecture_rules\\.yml)$
        pass_filenames: false
    -   id: ruff
        name: ruff
        entry: ruff check
        language: system
        types: [python]
"""

_DOCKERFILE = Template(
    """# syntax=docker/dockerfile:1.7
# Production image for $app (mirrors the Sandloom framework image): a virtualenv
# (/opt/venv) built by uv, byte-compiled, copied into a clean runtime with no
# uv / pip, running as a non-root user.
ARG PYTHON_IMAGE=python:3.12.16-slim-trixie
FROM ghcr.io/astral-sh/uv:0.13.4 AS uv

# ---------- Stage 1: build the virtualenv ----------
FROM $${PYTHON_IMAGE} AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \\
    UV_LINK_MODE=copy \\
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml ./
COPY uv.loc[k] ./
# Dependencies first (cached until pyproject.toml / uv.lock change): the
# LOCKED set when uv.lock exists (reproducible), else resolved from
# pyproject.toml.
RUN --mount=type=cache,target=/root/.cache/uv \\
    uv venv /opt/venv \\
    && if [ -f uv.lock ]; then \\
        uv export --frozen --no-dev --extra fast --no-emit-project \\
            --output-file requirements.txt; \\
    else \\
        uv pip compile pyproject.toml --extra fast --output-file requirements.txt; \\
    fi \\
    && uv pip install --python /opt/venv/bin/python --no-deps -r requirements.txt
# Then the project itself: its package only, never the whole context (env
# files and keys stay out); only its own package is byte-compiled here.
COPY $app /app/$app
RUN --mount=type=cache,target=/root/.cache/uv \\
    UV_COMPILE_BYTECODE=0 uv pip install --python /opt/venv/bin/python --no-deps . \\
    && /opt/venv/bin/python -m compileall -q -j 0 \\
        /opt/venv/lib/python3*/site-packages/$app

# ---------- Stage 2: runtime ----------
FROM $${PYTHON_IMAGE} AS production
RUN python -m pip uninstall -y -q pip setuptools wheel 2>/dev/null || true; \\
    useradd --create-home --uid 10001 appuser \\
    && install -d -o appuser -g appuser /app /app/data
# The application runs from the virtualenv: nothing of the build context is
# copied into the runtime image.
COPY --from=builder /opt/venv /opt/venv
RUN python - <<'HARDEN'
$runtime_hardening
HARDEN
WORKDIR /app
ENV PATH="/opt/venv/bin:$${PATH}" \\
    VIRTUAL_ENV=/opt/venv \\
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    GUNICORN_BIND=0.0.0.0:8000 \\
    HEALTH_URL=http://localhost:8000/health/simple
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \\
    CMD python -c "import os, urllib.request as r; \\
r.urlopen(os.environ['HEALTH_URL'])" || exit 1
# Worker count honours the container CPU quota (default min(2*cpus+1, 4),
# WEB_CONCURRENCY overrides), uvicorn workers run uvloop + httptools,
# GUNICORN_KEEPALIVE (default 75 s) must exceed the load balancer's idle
# timeout, and only one worker runs the outbox relay.
CMD ["gunicorn", "-c", "python:sandloom.core.cli.gunicorn_conf", "$app.asgi:app"]
"""
)

_DOCKERIGNORE = """.git
.venv
**/__pycache__
*.py[cod]
.pytest_cache
.ruff_cache
.mypy_cache
data/
test_data/

""" + _image_secrets.dockerignore_block()


def _normalize_modules(with_batteries: list[str]) -> list[str]:
    for m in with_batteries:
        if m not in _BUNDLED:
            raise ValueError(
                f"unknown bundled module '{m}'; choose from {', '.join(_BUNDLED)}"
            )
    # The console signs in through identity.
    chosen = {*with_batteries, *(["identity"] if "admin" in with_batteries else [])}
    # Preserve canonical order (identity, tenancy, billing, admin); configuration
    # names an installed module by its `sandloom.modules` entry-point name.
    return [m for m in _BUNDLED if m in chosen]


def _framework_backend(path: Path) -> Path:
    """The framework root, containing packages/ and src/sandloom/."""
    return path


def _sourced_names(bundled: list[str]) -> list[str]:
    """The framework distributions an unpublished checkout supplies:
    ``sandloom-redis`` and ``sandloom-celery`` too, which the
    ``sandloom[postgres,redis,celery]`` extras of every generated app require."""
    return [
        "sandloom",
        "sandloom-redis",
        "sandloom-celery",
        *(f"sandloom-{m}" for m in bundled),
    ]


#: The framework range a generated app depends on (0.x: one minor).
FRAMEWORK_RANGE = ">=0.7.0a17,<0.8"


def _framework_sources(
    source: str | None, path: Path | None, bundled: list[str]
) -> str:
    """Resolve published or editable local distribution sources."""
    checkout = Path(__file__).resolve().parents[4]
    if path is not None:
        path = _framework_backend(path.expanduser())
    if source is None:
        source = "path" if (checkout / "packages").is_dir() else "pypi"
    if source not in {"pypi", "git", "path"}:
        raise ValueError("framework_source must be pypi, git or path")
    if source == "pypi":
        return ""
    if source == "git":
        raise ValueError(
            "Git framework dependencies are not supported; "
            "use --framework-source path --framework-path <checkout> locally"
        )
    lines = ["", "[tool.uv.sources]"]
    for name in _sourced_names(bundled):
        if source == "path":
            package = (path or checkout) / "packages" / name
            if not (package / "pyproject.toml").is_file():
                raise ValueError(
                    f"framework package missing: {package}; --framework-path "
                    "must be the framework repository "
                    "directory (the one holding packages/)"
                )
            # Editable: the live tree, never a copy uv built once and keeps
            # reinstalling from its cache (it rebuilds a path dependency only
            # when the package's pyproject changes, not src).
            fields = f"path = {json.dumps(str(package.resolve()))}, editable = true"
        else:
            fields = (
                'git = "https://github.com/Sandloom-dev/sandloom.git", '
                f'rev = "main", subdirectory = "packages/{name}"'
            )
        lines.append(f"{name} = {{ {fields} }}")
    return "\n".join(lines) + "\n"


#: Importable standard-library packages that ``sys.stdlib_module_names``
#: leaves out (the test suite, IDLE's, the tooling ones), and the app's own
#: ``tests`` package.
_STDLIB_EXTRAS = frozenset(
    {
        "test",
        "tests",
        "idlelib",
        "lib2to3",
        "ensurepip",
        "venv",
        "turtledemo",
        "pydoc_data",
        "distutils",
        "__phello__",
    }
)


def _installed_import_names() -> dict[str, str]:
    """``{top-level import name: distribution}`` of what is installed here
    (the framework and its dependencies: the app installs the same), plus
    the bundled modules' packages and what each stack's extras install (a bare
    kernel has none of them installed, the app does)."""
    found: dict[str, str] = {
        "sandloom": "sandloom",
        **{f"sandloom_{name}": f"sandloom-{name}" for name in _BUNDLED},
        **{
            name: f"sandloom[{','.join(stack.extras)}]"
            for stack in _stack.STACKS
            for name in stack.imports
        },
    }
    for name, dists in importlib.metadata.packages_distributions().items():
        if name.isidentifier():
            found.setdefault(name.lower(), dists[0] if dists else name)
    return found


def app_name_problem(app: str) -> str | None:
    """Why ``app`` cannot name an application, or ``None``. The name is the
    project's Python package, so it must not be the name of another import
    in the app's environment: a standard library module (``json``, ``test``)
    or an installed package (``redis``, ``sandloom``). One would shadow the
    other and the app breaks on import (``No module named 'test.asgi'``)."""
    if not _NAME_RE.fullmatch(app):
        return "app name must be snake_case starting with a letter (e.g. 'helpdesk')"
    if keyword.iskeyword(app):
        return f"app name '{app}' is a Python keyword; choose another (e.g. 'helpdesk')"
    suggestion = f"pick another (e.g. '{app}_app')"
    if app in sys.stdlib_module_names or app in _STDLIB_EXTRAS:
        return (
            f"'{app}' is a Python module name (the standard library's); the "
            f"app's package and it would shadow each other: {suggestion}"
        )
    installed = _installed_import_names().get(app)
    if installed is not None:
        return (
            f"'{app}' is a Python module name (from {installed}, which the app "
            f"installs); the app's package and it would shadow each other: "
            f"{suggestion}"
        )
    return None


def generate_project(
    app: str,
    parent_dir: Path,
    *,
    with_batteries: list[str] | None = None,
    sample_module: str = "example",
    framework_req: str | None = None,
    framework_source: str | None = None,
    framework_path: Path | None = None,
    agents: Sequence[str] = _skills.DEFAULT_AGENTS,
    stack: str = _stack.DEFAULT_STACK,
) -> list[Path]:
    """Generate a standalone Sandloom application; returns created file paths.

    ``agents`` (keys of :data:`sandloom.core.cli.skills.CODING_AGENTS`) decides
    which skill folders and instruction pointers the application gets;
    ``stack`` (a name of :data:`sandloom.core.cli.stack_service.STACKS`) which
    backing servers it runs on.

    The stack's own lines (compose blocks, urls) come from its driver package:
    written here when the package is importable, else left as tokens that
    :func:`sandloom.core.cli.stack_files.complete` fills (``sandloom add
    stack``, run in the app's environment): the same bytes either way."""
    problem = app_name_problem(app)
    if problem is not None:
        raise ValueError(problem)
    if not _NAME_RE.fullmatch(sample_module) or keyword.iskeyword(sample_module):
        raise ValueError("module name must be snake_case starting with a letter")
    chosen = _stack.stack_choice(stack)

    bundled = _normalize_modules(with_batteries or [])
    source_config = _framework_sources(framework_source, framework_path, bundled)
    # The kernel with the extras of the chosen stack (``standard``: its Redis
    # and Celery drivers, which the generated compose runs), plus each module
    # package chosen with --with: ecosystem packages, each a compatible range.
    # In a local checkout (``source_config`` is not empty) the stack's packages
    # (``sandloom-redis``, ``sandloom-celery``) are named beside the extras that
    # require them: uv applies a ``[tool.uv.sources]`` entry (an unpublished
    # checkout) to direct dependencies only. A published app gets them through
    # ``sandloom[postgres,redis,celery]``.
    driver_packages: list[str] = []
    if framework_req is None:
        extras = ",".join(["postgres", *chosen.extras])
        framework_req = f"sandloom[{extras}]{FRAMEWORK_RANGE}"
        if source_config:
            driver_packages = [
                f"sandloom-{extra}{FRAMEWORK_RANGE}" for extra in chosen.extras
            ]
    framework_deps = [
        framework_req,
        *driver_packages,
        *(f"sandloom-{m}{FRAMEWORK_RANGE}" for m in bundled),
    ]
    app_module = f"{app}.modules.{sample_module}"
    sandloom_modules = [*bundled, app_module]
    modules_json = json.dumps(sandloom_modules)
    bundled_json = json.dumps(bundled)
    # The app's claim authorities (ADR-0014): who may write each protected
    # token claim, for the bundled modules chosen; the app's own issuer or
    # claims are added there by hand.
    authority_keys = {
        "identity": ("issuer", "roles", "permissions", "email", "email_verified"),
        "tenancy": ("tenant_ids",),
        "billing": ("subscription_status", "plan_code", "features", "limits"),
    }
    claim_authorities = {
        key: "_BILLING_AUTHORITY" if name == "billing" else f'"sandloom_{name}"'
        for name in bundled
        for key in authority_keys.get(name, ())
    }

    root = parent_dir / app
    if root.exists():
        raise FileExistsError(f"{root} already exists")

    # The stack's backing server: the package of its driver describes it. No
    # package installed here (a bare kernel) is not an error: the files keep
    # their tokens for ``sandloom add stack``. A package too old, broken or
    # registered twice is a ``StackServiceError`` (a ``ValueError``).
    service = _stack.available(chosen.driver)
    ctx = {
        "app": app,
        "runtime_hardening": (
            Path(__file__).parent / "templates/deploy/harden_runtime.py"
        ).read_text(),
        "module": sample_module,
        "modules_json": modules_json,
        "bundled_json": bundled_json,
        "billing_authority": (
            '\n_BILLING_AUTHORITY = Authority("billing", '
            'module="sandloom_billing", fail_safe=True)\n'
            if "billing" in bundled
            else ""
        ),
        "claim_authorities": _dict_literal(claim_authorities),
        "create_app": _create_app_call(app),
        "framework_deps": "\n".join(f'    "{dep}",' for dep in framework_deps),
        "framework_distribution": ", ".join(
            f"`{name}`" for name in _sourced_names(bundled)
        ).strip("`"),
        "fast_req": FAST_EXTRA_REQ,
        "dev_pins": "\n".join(
            f'    "{pin}",' for pin in (*DEV_TOOL_PINS, CHECK_EXTRA_REQ, f"{app}[fast]")
        ),
        "port_hint": _stack_files.PORT_HINT,
        # The stack's lines: tokens here, its package's text after the render.
        **{key: _stack_files.token(key) for key in _stack_files.KEYS},
        "identity_note": (
            _IDENTITY_NOTE
            if "identity" in bundled
            else (_BARE_APP_NOTE if not bundled else "")
        )
        + (_SINGLE_TENANT_NOTE if bundled and "tenancy" not in bundled else ""),
        "billing_note": _BILLING_NOTE if "billing" in bundled else "",
        "quickstart_section": _quickstart_section(
            quickstart(app, sample_module)[1:], admin="admin" in bundled
        ),
        "billing_env": (_env_text(_IDENTITY_ENV) if "identity" in bundled else "")
        + (_NOTIFIER_ENV if {"identity", "tenancy"} & set(bundled) else "")
        + (_env_text(_BILLING_ENV) if "billing" in bundled else "")
        + (_env_text(_ADMIN_ENV) if "admin" in bundled else ""),
    }
    # No tenancy module (``--no-batteries``, or identity / billing / admin
    # without tenancy): nothing vouches for a tenant.
    single_tenant = "tenancy" not in bundled
    tenant_id = str(uuid.uuid4())
    example_ctx = {
        **ctx,
        "tenancy": _single_tenant_env(tenant_id, local=False) if single_tenant else "",
        "jwt_secret": "change_me_in_production_use_openssl_rand_hex_32",
        "open_internal": "# INTERNAL_ENDPOINTS_OPEN_IN_DEV=true",
        "log_links": _LOG_LINKS if "identity" in bundled else "",
    }
    # The local .env gets a REAL random secret (git-ignored) so the placeholder
    # is never what a developer actually runs with.
    local_env_ctx = {
        **ctx,
        "tenancy": _single_tenant_env(tenant_id, local=True) if single_tenant else "",
        "jwt_secret": secrets.token_hex(32),
        "open_internal": "INTERNAL_ENDPOINTS_OPEN_IN_DEV=true",
        "log_links": _LOG_LINKS.replace("# NOTIFIER_LOG_LINKS", "NOTIFIER_LOG_LINKS")
        if "identity" in bundled
        else "",
    }

    files: dict[str, str] = {
        # The agent map: the 1.0 commands and the beginner names.
        "AGENTS.md": _AGENTS_HEADER.substitute(
            ctx, skill_dirs=", ".join(f"`{d}`" for d in _skills.skill_dirs(agents))
        ),
        "pyproject.toml": _PYPROJECT.substitute(ctx)
        + source_config
        + _stack_files.recording(chosen.name),
        "README.md": _README.substitute(ctx),
        ".gitignore": _GITIGNORE,
        ".gitattributes": _GITATTRIBUTES,
        ".env": _ENV_EXAMPLE.substitute(local_env_ctx),
        ".env.example": _ENV_EXAMPLE.substitute(example_ctx),
        "Dockerfile": _DOCKERFILE.substitute(ctx),
        ".dockerignore": _DOCKERIGNORE,
        ".env.test": _ENV_TEST.substitute(ctx),
        f"{app}/__init__.py": _APP_INIT.substitute(ctx),
        f"{app}/asgi.py": _ASGI.substitute(ctx),
        f"{app}/modules/__init__.py": _MODULES_INIT.substitute(ctx),
        f"{app}/shared/__init__.py": (
            f'"""Generated contract stubs, DTO copies and event schemas of {app}."""\n'
        ),
        f"{app}/shared/contracts/__init__.py": (
            '"""Generated contract stubs and DTO copies (`sandloom generate`)."""\n'
        ),
        "tests/__init__.py": "",
        "tests/test_smoke.py": _TEST_SMOKE.substitute(ctx),
        "architecture_rules.yml": _ARCH_RULES.substitute(ctx),
        "deploy/docker-compose.yml": _DOCKER_COMPOSE.substitute(ctx),
        "deploy/docker-compose.dev.yml": _DEV_COMPOSE.substitute(ctx),
        ".github/workflows/ci.yml": _CI_WORKFLOW.substitute(ctx),
        ".pre-commit-config.yaml": _PRE_COMMIT,
    }

    filled = _stack_files.values(service) if service is not None else None
    created: list[Path] = []
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if filled is not None:
            content = _stack_files.fill(content, filled)
        path.write_text(content, encoding="utf-8")
        created.append(path)
    # The agent files: skills where each chosen agent reads them, the
    # module-author contract for every module, and the record that lets
    # `sandloom add skills` update them later.
    written, _ = _skills.write_skills(root, app, agents, modules=f"{app}/modules")
    created.extend([*written, root / _skills.RECORD])

    # Sample business module under <app>/modules/<sample> (Public API 1.0).
    where = _add.Layout(
        modules_dir=root / app / "modules",
        modules_package=f"{app}.modules",
        shared_dir=root / app / "shared",
        shared_package=f"{app}.shared",
        source_root=root,
    )
    # The starter can pass static checks before its first settings snapshot.
    created.extend(_add.add_module(sample_module, where))
    return created


def write_generated_files(root: Path) -> str | None:
    """Write the generated files of a fresh app (``sandloom generate``), so
    its own CI gate (``sandloom test --ci``, which only checks them) passes
    before anything else. Boots the app in a child process (this
    interpreter, the app's ``.env.test``; nothing connects to a database);
    returns ``None``, or why it could not (e.g. this ``sandloom`` lacks the
    bundled modules the app loads: then run ``sandloom generate``)."""
    # `uvx --from sandloom` isolates the CLI from the app's optional packages.
    # Do not boot that incomplete composition: generation runs after `uv sync`
    # in the app environment, using the dependency versions it actually installed.
    if _stack_files.pending(root):
        return (
            "run `uv sync` and `uv run sandloom add stack` to write the stack's "
            "files first"
        )
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = {
        re.split(r"[\[<>=!~ ;]", dependency, maxsplit=1)[0]
        for dependency in metadata["project"]["dependencies"]
    }
    missing = [
        f"sandloom-{name}"
        for name in _BUNDLED
        if f"sandloom-{name}" in dependencies
        and importlib.util.find_spec(f"sandloom_{name}") is None
    ]
    if missing:
        return f"run `uv sync` to install the app dependencies: {', '.join(missing)}"
    # The fresh app's own modules (the sample) are drafts, named explicitly: a
    # snapshot written fresh would record them as released (ADR-0017).
    code, output = _add.run_generate(root, *_add.draft_options(_add.own_modules(root)))
    if code == 0:
        return None
    lines = output.strip().splitlines()
    return lines[-1] if lines else f"exit {code}"
