# KB-W1 fix: ledger entries and ADR mappings for wave W1 (2026-11-08)

W1 (KB-10, KB-20, KB-40, KB-50) built decisions of ADR-0012..0015 that the
ledger still listed as `planned`, and only KB-10 added an entry. This change
adds entries for what the existing tests already enforce and maps the ADR
headings to them. Every enforcing test listed below existed before this
change, except the two new tests of the settings typo check.

## New entries

- G-CONTRACT-20 (new, enforced; KB-20): a module's `consumes` is the single
  source of the copies generated into it, planned from the AST before
  composition; an unresolved method list, an import with no `consumes` entry
  and a planned set that differs from the composed one are refused; kernel
  families get no copy. ADR-0013 decision 2.
- G-CONTRACT-21 (new, enforced; KB-20): `<stem>.describe.json` narrowed to
  the consumed methods; a field added to a closed result DTO is
  incompatible. ADR-0013 decision 3 (its generation side; the startup
  narrowing is KB-21's and adds to this entry or its own).
- G-CONTRACT-22 (new, enforced; KB-20): provenance header with a body
  digest; provider absent keeps a verified copy, refuses a hand-edited or
  missing one (SANDLOOM-T018). ADR-0013 decisions 5 and 7.
- G-CONTRACT-23 (new, enforced; KB-20): byte-identical generation without
  the project formatter; stem collisions refused naming both. ADR-0013
  decisions 6 and 8 (the per-contract copy registry of decision 8 comes
  with KB-23).
- G-KERNEL-14 (new, enforced; KB-40): a module-declared generated file is
  rendered from data the kernel collects, the kernel names neither the
  console nor its file, a stale file fails `generate --check` and
  `sandloom test` (SANDLOOM-T016), and a bad declaration is a problem naming
  its module. ADR-0012 decision 4.
- C-GEN-16 (new, enforced; KB-40): a third-party module's screens reach the
  console's manifest; console and identity work out of the box. ADR-0012
  decision 4.
- G-CONFIG-09 (new, enforced; KB-50): declaration on registration (plain
  pydantic model, validation context, `settings_of`, no import-time read).
  ADR-0015 decision 1.
- G-CONFIG-10 (new, enforced; KB-50): one source read per process.
  ADR-0015 decision 2.
- G-CONFIG-11 (new, enforced; KB-50): validation before `setup`;
  `ModuleSettingsError` fatal for an optional module too and in every
  composed entry point. ADR-0015 decision 3.
- G-CONFIG-12 (new, enforced; KB-50 and this change): the post-load check
  (SANDLOOM-B036, a live removed setting, the prod typo check over the
  union, override logging). ADR-0015 decision 3.
- C-TEST-16 (new, enforced; KB-50): `override_settings` and
  `validate_settings`. ADR-0015 decision 1.

## ADR headings mapped

- ADR-0012 decision 4 -> G-KERNEL-14, C-GEN-16. The admin settings
  (`ADMIN_CONSOLE_*` as admin's declared settings) and the console's issuer
  check (KB-41) are not built yet; their PRs add to the heading.
- ADR-0013 decisions 2, 3, 5, 6, 7, 8 -> G-CONTRACT-20..23.
- ADR-0014 decisions 1, 2 and 7 -> G-AUTH-20 (KB-10 named these three in
  its delta and left them `planned`). Decision 7's status codes (501 with
  the capability absent, 402/403) come with KB-30.
- ADR-0015 decisions 1, 2, 3 -> G-CONFIG-09..12, C-TEST-16.

Every ADR still has a `planned` heading, so `planned_adrs` is unchanged.

## Behaviour change: the prod typo check and absent modules

The post-load typo check (G-CONFIG-12) scanned every process variable
against the declared names at cutoff 0.95, so a variable of a module another
service loads (`SHOP_EU_REGIONS` of `shop_eu`) refused the boot of a process
that loads `shop` with `SHOP_EU_REGION`. A variable under the prefix
(`<NAME>_`, longest match) of a module known in the deployment but not
loaded is now skipped (answer #25; known namespaces per answer #27: the
installed `sandloom.modules` entry points and the modules `SANDLOOM_MODULES`
names, read only once a near-miss is found). A near-miss under a loaded
module's own prefix, or of a module nobody knows, still refuses. The kernel's
own typo check (`Settings`) is unchanged. Unprefixed legacy module names
still in the kernel's `ModulesConfig` are kernel names until KB-52..54 move
them.

No guarantee weakens (I-16): the typo check stops refusing only variables
the ADR says must not interfere. No new SDK name, setting or risk id.
