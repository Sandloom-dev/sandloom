"""Infrastructure configuration: synthesized defaults + INFRA overlay."""

from __future__ import annotations

import pytest

import sandloom.core.infra.config as config_module
from sandloom.core.config.settings import settings
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    DEFAULTED_CAPABILITIES,
    InfraConfigError,
    load_infra_config,
    use_default_infra,
)


def _settings_with_infra(monkeypatch: pytest.MonkeyPatch, infra: dict) -> object:
    monkeypatch.setattr(settings, "infra", infra, raising=False)
    return settings


@pytest.fixture
def redis_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A composition that selects the ``redis`` cache and broadcast drivers (the
    standard one does). The configuration only names the drivers, builds none:
    the suite's own selection is ``memory`` where ``sandloom-redis`` is absent
    (``kernel-without-redis``), so these tests pin theirs."""
    monkeypatch.setattr(config_module, "_PIN", None)
    monkeypatch.setattr(config_module, "_APP", None)
    # The process INFRA (.env.test) names instances this selection does not.
    monkeypatch.setattr(settings, "infra", {}, raising=False)
    use_default_infra(
        {
            "database": {"default": {"driver": "sqlalchemy"}},
            "cache": {"default": {"driver": "redis"}},
            "broadcast": {"default": {"driver": "redis"}},
            "taskqueue": {"default": {"driver": "celery"}},
        }
    )


@pytest.mark.usefixtures("redis_selection")
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


@pytest.mark.usefixtures("redis_selection")
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


def test_infra_overlay_gives_a_replacement_driver_no_prior_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement driver gets the options the overlay states, none inherited."""
    monkeypatch.setattr(settings, "notifier_backend", "smtp")
    s = _settings_with_infra(
        monkeypatch,
        {"notification": {"default": {"driver": "file", "path": "/m.log"}}},
    )

    instance = load_infra_config(s)["notification"]["default"]  # type: ignore[arg-type]

    assert instance.driver == "file"
    assert instance.options == {"path": "/m.log"}


def test_flat_s3_settings_do_not_inherit_the_local_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "infra", {})
    monkeypatch.setattr(settings, "storage_type", "s3")

    instance = load_infra_config(settings)["objectstore"]["default"]

    assert instance.driver == "s3"
    assert "path" not in instance.options


@pytest.mark.usefixtures("redis_selection")
def test_the_broadcast_default_is_a_defaulted_capability() -> None:
    assert "broadcast" in DEFAULTED_CAPABILITIES
    broadcast = load_infra_config(settings)["broadcast"][DEFAULT_INSTANCE]
    assert broadcast.driver == "redis"
    assert broadcast.implicit is True


# The broadcast default following the resolved cache default URL is the Redis
# driver's own fill: ``packages/sandloom-redis/tests/test_redis_infra_defaults.py``.


def test_a_stated_broadcast_url_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings_with_infra(
        monkeypatch,
        {
            "cache": {"default": {"driver": "redis", "url": "redis://cache-host/0"}},
            "broadcast": {"default": {"driver": "redis", "url": "redis://fan/0"}},
        },
    )
    cfg = load_infra_config(s)  # type: ignore[arg-type]
    assert cfg["broadcast"][DEFAULT_INSTANCE].options["url"] == "redis://fan/0"
