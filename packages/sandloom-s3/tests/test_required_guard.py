"""``SANDLOOM_S3_REQUIRED=1`` turns a missing test bucket into a failure.

The endpoint suite needs a real bucket (``SANDLOOM_S3_TEST_BUCKET``). A lane that
runs it sets the variable so a bucket that was never configured fails the lane;
locally, without the variable, the suite skips.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

REQUIRED_ENV = "SANDLOOM_S3_REQUIRED"
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
#: One test of the endpoint suite whose body reaches for the bucket options.
_NEEDING_BUCKET = (
    "test_endpoint.py::TestS3Endpoint::test_the_app_resolves_the_s3_driver"
)


def _outcome(*, required: bool) -> str:
    """Run the endpoint test in a fresh pytest with no bucket; its output."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {REQUIRED_ENV, "SANDLOOM_S3_TEST_BUCKET"}
    }
    if required:
        env[REQUIRED_ENV] = "1"
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(HERE / _NEEDING_BUCKET),
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return done.stdout + done.stderr


def test_a_missing_bucket_skips_locally() -> None:
    local = _outcome(required=False)
    assert "1 skipped" in local and "failed" not in local, local[-1500:]


def test_a_missing_bucket_fails_where_the_lane_requires_it() -> None:
    lane = _outcome(required=True)
    assert "1 failed" in lane, lane[-1500:]
    assert "skipped" not in lane, lane[-1500:]
    assert f"{REQUIRED_ENV}=1: this lane may not skip" in lane, lane[-1500:]


def test_no_endpoint_test_skips_on_the_bucket_without_the_guard() -> None:
    """A ``pytest.skip`` added later would hide a missing bucket from the lane:
    only ``required_or_skip`` may skip."""
    tree = ast.parse((HERE / "test_endpoint.py").read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "skip"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
    ]
    assert not offenders, f"use required_or_skip(), not pytest.skip: {offenders}"
