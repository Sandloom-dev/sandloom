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
  Celery worker + beat via ``sandloom.core.worker``)

Usage:
    sandloom new myapp                              # minimal app + sample module
    sandloom new helpdesk --with identity,tenancy,billing
    sandloom new linkly --module links --no-batteries
"""

from __future__ import annotations

import json
import keyword
import re
import secrets
from pathlib import Path
from string import Template

import sandloom.core.cli.add as _add
import sandloom.core.cli.image_secrets as _image_secrets
import sandloom.core.cli.skills as _skills

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_BUNDLED = ("identity", "tenancy", "billing")

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

#: Dev requirement that brings `sandloom check`'s optional deps (PyYAML) and the
#: test kit's (pytest plugins) — through the framework's own `check` / `testing`
#: extras, so their ranges are owned in ONE place.
CHECK_EXTRA_REQ = "sandloom[check,testing]"

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
        "docker compose -f deploy/docker-compose.dev.yml up -d --wait",
        "Postgres + Redis",
    ),
    ("uv run sandloom add module {module}", "a module: discovered, nothing to wire"),
    ("uv run sandloom generate", "after each edit: snapshots, stubs, DTO copies"),
    ("uv run sandloom test", "tests + every gate (never writes)"),
    ("uv run sandloom migrate upgrade-all --apply", "core + module chains"),
    ("uv run uvicorn {app}.asgi:app --reload", "http://localhost:8000/health"),
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
# A deprecated call (in the app or a library it uses) fails the suite.
filterwarnings = ["error::DeprecationWarning"]

[tool.ruff]
fix = true
force-exclude = true
extend-exclude = ["$app/modules/*/migrations"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "C4", "UP"]
ignore = ["B008"]

[tool.ruff.lint.isort]
# Framework imports are grouped like first-party (matching sandloom add output).
known-first-party = ["sandloom", "$app"]

[tool.ruff.lint.per-file-ignores]
"**/tests/**" = ["E501"]

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
  a failure).

A module imports only `sandloom.sdk` (plus generated contract stubs and DTO copies
in `$app.shared`) and its own package; a provider's contract DTOs live in its own
`schemas.py`. The beginner names, all you need for
CRUD, events and cross-module calls:

| need | names (`from sandloom.sdk import ...`) |
| --- | --- |
| a module | `define_module` |
| tables | `Base`, `TenantScoped`, `db`, `new_id`, `CreatedAt` |
| routes | `module_router`, `public`, `CurrentUser` |
| events | `Event`, `on` |
| contracts | `contract`, `command`, `Contract` |
| authorization | `require`, `Subscribed`, `SubscriptionActive`, `Credits`, |
| | `OrgRole`, `Owner`, `authorize_resource` |
| effects | `record_audit`, `on_commit` |
| tests (`sandloom.sdk.testing`) | `client` (fixture), `as_user`, `published`, |
| | `deliver`, `fake_contract`, `fake_access_fact` |

Other SDK names are advanced: mark the import `# sandloom: advanced <reason>`.
Ids and times: `default=new_id` (UUIDv7) and `created_at: Mapped[CreatedAt]`
(`clock_timestamp()`), so a list ordered by `created_at, id` never ties.

Routes declare nothing but their exceptions: every route is authenticated
(`CurrentUser`, or a gate) unless it says `public(reason="...")`; writes get
the commit-time session check, automatic idempotency and the audit of
sensitive effects from the framework (there is no per-route class). A billing
gate names the account it checks, `require(Subscribed(of=Account.user()))` or
`of=Account.org(param="org_id")` on an organization route
(`from sandloom.sdk.authz import Account  # sandloom: advanced billing account`).

How-tos with tested code: `uv run sandloom docs recipes`. What you can call:
`uv run sandloom docs contracts`, `... events`, `... <contract or channel>`.
Skills (`.claude/skills`): sandloom-new-module, sandloom-quality-gates,
sandloom-event-version-roll, sandloom-split-deploy. Refresh: `sandloom add skills`.

"""
)

_README = Template(
    """# $app

An application built on the **Sandloom framework** (configurable monolith for SaaS).

The framework is supplied by the `$framework_distribution` dependencies.
This project owns only
its business logic — its modules live under `$app/modules/*` and import the
framework's public API (`sandloom.sdk`). The ASGI app is the framework's
`create_app`; it loads every module of `$app/modules` (set `SANDLOOM_MODULES` to
select a subset).

## Quick start

From the project root:

```bash
$quickstart
```

`uv sync` installs the app, the framework and the pinned dev tools (ruff,
mypy, pytest, pytest-xdist); `pre-commit install` adds the commit hooks. The
API reads `.env` (generated with a random `JWT_SECRET`; recreate it from
`.env.example` if you delete it), the tests read `.env.test`.

### Ports of the dev services

`deploy/docker-compose.dev.yml` publishes the dev Postgres on 5432 and Redis
on 6379 (used by `.env`), and the test Postgres on 5433 and Redis on 6380
(used by `.env.test`). When a port is taken (for example by the framework's
own test services), choose others and point the app at them:

```bash
export SANDLOOM_DEV_POSTGRES_PORT=15432 SANDLOOM_DEV_REDIS_PORT=16379
export SANDLOOM_DEV_TEST_POSTGRES_PORT=15433 SANDLOOM_DEV_TEST_REDIS_PORT=16380
docker compose -f deploy/docker-compose.dev.yml up -d --wait
# the API: edit DATABASE_URL / CACHE_URL / BROKER_URL in .env (or export them)
# Tests: export SANDLOOM_TEST_DATABASE_URL, SANDLOOM_TEST_CACHE_URL,
# SANDLOOM_TEST_BROKER_URL to use isolated services.
export SANDLOOM_TEST_DATABASE_URL=postgresql+asyncpg://test_user:test_password@localhost:15433/sandloom_test
export SANDLOOM_TEST_CACHE_URL=redis://localhost:16380/15 SANDLOOM_TEST_BROKER_URL=redis://localhost:16380/14
```

## Architecture checks

```bash
sandloom check            # module isolation rules against architecture_rules.yml
```

`sandloom check` runs the framework's own architecture analyzers over
`$app/modules/*`: no sibling-module imports, only the framework public API
(`sandloom.core.*`, `sandloom.shared.*`),
no ORM in endpoints, endpoints only in `api/`, no `db.commit()`, no raw
backend SDKs. CI (`.github/workflows/ci.yml`) and the pre-commit hook run it.

## Deploy (monolith)

```bash
export JWT_SECRET=$$(openssl rand -hex 32) INTERNAL_API_TOKEN=$$(openssl rand -hex 32)
docker compose -f deploy/docker-compose.yml up --build
```

The `migrate` service runs `sandloom migrate upgrade-all --apply` to completion
before the app starts; `worker` / `beat` run the framework Celery entrypoint
(`celery -A sandloom.core.worker worker|beat`), which registers the `tasks.py` of
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
During release preparation it is private and awaiting the source transfer;
the PyPI packages have not been published. `--framework-source git` is currently
disabled. Use `--framework-source pypi` only after the required distributions
have been released, or build and install the complete matching wheel set for a
portable deployment. Commit `uv.lock` after resolving dependencies and use
`uv sync --locked` in subsequent CI runs.

## Loaded modules

Every module of `$app/modules` (plus the bundled ones: `$bundled_json`);
`SANDLOOM_MODULES` selects a subset.
"""
)

_GITIGNORE = """__pycache__/
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

# ==== Database (default instance) ====
DATABASE_URL=postgresql+asyncpg://sandloom:sandloom@localhost:5432/$app

# ==== Cache / Broker (Redis) ====
CACHE_URL=redis://localhost:6379/1
BROKER_URL=redis://localhost:6379/2

# ==== Storage ====
STORAGE_TYPE=local
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
"""
)

_ENV_TEST = Template(
    """# ==== Test environment (loaded by the sandloom.core.testing pytest plugin) ====
ENVIRONMENT=test
CONTRACT_DTO_PACKAGES=["$app.shared"]
SANDLOOM_COMPOSITION_ROOT=$app.asgi

# Point these at your test infrastructure. Defaults match the framework's
# deploy/compose/test.yml (Postgres :5433, Redis :6380).
DATABASE_URL=postgresql+asyncpg://test_user:test_password@localhost:5433/sandloom_test
CACHE_URL=redis://localhost:6380/15
BROKER_URL=redis://localhost:6380/14

STORAGE_TYPE=local
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
'''
)

_ASGI = Template(
    '''"""ASGI entry point (the composition root).

Loads every module of ``$app.modules`` (found statically: each subpackage
whose ``__init__.py`` defines ``register``) plus the bundled ones, so adding
a module edits nothing here. ``SANDLOOM_MODULES`` selects a subset.

    uvicorn $app.asgi:app --reload
"""

from sandloom.sdk.app import app_modules, create_app  # sandloom: advanced app

BUNDLED: list[str] = $bundled_json

app = create_app(default_modules=[*BUNDLED, *app_modules("$app.modules")])
'''
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
# API 1.0), and the sandloom.core modules it supersedes until the 1.0 migration
# completes.
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
  - sandloom.sdk.tenant
  - sandloom.sdk.testing
  - sandloom.sdk.tooling
  - sandloom.sdk.web
  - sandloom.core.di
  - sandloom.core.facade
  - sandloom.core.module_api
  - sandloom.core.descriptors
  - sandloom.core.orm
  - sandloom.core.projections
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
    """# Local-only development and disposable test services. Host ports are
# configurable (defaults below): SANDLOOM_DEV_POSTGRES_PORT, SANDLOOM_DEV_REDIS_PORT,
# SANDLOOM_DEV_TEST_POSTGRES_PORT, SANDLOOM_DEV_TEST_REDIS_PORT (see README.md).
name: $compose_name
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: sandloom
      POSTGRES_PASSWORD: sandloom
      POSTGRES_DB: $app
    ports: ["127.0.0.1:$${SANDLOOM_DEV_POSTGRES_PORT:-5432}:5432"]
    volumes: ["dev_postgres:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sandloom"]
      interval: 2s
      timeout: 2s
      retries: 20
  redis:
    image: redis:7-alpine
    ports: ["127.0.0.1:$${SANDLOOM_DEV_REDIS_PORT:-6379}:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      timeout: 2s
      retries: 20
  postgres_test:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: test_user
      POSTGRES_PASSWORD: test_password
      POSTGRES_DB: sandloom_test
    ports: ["127.0.0.1:$${SANDLOOM_DEV_TEST_POSTGRES_PORT:-5433}:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U test_user"]
      interval: 2s
      timeout: 2s
      retries: 20
  redis_test:
    image: redis:7-alpine
    command: ["redis-server", "--databases", "256"]
    ports: ["127.0.0.1:$${SANDLOOM_DEV_TEST_REDIS_PORT:-6380}:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      timeout: 2s
      retries: 20
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
  CACHE_URL: redis://redis:6379/1
  BROKER_URL: redis://redis:6379/2
  SHUTDOWN_BUDGET_SECONDS: 30
  GUNICORN_GRACEFUL_TIMEOUT: 45
  # The weaker guarantees this stack accepts (logged at every start):
  # single-host local storage, persisted below — use a driver plugin for
  # replicas on hosts without a shared filesystem. A demo on the fake PSP adds
  # fake_psp_in_prod (comma-separated).
  SANDLOOM_ACCEPTED_RISKS: $${SANDLOOM_ACCEPTED_RISKS:-local_storage_in_prod}
  JWT_SECRET: $${JWT_SECRET:?set JWT_SECRET (openssl rand -hex 32)}
  INTERNAL_API_TOKEN: $${INTERNAL_API_TOKEN:?set INTERNAL_API_TOKEN}

$billing_env
x-app-build: &app-build
  # The project root (one level up from deploy/) holds the Dockerfile.
  context: ..
  dockerfile: Dockerfile

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
  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes", "--appendfsync", "always"]
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10

  # One-shot schema migration: runs to completion before the app starts.
  migrate:
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
      redis:
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
    build: *app-build
    volumes: ["app_data:/app/data"]
    command: ["celery", "-A", "sandloom.core.worker", "worker", "--loglevel=INFO"]
    environment:
      <<: *app-env
      SANDLOOM_COMPOSITION_ROOT: $app.asgi
      DB_POOL_SIZE: $${DB_POOL_SIZE:-5}
      DB_MAX_OVERFLOW: $${DB_MAX_OVERFLOW:-5}
    healthcheck:
      disable: true
    depends_on:
      redis:
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
    build: *app-build
    volumes: ["app_data:/app/data"]
    command: ["celery", "-A", "sandloom.core.worker", "beat", "--loglevel=INFO",
              "--schedule=/tmp/celerybeat-schedule"]
    environment:
      <<: *app-env
      SANDLOOM_COMPOSITION_ROOT: $app.asgi
    healthcheck:
      disable: true
    depends_on:
      redis:
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
  redis_data:
  app_data:
"""
)

# Billing refuses the fake PSP in prod, so a batteries app must choose.
# identity refuses to start in prod without a dedicated 2FA encryption key.
_IDENTITY_ENV = """\
  # identity encrypts TOTP secrets with a dedicated key (comma-separated,
  # newest first, for rotation) — required in prod.
  TWOFA_ENCRYPTION_KEY: ${TWOFA_ENCRYPTION_KEY:?set TWOFA_ENCRYPTION_KEY \
(openssl rand -hex 32)}
"""

_BILLING_ENV = """\
  # billing refuses the fake PSP in prod: choose explicitly.
  BILLING_PROVIDER: ${BILLING_PROVIDER:?set BILLING_PROVIDER (stripe, or fake \
with SANDLOOM_ACCEPTED_RISKS=local_storage_in_prod,fake_psp_in_prod for demos)}
  STRIPE_SECRET_KEY: ${STRIPE_SECRET_KEY:-}
  STRIPE_WEBHOOK_SECRET: ${STRIPE_WEBHOOK_SECRET:-}
"""

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
      redis:
        image: redis:7
        ports:
          - 6380:6379
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v10.2.0
        with:
          python-version: "3.12"
          version: "0.13.4"
      - name: Install
        run: |
          uv sync
      - name: The gate (tests, ruff, mypy, sandloom check, generated files)
        run: uv run sandloom test --ci
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
ARG PYTHON_IMAGE=python:3.12-slim
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
    # Preserve canonical order (identity, tenancy, billing); configuration
    # names an installed module by its `sandloom.modules` entry-point name.
    return [m for m in _BUNDLED if m in with_batteries]


def _framework_backend(path: Path) -> Path:
    """The framework root, containing packages/ and src/sandloom/."""
    return path


def _sourced_names(bundled: list[str]) -> list[str]:
    """The framework distributions an unpublished checkout supplies."""
    return ["sandloom", *(f"sandloom-{m}" for m in bundled)]


#: The framework range a generated app depends on (0.x: one minor).
FRAMEWORK_RANGE = ">=0.7.0a1,<0.8"


def _framework_sources(
    source: str | None, path: Path | None, bundled: list[str]
) -> str:
    """Explicit distribution sources while the checkout is not published."""
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
            "The public Sandloom Git source is not configured yet; "
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


def generate_project(
    app: str,
    parent_dir: Path,
    *,
    with_batteries: list[str] | None = None,
    sample_module: str = "example",
    framework_req: str | None = None,
    framework_source: str | None = None,
    framework_path: Path | None = None,
) -> list[Path]:
    """Generate a standalone Sandloom application; returns created file paths."""
    if not _NAME_RE.fullmatch(app) or keyword.iskeyword(app):
        raise ValueError(
            "app name must be snake_case starting with a letter (e.g. 'helpdesk')"
        )
    if not _NAME_RE.fullmatch(sample_module) or keyword.iskeyword(sample_module):
        raise ValueError("module name must be snake_case starting with a letter")

    bundled = _normalize_modules(with_batteries or [])
    source_config = _framework_sources(framework_source, framework_path, bundled)
    # The kernel with its Redis/Celery drivers (the generated compose uses
    # Redis and runs the Celery worker), plus each module package chosen with
    # --with: ecosystem packages, each a compatible range.
    if framework_req is None:
        framework_req = f"sandloom[redis,celery]{FRAMEWORK_RANGE}"
    framework_deps = [
        framework_req,
        *(f"sandloom-{m}{FRAMEWORK_RANGE}" for m in bundled),
    ]
    app_module = f"{app}.modules.{sample_module}"
    sandloom_modules = [*bundled, app_module]
    modules_json = json.dumps(sandloom_modules)
    bundled_json = json.dumps(bundled)

    root = parent_dir / app
    if root.exists():
        raise FileExistsError(f"{root} already exists")

    ctx = {
        "app": app,
        "module": sample_module,
        "modules_json": modules_json,
        "bundled_json": bundled_json,
        "framework_deps": "\n".join(f'    "{dep}",' for dep in framework_deps),
        "framework_distribution": ", ".join(
            f"`{name}`" for name in _sourced_names(bundled)
        ).strip("`"),
        "fast_req": FAST_EXTRA_REQ,
        "dev_pins": "\n".join(
            f'    "{pin}",' for pin in (*DEV_TOOL_PINS, CHECK_EXTRA_REQ, f"{app}[fast]")
        ),
        "compose_name": f"{app.replace('_', '-')}-dev",
        "quickstart": "\n".join(quickstart(app, sample_module)[1:]),
        "billing_env": (_IDENTITY_ENV if "identity" in bundled else "")
        + (_BILLING_ENV if "billing" in bundled else ""),
    }
    example_ctx = {
        **ctx,
        "jwt_secret": "change_me_in_production_use_openssl_rand_hex_32",
        "open_internal": "# INTERNAL_ENDPOINTS_OPEN_IN_DEV=true",
    }
    # The local .env gets a REAL random secret (git-ignored) so the placeholder
    # is never what a developer actually runs with.
    local_env_ctx = {
        **ctx,
        "jwt_secret": secrets.token_hex(32),
        "open_internal": "INTERNAL_ENDPOINTS_OPEN_IN_DEV=true",
    }

    files: dict[str, str] = {
        # The agent map: the 1.0 commands and the beginner names.
        "AGENTS.md": _AGENTS_HEADER.substitute(ctx),
        "CLAUDE.md": "@AGENTS.md\n",
        # The framework's procedures for this app's paths (sandloom add skills).
        **_skills.skill_files(app),
        "pyproject.toml": _PYPROJECT.substitute(ctx) + source_config,
        "README.md": _README.substitute(ctx),
        ".gitignore": _GITIGNORE,
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

    created: list[Path] = []
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(path)

    # Sample business module under <app>/modules/<sample> (Public API 1.0).
    where = _add.Layout(
        modules_dir=root / app / "modules",
        modules_package=f"{app}.modules",
        shared_dir=root / app / "shared",
        shared_package=f"{app}.shared",
        source_root=root,
    )
    created.extend(_add.add_module(sample_module, where))
    return created


def write_generated_files(root: Path) -> str | None:
    """Write the generated files of a fresh app (``sandloom generate``), so
    its own CI gate (``sandloom test --ci``, which only checks them) passes
    before anything else. Boots the app in a child process (this
    interpreter, the app's ``.env.test``; nothing connects to a database);
    returns ``None``, or why it could not (e.g. this ``sandloom`` lacks the
    bundled modules the app loads: then run ``sandloom generate``)."""
    code, output = _add.run_generate(root)
    if code == 0:
        return None
    lines = output.strip().splitlines()
    return lines[-1] if lines else f"exit {code}"
