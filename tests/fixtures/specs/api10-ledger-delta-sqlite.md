# Ledger delta: an application on SQLite (infra-agnostic slice 8b)

Slice 8b (2026-11-04) adds the accepted risk `development_database_in_prod`,
which admits a development-only database (SQLite) in production
(SANDLOOM-B029; the strict profile refuses it even when accepted), and a
guarantee for the refusal.

| Id | Disposition |
| --- | --- |
| G-RISK-03 | Retain: every registered risk is documented and pinned. The statement lists the thirteen current ids, `development_database_in_prod` among them; the pinning test is unchanged apart from the new id. |
| G-RISK-13 | New: a production process on a development-only database is refused unless `development_database_in_prod` is accepted, and the strict profile refuses it even then; enforced by `test_sqlite_boot.py`. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
