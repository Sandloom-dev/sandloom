#!/usr/bin/env bash
# Build the Python environment Snyk scans (CI workflow `snyk.yml`; run locally
# the same way). Snyk's native uv support skips dependency groups and extras,
# so this installs every group and extra of uv.lock into a clean venv and
# freezes it: the frozen file lists exactly what this platform resolves, with
# lockfile markers already evaluated.
# Usage: tools/checks/snyk_python_env.sh [OUT]   (default: <repo>/snyk-out)
# Then:  snyk test --file=OUT/requirements.txt --package-manager=pip \
#          --command=OUT/venv/bin/python
set -euo pipefail

PYTHON_VERSION="3.12"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-$ROOT/snyk-out}"
rm -rf "$OUT"
mkdir -p "$OUT"

cd "$ROOT"
# Workspace packages are the project itself, not PyPI requirements.
exclude_packages=()
for package_dir in "$ROOT"/packages/*; do
  if [[ -f "$package_dir/pyproject.toml" ]]; then
    exclude_packages+=(--no-emit-package "$(basename "$package_dir")")
  fi
done
uv export --frozen --no-hashes --no-emit-project "${exclude_packages[@]}" \
  --all-groups --all-extras --format requirements-txt >"$OUT/locked.txt"

uv venv --quiet --python "$PYTHON_VERSION" "$OUT/venv"
uv pip install --quiet --python "$OUT/venv/bin/python" --no-deps -r "$OUT/locked.txt"
# Snyk's pip plugin resolves the tree with pip inside the venv.
uv pip install --quiet --python "$OUT/venv/bin/python" pip
uv pip freeze --python "$OUT/venv/bin/python" --exclude pip >"$OUT/requirements.txt"
echo "Snyk Python environment: $(wc -l <"$OUT/requirements.txt" | tr -d ' ') packages in $OUT"
