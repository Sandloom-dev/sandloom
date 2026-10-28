# sandloom-billing

The Sandloom billing module: plans, subscriptions, the credits ledger and the
Stripe integration (with a `fake` PSP default). Import package
`sandloom_billing`; it registers the `sandloom.modules` entry point
`billing = "sandloom_billing"`, so its module name is `billing` (tables
`billing_*`, migration chain `alembic_version_billing`, channels and contracts
`billing.*`). Depends on `sandloom>=0.7.0a1,<0.8`; on no other module.

```bash
pip install "sandloom[redis,celery]==0.7.0a1" sandloom-billing==0.7.0a1
```

`sandloom.main:app` composes it once installed; an app selects it with
`SANDLOOM_MODULES=billing` (or `sandloom new <app> --with billing`). Other
modules call it through its contracts; their DTOs, stubs and event schemas
live in `sandloom.shared` and ship with `sandloom`.

Source of truth lives in the monorepo at `src/sandloom_billing`; this package
builds the wheel and source archive.

## Project links

[Source](https://github.com/Sandloom-dev/sandloom) ·
[Documentation](https://sandloom.dev/docs/) ·
[Changelog](https://github.com/Sandloom-dev/sandloom/blob/main/packages/sandloom-billing/CHANGELOG.md)

This is the initial `0.7.0a1` alpha distribution. The canonical source
repository is private and awaiting transfer; the documentation website is
not deployed yet. The project links above are the planned public locations.

## License

Apache-2.0. Copyright 2026 Vasyl Cherniavskyi. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
