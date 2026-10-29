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


def check(needs: Mapping, *, full_validation: bool, pull_request: bool) -> list[str]:
    """Selected jobs must succeed; only unselected jobs may be skipped."""
    if not isinstance(needs, Mapping) or set(needs) != ALL_JOBS:
        return ["Required CI job list is missing or differs from the reviewed workflow"]
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


def main() -> int:
    try:
        problems = check(
            json.loads(os.environ["CI_NEEDS"]),
            full_validation=boolean("CI_FULL_VALIDATION"),
            pull_request=boolean("CI_PULL_REQUEST"),
        )
    except (KeyError, ValueError, TypeError) as exc:
        print(f"Invalid required CI input: {type(exc).__name__}")
        return 1
    for problem in problems:
        print(problem)
    if not problems:
        print("CI required: all selected jobs succeeded")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
