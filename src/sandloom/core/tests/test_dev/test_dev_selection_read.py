"""The child process that reads the composition's ``DEFAULT_INFRA``.

``sandloom dev up`` reads the selection in a child, in the checkout. Its answer
is the line that carries the sentinel, so an app that prints at import or at exit
cannot corrupt it, and the checkout's own package is found even when
``PYTHONSAFEPATH=1`` drops the working directory from ``sys.path``.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from sandloom.core.cli.dev import commands, identity

_SELECTION = 'DEFAULT_INFRA = {"cache": {"default": {"driver": "redis"}}}\n'


def _ctx(root: Path, environ: dict[str, str]) -> commands.Context:
    return commands.Context(
        identity=identity.resolve(root, environ),
        environ=environ,
        out=io.StringIO(),
        err=io.StringIO(),
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    (root / "shopapp").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "shop"\n')
    return root


@pytest.fixture
def environ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("SANDLOOM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    env = {k: v for k, v in os.environ.items() if k not in {"CI", "DOCKER_HOST"}}
    env["SANDLOOM_COMPOSITION_ROOT"] = "shopapp.asgi"
    return env


def test_an_app_that_prints_at_import_and_at_exit_does_not_corrupt_the_answer(
    checkout: Path, environ: dict[str, str]
) -> None:
    (checkout / "shopapp" / "__init__.py").write_text(
        "import atexit\n"
        "print('banner at import')\n"
        "atexit.register(lambda: print('{\"goodbye\": true}'))\n" + _SELECTION
    )
    selection = commands.composition_selection(_ctx(checkout, environ), "", checkout)
    assert set(selection.infra) == {"cache"}


def test_the_checkout_package_is_found_under_pythonsafepath(
    checkout: Path, environ: dict[str, str]
) -> None:
    (checkout / "shopapp" / "__init__.py").write_text(_SELECTION)
    environ["PYTHONSAFEPATH"] = "1"
    selection = commands.composition_selection(_ctx(checkout, environ), "", checkout)
    assert set(selection.infra) == {"cache"}


def test_a_child_that_prints_no_answer_is_refused(
    checkout: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (checkout / "shopapp" / "__init__.py").write_text(_SELECTION)
    monkeypatch.setattr(commands, "_SELECTION_SENTINEL", "OTHER:")
    monkeypatch.setattr(
        commands,
        "_READ_SELECTION",
        'print(\'{"infra": {}, "url_options": {}}\')\n',
    )
    with pytest.raises(commands.DevError, match="DEFAULT_INFRA.*shopapp"):
        commands.composition_selection(_ctx(checkout, environ), "", checkout)
