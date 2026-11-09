# Kernel tools read the modules' declarations; config dump (KB-55, 2026-11-09)

- C-OPS-09: `sandloom config dump` also lists, per loaded module, the settings
  that module declares (same source labels and redaction), builds the
  composition in collect mode to do it, and takes `--kernel-only` for the
  kernel's table alone, byte for byte as before. The statement and its tests
  are G-CONFIG-13's.
- G-CONFIG-13 (new): the dump's contract: module values with provenance and
  redaction, a module's invalid setting diagnosed with exit 0, collect mode a
  context variable only `config dump` and `doctor` set (off in every serving
  entry point), no lifespan, no served request, no connection (socket spy over
  the bundled modules), `--kernel-only` unchanged.
- G-CONFIG-14 (new): the kernel's tools read the loaded modules' declarations
  (`console`, `shared`, `backup`, `deploy`, `dev_env`, `dev_risks`) and name no
  module's setting: tiers and `/diagnostics/core` show the modules' accepted
  risks, overrides and tier counts, doctor the same and every module's refusal,
  the deploy-profile check holds a process to the shared values of the modules
  it loads only, the dev env, the backup plan, the console list, the docs
  generator and the deploy scaffold's environment come from the declarations.
- G-CONFIG-07: `sandloom config dump --kernel-only` builds no composition; the
  default dump builds it in collect mode and diagnoses a module's refusal.

No new SDK name, setting, risk kind or dedupe mechanism: the declarations are
plain class attributes of the settings model.
