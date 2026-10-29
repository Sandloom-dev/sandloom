# Dependency security audit — 2026-10-29

This audit covers the 51 open Dependabot alerts captured on 2026-10-29 for
[Sandloom at `30cd55f`](https://github.com/Sandloom-dev/sandloom/tree/30cd55f5ff6bfab16d0c561912701998982288fb).
The snapshot contains 8 critical, 26 high and 17 medium alerts: 41 Pillow,
7 Authlib, 2 certifi and 1 virtualenv. The certifi advisory occurs in two
manifests; alert count is not a count of distinct vulnerabilities or confirmed
exploitable Sandloom paths. Severity and affected ranges below preserve that
snapshot; upstream advisory records can change.

## Remediation targets

| Package | Affected manifest at the audited commit | Before | First patched / selected version |
| --- | --- | --- | --- |
| Pillow | `benchmarks/ladder/cg/requirements.txt` | `12.3.0` | `13.0.0` / `13.0.0` |
| Authlib | `benchmarks/ladder/cg/requirements.txt` | `1.8.0` | `1.9.0` for six alerts; `1.9.1` for #49 / `1.9.1` |
| certifi | `benchmarks/ladder/cg/requirements.txt`; `benchmarks/uv.lock` | `2026.7.22` | `2026.10.18` / `2026.10.19` |
| virtualenv | `uv.lock` | `21.14.12` | `21.14.13` / at least `21.14.13` |

The callgrind harness export belongs to Sandloom's benchmark environment. Its
Pillow/Authlib pins had drifted from `benchmarks/uv.lock`, which already selected
Pillow `13.0.0` and Authlib `1.9.1`. Regenerate the export from the corrected
benchmark lock rather than maintain another independent version list.

The main lock already selected Pillow `13.0.0`, Authlib `1.9.1` and certifi
`2026.10.19`. The identity distribution already required `pillow>=13.0.0,<14`,
so no additional core Pillow upgrade is needed. Its `authlib>=1.8.0,<2`
requirement still permitted vulnerable fresh resolutions and needs a
`>=1.9.1,<2` floor. Core already requires `certifi>=2026.10.19`.
These are findings from the committed [identity metadata](https://github.com/Sandloom-dev/sandloom/blob/30cd55f5ff6bfab16d0c561912701998982288fb/packages/sandloom-identity/pyproject.toml),
[core metadata](https://github.com/Sandloom-dev/sandloom/blob/30cd55f5ff6bfab16d0c561912701998982288fb/packages/sandloom/pyproject.toml),
[main lock](https://github.com/Sandloom-dev/sandloom/blob/30cd55f5ff6bfab16d0c561912701998982288fb/uv.lock)
and [benchmark lock](https://github.com/Sandloom-dev/sandloom/blob/30cd55f5ff6bfab16d0c561912701998982288fb/benchmarks/uv.lock).

Select certifi `2026.10.19` rather than stop at the first patched release:
`2026.10.18` removed all emSign roots, while `2026.10.19` restored unaffected
roots. The [reviewed advisory](https://github.com/advisories/GHSA-6q48-6c7w-9gw2)
recommends `2026.10.19` or later and rebuilding images that vendor the bundle.

## Compatibility review

Official release metadata declares Python `>=3.11` for
[Pillow 13.0.0](https://pypi.org/pypi/pillow/13.0.0/json), `>=3.10` for
[Authlib 1.9.1](https://pypi.org/pypi/Authlib/1.9.1/json), `>=3.7` for
[certifi 2026.10.19](https://pypi.org/pypi/certifi/2026.10.19/json), and `>=3.9`
for [virtualenv 21.14.13](https://pypi.org/pypi/virtualenv/21.14.13/json).
All fit Sandloom's Python `>=3.11` and the benchmark environment's `>=3.12`.
This checks interpreter eligibility, not runtime compatibility by itself.

- **Pillow:** version 13 drops Python 3.10 and removes direct `I`-mode PNG
  saving and `fromarray(mode=...)` dtype conversion. The reviewed Sandloom
  [QR utility](https://github.com/Sandloom-dev/sandloom/blob/30cd55f5ff6bfab16d0c561912701998982288fb/src/sandloom_identity/utils.py)
  and [2FA service](https://github.com/Sandloom-dev/sandloom/blob/30cd55f5ff6bfab16d0c561912701998982288fb/src/sandloom_identity/services/twofa_service.py)
  generate a QR image through `qrcode` and save PNG; neither directly calls
  those removed APIs. Keep QR generation in compatibility checks.
  [Pillow release notes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html)
- **Authlib:** `1.9.1` rejects empty HMAC keys and requires `joserfc>=1.6.8`.
  The `1.9` series also tightens nonce validation, JAR URI retrieval and JWT
  bearer audiences; RFC 7592 token implementations need `check_client()`.
  Review any application-provided integration that relies on those behaviors;
  do not weaken the new validation to recover compatibility.
  [Authlib changelog](https://docs.authlib.org/en/stable/upgrades/changelog.html)
- **virtualenv:** `21.14.13` fixes generated shell activator prompt escaping.
  It is development tooling through pre-commit in the main lock, not a
  Sandloom request handler. Verify the locked tool environment as well as the
  application tests. [virtualenv release history](https://virtualenv.pypa.io/en/latest/changelog.html#v21-14-13-2026-10-22)

The Authlib empty-HMAC upstream report still contains older affected-version
text; the [reviewed advisory's structured fields](https://github.com/advisories/GHSA-8m4g-j4wp-wgcg)
and the official `1.9.1` changelog agree on the patched release. The ledger uses
those patched-version fields rather than the stale narrative.

## Complete alert ledger

Every captured alert is listed once. Ranges and minimum fixes come from the
Dependabot snapshot's `security_vulnerability` fields; each GHSA links to its
public advisory. Package names are case-normalized for grouping.

### Pillow — 41 alerts

Manifest: `benchmarks/ladder/cg/requirements.txt`. Selected version: `13.0.0`.

| Alert | Advisory | Affected range | Minimum fix |
| --- | --- | --- | --- |
| #1 | [GHSA-8g5g-8vmr-9782](https://github.com/advisories/GHSA-8g5g-8vmr-9782) | `<= 12.3.0` | `13.0.0` |
| #2 | [GHSA-c6hw-5grf-ff96](https://github.com/advisories/GHSA-c6hw-5grf-ff96) | `>= 8.1.0, < 13.0.0` | `13.0.0` |
| #3 | [GHSA-462x-pgg6-55fp](https://github.com/advisories/GHSA-462x-pgg6-55fp) | `>= 8.2.0, < 13.0.0` | `13.0.0` |
| #4 | [GHSA-p4wg-p73j-c528](https://github.com/advisories/GHSA-p4wg-p73j-c528) | `>= 5.1.0, < 13.0.0` | `13.0.0` |
| #5 | [GHSA-pchr-8j38-3x4p](https://github.com/advisories/GHSA-pchr-8j38-3x4p) | `<= 12.3.0` | `13.0.0` |
| #6 | [GHSA-q45j-58j2-45rg](https://github.com/advisories/GHSA-q45j-58j2-45rg) | `>= 5.3.0, < 13.0.0` | `13.0.0` |
| #7 | [GHSA-vx6w-vv9f-6gvg](https://github.com/advisories/GHSA-vx6w-vv9f-6gvg) | `>= 2.3.0, <= 12.3.0` | `13.0.0` |
| #8 | [GHSA-65qm-jvmh-6pqg](https://github.com/advisories/GHSA-65qm-jvmh-6pqg) | `>= 9.2.0, < 13.0.0` | `13.0.0` |
| #9 | [GHSA-58r4-6rv3-gfh8](https://github.com/advisories/GHSA-58r4-6rv3-gfh8) | `<= 12.3.0` | `13.0.0` |
| #10 | [GHSA-w889-685w-c8fh](https://github.com/advisories/GHSA-w889-685w-c8fh) | `>= 2.6.0, < 13.0.0` | `13.0.0` |
| #11 | [GHSA-4558-3h8r-v8xp](https://github.com/advisories/GHSA-4558-3h8r-v8xp) | `<= 12.3.0` | `13.0.0` |
| #12 | [GHSA-7mwm-ghpf-9vrm](https://github.com/advisories/GHSA-7mwm-ghpf-9vrm) | `<= 12.3.0` | `13.0.0` |
| #13 | [GHSA-j4m9-6w36-pw47](https://github.com/advisories/GHSA-j4m9-6w36-pw47) | `>= 10.0.0, < 13.0.0` | `13.0.0` |
| #14 | [GHSA-cp9v-662q-vvx9](https://github.com/advisories/GHSA-cp9v-662q-vvx9) | `>= 12.0.0, < 13.0.0` | `13.0.0` |
| #15 | [GHSA-27h8-gf7j-v5hc](https://github.com/advisories/GHSA-27h8-gf7j-v5hc) | `<= 12.3.0` | `13.0.0` |
| #16 | [GHSA-fc9g-5qg7-mf45](https://github.com/advisories/GHSA-fc9g-5qg7-mf45) | `>= 9.2.0, < 13.0.0` | `13.0.0` |
| #17 | [GHSA-22hg-r758-rrw9](https://github.com/advisories/GHSA-22hg-r758-rrw9) | `>= 7.2.0, < 13.0.0` | `13.0.0` |
| #18 | [GHSA-74r2-7ff9-mjj2](https://github.com/advisories/GHSA-74r2-7ff9-mjj2) | `>= 5.4.0, < 13.0.0` | `13.0.0` |
| #19 | [GHSA-rfgq-q3w2-pmv2](https://github.com/advisories/GHSA-rfgq-q3w2-pmv2) | `>= 9.1.0, < 13.0.0` | `13.0.0` |
| #20 | [GHSA-xgh8-jp6q-p5x8](https://github.com/advisories/GHSA-xgh8-jp6q-p5x8) | `<= 12.3.0` | `13.0.0` |
| #21 | [GHSA-254f-3936-288h](https://github.com/advisories/GHSA-254f-3936-288h) | `>= 5.1.0, < 13.0.0` | `13.0.0` |
| #22 | [GHSA-jg86-v3jv-hrpm](https://github.com/advisories/GHSA-jg86-v3jv-hrpm) | `>= 5.1.0, < 13.0.0` | `13.0.0` |
| #23 | [GHSA-gg2r-wvfm-q9rx](https://github.com/advisories/GHSA-gg2r-wvfm-q9rx) | `>= 4.1.0, < 13.0.0` | `13.0.0` |
| #24 | [GHSA-5xm2-6676-qcp9](https://github.com/advisories/GHSA-5xm2-6676-qcp9) | `>= 8.1.0, < 13.0.0` | `13.0.0` |
| #25 | [GHSA-f2g6-542v-9j58](https://github.com/advisories/GHSA-f2g6-542v-9j58) | `>= 5.1.0, < 13.0.0` | `13.0.0` |
| #26 | [GHSA-4rr5-89j8-mwwm](https://github.com/advisories/GHSA-4rr5-89j8-mwwm) | `>= 11.3.0, < 13.0.0` | `13.0.0` |
| #27 | [GHSA-332p-7hrc-92wf](https://github.com/advisories/GHSA-332p-7hrc-92wf) | `>= 2.0.0, < 13.0.0` | `13.0.0` |
| #28 | [GHSA-xvh5-g6gv-559v](https://github.com/advisories/GHSA-xvh5-g6gv-559v) | `<= 12.3.0` | `13.0.0` |
| #29 | [GHSA-p8hx-v2p6-rp84](https://github.com/advisories/GHSA-p8hx-v2p6-rp84) | `< 13.0.0` | `13.0.0` |
| #30 | [GHSA-g842-9rc4-69pw](https://github.com/advisories/GHSA-g842-9rc4-69pw) | `< 13.0.0` | `13.0.0` |
| #31 | [GHSA-24rc-8hj2-v98c](https://github.com/advisories/GHSA-24rc-8hj2-v98c) | `>= 9.1.0, < 13.0.0` | `13.0.0` |
| #32 | [GHSA-v9pv-5gc8-w2m6](https://github.com/advisories/GHSA-v9pv-5gc8-w2m6) | `>= 11.0.0, < 13.0.0` | `13.0.0` |
| #33 | [GHSA-mrfp-p49p-3hf2](https://github.com/advisories/GHSA-mrfp-p49p-3hf2) | `>= 5.1.0, < 13.0.0` | `13.0.0` |
| #34 | [GHSA-4rm8-3hmm-39fg](https://github.com/advisories/GHSA-4rm8-3hmm-39fg) | `< 13.0.0` | `13.0.0` |
| #35 | [GHSA-3j72-p6qg-6xp7](https://github.com/advisories/GHSA-3j72-p6qg-6xp7) | `< 13.0.0` | `13.0.0` |
| #36 | [GHSA-g4qv-2jxx-769h](https://github.com/advisories/GHSA-g4qv-2jxx-769h) | `>= 11.0.0, < 13.0.0` | `13.0.0` |
| #37 | [GHSA-9v9v-9qvf-h2hj](https://github.com/advisories/GHSA-9v9v-9qvf-h2hj) | `< 13.0.0` | `13.0.0` |
| #38 | [GHSA-g7cr-3p24-7wf7](https://github.com/advisories/GHSA-g7cr-3p24-7wf7) | `>= 9.4.0, < 13.0.0` | `13.0.0` |
| #39 | [GHSA-78g4-xqwv-q3g8](https://github.com/advisories/GHSA-78g4-xqwv-q3g8) | `>= 11.1.0, < 13.0.0` | `13.0.0` |
| #40 | [GHSA-654x-cwwv-5j9c](https://github.com/advisories/GHSA-654x-cwwv-5j9c) | `>= 11.2.1, < 13.0.0` | `13.0.0` |
| #41 | [GHSA-5x8w-8jj5-q52h](https://github.com/advisories/GHSA-5x8w-8jj5-q52h) | `<= 12.3.0` | `13.0.0` |

### Authlib — 7 alerts

Manifest: `benchmarks/ladder/cg/requirements.txt`. Selected version: `1.9.1`.

| Alert | Advisory | Affected range | Minimum fix |
| --- | --- | --- | --- |
| #43 | [GHSA-6v38-4h7w-rj9h](https://github.com/advisories/GHSA-6v38-4h7w-rj9h) | `<= 1.8.0` | `1.9.0` |
| #44 | [GHSA-7vr2-2fgp-9w7p](https://github.com/advisories/GHSA-7vr2-2fgp-9w7p) | `>= 1.3.1, <= 1.8.0` | `1.9.0` |
| #45 | [GHSA-q686-2qmg-hqc7](https://github.com/advisories/GHSA-q686-2qmg-hqc7) | `< 1.9.0` | `1.9.0` |
| #46 | [GHSA-fch4-3c6x-j4gc](https://github.com/advisories/GHSA-fch4-3c6x-j4gc) | `< 1.9.0` | `1.9.0` |
| #47 | [GHSA-c5mf-xmp2-59rc](https://github.com/advisories/GHSA-c5mf-xmp2-59rc) | `<= 1.8.0` | `1.9.0` |
| #48 | [GHSA-x4rv-c3wx-4cr8](https://github.com/advisories/GHSA-x4rv-c3wx-4cr8) | `>= 1.6.0, < 1.9.0` | `1.9.0` |
| #49 | [GHSA-8m4g-j4wp-wgcg](https://github.com/advisories/GHSA-8m4g-j4wp-wgcg) | `< 1.9.1` | `1.9.1` |

### certifi and virtualenv — 3 alerts

Selected versions: certifi `2026.10.19`; virtualenv at least `21.14.13`.

| Alert | Package / manifest | Advisory | Affected range | Minimum fix |
| --- | --- | --- | --- | --- |
| #42 | certifi / `benchmarks/ladder/cg/requirements.txt` | [GHSA-6q48-6c7w-9gw2](https://github.com/advisories/GHSA-6q48-6c7w-9gw2) | `>= 2019.03.09, < 2026.10.18` | `2026.10.18` |
| #50 | certifi / `benchmarks/uv.lock` | [GHSA-6q48-6c7w-9gw2](https://github.com/advisories/GHSA-6q48-6c7w-9gw2) | `>= 2019.03.09, < 2026.10.18` | `2026.10.18` |
| #51 | virtualenv / `uv.lock` | [GHSA-cx6f-vjhg-g3cc](https://github.com/advisories/GHSA-cx6f-vjhg-g3cc) | `>= 21.14.9, < 21.14.13` | `21.14.13` |

## Verification and reproducibility boundaries

The remediation was checked with frozen-lock installs, all 51 captured affected
version ranges, export parity and a negative control using the stale export.
`pip-audit 2.10.1` found no known vulnerabilities in the exported contributor
environment (136 packages) or benchmark environment (94 packages).

Validation passed: 5055 backend tests, 1229 architecture tests, three typing
tests, 560 benchmark security/correctness tests and eight load-harness negative
controls, with the suites' documented skips and expected failure. All four
distributions built and installed in clean environments; the identity wheel
enforces `authlib>=1.9.1,<2`. A rebuilt Linux/Python 3.13 Cachegrind image imports
all benchmark apps, matches the shared dependency versions and generates a real
identity QR/PNG. The virtualenv prompt advisory reproduces with a harmless
temporary marker on `21.14.12` and stays inert on `21.14.13`.

The complete backend run uses Python 3.12; interpreter eligibility above is not
a claim that this change reran the entire Python-version matrix. Normal
isolation and release gates remain intact.
A passing version-range check covers this snapshot; it does not establish the
absence of every future advisory or every application configuration risk.

A corrected Git branch does not modify published PyPI wheels or users' existing
environments. Updated dependency floors take effect for consumers with a new
package release; current installations must refresh their environment and any
vendored/container CA bundle. GitHub alert closure must be checked after the
fix reaches the default branch and Dependabot re-evaluates it.

Retain historical benchmark result artifacts and their recorded environments.
The repaired benchmark lock/export is the input to future runs; it does not
retroactively change past measurements. This audit runs no paid agent DX
benchmark and makes no new performance claim.

The source evidence was the 51-entry GitHub Dependabot API snapshot captured on
2026-10-29, SHA-256
`8560b3c052e3bb4563af27e58608c1fd8ab2b67cd9e51b962c9daaad40ff544c`.
The complete ledger above preserves its alert-to-manifest and version mapping
without relying on access to the maintainer's security dashboard.

## Related reading

- [Supply-chain checks](supply-chain.md)
- [CI profiles and required checks](ci.md)
- [Publishing packages](../guides/publishing.md)
- [Versioning policy](versioning.md)
