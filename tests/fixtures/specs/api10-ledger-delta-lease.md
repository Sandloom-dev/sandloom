# Ledger delta: the beat lease on the `lease` capability (idk-lease)

The beat lease is held in the `lease` instance `INFRA.lease.beat` instead of a cache instance.

| Id | Disposition |
| --- | --- |
| G-OPS-01 | Retain: beat still schedules only while it holds a fenced lease. The statement names a lease, not a cache lease, and the refusal is a production refusal of a lease that is not shared between processes (it was a refusal of a cache without `cas`). The enforcing tests in `test_beat_lease.py` run on the lease store. |
| G-INFRA-05 | Amend: the private `lock` capability is removed (idk-lease), so the statement no longer lists it among the experimental capabilities that raise `ExperimentalCapabilityError` without a driver. The enforcing tests in `test_extra_capabilities.py` are unchanged. |
| C-INFRA-06 | Amend: `lock` is removed from the experimental capability specs the kernel registers without a core driver, and `lease` (kernel-facing, with three core drivers) is not one of them. The enforcing tests are unchanged. |
| G-RISK-03 | Amend: the registry holds fourteen ids, the new one being `unverified_noeviction`, which the Redis `lease` driver and the Redis `atomic_store` driver share (one risk, one `assume_allowed` policy built by `sandloom.core.drivers.bootstrap.unverified_noeviction_allowed`). The pin in `test_ledger_runtime_pins.py` names it. |
