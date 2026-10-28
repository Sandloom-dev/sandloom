#!/usr/bin/env bash
# Download the logs of this workflow run's completed jobs whose name starts
# with one of the given prefixes. Results travel between jobs through the
# BENCHJSON lines in the logs — NOT artifacts (the account's artifact storage
# quota is exhausted). Needs GH_TOKEN with `actions: read`.
#
#   benchmarks/ci/fetch_job_logs.sh <out-dir> "bench (" "scaling"
set -euo pipefail

out="$1"
shift
mkdir -p "$out"

gh api --paginate \
  "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/attempts/${GITHUB_RUN_ATTEMPT:-1}/jobs?per_page=100" \
  --jq '.jobs[] | select(.status == "completed") | "\(.id)\t\(.name)"' |
  while IFS=$'\t' read -r id name; do
    for prefix in "$@"; do
      if [[ "$name" == "$prefix"* ]]; then
        if ! gh api "repos/${GITHUB_REPOSITORY}/actions/jobs/${id}/logs" >"$out/${id}.log"; then
          echo "warning: could not fetch the log of '$name'" >&2
        fi
      fi
    done
  done

ls -la "$out"
