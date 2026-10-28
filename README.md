# Sandloom

A Python framework for modular SaaS. Build isolated business modules with typed
contracts, durable events, tenant isolation and framework-owned transactions.
Deploy the same codebase as a monolith or split services by configuration;
moving live data into a service database is a separate offline migration.

Created by Vasyl Cherniavskyi.

The initial `0.7.0a1` alpha is available on [PyPI](https://pypi.org/project/sandloom/0.7.0a1/). The
[source snapshot policy](docs/contributing/source-export.md) describes its
reviewed contents. Apache-2.0 has been selected and distribution artifacts
have been checked locally; public source transfer and agent DX evaluation remain pending.

## Quickstart

The canonical repository is [Sandloom-dev/sandloom](https://github.com/Sandloom-dev/sandloom).
It is still private and awaiting the prepared source snapshot. The website at
[sandloom.dev](https://sandloom.dev/) is not deployed yet. The four alpha
packages can be installed from PyPI without access to the source repository; the
[installation guide](docs/getting-started/installation.md) covers contributor setup.

With Python 3.11+, uv 0.13.4+ and Docker:

```bash
uvx --from "sandloom[standard]==0.7.0a1" sandloom new myapp --with identity,tenancy,billing
cd myapp
uv sync
docker compose -f deploy/docker-compose.dev.yml up -d --wait
uv run sandloom add module notes
uv run sandloom generate
uv run sandloom test
uv run sandloom migrate upgrade-all --apply
uv run uvicorn myapp.asgi:app --reload
```

The generated app owns its namespace and imports only the public `sandloom.sdk`.
The app depends on `sandloom[redis,celery]` plus one `sandloom-<module>` package
per `--with` module, resolved from PyPI. Contributors can select a local checkout
with `--framework-source path --framework-path <checkout>`.
The API runs at `http://127.0.0.1:8000`; check `/health`. The generated compose
uses ports 5432/6379 for development and 5433/6380 for tests. If these are
already in use, follow the port overrides in the generated README before starting it.

## Why Sandloom

- Atomic business modules, with enforced import boundaries.
- Units of work, tenant isolation, durable events and command deduplication.
- Identity, organizations and billing as installable module packages.
- One codebase: monolith or split services, selected by configuration.
- A public SDK, code generators, testing kit and agent instructions.

## Repository

- [src/sandloom](src/sandloom): kernel, public SDK, CLI and composition root.
- `src/sandloom_identity`, `src/sandloom_tenancy`, `src/sandloom_billing`: the
  standard modules, each shipped as its own package.
- [packages](packages/README.md): the four Python distributions (`sandloom`,
  `sandloom-identity`, `sandloom-tenancy`, `sandloom-billing`); the root project
  `sandloom-dev` is a contributor environment, not a published package.
- [docs](docs/index.md): Zensical documentation.
- [examples](examples/README.md): tutorial, Linkly, Helpdesk, Academy and Projecthub.
- [admin](admin/README.md): optional owner console.
- [deploy](deploy/README.md): reference deployment profiles and observability.
- [contributor guide](CONTRIBUTING.md), [security](SECURITY.md),
  [changelog](CHANGELOG.md) and [agent map](AGENTS.md).

## One website

`website/` supplies the homepage at `/` and the application catalog at `/examples/`;
Zensical builds `docs/` under `/docs/`. Architecture lives in the docs' Concepts
section. The top navigation distinguishes documentation from example applications.
The release site is one static directory, `build/site/`, for `sandloom.dev`:
the homepage at `/`, documentation at `/docs/` and examples at `/examples/`.
The owner has registered the domain; the public website is not deployed yet.
The homepage and Zensical share the header in `website/theme/partials/site-header.html`,
design tokens in `website/assets/brand.css` and the site-wide theme preference.
Search uses Zensical's native UI and the same documentation index on both routes.
The shared mobile menu and desktop docs sidebar use Zensical's resolved navigation
from `zensical.toml`; the homepage reuses the same rendered tree during the build.
Technical text uses self-hosted Hack Nerd Font Mono web subsets; provenance and
license are in `website/assets/fonts/`. Shared CSS/JS URLs include content versions.

```bash
uv run --extra docs python tools/docs/docs_site.py build
uv run --extra docs python tools/docs/docs_site.py serve --dev-addr 127.0.0.1:8766
```

The builder applies one favicon set, page-specific title/description and Open
Graph / X metadata to the homepage, examples and every documentation page.
Sharing images are prepared PNG assets, so the build needs no graphics tools.
The default local build uses loopback URLs and `noindex`. For the future public
site, build with `--site-url https://sandloom.dev` (the origin, without `/docs/`);
this sets all canonical and sharing URLs, enables indexing, and writes one
sitemap and `robots.txt` for the entire site. This build option does not deploy
the site or change DNS. See the
[brand asset sources](website/assets/social/README.md) for regeneration details.

The unified site has been built locally with Zensical's strict mode. See the
[source snapshot policy](docs/contributing/source-export.md) for export checks. Hosting and
monitored release contacts still need configuration before publication.

## License

Original Sandloom framework code, documentation and examples are licensed under
the [Apache License 2.0](LICENSE). Copyright 2026 Vasyl Cherniavskyi. See [NOTICE](NOTICE).
Third-party components and assets retain their own licenses and notices.
Internal preparation material under `private/` is excluded from the public
distribution and this license grant.
