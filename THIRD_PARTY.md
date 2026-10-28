# Third-party material

Sandloom's Apache-2.0 license covers its original code, documentation and
examples. Dependencies and third-party assets retain their own terms.

## Files in this source snapshot

| Material | Location | Source and terms |
| --- | --- | --- |
| Hack Nerd Font Mono text subsets, Regular and Bold | `website/assets/fonts/*.woff2` | Nerd Fonts v3.5.1, Hack / Source Foundry, DejaVu and Bitstream Vera. Exact upstream URLs, input/output SHA-256 and subset procedure are in `Hack-SOURCE.json` and `README.md`. The supplied MIT and Bitstream Vera notices are retained unmodified in `Hack-LICENSE.md`; DejaVu's contribution is public domain. |
| Manrope lettering in sharing artwork | `website/assets/social/sandloom-*.svg` and `.png` | The Manrope Project Authors, SIL Open Font License 1.1. `SOURCE.json` records the source Git blob and SHA-256; `Manrope-OFL.txt` retains the notice and license. Lettering is outlined in the artwork; the source TTF is not vendored. |
| Sandloom mark, favicon and page illustrations | `docs/assets/`, `website/assets/favicon*`, `website/theme/partials/home-art.html` | Sandloom source assets and code; sharing/icon regeneration is documented in `website/assets/social/README.md` and `tools/docs/brand_assets.py`. |
| API TypeScript types, contract stubs, event schemas and snippets | `admin/src/api/generated.ts`, `src/sandloom/shared/`, `docs/snippets/generated/`, example apps | Generated from Sandloom-owned declarations by the checked-in generators and installed development tools. Regenerate with their owning tools; generation does not relicense the generator or its dependencies. |

No third-party framework implementation, vendored Python environment, npm
installation, native prototype or old competitor application is selected for
the public source snapshot. Benchmark adapters exercise separately installed
FastAPI/Litestar dependencies; they are not copies of those frameworks.

## Installed dependencies and built output

- Python distributions are resolved by `uv.lock`; benchmark dependencies have
  their own `benchmarks/uv.lock`. Dependency source is installed separately and
  keeps its upstream license. Use `tools/checks/supply_chain.sh` for generating a dependency
  SBOM; an SBOM alone does not establish source ownership.
- Admin dependencies are resolved by `admin/package-lock.json`. Its production
  build writes `third-party-licenses.txt` from installed dependency license and
  notice files, including bundled fonts and icons, using
  `admin/scripts/licenses.mjs`. The npm project remains private.
- The documentation theme is supplied by the pinned Zensical dependency. The
  website also requests Manrope from Google Fonts; no Manrope webfont is copied
  into this repository. Keep the notices accompanying installed/build assets.
- Load-generator binaries are fetched separately by the benchmark tooling at
  pinned revisions. They are not part of the source export.

This inventory records the inspected source and retained notices. It does not
claim a legal clearance of every historical contribution or future dependency.
New copied code or assets require an upstream source, version and applicable
notice before adding their paths to the export policy.
