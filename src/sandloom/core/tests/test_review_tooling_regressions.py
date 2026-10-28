"""Safety and generated-project regressions from the 22 October review."""

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import MagicMock

import psycopg2.errors
import pytest
import yaml

from sandloom.core.cli import keys
from sandloom.core.cli.project import generate_project
from sandloom.core.testing import ownership
from sandloom.core.testing.database import assert_disposable_test_database


@pytest.mark.parametrize(
    ("purpose", "filename"),
    [
        ("jwt", "jwt_private_key"),
        ("rpc", "rpc_dh_private_key"),
        ("rpc-dh", "rpc_dh_private_key"),
    ],
)
def test_key_generation_refuses_existing_files_without_partial_rotation(
    tmp_path: Path, purpose: str, filename: str
) -> None:
    target = tmp_path / filename
    target.write_text("existing-key-must-survive")
    before = set(tmp_path.iterdir())
    result = keys.main(
        [
            "generate",
            "--purpose",
            purpose,
            "--service",
            "identity",
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert result == 2
    assert target.read_text() == "existing-key-must-survive"
    assert set(tmp_path.iterdir()) == before


def test_generated_private_key_is_private(tmp_path: Path) -> None:
    assert keys.main(["generate", "--out-dir", str(tmp_path)]) == 0
    assert stat.S_IMODE((tmp_path / "jwt_private_key").stat().st_mode) == 0o600


def test_environment_test_does_not_authorize_wiping_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.delenv("SANDLOOM_TEST_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="Refusing"):
        assert_disposable_test_database("postgresql+asyncpg://user:pass@db/production")


def test_resource_ownership_fails_closed_when_role_cannot_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock()
    monkeypatch.setattr("psycopg2.connect", lambda *a, **k: connection)
    monkeypatch.setattr(ownership, "database_owner", lambda cursor: None)

    def refused(*args: object) -> None:
        raise psycopg2.errors.InsufficientPrivilege("cannot mark")

    monkeypatch.setattr(ownership, "stamp_database", refused)
    with pytest.raises(ownership.TestResourceOwnershipError, match="mark"):
        ownership.claim_database(
            "postgresql+asyncpg://user:pass@localhost/sandloom_test_cannot_mark",
            owner="isolated-regression-probe",
        )


def test_generated_typing_configuration_is_supported(tmp_path: Path) -> None:
    generate_project("reviewprobe", tmp_path)
    root = tmp_path / "reviewprobe"
    assert "sqlalchemy.ext.mypy.plugin" not in (root / "pyproject.toml").read_text()
    assert "mypy" in (root / ".github/workflows/ci.yml").read_text()


def test_generated_compose_persists_data_and_allows_shutdown(tmp_path: Path) -> None:
    generate_project("reviewprobe", tmp_path)
    config = yaml.safe_load(
        (tmp_path / "reviewprobe/deploy/docker-compose.yml").read_text()
    )
    services = config["services"]
    assert services["db"]["volumes"]
    assert services["redis"]["volumes"]
    assert services["redis"]["healthcheck"]["test"]
    for name in ("app", "worker", "beat"):
        assert services[name]["stop_grace_period"] == "60s"
