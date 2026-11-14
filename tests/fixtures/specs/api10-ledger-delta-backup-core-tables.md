# Ledger delta: `sandloom backup fence-leases` (backup-core-tables)

After a database restore, `sandloom backup fence-leases` lifts every `infra_lease` fence of the database lease driver and releases the leases.

| Id | Disposition |
| --- | --- |
| G-OPS-25 | new: after a database restore `sandloom backup fence-leases` lifts every `infra_lease` fence by 10**12 and releases every lease (dry-run unless `--apply --reason`), builds the core before any write, commits each database's bump with its `core.lease.fences_bumped` audit row, and refuses a database that takes its schema from the models; a lease name first acquired afterwards starts above the highest fence of the table. Enforced by `test_backup_cli.py` (`test_fence_leases_*`, `test_core_tables_equal_what_the_core_chain_creates`) and `test_lease.py` (`test_lift_fences_*`, `test_a_new_name_starts_above_the_highest_fence`). |
