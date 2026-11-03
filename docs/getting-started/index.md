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
    The current alpha is `0.7.0a8`. All five packages are available on <!-- sandloom-release -->
    [PyPI](https://pypi.org/project/sandloom/0.7.0a8/). The source is maintained in
    [Sandloom-dev/sandloom](https://github.com/Sandloom-dev/sandloom).

You need Python 3.11+, [uv 0.13.4+](https://docs.astral.sh/uv/) and Docker.
In an empty directory, generate an application from the published alpha:

```bash
uvx --from "sandloom[standard]==0.7.0a8" sandloom new myapp --with identity,tenancy,billing
```

This starter selects all three [standard modules](../modules/batteries/index.md). For a
minimal backend, use
`uvx --from "sandloom==0.7.0a8" sandloom new myapp --no-batteries --module notes`
instead. That command already creates `notes`, so its printed steps add a
different module; the other steps are the same. The
[README](https://github.com/Sandloom-dev/sandloom#quickstart) demonstrates that minimal path.

!!! tip "Or answer a few questions"
    At a terminal, `sandloom new` without a name asks for what its flags
    leave open. It asks for the name, the standard modules and the coding
    agents. Then it offers to set the app up and to create the first
    administrator.

    ```bash
    uvx --from "sandloom[standard]==0.7.0a8" sandloom new
    ```

    A yes to the setup runs the steps below up to the migrations. The
    summary ends with one command that makes the same app again without
    questions. Scripts and coding agents pass `--yes`: they get no question,
    and the output is the one below.

`sandloom new` prints the next steps; run them as printed:

<!-- quickstart:begin (sandloom.core.cli.project.QUICKSTART; src/sandloom/core/tests/test_quickstart.py) -->
```bash
cd myapp
uv sync                                      # the app, the framework, the pinned dev tools
uv run sandloom generate                     # snapshots, stubs and DTOs from installed dependencies
uv run sandloom dev up                       # Postgres + Redis (Docker); writes .env
uv run sandloom add module notes             # a module: discovered, nothing to wire
uv run sandloom test                         # tests + every gate (never writes)
uv run sandloom migrate upgrade-all --apply  # core + module chains
uv run sandloom dev run                      # http://127.0.0.1:8000/health
```
<!-- quickstart:end -->

`uv run sandloom test` ends with `sandloom test: OK`; the API answers on
`http://127.0.0.1:8000` (`/health`, and `/api/v1/notes/items` with a bearer
token). The CI job `quickstart` (`tools/smoke/quickstart_check.py`) runs these lines during full validation. See
[CI profiles](../contributing/ci.md) for the automatic and full checks.

!!! note "The dev services"
    `uv run sandloom dev up` starts Postgres and Redis in Docker. It picks free
    ports for this app once and keeps them, so two apps never fight over a
    port. It writes the app's URLs into a marked block of `.env` and the test
    URLs into `.env.test.local`. Run it again whenever you like: it only
    checks that everything is up.

    - `uv run sandloom dev status` shows the ports and the URLs. Paste the
      URL into a database GUI.
    - `uv run sandloom dev run` serves on port 8000. When that port is taken,
      it says by what; `--port auto` picks a free one.
    - A git worktree of the app shares the containers and gets its own
      database: run `uv run sandloom dev up` in the worktree too.
    - Each `sandloom test` run gets its own test database, so two runs never
      clash.
    - `uv run sandloom dev down` stops the containers; the data stays.

For an app created with `--with identity`, follow [authentication and email](../modules/batteries/first-access-token.md)
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
