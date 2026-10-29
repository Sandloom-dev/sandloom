---
title: Get started
description: The tutorial, installation and building a product on Sandloom.
type: tutorial
---

# Get started

## Quickstart

!!! note "Initial alpha: 0.7.0a1"
    All four alpha packages are available on
    [PyPI](https://pypi.org/project/sandloom/0.7.0a1/). The source is maintained in
    [Sandloom-dev/sandloom](https://github.com/Sandloom-dev/sandloom).

You need Python 3.11+, [uv 0.13.4+](https://docs.astral.sh/uv/) and Docker.
In an empty directory, generate an application from the published alpha:

```bash
uvx --from "sandloom[standard]==0.7.0a1" sandloom new myapp --with identity,tenancy,billing
```

`sandloom new` prints the next steps; run them as printed:

<!-- quickstart:begin (sandloom.core.cli.project.QUICKSTART; src/sandloom/core/tests/test_quickstart.py) -->
```bash
cd myapp
uv sync                                                       # the app, the framework, the pinned dev tools
docker compose -f deploy/docker-compose.dev.yml up -d --wait  # Postgres + Redis
uv run sandloom add module notes                              # a module: discovered, nothing to wire
uv run sandloom generate                                      # after each edit: snapshots, stubs, DTO copies
uv run sandloom test                                          # tests + every gate (never writes)
uv run sandloom migrate upgrade-all --apply                   # core + module chains
uv run uvicorn myapp.asgi:app --reload                        # http://localhost:8000/health
```
<!-- quickstart:end -->

`uv run sandloom test` ends with `sandloom test: OK`; the API answers on
`http://localhost:8000` (`/health`, and `/api/v1/notes/items` with a bearer
token). The CI job `quickstart` (`tools/smoke/quickstart_check.py`) runs these lines during full validation. See
[CI profiles](../contributing/ci.md) for the automatic and full checks.

!!! note "Ports"
    `deploy/docker-compose.dev.yml` publishes Postgres on 5432 and Redis on
    6379 (the app's `.env`) and the test Postgres on 5433 and Redis on 6380
    (`.env.test`): the same ports as the framework's own test services. When
    a port is taken, set `SANDLOOM_DEV_POSTGRES_PORT`, `SANDLOOM_DEV_REDIS_PORT`,
    `SANDLOOM_DEV_TEST_POSTGRES_PORT` or `SANDLOOM_DEV_TEST_REDIS_PORT` before
    `docker compose ... up`, then point the app at them: `DATABASE_URL` /
    `CACHE_URL` / `BROKER_URL` in `.env`, and `SANDLOOM_TEST_DATABASE_URL` /
    `SANDLOOM_TEST_CACHE_URL` / `SANDLOOM_TEST_BROKER_URL` for the tests. The
    generated `README.md` shows the commands.

`--framework-path` is the repository root (the directory containing `packages/`). The
sources it writes are editable: the app runs the checkout's live tree, so a
framework change shows up without a reinstall.
Without `--framework-source path`, `sandloom new` uses local editable sources
when run from a source checkout. An installed release resolves the
framework from published packages.

## Next

Start with the tutorial; it is one linear path.

1. **[Tutorial: your first feature](tutorial.md)**: an application with two
   modules, a table, routes, an event consumed exactly once, a contract call
   and tests, built with the `sandloom new` and `sandloom add` generators.
2. **[Installation](installation.md)**: prerequisites, dependencies, the test
   services and running the API from a framework checkout.
3. **[Building a product](building-a-product.md)**: `sandloom new`, the framework
   wheel, and how the same codebase deploys as a monolith or microservices.

!!! tip "Learn by example"
    `examples/tutorial/` is the finished tutorial application. The
    `examples/` directory also ships four applications: `linkly`
    (minimal), `helpdesk` (multi-tenant, bundled modules), `academy`
    (cross-module sagas) and `projecthub` (query projections). See the
    [example catalog](https://github.com/Sandloom-dev/sandloom/tree/main/examples).
