# Ledger delta: an application on SQLite (infra-agnostic slices 8b, 8c and 9)

Slice 8b (2026-11-04) adds the accepted risk `development_database_in_prod`,
which admits a development-only database (SQLite) in production
(SANDLOOM-B029; the strict profile refuses it even when accepted), and a
guarantee for the refusal.

| Id | Disposition |
| --- | --- |
| G-RISK-03 | Retain: every registered risk is documented and pinned. The statement lists the thirteen current ids, `development_database_in_prod` among them; the pinning test is unchanged apart from the new id. |
| G-RISK-13 | New: a production process on a development-only database is refused unless `development_database_in_prod` is accepted, and the strict profile refuses it even then; enforced by `test_sqlite_boot.py`. |
| G-UOW-09 | New (slice 8c, approved by the owner 2026-11-05): on a serial-writer database a nested write unit of the same task runs as a SAVEPOINT of its caller and commits with it; its post-commit effects wait for the caller's COMMIT; labelled "commits with the caller"; PostgreSQL unchanged; enforced by `test_sqlite_nested_units.py`. |
| G-INFRA-09 | New (slice 9, approved by the owner 2026-11-05): a module using a database feature form declares the feature (C030); load refuses an instance lacking it (B033); a statement compiled without it refuses naming it (R015); enforced by `test_database_feature_forms.py`. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
