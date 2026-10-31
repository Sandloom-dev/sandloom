# Ledger delta: the driver API (ADR-0011)

Drivers are found by name instead of through a `register(registry)` callable.

| Id | Disposition |
| --- | --- |
| C-INFRA-01 | Retain: a backend is a driver, not a kernel edit. The statement no longer names the registry calls, which are not the public way in. |
| C-INFRA-02 | Replace: the `sandloom.infra` group with a log-and-continue loader becomes the `sandloom.drivers` group, imported only when selected, with conflicts and load failures stopping startup. New tests in `test_driverkit.py`. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
