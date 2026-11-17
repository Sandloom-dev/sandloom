"""``sandloom generate`` against a composition that cannot be built from INFRA.

The default composition (``sandloom.main``) selects no driver; an INFRA that
names only a url for an instance (the framework checkout's ``.env.test``) then
fails to load. The command says what to do instead of ending in a traceback.
"""

from __future__ import annotations

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
    def refuse() -> object:
        raise InfraConfigError("INFRA['cache']['default'] needs a 'driver': say which")

    monkeypatch.setattr(generated, "load_composition_app", refuse)
    root = Path(__file__).resolve().parents[4]
    assert generated.main(["--check", "--root", str(root)]) == 2
    out = capsys.readouterr().out
    assert "the composition cannot be built" in out
    assert "needs a 'driver'" in out
