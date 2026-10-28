"""The ``sandloom.core.testing`` pytest plugin, exercised in throwaway projects.

Each case runs a real ``pytest`` subprocess on a tiny project that activates
the plugin the way an application does (``-p sandloom.core.testing.plugin`` in its
addopts), so plugin ordering (``.env.test`` before any framework import) is
tested for real. The full DB/HTTP flow is covered by
``test_project.py::test_generated_project_passes_its_own_tests``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_PYPROJECT = """\
[tool.pytest.ini_options]
pythonpath = ["."]
addopts = ["-p", "sandloom.core.testing.plugin", "-p", "no:cacheprovider"]
asyncio_mode = "auto"
{extra}
"""


def _project(root: Path, files: dict[str, str], *, extra_ini: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(_PYPROJECT.format(extra=extra_ini))
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body))
    return root


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SANDLOOM_", "PYTEST_", "JWT_")) and k != "ENVIRONMENT"
    }
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_missing_env_test_is_a_clear_usage_error(tmp_path: Path) -> None:
    root = _project(tmp_path / "app", {"test_x.py": "def test_x(): pass\n"})
    proc = _run(root)
    assert proc.returncode == pytest.ExitCode.USAGE_ERROR, proc.stdout + proc.stderr
    assert "no .env.test found" in proc.stdout + proc.stderr


_URLS = (
    "DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:1/probe_test\n"
    "CACHE_URL=redis://127.0.0.1:1/0\n"
    "BROKER_URL=redis://127.0.0.1:1/1\n"
)
_ENV_TEST = (
    "ENVIRONMENT=test\nJWT_SECRET=plugin_probe_secret_value_long_enough_0123\n" + _URLS
)


def test_plugin_exposes_db_uow_and_binds_service_effects(tmp_path: Path) -> None:
    root = _project(
        tmp_path / "app",
        {
            ".env.test": _ENV_TEST,
            "conftest.py": """
            import pytest_asyncio
            from sqlalchemy.ext.asyncio import AsyncSession

            @pytest_asyncio.fixture
            async def db():
                async with AsyncSession() as session:
                    yield session
        """,
            "test_effects.py": """
            import pytest
            from sandloom.core.di import record_audit

            @pytest.mark.asyncio
            async def test_effect(db_uow):
                record_audit("probe.updated", resource_type="probe")
                # pending plain rows of the UoW (effect_rows), not ORM objects
                rows = db_uow.sync_session.info["sandloom_effect_rows"]
                assert [row.action for row in rows] == ["probe.updated"]
        """,
        },
    )
    proc = _run(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_env_test_is_loaded_before_any_conftest_imports_settings(
    tmp_path: Path,
) -> None:
    root = _project(
        tmp_path / "app",
        {
            ".env.test": _ENV_TEST,
            # A conftest importing settings at import time must already see it.
            "conftest.py": """
                from sandloom.core.config.settings import settings

                assert settings.jwt_secret.startswith("plugin_probe"), "late env"
            """,
            "pkg/__init__.py": "",
            "pkg/tests/__init__.py": "",
            "pkg/tests/test_headers.py": """
                import uuid

                from sandloom.core.auth.tokens import decode_token
                from sandloom.core.testing import make_user_headers


                def test_make_headers_fixture(make_headers, user_headers):
                    uid, tid = uuid.uuid4(), uuid.uuid4()
                    got_uid, headers = make_headers(uid, tid, roles=["admin"])
                    assert got_uid == uid
                    token = headers["Authorization"].removeprefix("Bearer ")
                    claims = decode_token(token)
                    assert claims["sub"] == str(uid)
                    assert claims["token_type"] == "access"
                    assert claims["tenant_id"] == str(tid)
                    assert claims["tenant_ids"] == [str(tid)]
                    assert claims["email_verified"] is True
                    assert claims["email"].endswith("@example.com")
                    assert claims["roles"] == ["admin"]
                    assert "iat" in claims
                    assert user_headers["Authorization"].startswith("Bearer ")


                def test_plain_helper_without_tenant():
                    _, headers = make_user_headers(tenant=False, email=None)
                    token = headers["Authorization"].removeprefix("Bearer ")
                    claims = decode_token(token)
                    assert "tenant_id" not in claims and "email" not in claims
            """,
        },
    )
    proc = _run(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2 passed" in proc.stdout


def test_env_test_is_found_walking_up(tmp_path: Path) -> None:
    (tmp_path / ".env.test").write_text(_ENV_TEST)
    root = _project(
        tmp_path / "nested" / "app",
        {
            "test_env.py": """
                import os

                def test_env():
                    assert os.environ["JWT_SECRET"].startswith("plugin_probe")
            """,
        },
    )
    proc = _run(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_composition_root_ini_option(tmp_path: Path) -> None:
    root = _project(
        tmp_path / "app",
        {
            ".env.test": _ENV_TEST,
            "fakeroot.py": """
                class _Core:
                    loaded_modules = []

                    def get_service(self, name):
                        return None


                class _State:
                    core = _Core()


                class _App:
                    state = _State()
                    dependency_overrides = {}


                app = _App()
            """,
            "test_app.py": """
                import fakeroot


                def test_app(sandloom_app):
                    assert sandloom_app is fakeroot.app
            """,
        },
        extra_ini='sandloom_composition_root = "fakeroot"',
    )
    proc = _run(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_refuses_a_non_test_database(tmp_path: Path) -> None:
    root = _project(
        tmp_path / "app",
        {
            ".env.test": (
                "ENVIRONMENT=dev\n"
                "JWT_SECRET=plugin_probe_secret_value_long_enough_0123\n"
                "DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:1/production\n"
                "CACHE_URL=redis://127.0.0.1:1/0\n"
                "BROKER_URL=redis://127.0.0.1:1/1\n"
            ),
            "test_db.py": """
                def test_db(db):
                    pass
            """,
        },
    )
    proc = _run(root)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Refusing to reset tables on database 'production'" in proc.stdout
