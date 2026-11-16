"""What the Redis drivers take through the kernel's config: only their own ``INFRA`` entry.

``load_infra_config`` fills nothing from the flat settings, and no instance
inherits another's url (ADR-0016). ``fill`` builds the registry, which loads the
``redis`` driver of this package.
"""

from __future__ import annotations

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.infra.config import DEFAULT_INSTANCE, load_infra_config
from sandloom.core.tests._driver_fill import fill


def test_the_broadcast_default_does_not_follow_the_cache_default_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An INFRA ``cache.default.url`` does not move the fan-out with the cache:
    no instance inherits another's url, the broadcast states its own."""
    monkeypatch.setattr(
        settings,
        "infra",
        {
            "cache": {
                "default": {"driver": "redis", "url": "redis://cache-host:6390/4"}
            },
            "broadcast": {"default": {"url": "redis://fanout-host:6390/7"}},
        },
        raising=False,
    )
    cfg = fill(load_infra_config(settings))
    assert cfg["cache"][DEFAULT_INSTANCE].options["url"] == "redis://cache-host:6390/4"
    assert (
        cfg["broadcast"][DEFAULT_INSTANCE].options["url"]
        == "redis://fanout-host:6390/7"
    )


def test_a_broadcast_default_without_a_url_gets_none_from_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "infra",
        {"cache": {"default": {"driver": "redis", "url": "redis://cache-host:6390/4"}}},
        raising=False,
    )
    cfg = fill(load_infra_config(settings))
    assert "url" not in cfg["broadcast"][DEFAULT_INSTANCE].options
