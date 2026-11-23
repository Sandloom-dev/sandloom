### Changed

- **Flat `STORAGE_TYPE` / `NOTIFIER_BACKEND` no longer replace the app's driver** (dx-config-no-silent). Both default to unset: an app whose `default_infra` (or `INFRA`) selects `s3` or `smtp` keeps that driver and its options (`bucket`, `host`) instead of coming out as `local` / `log` with the options dropped. With nothing selected the object store is still `local` and the notifier `log`. A flat setting you set explicitly still wins over the composition, and now logs a warning that the composition's choice and its options are dropped; `INFRA` over an explicit flat setting logs that the flat setting is ignored. Upgrade: if you set `STORAGE_TYPE=local` or `NOTIFIER_BACKEND=log` only to get the default, delete the line.
- **A misspelled framework setting warns in dev and test** (dx-config-no-silent). `TENANCY_MOD=single` was ignored without a word outside production, which refuses it. Every environment now logs `Unknown framework setting TENANCY_MOD is ignored; did you mean TENANCY_MODE?`, and `sandloom doctor` and `sandloom config dump` list it.
- **The configuration reference is sorted** (dx-config-no-silent). `docs/reference/configuration.md` files every setting under a section (the ~100 settings under "Other" are placed), writes enum types as their values, drops a migration tier that does not exist, and lists `WORKER_CONCURRENCY` and `SANDLOOM_WORKER_TASKQUEUE`.

### Added

- **SANDLOOM-B042: a misspelled `INFRA` option is refused with the accepted keys** (dx-config-no-silent). `urll` for `url` raised a bare `extra_forbidden`. The refusal now names the instance and driver, every unknown key with the nearest accepted one, and the accepted keys. `UnknownDriverOptionError` is a `ConfigSchemaError`, so code that catches that keeps working.
