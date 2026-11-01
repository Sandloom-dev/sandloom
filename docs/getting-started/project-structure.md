---
title: Your project structure
description: What sandloom new generates - which files are yours, which are generated and how the framework reaches your application.
type: concept
---

# Your project structure

`sandloom new myapp` creates a standalone project. Your code lives in it, and the
framework arrives as a dependency in `pyproject.toml`, so an upgrade changes a version
rather than your files.

```text
myapp/
  pyproject.toml            # dependencies: sandloom, the standard modules
  .env                      # local settings, not committed
  .env.example              # the template for .env
  .env.test                 # settings for the tests
  myapp/
    asgi.py                 # the composition root: builds the application
    modules/
      example/              # your first module; each module is one package here
    shared/                 # generated: contract stubs, DTO copies, event schemas
  deploy/
    docker-compose.dev.yml  # Postgres and Redis for development and tests
    docker-compose.yml      # the monolith: migrations, app, worker, beat
  Dockerfile
  architecture_rules.yml    # the import boundaries `sandloom check` enforces
  .github/workflows/ci.yml  # the same checks in CI
  .pre-commit-config.yaml
  AGENTS.md, CLAUDE.md      # instructions for coding agents
  .agents/, .claude/        # skills for coding agents
  .sandloom/inspection.json # generated: what the inspection commands read
```

[What a module contains](../modules/index.md) has its own page.

## What you edit, and what you leave alone

| Files | Who changes them |
| --- | --- |
| `myapp/modules/` except the snapshots | you, or `sandloom add` as a starting point |
| `.env`, `pyproject.toml`, `deploy/` | you |
| `myapp/asgi.py` | rarely: a new module needs no change here |
| `myapp/shared/`, each module's `module.snapshot.json` and `routes.snapshot.json`, `.sandloom/inspection.json` | `sandloom generate`. Commit them and never edit them by hand |

`sandloom test` checks the generated files and never writes them. If it reports them out
of date, run [`sandloom generate`](../modules/tooling/generate.md).

## What comes from the framework

| Package | What it gives you |
| --- | --- |
| `sandloom` | `sandloom.sdk`, the only framework API your modules import, plus the CLI |
| `sandloom-identity`, `sandloom-tenancy`, `sandloom-billing` | the [standard modules](../modules/standard/index.md), if you chose them |

Choose the standard modules when you create the project: `--with identity,tenancy,billing`
for any subset, or `--no-batteries` for none. All options are in the
[CLI reference](../reference/cli.md).

A standard module runs in your application but stays in its package. Your modules reach it
through the contract stubs in `myapp/shared/`, the same way they reach each other. To
change its behaviour, see [Customize a standard module](../modules/standard/customize.md).
To move to a new framework version, see [Upgrade an application](../operate/upgrade.md).

## How the application starts

`myapp/asgi.py` loads every package in `myapp/modules` that defines `register`, plus the
standard modules you chose, so a new module is picked up on its own. Set
`SANDLOOM_MODULES` to load a subset, which is how one codebase
[runs as several services](../operate/split-into-services.md).
