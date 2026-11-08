# KB-W0 review fixes on KB-09: revocation applier, readiness, ledger (2026-11-08)

- G-REVOKE-07 (statement reworded, not weakened; test added): it named
  identity's consumer `sessions@tokens_revoked`, which KB-09 removed. It now
  names the kernel's applier `auth@tokens_revoked` and its per-service group,
  and adds `test_applier_writes_the_pending_mark_once_the_cache_is_back`.
  Every promise it made (outbox in the same UoW, retried write, counted
  deferral, idempotent and monotonic application) is kept.
- G-BLOCK-05 (statement reworded, not weakened): the user_blocked consumer is
  identity's `sessions@user_blocked` and, generally, the SANDLOOM-B035
  obligation of every token issuer (G-BLOCK-08).
- G-REVOKE-25 (new): the kernel registers the applier in every composition,
  identity loaded or not (D1 closed), one group per service.
- G-REVOKE-26 (new, with a gap): the `SANDLOOM_SERVICE_NAME` refusal on a
  shared bus, decided from the resolved `default` eventbus driver (`INFRA`
  applied, fail closed). Gap M: two services under one name, or one group
  consumed by processes with different module sets, is refused only
  statically by SANDLOOM-T014 within one profile; no existing inventory can
  detect it at runtime (one inventory entry per stream and group; the owner
  fingerprint compares module and database only, and adding the module set
  to it would refuse a rolling deploy that adds a module).
- G-REVOKE-27 (new): the `/ready` revocation criteria, including the
  fail-closed dead-letter count over the whole DLQ, one read per TTL and the
  lag gauge reset.
- G-BLOCK-08 (new): the SANDLOOM-B035 issuer obligation.
- ADR-0012 decision 5 maps to G-REVOKE-07, 25, 26, 27 and G-BLOCK-05, 08
  (it was `planned`).

No guarantee weakens and no capability is removed (I-16). No new setting,
error code or risk id. `SANDLOOM_SERVICE_NAME`'s description now says it
names the applier group.
