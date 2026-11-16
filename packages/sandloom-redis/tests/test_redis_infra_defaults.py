"""What the Redis drivers take from the flat settings, through the kernel's config.

``load_infra_config`` fills nothing from the flat settings: each driver class
declares what it takes (``settings_fill``) and the registry applies it once it
has the classes. ``fill`` builds that registry, which loads the ``redis`` driver
of this package.
"""

from __future__ import annotations

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.infra.config import DEFAULT_INSTANCE, load_infra_config
from sandloom.core.tests._driver_fill import fill


def test_the_broadcast_default_follows_the_resolved_cache_default_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An INFRA override of ``cache.default.url`` moves the fan-out with the
    cache: it is not read again from the raw ``CACHE_URL``."""
    monkeypatch.setattr(
        settings,
        "infra",
        {"cache": {"default": {"driver": "redis", "url": "redis://cache-host:6390/4"}}},
        raising=False,
    )
    cfg = fill(load_infra_config(settings))
    assert cfg["cache"][DEFAULT_INSTANCE].options["url"] == "redis://cache-host:6390/4"
    assert cfg["broadcast"][DEFAULT_INSTANCE].options["url"] == (
        "redis://cache-host:6390/4"
    )
