# Module ecosystem distributions: ledger disposition

The distributions were restructured for a module ecosystem (owner decision
2026-10-28): one `sandloom` distribution (kernel, `sandloom.sdk`, CLI) and the
standard modules as ecosystem packages `sandloom-identity`, `sandloom-tenancy`
and `sandloom-billing` (import names `sandloom_identity`, ...), discovered
through the `sandloom.modules` entry-point group. Statements and test paths
change with the package layout; this records the dispositions.

| Id | Disposition |
| --- | --- |
| C-MODULE-03 | Restated for the new distributions: `sandloom` plus one package per `--with` module, compatible ranges; same import-ships and kernel-factory tests, the tier test renamed with its new assertions. |
| G-KERNEL-13 | Restated: the standard root composes installed entry points; the S gap is closed by `test_the_kernel_names_no_module`. |
| C-MODULE-12 | New: entry-point discovery, module names and the SANDLOOM-B020 ambiguity refusal. |
| C-MODULE-13 | New: import layering across distributions and the clean-venv install of `sandloom` alone. |

Module tests moved with their packages (`src/sandloom/<m>/` to
`src/sandloom_<m>/`; `tests/fixtures/specs/layout-path-map.json` maps the new
paths for the pre-wave fingerprints). No guarantee, enforcing assertion or
residual-risk requirement is removed.
