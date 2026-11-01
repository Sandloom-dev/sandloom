---
title: Quickstart
description: Create a Sandloom application from the published packages, add a module, run the checks and start the API.
type: tutorial
footer_previous:
  title: Overview
  url: ""
---

# Quickstart

!!! note "Current alpha"
    The current alpha is `0.7.0a5`. All four packages are available on <!-- sandloom-release -->
    [PyPI](https://pypi.org/project/sandloom/0.7.0a5/). The source is maintained in
    [Sandloom-dev/sandloom](https://github.com/Sandloom-dev/sandloom).

You need Python 3.11+, [uv 0.13.4+](https://docs.astral.sh/uv/) and Docker.
In an empty directory, generate an application from the published alpha:

```bash
uvx --from "sandloom[standard]==0.7.0a5" sandloom new myapp --with identity,tenancy,billing
```

This starter selects all three [standard modules](../modules/index.md). For a
minimal backend, use
`uvx --from "sandloom==0.7.0a5" sandloom new myapp --no-batteries --module notes`
instead. That command already creates `notes`, so its printed steps add a
different module; the other steps are the same. The
[README](https://github.com/Sandloom-dev/sandloom#quickstart) demonstrates that minimal path.

`sandloom new` prints the next steps; run them as printed:

<!-- quickstart:begin (sandloom.core.cli.project.QUICKSTART; src/sandloom/core/tests/test_quickstart.py) -->
```bash
cd myapp
uv sync                                                       # the app, the framework, the pinned dev tools
uv run sandloom generate                                      # snapshots, stubs and DTOs from installed dependencies
docker compose -f deploy/docker-compose.dev.yml up -d --wait  # Postgres + Redis
uv run sandloom add module notes                              # a module: discovered, nothing to wire
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
    `SANDLOOM_TEST_CACHE_URL` / `SANDLOOM_TEST_BROKER_URL` for the tests. Pick ports
    below 32768, or use `127.0.0.1` in the URLs
    ([why](../guides/testing.md#isolation-guards)). The generated `README.md` shows the
    commands.

For an app created with `--with identity`, follow [authentication and email](authentication.md)
to receive verification mail locally and obtain access/refresh tokens.

## Next

1. **[Your project structure](project-structure.md)**: what `sandloom new`
   generated, which files are yours and which are generated.
2. **[Tutorial: your first feature](tutorial.md)**: an application with two
   modules, a table, routes, an event consumed exactly once, a contract call
   and tests.

!!! tip "Learn by example"
    `examples/tutorial/` is the finished tutorial application. The
    `examples/` directory also ships four applications: `linkly`
    (minimal), `helpdesk` (multi-tenant, standard modules), `academy`
    (cross-module sagas) and `projecthub` (query projections). See the
    [example catalog](https://github.com/Sandloom-dev/sandloom/tree/main/examples).
