# Settings fixture modules (ADR-0015, KB-50)

Business modules that live outside `src/` and declare settings through the
public SDK only. Adding them edits no kernel file (P1): they load by
`SANDLOOM_MODULES` with this directory on `PYTHONPATH`, in the subprocess
tests of `tests/integration/test_module_settings_fixture.py`.

- `kb_inventory`: a secret (`KB_INVENTORY_API_TOKEN`), a bounded ops setting
  (`KB_INVENTORY_RESERVE_TTL_SECONDS`, 1..3600), a semantic setting with a
  declared risk (`KB_INVENTORY_ALLOW_OVERSELL`, `kb_inventory.oversell_in_prod`),
  a production check, a removed setting (`KB_INVENTORY_LEGACY_SYNC`), a reader
  route and a `setup` reader.
- `kb_inventory_dup`: declares `KB_INVENTORY_RESERVE_TTL_SECONDS` again
  (SANDLOOM-B036 when both load).
- `kb_inventory_bad_import`: reads its settings at import (the import audit
  must fail it).
