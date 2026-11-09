"""The starter environment ``sandloom new`` writes for a bundled module is the
module's declared ``deploy`` text (edge row B-N6, ADR-0015).

``sandloom new --with ...`` writes the monolith profile before the chosen
packages are installed, so the kernel keeps a copy of each one's environment
lines (``_IDENTITY_ENV``, ``_BILLING_ENV``, ``_ADMIN_ENV``: the row's retained
reason). The copy is only allowed while it is the declaration: what
``module_env`` reads from the installed package's settings and ``sandloom add
deploy`` writes per service. Here the packages are installed, so both are
read and compared, and the generated profiles carry the same lines.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sandloom.core.cli import project
from sandloom.core.cli.project import generate_project
from sandloom.core.cli.scaffold_deploy import module_env

#: The starter copy of each bundled module that declares environment lines,
#: by import path.
COPIES = {
    "sandloom_identity": project._IDENTITY_ENV,
    "sandloom_billing": project._BILLING_ENV,
    "sandloom_admin": project._ADMIN_ENV,
}


@pytest.mark.parametrize("module", sorted(COPIES))
def test_the_starter_copy_is_the_declared_deploy_text(module: str) -> None:
    declared = module_env(module)
    assert declared, f"{module} declares no deploy lines: drop its starter copy"
    assert COPIES[module] == declared


@pytest.mark.parametrize(
    ("battery", "module"),
    [
        ("identity", "sandloom_identity"),
        ("billing", "sandloom_billing"),
        ("admin", "sandloom_admin"),
    ],
)
def test_the_monolith_profile_carries_the_declared_lines(
    tmp_path: Path, battery: str, module: str
) -> None:
    generate_project("shop", tmp_path, with_batteries=[battery])
    compose = (tmp_path / "shop" / "deploy" / "docker-compose.yml").read_text()
    for line in module_env(module):
        assert f"\n  {line}\n" in compose, line


def test_an_app_without_the_console_sets_no_console_switch(tmp_path: Path) -> None:
    """The switch is the console's declaration: a profile without the console
    does not carry it."""
    generate_project("plain", tmp_path, with_batteries=["identity", "billing"])
    compose = (tmp_path / "plain" / "deploy" / "docker-compose.yml").read_text()
    assert "ADMIN_CONSOLE_ENABLED" not in compose
