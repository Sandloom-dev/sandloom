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
    The current alpha is `0.7.0a12`. All five packages are available on <!-- sandloom-release -->
    [PyPI](https://pypi.org/project/sandloom/0.7.0a12/). The source is maintained in
    [Sandloom-dev/sandloom](https://github.com/Sandloom-dev/sandloom).

You need Python 3.11+ and [uv 0.13.4+](https://docs.astral.sh/uv/). The app
also needs PostgreSQL and Redis: Docker runs them for you, or use the
servers you already have (see "Without Docker" below). In an empty directory, generate an application from the published alpha:

```bash
uvx --from "sandloom[standard]==0.7.0a12" sandloom new myapp --with identity,tenancy,billing
```

This starter selects all three [standard modules](../modules/batteries/index.md). For a
minimal backend, use
`uvx --from "sandloom==0.7.0a12" sandloom new myapp --no-batteries --module notes`
instead. That command already creates `notes`, so its printed steps add a
different module; the other steps are the same. The
[README](https://github.com/Sandloom-dev/sandloom#quickstart) demonstrates that minimal path.

!!! tip "Or answer a few questions"
    At a terminal, `sandloom new` without a name guides you step by step.
    It asks for the name, the standard modules and the coding agents. Then
    it asks whether Docker should run PostgreSQL and Redis, or servers you
    already have. Last, it offers to set the app up.

    ```bash
    uvx --from "sandloom[standard]==0.7.0a12" sandloom new
    ```

    A yes to the setup runs the steps below up to the migrations. With the
    console, it then asks for the administrator's email, and
    `sandloom admin create` asks for the password itself: the wizard never
    sees it. The summary says how to start the app and what to open. It
    ends with one command that makes the same app again without questions.
    Scripts and coding agents pass `--yes`: they get no question, and the
    output is the one below.

`sandloom new` also makes the app a git repository with a first commit, as
`cargo new` and `rails new` do; `--no-git` skips it. Inside an existing
repository it creates none and commits nothing: the files stay there for you
to commit. The first commit runs no hooks and is not signed; your own
commits keep your settings.

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
uv run sandloom dev run                      # serves the app; prints its address
```
<!-- quickstart:end -->

`uv run sandloom test` ends with `sandloom test: OK`; the API answers on
the address `sandloom dev run` prints, `http://127.0.0.1:8000` when that port
was free (`/health`, and `/api/v1/notes/items` with a bearer token). The CI job `quickstart` (`tools/smoke/quickstart_check.py`) runs these lines during full validation. See
[CI profiles](../contributing/ci.md) for the automatic and full checks.

!!! note "The dev services"
    `uv run sandloom dev up` starts Postgres and Redis in Docker. It picks free
    ports for this app once and keeps them, so two apps never fight over a
    port. It writes the app's URLs into a marked block of `.env` and the test
    URLs into `.env.test.local`. Run it again whenever you like: it only
    checks that everything is up.

    - `uv run sandloom dev status` shows the ports and the URLs. Paste the
      URL into a database GUI.
    - `uv run sandloom dev run` serves on this app's own port: 8000, or the
      next free one when 8000 was taken at the first `dev up`. It keeps that
      port. When something else holds it later, it says what; `--port auto`
      picks a free one.
    - A git worktree of the app shares the containers and gets its own
      database: run `uv run sandloom dev up` in the worktree too.
    - Each `sandloom test` run gets its own test database, so two runs never
      clash.
    - `uv run sandloom dev down` stops the containers; the data stays.
    - Two apps in one repository share the containers; each gets its own
      database, port and `.env` block.

!!! note "Without Docker"
    PostgreSQL and Redis from Homebrew, Postgres.app or a package manager
    work too. Point `dev up` at them once:

    ```bash
    uv run sandloom dev up --services external \
        --database-url postgresql://$USER@127.0.0.1:5432/postgres \
        --redis-url redis://127.0.0.1:6379
    ```

    Both URLs are optional when the servers answer on these usual local
    ports. The role must be allowed to create databases. `dev up` checks
    both servers and creates this app's database there. It writes the same
    `.env` block and `.env.test.local`, and keeps the choice: `dev up`,
    `dev run` and `dev status` never call Docker after that. When a server
    does not answer, `dev run` says how to start it. `dev down` has nothing
    to stop. `--services docker` switches back.
With `--with identity`, development prints the links of the emails identity
sends (verification, password reset) in the `sandloom dev run` output instead
of mailing them. For tokens, see [your first access token](../modules/batteries/first-access-token.md).

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
