# Workspace integration tests

These tests exercise more than one business module together (or a business
module's contribution to a kernel mechanism) and therefore import
`sandloom_identity` / `sandloom_tenancy` / `sandloom_billing` / `sandloom.shared`.

They deliberately live **outside** `src/sandloom/core/tests/` so the kernel's own
test suite stays importable without any module package installed — a
prerequisite for shipping `sandloom` as a standalone package (see
[ADR-0001](../../docs/adr/0001-framework-distribution.md), boundary
remediation #2).

The repository builds four distributions (`sandloom` and the module packages
`sandloom-identity`, `sandloom-tenancy`, `sandloom-billing`); this directory is
the workspace integration suite that depends on all of them.
`test_package_distributions.py` (slow) wraps
`tools/packaging/check_package_distributions.py`.
</content>
