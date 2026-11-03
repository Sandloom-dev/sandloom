---
title: Installation
description: What Sandloom needs, how to install it from PyPI and which packages and extras exist.
type: how-to
---

# Installation

You need:

- **Python 3.11+** (3.12 recommended);
- **[uv 0.13.4+](https://docs.astral.sh/uv/)**, the package manager Sandloom uses;
- **PostgreSQL and Redis**: Docker with Compose runs them for you, or use servers you
  already run (Homebrew, Postgres.app) with `uv run sandloom dev up --services external`.

## Create an application

Nothing has to be installed first. `uvx` runs the published CLI: <!-- sandloom-release -->

```bash
uvx --from "sandloom[standard]==0.7.0a9" sandloom new myapp --with identity,tenancy,billing
```

Then follow the [quickstart](index.md) from its `cd myapp` step. `uv sync` inside the
project installs the framework and the modules you chose.

At a terminal, leave out the name and the flags: `sandloom new` asks for them,
and it can run the setup for you. Scripts and coding agents add `--yes`, so
nothing waits for an answer.

## The packages

| Package | What it is |
| --- | --- |
| `sandloom` | the framework: the SDK, the kernel and the CLI |
| `sandloom-identity` | accounts, sign-in, roles, 2FA, SSO |
| `sandloom-tenancy` | organizations, roles, invitations |
| `sandloom-billing` | subscriptions, credits, Stripe |
| `sandloom[standard]` | `sandloom` with all three modules |

To work on the framework itself, see [Set up the repository](../contributing/setup.md).
