# Sandloom

**Python backend framework for AI-driven development.**

Build with people and AI agents while the framework enforces module boundaries,
owns transactions and provides shared mechanisms for authorization, tenancy,
event delivery and idempotency.

## Why Sandloom

- **AI-driven development:** a consistent module structure, typed contracts,
  CLI scaffolding, agent instructions and executable quality gates keep changes
  scoped as the application grows.
- **Enforced module boundaries:** business modules communicate through typed
  events and contracts. Architecture checks reject cross-module imports.
- **Pluggable infrastructure:** application modules use capability contracts
  instead of raw backend clients. Configure compatible registered drivers or
  implement adapters without spreading infrastructure SDKs through business code.
  PostgreSQL and Redis are the documented production backends.
- **Monolith to services:** deploy the same business modules together or in
  separate services using module selection and service bindings. Moving existing
  data to a module's own database is a separate offline extraction step.
- **Built-in mechanisms:** transaction ownership, one writer per request,
  authorization rechecks before commit, tenant isolation, a transactional outbox,
  durable consumers and idempotent commands.

## Install and create an application

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and Docker for local
PostgreSQL and Redis services. Sandloom is an alpha; `--pre` allows pip to select
its prerelease packages.

```bash
python -m pip install --pre "sandloom[standard]"
sandloom new myapp --with identity,tenancy,billing --module notes
```

Follow the printed next steps to install application dependencies, start local
services, run checks and migrations, and launch the API.

The `standard` extra includes Redis, Celery and the separate `sandloom-identity`,
`sandloom-tenancy` and `sandloom-billing` packages. Install `sandloom` alone for
the kernel, public `sandloom.sdk`, shared contracts, CLI and composition root.
Other extras: `redis`, `celery`, `fast`, `check`, `testing` and `sentry`.

## Inspect before editing

From the application root, after installing its dependencies:

```bash
uv run sandloom generate
uv run sandloom graph
uv run sandloom context notes
uv run sandloom explore
```

`graph` shows module purposes and declared connections; `context` gives a compact
map of one module's contracts, events and owned files. Both default to text;
request JSON explicitly with `--format json`. `explore` opens an offline browser
view with declared processes, interface impact and architecture comparisons.
See [module maps](https://sandloom.dev/docs/modules/tooling/module-context/) and the
[architecture explorer](https://sandloom.dev/docs/modules/tooling/architecture-graph/).

## Alpha status

Sandloom is under active development. Review the compatibility policy and release
notes before upgrading; a minor version in the `0.x` series may introduce
breaking changes. The framework's documented guarantees have specific scopes;
consult the guarantee reference for their mechanisms and limits.

## Links

- [Source and issue tracker](https://github.com/Sandloom-dev/sandloom)
- [Documentation](https://sandloom.dev/docs/)
- [Examples](https://github.com/Sandloom-dev/sandloom/tree/main/examples)
- [Compatibility policy](https://github.com/Sandloom-dev/sandloom/blob/main/docs/contributing/versioning.md)

## License

Apache-2.0. Copyright 2026 Vasyl Cherniavskyi.
See [LICENSE](https://github.com/Sandloom-dev/sandloom/blob/main/LICENSE) and [NOTICE](https://github.com/Sandloom-dev/sandloom/blob/main/NOTICE).
