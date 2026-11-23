# Ledger delta: a business module that registers a listener at the cursor is reported by `sandloom check` (archcheck-cursor-listener)

A listener on a cursor-level event runs around the cursor tenant check: it can rewrite the statement
the check read, or make SQLAlchemy skip the check. `sandloom check` now reports the registration in a
business module, whatever its spelling, with `SANDLOOM-C039` (rule `cursor-listener`; ADR-0018,
decision 8). The runtime seam is unchanged.

| Id | Disposition |
| --- | --- |
| G-TENANT-29 | New: rule `cursor-listener` reports a listener on a cursor-level or a pool or connect event in a business module (migrations included, a module's tests not), the event module passed around or star imported, and the listener lists and dialect methods; enforced by `test_cursor_listener_rule.py`. |
| G-TENANT-25 | Changed: the "Not covered" text says the registration is refused statically in module code (G-TENANT-29) and stays open to code that is not a module; the runtime tests are unchanged. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
