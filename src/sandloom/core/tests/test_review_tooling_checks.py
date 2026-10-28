from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[4]


def load_script(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_api_gate_preserves_generic_type_arguments_and_default_values() -> None:
    api = load_script(ROOT / "tools/packaging/check_public_api.py")

    def before(value=None):
        pass

    def after(value=None):
        pass

    before.__annotations__ = {"value": list[int]}
    after.__annotations__ = {"value": list[str]}
    assert api._signature_of(before) != api._signature_of(after)

    def enabled(value: bool = True):
        pass

    def disabled(value: bool = False):
        pass

    assert api._signature_of(enabled) != api._signature_of(disabled)
    assert api._default_str(object()) == api._default_str(object())
    assert "sandloom.core.testing" in api._public_modules()


def test_credential_scan_never_echoes_values_and_limits_fixture_exemptions() -> None:
    scanner = load_script(ROOT / "tools/checks/check_secrets.py")
    secret = "GOCSPX-" + "A" * 32
    assert scanner.findings(Path(".env.test"), secret) == [(1, "google-oauth")]
    assert scanner.findings(Path("app.py"), "# sandloom: secret-fixture\n" + secret)
    assert not scanner.findings(
        Path("tests/probe.py"), "# sandloom: secret-fixture\n" + secret
    )
    assert not scanner.findings(Path(".env.test"), "ci-dummy-google-client-secret")
