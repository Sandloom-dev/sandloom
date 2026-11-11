# KB-09: the kernel applies token revocation (2026-11-08)

- G-REVOKE-07 (enforcing test moved, statement unchanged): the durable
  `core.auth.tokens_revoked.v1` applier moved from identity into the kernel
  (ADR-0012 decision 5), so its test
  `test_applier_never_moves_a_newer_mark_backwards` moved from
  `src/sandloom_identity/tests/test_revocation_applier.py` to
  `src/sandloom/core/tests/test_revocation_applier.py` and now drives the
  kernel's handler. The statement still names identity's consumer
  `sessions@tokens_revoked`; KB-09 was told not to touch the G-REVOKE-07/08
  statements, so their rewording is left to the owner.

No guarantee weakens and no capability is removed. New SDK name: the
`refresh_sessions=` parameter of `define_module` (SANDLOOM-B035). No new
setting or risk id of `SANDLOOM_ACCEPTED_RISKS`; `SANDLOOM_SERVICE_NAME`
becomes required on a shared event bus.
