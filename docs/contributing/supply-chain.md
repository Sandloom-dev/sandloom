---
title: Supply-chain gates
---

# Supply-chain gates: SBOM, SAST, dependency review

Review-map item A-30. One script, `tools/checks/supply_chain.sh`, is the CI job
`supply-chain` (`.github/workflows/ci.yml`, SBOM) and runs the same way locally:

```bash
tools/checks/supply_chain.sh            # SBOM (output in ./sbom-out, git-ignored)
tools/ci/ci_local.sh HEAD --jobs supply-chain   # the real workflow job under act
```

Tool versions are pinned in the script (`CYCLONEDX_BOM_VERSION`); bump them deliberately, in their own change.

## SBOM

`uv export --frozen` of `uv.lock` (runtime + dev groups) piped through
`cyclonedx-py requirements` gives a CycloneDX 1.5 JSON document
(`sandloom-backend.cdx.json`), uploaded by CI as the `backend-sbom` artifact
(90 days). Not covered yet: the `admin/` npm tree and the production image
(`syft`) — both need a tool outside the pinned set; add them to the script
when an audit or release needs them.

## SAST

Covered by ruff rule family `S` (flake8-bandit) in `pyproject.toml`,
with its own baselined suppression ratchet, enforced by `backend-static`. There
is deliberately no separate bandit run or baseline here (two overlapping
baselines would drift). A bandit scan of the tree on 2026-10-24 (124 findings)
found nothing exploitable: assert/try-except-pass/jitter-`random`/token-constant
false positives, subprocess calls with argv lists (`backup.py`), and 26 SQL
string-building sites (mostly `migrations/extract.py`) whose identifiers come
from the registry and whose values are bound — worth one audit that none can
originate from request input. Rules ruff `S` lacks would be a semgrep addition
with a pinned ruleset; not added yet.

## Dependency review on PRs

`.github/workflows/dependency-review.yml` runs `actions/dependency-review-action`
on PRs touching lockfiles/manifests: fails on a newly introduced dependency
with a `high`+ advisory or an AGPL/GPL-3.0 licence. It needs the GitHub
dependency graph, so it is **not** reproducible in `tools/ci/ci_local.sh`.
Existing dependencies are covered by `pip-audit` in `backend-static`.

## Snyk review and reproducibility

The 2026-10-31 audit used Snyk CLI **1.1308.3**, authenticated to the
maintainer's organization. The review ledger is
[`tools/checks/snyk-audit-2026-10-31.json`](https://github.com/Sandloom-dev/sandloom/blob/main/tools/checks/snyk-audit-2026-10-31.json).
It records all **384 Code results** against the original commit and all **five
unique dependency advisories**, with a disposition and evidence for each.
It is review evidence, not a Snyk ignore policy. Future findings still need review.

The dependency scope includes the admin's **complete development tree** and
its production-only tree, every group/extra of the framework lock, and the
benchmark Cachegrind requirements. Python scanning used a clean CPython 3.12
macOS environment: evaluate lockfile markers for that environment before
installing and scanning (136 framework and 94 benchmark dependency records).
The two inapplicable records, Windows `colorama` and Python <3.11.3
`async-timeout`, are listed in the ledger; no unresolved dependency was skipped.
This is not a claim that every platform-specific resolution was tested.

```bash
snyk code test .
snyk test --file=admin/package.json --dev
snyk test --file=admin/package.json
# Export uv.lock with all groups/extras, excluding the four workspace packages.
# Evaluate markers, then install the resulting requirements in a clean environment.
snyk test --file=/path/to/evaluated-requirements.txt --package-manager=pip \
  --command=/path/to/clean-env/bin/python
snyk test --file=benchmarks/ladder/cg/requirements.txt --package-manager=pip \
  --command=/path/to/benchmark-env/bin/python
```

Authenticate with `snyk auth`; never put the token in a command, report or PR.
Keep raw scan artifacts outside the public source tree. Running a local scan
does not install a CI publisher or create additional monitored projects.

### Remaining dependency alert

Snyk's [picomatch advisory](https://security.snyk.io/vuln/SNYK-JS-PICOMATCH-21248802)
has no upstream fixed version at the audit date. The admin pins the reviewed
parser and rejects the vulnerable single-branch repeated-wildcard extglob
before creating a regular expression. `npm ci` applies the patch only when
the version and source checksums match, then runs timeout and real-consumer
regressions. See
[`admin/patches/README.md`](https://github.com/Sandloom-dev/sandloom/blob/main/admin/patches/README.md).

**One admin development-dependency alert remains visible**: scanning does not inspect
the installed source patch. It is mitigated locally, not ignored or represented
as an upstream fix. Do not install these build dependencies with
`--ignore-scripts`. Replace the patch with an upstream fix when one is available,
after running the compatibility suite and admin builds. The admin production
dependency tree and the two scanned Python environments have no Snyk advisories.

### Runtime container scope

The audit also built and scanned both final runtime images on **linux/arm64**,
including OS packages and application dependencies. The admin's pinned Alpine
base contained 27 unique advisories. Exact security backports for brotli, curl,
musl and zlib remove all 27; a repeat scan of 70 dependencies reports **zero**.
The image still runs as UID 101 and passes the nonroot/read-only proxy smokes
in both deployment topologies.

The Python runtime's Debian 13 image has **68 source-package advisories**
(three high, one medium, 64 low). They remain visible in the ledger; Snyk lists
no fixed stable versions. `apt-cache policy` confirms that the installed
`libacl1`, `libattr1` and `zlib1g` are the current stable candidates.
Debian tracks [ACL](https://security-tracker.debian.org/tracker/CVE-2026-54369)
and [zlib](https://security-tracker.debian.org/tracker/CVE-2026-85091) as still
vulnerable in trixie. The high attr advisory concerns privileged `getfattr` /
`setfattr` utilities, neither of which is installed. The runtime is UID 10001;
this narrows the privilege-escalation context but does not close every finding.
Do not treat this image as having a clean vulnerability scan. Re-scan when
stable security packages or a reviewed replacement base become available;
do not mix Debian unstable packages into the stable image.

`snyk iac test deploy` found no supported IaC files. Docker Compose is not
covered by that scan; this result is **not** a passing infrastructure audit.

### Code findings and trust boundaries

Confirmed defects have regression tests for PostgreSQL names containing quotes,
hostile archive links/special files, private profiler output, and executable
documentation URL schemes. Viewer labels stay text even when the inspection
model contains HTML. SHA-1 uses retained for Git object IDs and local resource
names are explicitly non-security uses.

The repeat Code scan reports **370 results** (eight high, 72 medium, 290 low),
with zero ignores and no new rule/file pairs. The high results are the eight
explicit local build/benchmark secret placeholders. The ledger accounts for
every original finding; SQL identifier composition and the `data` extraction
filter still produce scanner warnings despite the attack regressions passing.

The other findings include synthetic test credentials, negative security tests,
route authentication labels, and local tools intentionally reading paths or
executing project commands chosen by their operator. Benchmark/DX and local CI
tools execute project code and are **not sandboxes for hostile repositories**.
Review those trust boundaries again if a tool becomes a remotely callable service.
No production authorization, secret validation or security gate was relaxed.

## Related reading

- [Contributor workflow](index.md)
- [Package publishing](../guides/publishing.md)
