# Ledger delta: the admin console as a package (2026-11-01)

## New entries

| id | what | enforced by |
| --- | --- | --- |
| C-CONSOLE-01 | the wheel carries the build of the current source | `test_admin_console_package.py` |
| C-CONSOLE-02 | off in production by default, no foreign code | `test_console.py` |
| C-CONSOLE-03 | the manifest is generated and checked | `test_generated_admin_manifest.py`, `test_console.py` |
| C-CONSOLE-04 | the first administrator | `test_security_recovery_ops.py` |
