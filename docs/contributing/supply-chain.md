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

## Related reading

- [Contributor workflow](index.md)
- [Package publishing](../guides/publishing.md)
