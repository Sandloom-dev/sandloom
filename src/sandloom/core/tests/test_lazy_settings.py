"""Lazy process settings (Public API 1.0, spec §2.4).

Importing the framework constructs no ``Settings``; the first use (or an
entry point's ``load_settings()`` / ``enforce_configuration()``) validates
the environment, and an invalid one still exits at startup.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sandloom.core.config.settings import (
    Settings,
    load_settings,
    settings,
    settings_loaded,
)


def _run(
    code: str, tmp_path: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_loaded_settings_are_a_plain_settings_instance() -> None:
    load_settings()
    assert settings_loaded()
    assert type(settings) is Settings
    assert isinstance(settings, Settings)
    assert load_settings() is settings


@pytest.mark.subprocess_probe
def test_import_does_not_validate_and_first_use_does(tmp_path: Path) -> None:
    code = (
        "import sandloom.core.config.settings as s\n"
        "assert not s.settings_loaded()\n"
        "assert isinstance(s.settings, s.Settings)\n"
        "print(s.settings.environment.value)\n"
        "assert s.settings_loaded() and type(s.settings) is s.Settings\n"
    )
    env = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
        "CACHE_URL": "redis://h:6379/0",
        "BROKER_URL": "redis://h:6379/1",
        "JWT_SECRET": "lazy-settings-test-secret-0123456789abcdef",
    }
    result = _run(code, tmp_path, env)
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.strip() == "dev"


@pytest.mark.subprocess_probe
def test_an_invalid_environment_still_fails_at_the_entry_point(tmp_path: Path) -> None:
    code = (
        "import sandloom.core.app_factory as f\n"  # importing is fine
        "print('imported')\n"
        "f.enforce_configuration()\n"  # the entry point forces the settings
    )
    result = _run(code, tmp_path, {})
    assert result.stdout.strip() == "imported"
    assert result.returncode != 0
    assert "Invalid configuration" in result.stderr
    assert "DATABASE_URL" in result.stderr
