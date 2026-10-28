"""A worker xdist starts to replace a crashed one gets a Redis index
deterministically (an allocated spare, else a shared slot with a warning),
never a conftest import failure that crashes every replacement in turn."""

from __future__ import annotations

import pytest

from sandloom.core.testing.workers import mapped_redis_url


def test_replacement_workers_get_the_spares_then_a_shared_slot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mapping = {14: [0, 1, 2, 3, 8, 9]}  # 4 workers + 2 spares
    assert mapped_redis_url("redis://r:6379/14", 2, mapping) == "redis://r:6379/2"
    assert mapped_redis_url("redis://r:6379/14", 4, mapping) == "redis://r:6379/8"
    assert mapped_redis_url("redis://r:6379/14", 5, mapping) == "redis://r:6379/9"
    assert mapped_redis_url("redis://r:6379/14", 7, mapping) == "redis://r:6379/1"
    assert "sharing gw1's index 1" in capsys.readouterr().err
    with pytest.raises(RuntimeError, match="no Redis index allocated"):
        mapped_redis_url("redis://r:6379/15", 0, mapping)
