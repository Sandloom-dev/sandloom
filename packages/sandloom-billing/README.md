# sandloom-billing

**Subscriptions and billing for Sandloom.**

Plans, subscriptions, a credits ledger and Stripe integration for Sandloom
applications, with a fake payment provider for development and tests.

Part of [Sandloom](https://github.com/Sandloom-dev/sandloom), the Python backend framework for AI-driven
development. This module is a separate distribution and does not depend on
sibling business modules.

## Included

- Products, prices, plans and subscriptions.
- Credits, balances and ledger operations.
- Stripe payment integration and webhook handling.
- Typed contracts for cross-module billing access.

## Install

```bash
python -m pip install --pre sandloom-billing
```

This installs the module and its framework dependency. To create an application
with the module, use the Sandloom CLI:

```bash
sandloom new myapp --with billing
```

The import package is `sandloom_billing`. Installation registers the
`sandloom.modules` entry point `billing`; `sandloom.main:app` discovers installed
module entry points, and applications select their deployed modules with
`SANDLOOM_MODULES`. Other modules interact through typed contracts and events.
The framework ships the standard modules' generated contract DTOs, stubs and
event schemas in `sandloom.shared`.

The same module code runs in a monolith or a separate service. Infrastructure
configuration and service bindings belong to the application; moving existing
module data to its own database requires a separate extraction step.

This is alpha software. Review the compatibility policy before upgrading.

## Links

- [Source and issue tracker](https://github.com/Sandloom-dev/sandloom)
- [Documentation](https://sandloom.dev/docs/)
- [Examples](https://github.com/Sandloom-dev/sandloom/tree/main/examples)
- [Compatibility policy](https://github.com/Sandloom-dev/sandloom/blob/main/docs/contributing/versioning.md)

## License

Apache-2.0. Copyright 2026 Vasyl Cherniavskyi.
See [LICENSE](https://github.com/Sandloom-dev/sandloom/blob/main/LICENSE) and [NOTICE](https://github.com/Sandloom-dev/sandloom/blob/main/NOTICE).
