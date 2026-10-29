"""The website builds for review; only an explicit main dispatch can publish."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/pages.yml").read_text())


def test_pages_has_no_automatic_or_default_publication() -> None:
    document = workflow()
    trigger = document.get("on", document.get(True))
    assert set(trigger) == {"workflow_dispatch"}
    assert trigger["workflow_dispatch"]["inputs"]["deploy"]["default"] is False
    assert document["permissions"] == {"contents": "read"}
    build = document["jobs"]["build"]
    assert "environment" not in build
    assert not any("deploy-pages" in step.get("uses", "") for step in build["steps"])
    assert not any("configure-pages" in step.get("uses", "") for step in build["steps"])
    deploy = document["jobs"]["deploy"]
    assert deploy["if"] == "${{ inputs.deploy && github.ref == 'refs/heads/main' }}"
    assert deploy["needs"] == "build"
    assert deploy["environment"]["name"] == "github-pages"
    assert deploy["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    setup = next(s for s in deploy["steps"] if "configure-pages" in s.get("uses", ""))
    assert setup["with"]["enablement"] is False


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
