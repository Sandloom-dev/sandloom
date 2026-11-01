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
# Export uv.lock with all groups/extras, excluding the five workspace packages.
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

The original Python runtime had **68 Debian source-package advisories**.
The follow-up removes runtime APT and mount/account administration packages,
strips SUID/SGID bits, and restricts application containers to a read-only root,
zero capabilities and no privilege gain. Repeat scanning reports **67 advisories**:
APT disappears, while `login.defs` retains shadow source-package findings even
after the affected account tools are removed. Package metadata remains intact.

Every original CVE has a separate applicability/remediation record in
[`debian-runtime-review-2026-10-31.json`](https://github.com/Sandloom-dev/sandloom/blob/main/tools/checks/debian-runtime-review-2026-10-31.json).
The review covers all 18 source-package groups against Debian's tracker,
including the original 64 low and one medium results. No fixed trixie version
is available for these baseline CVEs; live-library and conditional findings
remain open. An absent systemd daemon is distinguished from an installed
systemd library; optional SQLite and Perl components and architecture-specific
paths are checked separately. Severity alone never authorizes an exception.

The three high conclusions are:

- **attr:** the affected `getfattr`/`setfattr` utilities are absent. The executable
  presence predicate must continue to pass before using that conclusion.
- **ACL:** real local privileged-path risk, mitigated by the runtime boundary;
  the installed library is not declared fixed or universally unreachable.
- **zlib:** the exact installed binary matches the authenticated Debian package.
  The authenticated source lacks the newer nonblocking writer mechanism, and
  errors return before the affected buffer movement. This supports investigating
  a version-classification error, but the status remains **under investigation**
  while [Debian](https://security-tracker.debian.org/tracker/CVE-2026-85091) marks
  stable vulnerable.

Reproduce evidence from the repository root after building the production image:

```bash
docker build -f deploy/docker/Dockerfile --target production -t sandloom-runtime-review .
docker run --rm -i --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=0700 \
  --tmpfs /app/data:rw,noexec,nosuid,nodev,size=8m,uid=10001,gid=10001,mode=0700 \
  --entrypoint python sandloom-runtime-review - --enforce \
  < tools/checks/runtime_inventory.py > /tmp/runtime-inventory.json
snyk container test sandloom-runtime-review --file=deploy/docker/Dockerfile \
  --json-file-output=/tmp/runtime-scan.json
# A scan with known findings exits 1 but still writes the report; inspect it.
python3 tools/checks/zlib_source_evidence.py --inventory /tmp/runtime-inventory.json \
  > /tmp/zlib-evidence.json
python3 tools/checks/runtime_review.py --image sandloom-runtime-review \
  --inventory /tmp/runtime-inventory.json --scan /tmp/runtime-scan.json \
  --zlib-evidence /tmp/zlib-evidence.json > /tmp/runtime-review.json
```

If boundary enforcement fails, the inventory command exits 1 and still emits
the collected JSON with `boundary_checks: "fail"` and `boundary_failure`.
Docker CI uploads `runtime-security-inventory` even when that probe fails.

The source evidence command uses a disposable reference container and signed
Debian indexes; it verifies source/package checksums and exact library bytes.
It is separate from normal CI because it needs live repository access. The
evidence checker rejects new CVEs, detects changed components, and requires
new source review when zlib bytes or source hashes change. It creates **no
scanner ignore or automatic risk acceptance**. Re-scan after vendor updates;
do not mix Debian unstable packages into the stable runtime.

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
- [Package publishing](publishing.md)
