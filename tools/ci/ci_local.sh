#!/usr/bin/env bash
# Local Docker CI runner (GitHub Actions stand-in). All logic lives in
# tools/ci/ci_local.py (a uv inline-metadata script); see its docstring or
# docs/contributing/local-ci.md.
#
#   tools/ci/ci_local.sh <git-ref> [--jobs a,b] [--python 3.11,3.13] [--parallel N]
#   tools/ci/ci_local.sh <git-ref> --rerun-failed   # only the last run's failures
#   (--parallel defaults to min(3, docker CPUs//3, memory//2.5GB); 1 = sequential)
#   tools/ci/ci_local.sh --list | --check
#
# The runner that verifies a ref is the ref's OWN tools/ci/ci_local.py (when it
# has one): a PR that changes the runner together with the workflows it maps
# (e.g. a new CI step) is checked by its own runner, not by the working tree's.
# SANDLOOM_CI_LOCAL_USE_WORKTREE_RUNNER=1 forces the working-tree copy.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v uv >/dev/null || { echo "ci_local: uv is required (brew install uv)" >&2; exit 2; }
runner="$here/ci_local.py"
ref="${1:-}"
if [[ -n "$ref" && "$ref" != -* && "${SANDLOOM_CI_LOCAL_USE_WORKTREE_RUNNER:-0}" != "1" ]]; then
  if git -C "$here" cat-file -e "$ref:tools/ci/ci_local.py" 2>/dev/null; then
    tmp="$(mktemp -d "${TMPDIR:-/tmp}/sandloom-ci-runner.XXXXXX")"
    trap 'rm -rf "$tmp"' EXIT
    git -C "$here" show "$ref:tools/ci/ci_local.py" > "$tmp/ci_local.py"
    # The runner imports its sibling lock module; copy it from the same ref.
    if git -C "$here" cat-file -e "$ref:tools/ci/ci_lock.py" 2>/dev/null; then
      git -C "$here" show "$ref:tools/ci/ci_lock.py" > "$tmp/ci_lock.py"
    fi
    if ! cmp -s "$tmp/ci_local.py" "$runner"; then
      echo "ci_local: using the runner from $ref (differs from the working tree)" >&2
    fi
    runner="$tmp/ci_local.py"
    # not exec: the EXIT trap must remove the temp copy
    uv run --quiet --script "$runner" "$@"
    exit $?
  fi
fi
exec uv run --quiet --script "$runner" "$@"
