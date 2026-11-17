"""``sandloom generate`` against a composition that cannot be built from INFRA.

The default composition (``sandloom.main``) selects no driver; an INFRA that
names only a url for an instance (the framework checkout's ``.env.test``) then
fails to load. The command says what to do instead of ending in a traceback.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import sandloom.core.infra.config as config_module
from sandloom.core.cli import generated
from sandloom.core.config.settings import settings
from sandloom.core.infra.config import (
    InfraConfigError,
    load_infra_config,
    use_default_infra,
)


def test_an_instance_with_no_driver_says_how_to_select_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "infra", {"cache": {"default": {"url": "redis://127.0.0.1:1/0"}}}
    )
    # a composition that selects no driver (the kernel's own, sandloom.main)
    monkeypatch.setattr(config_module, "_PIN", None)
    monkeypatch.setattr(config_module, "_APP", None)
    use_default_infra({})
    with pytest.raises(InfraConfigError) as caught:
        load_infra_config(settings)
    message = str(caught.value)
    assert "INFRA['cache']['default'] needs a 'driver'" in message
    assert "SANDLOOM_COMPOSITION_ROOT" in message


def test_generate_refuses_with_a_message_when_the_composition_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*, project_root: Path) -> object:
        raise InfraConfigError("INFRA['cache']['default'] needs a 'driver': say which")

    monkeypatch.setattr(generated, "load_composition_app", refuse)
    root = Path(__file__).resolve().parents[4]
    assert generated.main(["--check", "--root", str(root)]) == 2
    out = capsys.readouterr().out
    assert "the composition cannot be built" in out
    assert "needs a 'driver'" in out


@pytest.mark.subprocess_probe
def test_generate_on_the_lean_composition_refuses_a_driverless_instance_for_real(
    tmp_path: Path,
) -> None:
    """No mock: the lean composition (``sandloom.main``, named explicitly: the
    checkout's project default is the standard one) in a child process, an INFRA
    that names a url for the cache and no driver. The command exits 2 with the
    message instead of a traceback."""
    root = Path(__file__).resolve().parents[4]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SANDLOOM_COMPOSITION_ROOT": "sandloom.main",
        "ENVIRONMENT": "dev",
        "SANDLOOM_SERVICE_NAME": "generate-refusal",
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        "JWT_SECRET": "generate-refusal-test-secret-0123456789abcdef0123",
        "INFRA": json.dumps({"cache": {"default": {"url": "redis://127.0.0.1:1/0"}}}),
    }
    run = subprocess.run(
        [
            sys.executable,
            "-c",
            "from sandloom.core.cli.generated import main; "
            "raise SystemExit(main(['--check']))",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert run.returncode == 2, run.stdout + run.stderr
    assert "the composition cannot be built" in run.stdout
    assert "INFRA['cache']['default'] needs a 'driver'" in run.stdout
    assert "Traceback" not in run.stderr
