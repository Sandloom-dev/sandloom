# Ledger delta: the driver-import places (idk-db-provider)

`sandloom.core._internal.db_provider` no longer imports a driver; the test harness reaches the built-in SQL driver through `sandloom.core.testing._driver_seams`.

| Id | Disposition |
| --- | --- |
| G-INFRA-01 | reworded: concrete drivers are imported only by the composition layer (`app_factory`, `testing/_driver_seams`), no longer by `_internal.db_provider`; `get_db` takes its session from the declared `session_factory` of the active core's `default` database. Enforced by `test_core_kernel_layering.py` (`DRIVER_IMPORT_ALLOWLIST`) and `test_db_provider_get_db.py`. |
