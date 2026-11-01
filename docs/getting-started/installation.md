---
title: Installation
description: What Sandloom needs, how to install it from PyPI and which packages and extras exist.
---

# Installation

You need:

- **Python 3.11+** (3.12 recommended);
- **[uv 0.13.4+](https://docs.astral.sh/uv/)**, the package manager Sandloom uses;
- **Docker with Compose**, for the local PostgreSQL and Redis.

## Create an application

Nothing has to be installed first. `uvx` runs the published CLI: <!-- sandloom-release -->

```bash
uvx --from "sandloom[standard]==0.7.0a5" sandloom new myapp --with identity,tenancy,billing
```

Then continue with the [quickstart](index.md). `uv sync` inside the project installs
the framework and the modules you chose.

## The packages

| Package | What it is |
| --- | --- |
| `sandloom` | the framework: the SDK, the kernel and the CLI |
| `sandloom-identity` | accounts, sign-in, roles, 2FA, SSO |
| `sandloom-tenancy` | organizations, roles, invitations |
| `sandloom-billing` | subscriptions, credits, Stripe |
| `sandloom[standard]` | `sandloom` with all three modules |

To work on the framework itself, see [Set up the repository](../contributing/setup.md).
