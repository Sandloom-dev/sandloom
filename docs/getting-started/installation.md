---
title: Installation
---

# Installation

## Prerequisites

- **Python 3.11+** (3.12 recommended)
- **[uv 0.13.4+](https://docs.astral.sh/uv/)** — the package/dependency manager Sandloom uses
- **Docker & Docker Compose** — for the local PostgreSQL and Redis services

## Install the alpha from PyPI

The four `0.7.0a1` distributions are published. Create an application without
access to the private source repository:

```bash
uvx --from "sandloom[standard]==0.7.0a1" sandloom new myapp --with identity,tenancy,billing
```

Continue with the [application quickstart](index.md#quickstart).
The remaining sections describe running the framework source checkout as a contributor.

## Get the source checkout

The canonical repository is [Sandloom-dev/sandloom](https://github.com/Sandloom-dev/sandloom).
It is currently private and awaiting transfer of the prepared source snapshot;
the website at `sandloom.dev` is not deployed yet.
During preparation, use the existing checkout. After the source transfer, an
account with access can clone it:

```bash
git clone https://github.com/Sandloom-dev/sandloom.git
cd sandloom
```

This page runs the framework checkout with its standard modules (identity,
tenancy, billing), which `sandloom.main:app` composes from their installed
`sandloom.modules` entry points. To create your
own application instead, follow the [quickstart](index.md#quickstart).

## Install dependencies

From the checkout root:

```bash
uv sync --locked
```

`uv sync` installs the locked dependencies **including** the `dev` dependency
group (pytest, ruff, mypy — pinned exactly, so every machine, pre-commit and CI
use the same versions). Use `uv sync --frozen` for a strictly lock-faithful
install (what CI runs) and `uv sync --no-dev` for a runtime-only environment.

## Configure the environment

Copy the example env file and adjust as needed. Generate a local signing secret
with `openssl rand -hex 32` and set `JWT_SECRET` in `.env` to that value:

```bash
cp .env.example .env
```

The four mandatory settings are `DATABASE_URL`, `CACHE_URL`, `BROKER_URL`, and
`JWT_SECRET`. The internal/ops endpoints (`/diagnostics/*`, `/contracts/*`,
`/metrics`, internal RPC) stay closed (`404`) until you set
`INTERNAL_API_TOKEN` — or, for local development only,
`INTERNAL_ENDPOINTS_OPEN_IN_DEV=true`. See the [Configuration Reference](../reference/configuration.md)
for every setting, its default, and meaning.

## Start the test services

```bash
docker compose -f deploy/compose/test.yml up -d --wait
```

This brings up PostgreSQL (`:5433`) and Redis (`:6380`) used by the test suite
and local development.

The example environment uses a separate `sandloom_dev` database for development.
Create it once after starting the services:

```bash
docker compose -f deploy/compose/test.yml exec postgres_test createdb -U test_user sandloom_dev
```

Use a separately named database containing `test` for the test harness; never
point destructive test fixtures at `sandloom_dev`.

## Run the database migrations

Sandloom uses a **core migration chain** plus one **chain per module**. Apply them
all with the framework's migrator (not raw Alembic):

```bash
uv run sandloom migrate upgrade-all --apply
```

See the [Migrations & Upgrades](../guides/migrations.md) guide for the full
model.

## Run the API

```bash
uv run uvicorn sandloom.main:app --reload   # API on :8000
```

Interactive API docs are served at `/docs` outside production (see
`DOCS_ENABLED`).

## Verify the install

Run the quality gates — the tests in parallel, then ruff, format, mypy,
`sandloom check`, the architecture tests and the snapshot checks:

```bash
uv run sandloom test --full
```

Next: the [tutorial](tutorial.md) builds an application with two modules.
