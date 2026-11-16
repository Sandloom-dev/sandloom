"""``create_app`` sets the driver selection of its own app, every call.

The selection lives on the process (``sandloom.core.infra.config``): an app that
passes none must not inherit the one a previous ``create_app`` of the process
set. It adopts its composition root's own (``DEFAULT_INFRA``), or none, which
startup refuses with SANDLOOM-B037. A selection a tool or a test suite set with
``use_default_infra`` itself is not an app's, and stays.
"""

from __future__ import annotations

import pytest

import sandloom.core.app_factory as app_factory
import sandloom.core.infra.config as config_module
from sandloom.core.app_factory import create_app


class _StopBeforeBoot(Exception):
    """Raised in place of the configuration checks, right after the selection."""


@pytest.fixture(autouse=True)
def _isolated_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the process selection after each test; stop ``create_app`` once
    the selection is set, so no test boots an app."""
    monkeypatch.setattr(config_module, "_APP", None)
    monkeypatch.setattr(config_module, "_PIN", None)

    def stop(_settings: object) -> None:
        raise _StopBeforeBoot

    monkeypatch.setattr(app_factory, "enforce_configuration", stop)


def _selected_drivers() -> dict[str, str]:
    """``{capability/instance: driver}`` the process selection holds now."""
    return {
        f"{capability}/{name}": cfg.driver
        for capability, instances in config_module._composition_instances().items()
        for name, cfg in instances.items()
    }


def _boot(**kwargs: object) -> None:
    with pytest.raises(_StopBeforeBoot):
        create_app(**kwargs)  # type: ignore[arg-type]


def test_an_app_with_no_selection_does_not_inherit_the_previous_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDLOOM_COMPOSITION_ROOT", "sandloom.main")  # selects none
    _boot(default_infra={"cache": {"default": {"driver": "redis"}}})
    assert _selected_drivers() == {"cache/default": "redis"}
    _boot()
    assert _selected_drivers() == {}


def test_an_app_with_no_selection_adopts_its_roots_not_the_previous_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("sandloom_standard")  # absent in the kernel-only venv
    monkeypatch.setenv("SANDLOOM_COMPOSITION_ROOT", "sandloom_standard.main")
    _boot(default_infra={"cache": {"default": {"driver": "other"}}})
    assert _selected_drivers() == {"cache/default": "other"}
    _boot()
    selected = _selected_drivers()
    assert selected["cache/default"] == "redis"
    assert selected["database/default"] == "sqlalchemy"


def test_an_explicit_selection_replaces_the_previous_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDLOOM_COMPOSITION_ROOT", "sandloom.main")
    _boot(default_infra={"cache": {"default": {"driver": "redis"}}})
    _boot(default_infra={"eventbus": {"default": {"driver": "streams"}}})
    assert _selected_drivers() == {"eventbus/default": "streams"}


def test_a_selection_set_outside_create_app_survives_an_app_with_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The framework's own suite selects the drivers for its lean root, which
    passes none: that selection is not an earlier app's."""
    monkeypatch.setenv("SANDLOOM_COMPOSITION_ROOT", "sandloom.main")
    config_module.use_default_infra({"cache": {"default": {"driver": "redis"}}})
    _boot()
    assert _selected_drivers() == {"cache/default": "redis"}


_PIN = {"cache": {"default": {"driver": "redis"}}}
_ARG = {"eventbus": {"default": {"driver": "streams"}}}


def test_the_argument_wins_over_a_pin_which_an_app_never_overwrites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One precedence: the app's argument, then the pin, then the root's. The
    sequence that lost a pin: pin, ``create_app(default_infra=X)``, then an app
    with none."""
    monkeypatch.setenv("SANDLOOM_COMPOSITION_ROOT", "sandloom.main")  # selects none
    config_module.use_default_infra(_PIN)
    _boot(default_infra=_ARG)
    assert _selected_drivers() == {"eventbus/default": "streams"}  # argument > pin
    _boot()
    assert _selected_drivers() == {"cache/default": "redis"}  # the pin is back
    _boot(default_infra=_ARG)
    _boot(default_infra=_ARG)
    _boot()
    assert _selected_drivers() == {"cache/default": "redis"}


def test_a_new_pin_is_the_later_word_over_an_earlier_apps_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDLOOM_COMPOSITION_ROOT", "sandloom.main")
    _boot(default_infra=_ARG)
    config_module.use_default_infra(_PIN)
    assert _selected_drivers() == {"cache/default": "redis"}


def test_a_pin_wins_over_the_roots_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("sandloom_standard")  # absent in the kernel-only venv
    monkeypatch.setenv("SANDLOOM_COMPOSITION_ROOT", "sandloom_standard.main")
    config_module.use_default_infra(_PIN)
    _boot()
    assert _selected_drivers() == {"cache/default": "redis"}  # not the root's set


def test_an_empty_pin_is_a_pin_not_an_absent_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``use_default_infra(None)`` pins "no selection": the root's is not read."""
    pytest.importorskip("sandloom_standard")  # absent in the kernel-only venv
    monkeypatch.setenv("SANDLOOM_COMPOSITION_ROOT", "sandloom_standard.main")
    config_module.use_default_infra(None)
    _boot()
    assert _selected_drivers() == {}
