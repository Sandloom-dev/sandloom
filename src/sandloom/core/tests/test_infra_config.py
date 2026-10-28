"""Infrastructure configuration: synthesized defaults + INFRA overlay."""

from __future__ import annotations

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    DEFAULTED_CAPABILITIES,
    InfraConfigError,
    load_infra_config,
)


def _settings_with_infra(monkeypatch: pytest.MonkeyPatch, infra: dict) -> object:
    monkeypatch.setattr(settings, "infra", infra, raising=False)
    return settings


def test_defaults_synthesized_for_every_capability() -> None:
    cfg = load_infra_config(settings)
    for capability in DEFAULTED_CAPABILITIES:
        assert DEFAULT_INSTANCE in cfg[capability]
    assert cfg["database"][DEFAULT_INSTANCE].driver == "sqlalchemy"
    assert cfg["database"][DEFAULT_INSTANCE].options["url"] == settings.database_url
    assert cfg["cache"][DEFAULT_INSTANCE].driver == "redis"
    # eventbus auto resolves to memory outside prod
    assert cfg["eventbus"][DEFAULT_INSTANCE].driver in {"memory", "redis"}


def test_infra_overlay_adds_named_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings_with_infra(
        monkeypatch,
        {
            "database": {
                "analytics": {
                    "driver": "sqlalchemy",
                    "url": "postgresql+asyncpg://u:p@h:5432/analytics",
                }
            }
        },
    )
    cfg = load_infra_config(s)  # type: ignore[arg-type]
    assert set(cfg["database"]) == {"default", "analytics"}
    analytics = cfg["database"]["analytics"]
    assert analytics.driver == "sqlalchemy"
    assert analytics.options["url"].endswith("/analytics")
    # default is untouched
    assert cfg["database"]["default"].options["url"] == settings.database_url


def test_infra_overlay_overrides_default_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings_with_infra(
        monkeypatch,
        {"cache": {"default": {"url": "redis://other:6379/9"}}},
    )
    cfg = load_infra_config(s)  # type: ignore[arg-type]
    # driver retained from synthesized default, options overridden
    assert cfg["cache"]["default"].driver == "redis"
    assert cfg["cache"]["default"].options["url"] == "redis://other:6379/9"


def test_unknown_capability_accepted_by_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # config.py validates SHAPE only: a capability a plugin may register
    # (workflow, graph_store, …) must NOT be rejected here. Whether the
    # capability is valid is the registry's job (it owns the specs).
    s = _settings_with_infra(
        monkeypatch, {"workflow": {"default": {"driver": "temporal"}}}
    )
    cfg = load_infra_config(s)  # type: ignore[arg-type]
    assert cfg["workflow"]["default"].driver == "temporal"


def test_new_instance_requires_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings_with_infra(monkeypatch, {"database": {"x": {"url": "..."}}})
    with pytest.raises(InfraConfigError):
        load_infra_config(s)  # type: ignore[arg-type]
