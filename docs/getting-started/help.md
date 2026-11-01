---
title: Getting help
description: Find answers, report a reproducible Sandloom problem and report security vulnerabilities through the appropriate private channel.
---

# Getting help

For a framework error, start with its code:

```bash
uv run sandloom explain SANDLOOM-C001
uv run sandloom docs db
```

The [error catalogue](../reference/error-codes.md) explains every code and its
fix. The [CLI reference](../reference/cli.md) lists commands; each command's
`--help` is authoritative for the installed version. Local documentation search
works without an external service.

## Ask or report a problem

Use the public [GitHub issue tracker](https://github.com/Sandloom-dev/sandloom/issues).
GitHub Discussions is not currently the support channel. Include:

- the installed Sandloom/Python versions and operating system;
- the smallest application or sequence of commands that reproduces the issue;
- expected and actual behavior, the error code and a redacted traceback;
- the test-order seed when a failure depends on order.

Run `uv run sandloom version` for the framework version. Remove credentials,
authentication tokens, personal data and production URLs before posting logs
or configuration. A small reproduction is more useful than a production dump.
There is no guaranteed support response time.

## Security reports

Do not disclose a suspected vulnerability in a public issue. Use
[GitHub Private Vulnerability Reporting](https://github.com/Sandloom-dev/sandloom/security/advisories/new)
and follow the repository's
[security policy](https://github.com/Sandloom-dev/sandloom/blob/main/SECURITY.md).

## Keep learning

Start with the [quickstart](index.md) and the [tutorial](tutorial.md). For an existing
application, use the [upgrade checklist](../operate/upgrade.md).
