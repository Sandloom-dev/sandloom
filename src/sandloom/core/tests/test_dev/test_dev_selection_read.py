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


def test_a_banner_printed_without_a_newline_does_not_hide_the_answer(
    checkout: Path, environ: dict[str, str]
) -> None:
    (checkout / "shopapp" / "__init__.py").write_text(
        "import sys\nsys.stdout.write('banner without a newline')\n" + _SELECTION
    )
    selection = commands.composition_selection(_ctx(checkout, environ), "", checkout)
    assert set(selection.infra) == {"cache"}


_FAILING_IMPORT = (
    'raise RuntimeError("cannot reach redis://app:s3cretpw@cache.internal:6379/0")\n'
)


def test_a_failed_read_names_the_exit_code_and_echoes_nothing_the_app_printed(
    checkout: Path, environ: dict[str, str]
) -> None:
    (checkout / "shopapp" / "__init__.py").write_text(_FAILING_IMPORT)
    with pytest.raises(commands.DevError) as caught:
        commands.composition_selection(_ctx(checkout, environ), "", checkout)
    message = str(caught.value)
    assert "exited with code 1" in message and "--debug" in message
    assert "s3cretpw" not in message and "cache.internal" not in message


def test_the_last_error_line_shows_under_debug_without_url_credentials(
    checkout: Path, environ: dict[str, str]
) -> None:
    (checkout / "shopapp" / "__init__.py").write_text(_FAILING_IMPORT)
    ctx = _ctx(checkout, environ)
    ctx.verbose = True
    with pytest.raises(commands.DevError) as caught:
        commands.composition_selection(ctx, "", checkout)
    message = str(caught.value)
    assert "RuntimeError: cannot reach redis://***@cache.internal:6379/0" in message
    assert "s3cretpw" not in message and "app:" not in message


def test_dev_up_reads_the_selection_once_per_checkout(
    checkout: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``write_checkout_files`` and the summary both ask for it: one child."""
    (checkout / "shopapp" / "__init__.py").write_text(_SELECTION)
    reads: list[Path | None] = []
    real = commands.composition_selection

    def counting(
        ctx: commands.Context, env_text: str, at: Path | None = None
    ) -> commands.CompositionSelection:
        reads.append(at)
        return real(ctx, env_text, at)

    monkeypatch.setattr(commands, "composition_selection", counting)
    ctx = _ctx(checkout, environ)
    row = commands.CheckoutRow(
        path=str(checkout),
        database="shop",
        cache_index=1,
        broker_index=2,
        test_database="shop_test",
    )
    first = commands.row_selection(ctx, row)
    assert commands.row_selection(ctx, row) is first
    assert reads == [checkout]
    other = commands.CheckoutRow(
        path=str(checkout.parent),
        database="x",
        cache_index=3,
        broker_index=4,
        test_database="x_test",
    )
    # another checkout (no package there: nothing to manage) is its own read
    assert commands.row_selection(ctx, other) == commands.CompositionSelection({}, {})
    assert reads == [checkout, checkout.parent]
