"""Fail the single required check unless every selected CI job succeeded.

GitHub passes only job results and classifier outputs through environment JSON.
This script performs no network access and needs no credentials or dependencies.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

AREAS = frozenset(
    {
        "backend",
        "admin",
        "examples",
        "deploy",
        "benchmarks",
        "docs",
        "packages",
        "architecture",
        "dependency_review",
    }
)
ALWAYS = {"changes", "workflow-policy"}
AREA_JOBS = {
    "backend-static": ("backend",),
    "supply-chain": ("backend",),
    "backend-tests": ("backend",),
    "benchmarks-security": ("benchmarks",),
    "examples": ("examples",),
    "deploy-profiles": ("deploy",),
    "docker-build": ("backend", "deploy"),
    "admin-contracts": ("admin",),
}
FULL_JOBS = {
    "backend-tests-isolation",
    "quickstart",
    "app-split",
    "app-recipes",
    "microservices-smoke",
}
PR_JOBS = {
    "docs": "docs",
    "packages": "packages",
    "architecture": "architecture",
    "admin-ui": "admin",
    "dependency-review": "dependency_review",
}
ALL_JOBS = ALWAYS | AREA_JOBS.keys() | FULL_JOBS | PR_JOBS.keys()
# Jobs that run the same commands in the PR and the full profile. A release
# may skip one when a green PR run tested the release commit's exact tree
# (tools/ci/pr_evidence.py); backend-tests is not here, because the full
# matrix runs other Pythons, coverage and the slow suites.
REUSABLE_JOBS = (
    "backend-static",
    "supply-chain",
    "benchmarks-security",
    "examples",
    "deploy-profiles",
    "docker-build",
    "admin-contracts",
)


def check(
    needs: Mapping,
    *,
    full_validation: bool,
    pull_request: bool,
    reused: frozenset[str] = frozenset(),
) -> list[str]:
    """Selected jobs must succeed; only unselected jobs may be skipped.

    ``reused``: jobs a full validation skipped because a green PR run tested
    the same tree; only a full validation outside a PR may name any, and only
    :data:`REUSABLE_JOBS`.
    """
    if not isinstance(needs, Mapping) or set(needs) != ALL_JOBS:
        return ["Required CI job list is missing or differs from the reviewed workflow"]
    if reused and (pull_request or not full_validation):
        return ["Reused PR results are accepted only by a full validation"]
    if not reused <= set(REUSABLE_JOBS):
        return [f"Not reusable: {', '.join(sorted(reused - set(REUSABLE_JOBS)))}"]
    if any(not isinstance(job, Mapping) for job in needs.values()):
        return ["Invalid job result payload"]
    outputs = needs["changes"].get("outputs", {})
    if not isinstance(outputs, Mapping) or any(
        outputs.get(area) not in {"true", "false"} for area in AREAS
    ):
        return [
            "Change classification is incomplete; required suites cannot be determined"
        ]
    selected = dict.fromkeys(ALWAYS, True)
    selected.update(
        {
            name: any(outputs[area] == "true" for area in areas)
            for name, areas in AREA_JOBS.items()
        }
    )
    selected.update(dict.fromkeys(FULL_JOBS, full_validation))
    selected.update(
        {
            name: pull_request and outputs[area] == "true"
            for name, area in PR_JOBS.items()
        }
    )
    for name in reused:
        selected[name] = False
    return [
        f"{name}: {needs[name].get('result', 'missing')} (required: {required})"
        for name, required in sorted(selected.items())
        if needs[name].get("result")
        not in ({"success"} if required else {"success", "skipped"})
    ]


def boolean(name: str) -> bool:
    value = os.environ[name]
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def reused_jobs(raw: str) -> frozenset[str]:
    value = json.loads(raw or "[]")
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("CI_REUSED_JOBS must be a JSON list of job names")
    return frozenset(value)


def main() -> int:
    try:
        reused = reused_jobs(os.environ.get("CI_REUSED_JOBS", "[]"))
        problems = check(
            json.loads(os.environ["CI_NEEDS"]),
            full_validation=boolean("CI_FULL_VALIDATION"),
            pull_request=boolean("CI_PULL_REQUEST"),
            reused=reused,
        )
    except (KeyError, ValueError, TypeError) as exc:
        print(f"Invalid required CI input: {type(exc).__name__}")
        return 1
    for problem in problems:
        print(problem)
    if not problems:
        print("CI required: all selected jobs succeeded")
        if reused:
            print(
                "Reused from a green PR run on the same tree: "
                + ", ".join(sorted(reused))
            )
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
