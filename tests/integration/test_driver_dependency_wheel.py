"""Dependency preflight and conformance for an installed third-party driver."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.subprocess_probe,
    pytest.mark.skipif(shutil.which("uv") is None, reason="building wheels needs uv"),
]

DRIVER = """\
from sandloom.driverkit import NotifierProvider

class WheelNotifier(NotifierProvider):
    contract_version = "v1"
    supported_features = frozenset({"email"})
    required_imports = ("wheel_notifier_backend",)
    install_hint = "pip install 'wheel-notifier[backend]'"

    async def send(self, notification):
        from wheel_notifier_backend import deliver

        deliver(self.options["path"], notification.body)
"""

BACKEND = """\
from pathlib import Path

def deliver(path, body):
    Path(path).write_text(body)
"""

PROBE = """\
import sys
from importlib.util import find_spec

import pytest

from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.registry import DriverUnavailableError
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.driverkit import Notification
from sandloom.driverkit.testing import NotifierConformance


def registry_for(driver, **options):
    config = {"notification": {"default": InstanceConfig(
        "notification", "default", driver, options
    )}}
    return build_infra_registry(InMemorySchemaRegistry(), config=config)


def test_wheel_is_lazy_and_preflight_reports_the_missing_extra():
    assert find_spec("wheel_notifier") is not None
    assert find_spec("wheel_notifier_backend") is None
    registry_for("log").validate_requirement("notification")
    assert "wheel_notifier" not in sys.modules

    with pytest.raises(DriverUnavailableError) as caught:
        registry_for("external")
    assert caught.value.sandloom_code == "SANDLOOM-B037"
    assert caught.value.missing == ("wheel_notifier_backend",)
    assert "pip install 'wheel-notifier[backend]'" in str(caught.value)
    assert "in INFRA[" in str(caught.value)


@pytest.fixture
def installed_registry(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(BACKEND_SITE))
    registry = registry_for("external", path=str(tmp_path / "delivery.txt"))
    registry.validate_requirement("notification", features=frozenset({"email"}))
    assert registry.all_built() == []
    return registry


async def test_installed_wheel_opens_and_delivers(installed_registry, tmp_path):
    try:
        await installed_registry.aopen_all(strict=True)
        provider = installed_registry.provider("notification")
        await provider.send(Notification(
            channel="email", recipient="reader@example.test", subject="Test", body="hello"
        ))
        assert (tmp_path / "delivery.txt").read_text() == "hello"
    finally:
        await installed_registry.aclose_all()


class TestInstalledNotifier(NotifierConformance):
    @pytest.fixture
    def make_provider(self, installed_registry):
        return lambda: installed_registry.provider("notification")
"""


def _run(command: list[str], cwd: Path) -> None:
    result = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, timeout=180, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _install_wheel(root: Path, module: str, code: str, project_fields: str) -> Path:
    source = root / module
    source.mkdir()
    package = source / module
    package.mkdir()
    (package / "__init__.py").write_text(code)
    (source / "pyproject.toml").write_text(
        f'[project]\nname = "{module.replace("_", "-")}"\nversion = "0.1.0"\n'
        f'{project_fields}\n[build-system]\nrequires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n'
    )
    dist = source / "dist"
    _run(["uv", "build", "--wheel", "--out-dir", str(dist)], source)
    site = source / "site"
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(site),
            "--no-deps",
            str(next(dist.glob("*.whl"))),
        ],
        source,
    )
    return site


def test_external_notification_wheel_dependency_contract(tmp_path: Path) -> None:
    build = tmp_path / "build"
    build.mkdir()
    backend = _install_wheel(build, "wheel_notifier_backend", BACKEND, "")
    driver = _install_wheel(
        build,
        "wheel_notifier",
        DRIVER,
        'dependencies = ["sandloom"]\n'
        '[project.optional-dependencies]\nbackend = ["wheel-notifier-backend"]\n'
        '[project.entry-points."sandloom.drivers"]\n'
        '"notification.external" = "wheel_notifier:WheelNotifier"\n',
    )
    probe = tmp_path / "test_installed_driver.py"
    probe.write_text(
        f"import sys\nsys.path.insert(0, {str(driver)!r})\n"
        f"BACKEND_SITE = {str(backend)!r}\n" + PROBE
    )
    _run([sys.executable, "-m", "pytest", "--asyncio-mode=auto", str(probe)], tmp_path)
