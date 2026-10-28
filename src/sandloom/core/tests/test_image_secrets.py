"""No secret file enters an application image (G-DEPLOY-SECRETS).

The split workflow writes ``deploy/.env.microservices`` (every service's
private keys) and ``sandloom keys generate --out-dir`` writes key files inside the
project, whose root is the image's build context. The generated
``.dockerignore`` excludes them at any depth, the Dockerfile copies only what
it builds (never the context into the runtime stage), and ``sandloom add deploy``
patches an existing app's ``.dockerignore`` that would ship them.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from sandloom.core.cli.image_secrets import (
    context_files,
    excluded,
    patch_dockerignore,
    secret_files,
)
from sandloom.core.cli.project import generate_project

#: Secret files the generators write (and one nested ``.env`` by hand).
SECRETS = (
    ".env",
    ".env.test",
    "deploy/.env.microservices",
    "deploy/keys/jwt_private_key",
    "deploy/keys/rpc_private_key",
    "deploy/keys/rpc_dh_private_key",
    "deploy/tls/server.pem",
    "probe/modules/example/.env",
)


def _with_secrets(root: Path) -> None:
    for rel in SECRETS:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SECRET=1\n")


def test_dockerignore_rules_follow_docker() -> None:
    """Root-only patterns miss nested files: the bug the old file had."""
    old = ".env\n.env.*\n!.env.example\n"
    assert excluded(old, ".env") and excluded(old, ".env.local")
    assert not excluded(old, "deploy/.env.microservices")
    new = "**/.env\n**/.env.*\n!**/.env.example\ndata/\n"
    assert excluded(new, "deploy/.env.microservices")
    assert excluded(new, ".env") and excluded(new, "a/b/.env")
    assert not excluded(new, ".env.example")
    assert not excluded(new, "deploy/.env.example")
    assert excluded(new, "data/x/y.db")
    assert not excluded(new, "probe/data.py")


def test_the_generated_context_ships_no_secret_file(tmp_path: Path) -> None:
    generate_project("probe", tmp_path)
    root = tmp_path / "probe"
    _with_secrets(root)
    shipped = context_files(root)
    assert not [f for f in shipped if f in SECRETS], shipped
    assert ".env.example" in shipped
    assert "probe/asgi.py" in shipped and "pyproject.toml" in shipped


def test_the_generated_dockerfile_copies_only_what_it_builds(tmp_path: Path) -> None:
    generate_project("probe", tmp_path)
    dockerfile = (tmp_path / "probe" / "Dockerfile").read_text()
    sources = [
        m.group(1).split()[-2]
        for m in re.finditer(r"^COPY (?!--from)(.*)$", dockerfile, re.M)
    ]
    assert sorted(sources) == ["probe", "pyproject.toml", "uv.loc[k]"], sources
    runtime = dockerfile.split("AS production", 1)[1]
    assert not re.search(r"^COPY (?!--from)", runtime, re.M)


def test_sandloom_add_deploy_patches_an_old_dockerignore(tmp_path: Path) -> None:
    root = tmp_path
    (root / ".dockerignore").write_text(".git\n.env\n.env.*\n!.env.example\n")
    _with_secrets(root)
    assert patch_dockerignore(root) is True
    assert patch_dockerignore(root) is False  # idempotent
    text = (root / ".dockerignore").read_text()
    assert text.startswith(".git\n.env\n")  # the app's own lines are kept
    assert not [f for f in context_files(root) if f in SECRETS]
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert patch_dockerignore(fresh) is True  # no file yet: created


def test_secret_files_lists_what_an_image_tree_would_leak(tmp_path: Path) -> None:
    _with_secrets(tmp_path)
    (tmp_path / ".env.example").write_text("X=\n")
    assert secret_files(tmp_path) == sorted(SECRETS)


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker")
def test_docker_agrees_the_context_has_no_secret(tmp_path: Path) -> None:
    """The real build context, as Docker filters it with the generated
    ``.dockerignore`` (exported, nothing pulled or run)."""
    generate_project("probe", tmp_path / "app")
    root = tmp_path / "app" / "probe"
    _with_secrets(root)
    probe = tmp_path / "Dockerfile.context"
    probe.write_text("FROM scratch\nCOPY . /\n")
    out = tmp_path / "context"
    done = subprocess.run(
        [
            "docker",
            "build",
            "-q",
            "-f",
            str(probe),
            "--output",
            f"type=local,dest={out}",
            str(root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        pytest.skip(f"docker build unavailable: {done.stderr[-300:]}")
    assert secret_files(out) == []
    assert (out / ".env.example").is_file() and (out / "probe" / "asgi.py").is_file()
