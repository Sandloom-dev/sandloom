# `sandloom generate` and provider-authored contract DTOs: ledger disposition

Two owner decisions of 2026-10-28 (design note:
`private/research/generate-and-provider-dtos-2026-10-28.md`; ADR-0010, note of
2026-10-28):

- **Generation is its own command.** `sandloom generate [module ...]` writes
  every generated file after the existing compatibility checks; `sandloom
  test` never writes one (dev and `--ci` alike) and fails on a stale file
  with "generated files are stale: run `sandloom generate`"; every `sandloom
  add` generator and `sandloom new` run it last. A weaker route policy keeps
  its explicit approval, `sandloom routes snapshot --write`.
- **Contract DTOs are authored in the provider.** `sandloom generate` writes a
  wire-shape copy of each DTO a provided signature names into the shared DTO
  package; the stub names the copy; a `Contract(REF)` caller receives the
  copy's class on both paths. SANDLOOM-C012 is restated: what it refuses is a
  DTO no faithful copy can come from (outside the closed set, another
  module's, function-local), a missing copy, or a hand-edited / stale one.

No guarantee is weakened and no capability is removed; every entry below
gains assertions or keeps its own.

| Id | Disposition |
| --- | --- |
| G-ROUTE-04 | Restated: `sandloom generate` (not `sandloom test`) never writes a weaker route policy; it fails with SANDLOOM-T001, writes nothing and names `sandloom routes snapshot --write`, and `sandloom test` never writes any generated file. New test `test_generate_refuses_a_weakened_route_and_names_the_approval`; every previous test kept. |
| G-CONTRACT-07 | Strengthened: a `Contract(REF)` caller receives each DTO as the class its generated stub names, on both paths. New test `test_a_caller_receives_the_copy_equal_to_the_providers_value` (local, RPC, RPC without the provider imported). |
| G-CONTRACT-08 | Restated for SANDLOOM-C012's new wording. The concern it guards (a caller decoding a provider class it cannot import as a `dict`) is kept: registration still refuses every DTO no copy can come from (another module's class: `test_nested_enum_of_another_loaded_module_is_refused`, kept; function-local: new `test_a_function_local_dto_is_refused`), the remote decode still never imports a peer-named module (`test_remote_decode_degrades_when_the_dto_is_not_importable`, kept) and now resolves a loaded generated copy (`test_remote_decode_resolves_a_loaded_generated_copy`). A DTO of the provider's own package is accepted (the generator copies it): `test_module_contract_with_own_package_dto_is_refused` became `test_module_contract_with_own_package_dto_is_accepted` by the owner decision. `sandloom check` (contract-dtos) now reports an own DTO without its generated copy, and covers L1 `@contract` classes too (`test_contract_dto_leaks_detected`, rewritten). |
| G-CONTRACT-19 | New: the generated copies, the stub's `ContractRef(name, Protocol)`, the caller-side conversion (fast path for frozen scalar DTOs), the provider receiving its own class. |
| G-COMPAT-01 | Restated: `sandloom generate` compares and writes (C012 DTO copies added to the compared files); `sandloom test` only checks. New tests `test_a_stale_file_asks_for_sandloom_generate_and_is_left_alone` and `test_sandloom_test_never_writes_a_generated_file`. |
| G-DECL-07 | New: the closed set of contract DTO shapes (the event set within the contract wire types, plus the copy's limits), refused at `@contract` with the field path (SANDLOOM-C012), and the property test that the copy accepts exactly what the provider's class accepts. |
| C-GEN-03 | Restated: `sandloom add contract` scaffolds the DTO in the module's schemas and bootstraps the stub and DTO copy exactly as `sandloom generate` renders them; the stub names the copies and passes its Protocol. Same tests, stronger assertions. |
| C-GEN-15 | New: the `sandloom generate` command, `sandloom test` never writing, `sandloom add` and `sandloom new` running generate last. |
| C-SDK-05 | Restated: the recipes check runs `sandloom generate` before `sandloom test` (the recipe files change declarations after the last `sandloom add`). Same tests. |

ADR-0010 gains the section "Note (2026-10-28): contract DTOs authored in the
provider, generated into the DTO package", mapped to G-CONTRACT-08,
G-CONTRACT-19, G-DECL-07, C-GEN-03 and C-GEN-15.

Moved without a ledger change: `sandloom.shared.schemas.contact.UserContact`
to `sandloom_identity.schemas.contact` (copy
`sandloom.shared.contracts.identity_dto`), `MemberState` to
`sandloom_tenancy.schemas.membership` (copy
`sandloom.shared.contracts.tenancy_dto`), the member-event ordering rule to
`sandloom.shared.membership`, `UserContext` to `sandloom.shared.subject`.
