# sandloom-identity

**Identity and authentication for Sandloom.**

Users, JWT authentication, role-based access control, two-factor authentication
and single sign-on for Sandloom applications.

Part of [Sandloom](https://github.com/Sandloom-dev/sandloom), the Python backend framework for AI-driven
development. This module is a separate distribution and does not depend on
sibling business modules.

## Included

- User accounts and authentication sessions.
- JWT access tokens and role-based permissions.
- Two-factor authentication and single sign-on integrations.

## Install

```bash
python -m pip install --pre sandloom-identity
```

This installs the module and its framework dependency. To create an application
with the module, use the Sandloom CLI:

```bash
sandloom new myapp --with identity
```

The import package is `sandloom_identity`. Installation registers the
`sandloom.modules` entry point `identity`; `sandloom.main:app` discovers installed
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
- [Documentation](https://github.com/Sandloom-dev/sandloom/tree/main/docs)
- [Examples](https://github.com/Sandloom-dev/sandloom/tree/main/examples)
- [Compatibility policy](https://github.com/Sandloom-dev/sandloom/blob/main/docs/contributing/versioning.md)

## License

Apache-2.0. Copyright 2026 Vasyl Cherniavskyi.
See [LICENSE](https://github.com/Sandloom-dev/sandloom/blob/main/LICENSE) and [NOTICE](https://github.com/Sandloom-dev/sandloom/blob/main/NOTICE).
