#!/usr/bin/env bash
# Run a pre-push check unless a FULL local CI run already passed on exactly
# the content being pushed.
#
#   tools/ci/unless_ci_passed.sh <command> [args...]
#
# tools/ci/ci_local.py writes a marker named after the pushed commit's TREE
# (content, not commit id) when every CI job of a run passed; a run limited
# with --jobs / --python writes none. Pre-commit exports the pushed commit as
# PRE_COMMIT_TO_REF. Without it (a manual run), without a marker, or with
# SANDLOOM_PREPUSH_IGNORE_CI_PASS=1 the command runs as before.
set -euo pipefail

ref="${PRE_COMMIT_TO_REF:-}"
if [[ -n "$ref" && "${SANDLOOM_PREPUSH_IGNORE_CI_PASS:-0}" != "1" ]]; then
  tree="$(git rev-parse --verify --quiet "$ref^{tree}" || true)"
  marker="${SANDLOOM_CI_PASS_DIR:-$HOME/.cache/sandloom-ci-local/passed}/$tree"
  if [[ -n "$tree" && -f "$marker" ]]; then
    echo "skipped: full local CI passed on this exact tree ($(tr '\n' ' ' < "$marker"))"
    exit 0
  fi
fi
exec "$@"
