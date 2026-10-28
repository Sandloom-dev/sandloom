# sandloom

The Sandloom framework: the kernel (`sandloom.core`: dependency injection, the
core facade, the ORM base, eventing with the transactional outbox, the
infrastructure capability registry, the migrations runner), the public SDK
(`sandloom.sdk`; `sandloom docs <name>`), the framework DTOs, guards and authz
algebra (`sandloom.shared`), the `sandloom` CLI (`sandloom new`, `sandloom add`,
`sandloom migrate`, `sandloom test`, ...) and the composition root
`sandloom.main:app`, which composes every installed module package.

```bash
pip install "sandloom[standard]==0.7.0a1"   # + Redis, Celery, identity, tenancy, billing
```

Extras: `redis`, `celery`, `standard` (Redis + Celery + `sandloom-identity`,
`sandloom-tenancy`, `sandloom-billing`), `fast` (hiredis, jsonschema-rs),
`check` (PyYAML for `sandloom check`), `testing` (pytest, pytest-asyncio,
pytest-xdist for the test kit) and `sentry` (imported only when `SENTRY_DSN`
is set). The kernel names no module: modules are separate distributions that
register a `sandloom.modules` entry point.

Source of truth lives in the monorepo at `src/sandloom`; this package builds
the wheel and source archive through the shared build hook.

## Project links

[Source](https://github.com/Sandloom-dev/sandloom) ·
[Documentation](https://sandloom.dev/docs/) ·
[Changelog](https://github.com/Sandloom-dev/sandloom/blob/main/packages/sandloom/CHANGELOG.md)

This is the initial `0.7.0a1` alpha distribution. The canonical source
repository is private and awaiting transfer; the documentation website is
not deployed yet. The project links above are the planned public locations.

## License

Apache-2.0. Copyright 2026 Vasyl Cherniavskyi. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
