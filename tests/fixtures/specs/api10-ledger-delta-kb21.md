# KB-21: runtime expectations from the consumer's own copy (2026-11-09)

A consumer checks a remote provider against what it expects of it: the describe copy its own
module ships beside the generated stub, else `CONTRACT_SNAPSHOT_PATHS`, else the provider's
snapshot. A required remote contract with no expectation is never a warning. ADR-0013
decisions 3 and 4.

## Changed entries

- G-COMPAT-04 (changed): the expectation order and the consumer-only install are part of the
  statement. Four tests are added in `test_contract_compat_expectations.py`.
- G-COMPAT-07 (changed): the recorded gap (the startup log for a binding with no snapshot is
  not asserted) is closed by the G-COMPAT-08 tests. The statement and its test are unchanged.

## New entries

- G-COMPAT-08 (new, enforced): `CONTRACT_COMPAT_CHECK=strict` refuses startup before any probe
  for a required remote contract with no expectation, a copy whose digest does not verify, or a
  consumed method the copy does not describe; `/ready` fails with `contract_unverified`. The
  expectations are read at startup only. ADR-0013 decision 4.
