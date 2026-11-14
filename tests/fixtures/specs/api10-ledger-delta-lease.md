# Ledger delta: the beat lease on the `lease` capability (idk-lease)

The beat lease is held in the `lease` instance `INFRA.lease.beat` instead of a cache instance.

| Id | Disposition |
| --- | --- |
| G-OPS-01 | Retain: beat still schedules only while it holds a fenced lease. The statement names a lease, not a cache lease, and the refusal is a production refusal of a lease that is not shared between processes (it was a refusal of a cache without `cas`). The enforcing tests in `test_beat_lease.py` run on the lease store. |
