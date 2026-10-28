# sandloom-tenancy

The Sandloom tenancy module: organizations, membership (owner/admin/member) and
invitations. Import package `sandloom_tenancy`; it registers the
`sandloom.modules` entry point `tenancy = "sandloom_tenancy"`, so its module
name is `tenancy` (tables `tenancy_*`, migration chain
`alembic_version_tenancy`, channels and contracts `tenancy.*`). Depends on
`sandloom>=0.7.0a1,<0.8` and email-validator; on no other module.

```bash
pip install "sandloom[redis,celery]==0.7.0a1" sandloom-tenancy==0.7.0a1
```

`sandloom.main:app` composes it once installed; an app selects it with
`SANDLOOM_MODULES=tenancy` (or `sandloom new <app> --with tenancy`). Other
modules call it through its contracts; their DTOs, stubs and event schemas
live in `sandloom.shared` and ship with `sandloom`.

Source of truth lives in the monorepo at `src/sandloom_tenancy`; this package
builds the wheel and source archive.

## Project links

[Source](https://github.com/Sandloom-dev/sandloom) ·
[Documentation](https://sandloom.dev/docs/) ·
[Changelog](https://github.com/Sandloom-dev/sandloom/blob/main/packages/sandloom-tenancy/CHANGELOG.md)

This is the initial `0.7.0a1` alpha distribution. The canonical source
repository is private and awaiting transfer; the documentation website is
not deployed yet. The project links above are the planned public locations.

## License

Apache-2.0. Copyright 2026 Vasyl Cherniavskyi. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
