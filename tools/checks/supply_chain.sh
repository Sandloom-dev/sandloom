#!/usr/bin/env bash
# Supply-chain gates (CI job `supply-chain`; run locally the same way):
#   sbom  - CycloneDX SBOM of the locked backend dependencies
# SAST is NOT here: ruff rule family S (flake8-bandit) in pyproject.toml
# covers it, with its own baselined suppression ratchet (run in backend-static).
# Usage: tools/checks/supply_chain.sh [sbom|all]   (default: all)
# Output: $SUPPLY_CHAIN_OUT (default: <repo>/sbom-out)
# Tool versions are pinned HERE; bump them deliberately.
set -euo pipefail

CYCLONEDX_BOM_VERSION="7.2.1"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${SUPPLY_CHAIN_OUT:-$ROOT/sbom-out}"
mkdir -p "$OUT"

sbom() {
  cd "$ROOT"
  # Frozen third-party dependencies, including runtime + dev groups.
  # Local package artifacts are checked separately; editable paths are not PyPI requirements.
  local exclude_packages=()
  for package_dir in "$ROOT"/packages/*; do
    if [[ -f "$package_dir/pyproject.toml" ]]; then
      exclude_packages+=(--no-emit-package "$(basename "$package_dir")")
    fi
  done
  uv export --frozen --no-hashes --no-emit-project "${exclude_packages[@]}" --all-groups \
    --format requirements-txt >"$OUT/requirements.lock.txt"
  uvx --from "cyclonedx-bom==$CYCLONEDX_BOM_VERSION" cyclonedx-py requirements \
    --pyproject pyproject.toml --of JSON --sv 1.5 \
    -o "$OUT/sandloom-backend.cdx.json" "$OUT/requirements.lock.txt"
  python3 -c "import json,sys; d=json.load(open('$OUT/sandloom-backend.cdx.json')); \
n=len(d.get('components',[])); print(f'SBOM: {n} components'); sys.exit(0 if n else 1)"
}

case "${1:-all}" in
  sbom) sbom ;;
  all) sbom ;;
  *) echo "usage: $0 [sbom|all]" >&2; exit 2 ;;
esac
