"""Reuse a green PR run of ci.yml for the release commit, or nothing.

Two commands, no dependencies beyond the standard library and ``gh``:

``record``  (ci.yml, the PR's ``CI required`` job) writes the tree the run
            checked out, the one fact GitHub does not keep about a PR run:
            the tested merge commit is gone once the PR merges.
``select``  (release.yml, the ``plan`` job) names the ci.yml jobs whose green
            PR result covers the release commit. It reuses a job only when

            * the commit is the merge commit of a PR merged into ``main``;
            * a ``pull_request`` run of ci.yml for that PR's head succeeded
              (its ``CI required`` gate passed);
            * that run recorded the release commit's exact tree;
            * GitHub's own job list for the run shows the job succeeded (a
              matrix job: every entry) — results are never self-reported.

            Anything missing, expired or different selects nothing, and the
            release runs every job: reuse only ever skips work already done
            on the same tree, never a check.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from required_ci import REUSABLE_JOBS

ARTIFACT = "ci-evidence"
EVIDENCE_FILE = "evidence.json"
WORKFLOW_PATH = ".github/workflows/ci.yml"
SCHEMA = 1

Api = Callable[[str], Any]
Download = Callable[[int, Path], Path]


def record(out: Path, *, tree: str, sha: str, head_sha: str) -> dict[str, Any]:
    evidence = {"schema": SCHEMA, "tree": tree, "sha": sha, "head_sha": head_sha}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, sort_keys=True) + "\n")
    return evidence


def merged_pull_heads(api: Api, repo: str, sha: str) -> list[str]:
    """Head commits of the PRs merged into main as exactly ``sha``."""
    return [
        pr["head"]["sha"]
        for pr in api(f"repos/{repo}/commits/{sha}/pulls")
        if pr.get("merged_at")
        and pr.get("merge_commit_sha") == sha
        and pr.get("base", {}).get("ref") == "main"
    ]


def green_runs(api: Api, repo: str, head_sha: str) -> list[dict[str, Any]]:
    """Successful pull_request runs of ci.yml for ``head_sha``, newest first."""
    found = api(
        f"repos/{repo}/actions/workflows/ci.yml/runs"
        f"?head_sha={head_sha}&event=pull_request&status=success&per_page=20"
    )
    runs = [
        run
        for run in found.get("workflow_runs", [])
        if run.get("conclusion") == "success"
        and run.get("event") == "pull_request"
        and run.get("path") == WORKFLOW_PATH
        and run.get("head_sha") == head_sha
        and run.get("repository", {}).get("full_name") == repo
    ]
    return sorted(runs, key=lambda run: run["created_at"], reverse=True)


def reused_jobs(jobs: Iterable[Mapping[str, Any]]) -> list[str]:
    """The reusable jobs GitHub reports successful in one run (latest attempt).

    A matrix job counts only when it ran and every entry succeeded.
    """
    results: dict[str, list[str]] = {name: [] for name in REUSABLE_JOBS}
    for job in jobs:
        name = str(job.get("name", ""))
        for reusable in REUSABLE_JOBS:
            if name == reusable or name.startswith(f"{reusable} ("):
                results[reusable].append(str(job.get("conclusion")))
    return sorted(
        name
        for name, conclusions in results.items()
        if conclusions and all(c == "success" for c in conclusions)
    )


def select(
    api: Api, download: Download, *, repo: str, sha: str, tree: str
) -> tuple[list[str], str]:
    """``(jobs to reuse, why)``; an empty list means the release runs all."""
    heads = merged_pull_heads(api, repo, sha)
    if not heads:
        return [], f"{sha} is not the merge commit of a PR merged into main"
    for head in heads:
        for run in green_runs(api, repo, head):
            artifacts = api(f"repos/{repo}/actions/runs/{run['id']}/artifacts")
            if not any(
                a.get("name") == ARTIFACT and not a.get("expired")
                for a in artifacts.get("artifacts", [])
            ):
                continue
            try:
                path = download(int(run["id"]), Path(tempfile.mkdtemp()))
                evidence = json.loads(path.read_text())
            except (OSError, ValueError, subprocess.CalledProcessError):
                continue
            if evidence.get("schema") != SCHEMA or evidence.get("tree") != tree:
                continue
            listed = api(
                f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100"
            )
            jobs = reused_jobs(listed.get("jobs", []))
            return jobs, (
                f"PR run {run['id']} ({run.get('html_url', '')}) tested tree {tree}"
            )
    return [], f"no green PR run of ci.yml recorded tree {tree}"


def _run(*argv: str) -> str:
    # gh and git come from the runner image's PATH, as in every workflow step.
    return subprocess.run(  # noqa: S603 - static argv, no shell
        argv,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def gh_api(path: str) -> Any:
    return json.loads(_run("gh", "api", path))


def gh_download(repo: str) -> Download:
    def download(run_id: int, dest: Path) -> Path:
        _run(
            "gh",
            "run",
            "download",
            str(run_id),
            "-R",
            repo,
            "-n",
            ARTIFACT,
            "-D",
            str(dest),
        )
        return dest / EVIDENCE_FILE

    return download


def git(*args: str) -> str:
    return _run("git", *args).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--out", type=Path, required=True)
    sub.add_parser("select")
    args = parser.parse_args(argv)
    if args.command == "record":
        evidence = record(
            args.out,
            tree=git("rev-parse", "HEAD^{tree}"),
            sha=git("rev-parse", "HEAD"),
            head_sha=os.environ.get("PR_HEAD_SHA", ""),
        )
        print(f"tested tree {evidence['tree']} ({evidence['sha']})")
        return 0
    repo = os.environ["GITHUB_REPOSITORY"]
    try:
        jobs, why = select(
            gh_api,
            gh_download(repo),
            repo=repo,
            sha=git("rev-parse", "HEAD"),
            tree=git("rev-parse", "HEAD^{tree}"),
        )
    except (subprocess.CalledProcessError, KeyError, ValueError) as exc:
        # The API being unavailable costs time, never a check.
        jobs, why = [], f"PR evidence unavailable ({type(exc).__name__})"
    print(f"{why}; reusing: {', '.join(jobs) or 'nothing (every job runs)'}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as handle:
            handle.write(f"reused_jobs={json.dumps(jobs)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
