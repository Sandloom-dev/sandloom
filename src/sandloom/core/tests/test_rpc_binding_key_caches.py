"""Response-binding key caches stay bounded without wholesale clears (GLM-7)."""

from __future__ import annotations

from typing import Any

import pytest

from sandloom.core import rpc_binding
from sandloom.core.config.settings import settings
from sandloom.core.contract_testing import rpc_keys, rpc_settings


@pytest.fixture
def cfg() -> Any:
    return settings.model_copy(update=rpc_settings("provider", peers=("caller",)))


def test_unverified_or_unknown_callers_never_churn_the_pair_key_cache(
    cfg: Any,
) -> None:
    """Review GLM-7: a provider keyed a response with whatever the request
    PRESENTED, so a stream of bogus credentials could churn the key cache. A
    provider now keys only a VERIFIED caller whose X25519 key is published:
    an unverified request or an unknown caller kid derives (and caches)
    nothing."""
    rpc_binding._pair_keys.clear()
    legit = rpc_binding.provider_key(cfg, ("caller", rpc_keys("caller").kid))
    assert legit is not None
    cached = dict(rpc_binding._pair_keys)
    assert len(cached) == 1
    assert rpc_binding.provider_key(cfg, None) is None
    for n in range(100):
        assert rpc_binding.provider_key(cfg, ("caller", f"bogus-{n}")) is None
        assert rpc_binding.provider_key(cfg, (f"bogus-{n}", "k1")) is None
    assert rpc_binding._pair_keys == cached
    assert rpc_binding.provider_key(cfg, ("caller", rpc_keys("caller").kid)) == legit


def test_bounded_key_caches_evict_the_oldest_entry() -> None:
    rpc_binding._pair_keys.clear()
    own = rpc_keys("provider").dh_private_key
    peer = rpc_keys("caller").dh_public_key
    limit = rpc_binding._CACHE_MAX
    infos = [f"info-{n:04d}".encode() for n in range(limit + 4)]
    for info in infos:
        rpc_binding.pair_key(own, peer, info)
    assert [key[2] for key in rpc_binding._pair_keys] == infos[-limit:]
