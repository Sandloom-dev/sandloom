"""The website publishes from main only: an explicit dispatch with deploy, or
a release from main that published (its github-release job succeeded)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/pages.yml").read_text())


def test_pages_publishes_on_a_main_dispatch_or_after_a_published_release() -> None:
    document = workflow()
    trigger = document.get("on", document.get(True))
    assert set(trigger) == {"workflow_dispatch", "workflow_run"}
    assert trigger["workflow_dispatch"]["inputs"]["deploy"]["default"] is False
    assert trigger["workflow_run"] == {
        "workflows": ["Release (PyPI)"],
        "types": ["completed"],
        "branches": ["main"],
    }
    assert document["permissions"] == {"contents": "read"}
    released = document["jobs"]["released"]
    assert "workflow_run.conclusion == 'success'" in released["if"]
    assert released["permissions"] == {"actions": "read"}
    assert '"github-release"' in released["steps"][0]["run"]
    build = document["jobs"]["build"]
    assert build["needs"] == "released"
    assert "needs.released.outputs.publish == 'true'" in build["if"]
    assert "environment" not in build
    assert not any("deploy-pages" in step.get("uses", "") for step in build["steps"])
    assert not any("configure-pages" in step.get("uses", "") for step in build["steps"])
    deploy = document["jobs"]["deploy"]
    assert "github.ref == 'refs/heads/main'" in deploy["if"]
    assert "needs.build.result == 'success'" in deploy["if"]
    assert "(inputs.deploy || needs.released.outputs.publish == 'true')" in deploy["if"]
    assert deploy["needs"] == ["released", "build"]
    assert deploy["environment"]["name"] == "github-pages"
    assert deploy["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    setup = next(s for s in deploy["steps"] if "configure-pages" in s.get("uses", ""))
    assert setup["with"]["enablement"] is False


@pytest.mark.parametrize(
    ("conclusion", "publish"),
    [("success", "true"), ("skipped", "false"), ("failure", "false"), ("", "false")],
)
def test_only_a_release_whose_github_release_succeeded_publishes(
    tmp_path: Path, conclusion: str, publish: str
) -> None:
    """The gate reads the release run's github-release job (a dry run skips it)."""
    gh = tmp_path / "gh"
    gh.write_text(f"#!/bin/sh\nprintf '%s' '{conclusion}'\n")
    gh.chmod(0o755)
    out = tmp_path / "out"
    step = workflow()["jobs"]["released"]["steps"][0]
    result = subprocess.run(
        ["bash", "-eu", "-c", step["run"]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(out),
            "REPO": "o/r",
            "RUN_ID": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert out.read_text().strip() == f"publish={publish}"


@pytest.mark.parametrize(
    ("host", "prefix", "accepted"),
    [
        ("sandloom.dev", "", True),
        ("sandloom-dev.github.io", "/sandloom", False),
        ("example.com", "", False),
        ("sandloom.dev", "/sandloom", False),
    ],
)
def test_pages_refuses_a_host_or_path_that_breaks_root_assets(
    host: str, prefix: str, accepted: bool
) -> None:
    guard = next(
        step
        for step in workflow()["jobs"]["deploy"]["steps"]
        if step.get("name") == "Require the configured production domain"
    )
    result = subprocess.run(
        ["bash", "-eu", "-c", guard["run"]],
        env={**os.environ, "PAGES_HOST": host, "PAGES_BASE_PATH": prefix},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is accepted
