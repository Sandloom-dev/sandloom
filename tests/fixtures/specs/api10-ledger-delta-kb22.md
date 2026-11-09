# KB-22: bundled artifacts leave the kernel (2026-11-09)

The kernel wheel holds no generated contract stub, DTO copy or event schema of a module:
`sandloom/shared/contracts/`, `sandloom/shared/events/*.json` and `sandloom/shared/membership.py`
are gone (edges C1 to C5). A module keeps the copies of the contracts it consumes in its own
`_generated/` package and its producer schemas in `module.snapshot.json`. ADR-0013 decisions 9
and 10.

## Changed entries

- C-GEN-03 (changed, owner confirmation B5): the statement is the normative text quoted in
  ADR-0013 decision 10, unchanged. The kernel-home clause goes (a stub in `sandloom.shared`
  imported `ContractRef` from `sandloom.core`): a generated stub and a DTO copy import from the
  public `sandloom.sdk.contracts` wherever they are written, and `add contract` writes them into
  `<module>/_generated/contracts/` for a module and `<app>.shared/contracts/` for an application.
  `test_the_framework_tree_stubs_keep_the_kernel_home` is replaced by
  `test_the_kernel_holds_no_generated_file` (no generated file, event schema or `shared/contracts`
  in the kernel tree); `test_an_application_stub_imports_the_public_home` gains the module-local
  home. Approval under I-16: the entry removes a case that no longer exists.
