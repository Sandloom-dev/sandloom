"""Import layering across distributions (tools/packaging/distribution_imports).

Synthetic wheels: the checker must catch an import of a distribution the
importing one does not depend on, in every spelling the CLI ever used
(module level, function-local, ``importlib.import_module`` / a helper's
literal), and of a first-party module no distribution ships. The real wheels
are checked by ``check_package_distributions.py`` (CI ``packages`` job).
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "packaging"))

import distribution_imports as di  # noqa: E402

TOPS = {"fastapi": {"fastapi"}, "redis": {"redis"}, "pyotp": {"pyotp"}}


def _requires(dist: str, extras: frozenset[str]) -> list[tuple[str, frozenset[str]]]:
    return []


def _check(*wheels: di.Wheel) -> list[str]:
    return di.check(
        wheels,
        first_party=["sandloom", "sandloom_identity"],
        top_levels=TOPS,
        requires_of=_requires,
    )


def _kernel(**modules: str) -> di.Wheel:
    wheel = di.Wheel("sandloom", {"fastapi", "redis"})
    wheel.modules = {"sandloom": "", "sandloom.sdk": "", "sandloom.core": "", **modules}
    wheel.packages = {"sandloom", "sandloom.sdk", "sandloom.core"}
    return wheel


def _identity(**modules: str) -> di.Wheel:
    wheel = di.Wheel("sandloom-identity", {"sandloom", "pyotp"})
    wheel.modules = {"sandloom_identity": "", **modules}
    wheel.packages = {"sandloom_identity"}
    return wheel


def test_a_clean_set_passes() -> None:
    kernel = _kernel(**{"sandloom.core.app": "import fastapi\nfrom . import sdk\n"})
    identity = _identity(
        **{"sandloom_identity.api": "import pyotp\nfrom sandloom.sdk import x\n"}
    )
    assert _check(kernel, identity) == []


@pytest.mark.parametrize(
    "source",
    [
        "import sandloom_identity\n",
        "def cmd():\n    from sandloom_identity import api\n",
        "import importlib\n\ndef cmd():\n    importlib.import_module('sandloom_identity.api')\n",
        "def cmd():\n    return _static_import('sandloom_identity')\n",
        "try:\n    import sandloom_identity\nexcept ImportError:\n    pass\n",
    ],
    ids=["module-level", "function-local", "import_module", "helper", "guarded"],
)
def test_a_kernel_import_of_a_module_package_is_caught(source: str) -> None:
    problems = _check(_kernel(**{"sandloom.cli": source}), _identity())
    assert len(problems) == 1, problems
    assert "sandloom.cli" in problems[0]
    assert "sandloom-identity, which sandloom does not depend on" in problems[0]


def test_the_lazy_cli_import_of_an_absent_distribution_is_caught() -> None:
    """``sandloom docs`` once crashed: the CLI imported ``sandloom.sdk``
    function-locally while another distribution shipped it."""
    kernel = di.Wheel("sandloom", set())
    kernel.modules = {
        "sandloom": "def main():\n    _static_import('sandloom.sdk.tooling')\n"
    }
    kernel.packages = {"sandloom"}
    sdk = di.Wheel("sandloom-shared", {"sandloom"})
    sdk.modules = {"sandloom.sdk": "", "sandloom.sdk.tooling": ""}
    sdk.packages = {"sandloom.sdk"}
    (problem,) = _check(kernel, sdk)
    assert "imports sandloom.sdk.tooling from sandloom-shared" in problem


def test_an_unshipped_first_party_module_is_caught() -> None:
    kernel = _kernel(**{"sandloom.docs": "from sandloom.shared.contracts import x\n"})
    (problem,) = _check(kernel)
    assert "which no distribution ships" in problem


def test_type_checking_imports_are_not_runtime_imports() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    import sandloom_identity\n"
    )
    assert _check(_kernel(**{"sandloom.x": source}), _identity()) == []


def test_undeclared_third_party_is_caught_unless_optional() -> None:
    bare = _identity(**{"sandloom_identity.x": "import redis\n"})
    bare.requires = {"pyotp"}  # no sandloom: redis is not reachable
    (problem,) = _check(bare)
    assert (
        "imports redis from redis, which sandloom-identity does not declare" in problem
    )
    optional = _identity(
        **{
            "sandloom_identity.x": "try:\n    import redis\nexcept ImportError:\n    redis = None\n"
        }
    )
    optional.requires = {"pyotp"}
    assert _check(optional) == []


def test_a_dependency_brings_its_imports_transitively() -> None:
    identity = _identity(**{"sandloom_identity.x": "import fastapi\n"})
    assert _check(_kernel(), identity) == []


def test_relative_imports_resolve_against_the_package(tmp_path: Path) -> None:
    wheel = tmp_path / "sandloom_identity-0.7.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "sandloom_identity-0.7.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: sandloom-identity\nVersion: 0.7.0\n"
            "Requires-Dist: sandloom<0.8,>=0.7\n",
        )
        archive.writestr("sandloom_identity/__init__.py", "from .api import x\n")
        archive.writestr("sandloom_identity/api.py", "from ..sandloom import y\n")
    read = di.read_wheel(wheel)
    assert read.name == "sandloom-identity" and read.requires == {"sandloom"}
    assert read.packages == {"sandloom_identity"}
    imports = di.module_imports(
        read.modules["sandloom_identity"], "sandloom_identity", is_package=True
    )
    assert [i.target for i in imports] == ["sandloom_identity.api.x"]
