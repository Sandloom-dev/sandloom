### Changed

- **A `sandloom new --no-batteries` app starts in single-tenant mode** (dx-nobatteries-single-tenant). The generated `.env` writes `TENANCY_MODE=single` and a `SINGLE_TENANT_ID` generated for the app, so `sandloom dev token` with no flag reaches the generated module's route (`200`). In the default `TENANCY_MODE=multi` no token can bind a tenant in an app without the tenancy battery, and that route answered `400 tenant_required` (`403 tenant_not_authorized` with an `X-Tenant-Id`). `.env.example` carries the two lines commented out, and the generated README says so.
  Upgrade: an app made earlier adds the two lines to its `.env` by hand, with any UUID ([first access token](docs/modules/batteries/first-access-token.md#an-application-without-batteries)). Apps made with `--with` are unchanged.
  Docs: `sandloom dev token` joins the `sandloom dev` row of [the CLI reference](docs/reference/cli.md).
