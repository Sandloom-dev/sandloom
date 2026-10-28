"""Client-IP resolution behind proxies + sliding-window rate limiting."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from sandloom.core.ingress.client_ip import client_ip_from_parts, get_client_ip
from sandloom.core.ingress.rate_limit import rate_limiter

GATEWAY = ["10.0.0.0/24"]


def _resolve(peer: str, xff: str | None, *, trusted=GATEWAY, prod=True) -> str:
    return client_ip_from_parts(
        peer,
        xff,
        trust_proxy_headers=True,
        trusted_proxy_ips=trusted,
        is_prod=prod,
    )


def test_spoofed_leftmost_xff_is_ignored() -> None:
    # The client sent "X-Forwarded-For: 6.6.6.6"; the gateway appended the real
    # peer it saw (203.0.113.7). The leftmost entry is attacker-controlled.
    assert _resolve("10.0.0.5", "6.6.6.6, 203.0.113.7") == "203.0.113.7"


def test_walks_right_to_left_skipping_trusted_hops() -> None:
    # client -> edge proxy (10.0.0.9) -> gateway (10.0.0.5) -> app
    assert _resolve("10.0.0.5", "1.1.1.1, 203.0.113.7, 10.0.0.9") == "203.0.113.7"


def test_rotating_spoofed_prefix_maps_to_one_bucket() -> None:
    seen = {_resolve("10.0.0.5", f"9.9.9.{i}, 203.0.113.7") for i in range(50)}
    assert seen == {"203.0.113.7"}


def test_untrusted_peer_headers_ignored() -> None:
    assert _resolve("198.51.100.1", "6.6.6.6") == "198.51.100.1"


def test_all_hops_trusted_returns_leftmost() -> None:
    assert _resolve("10.0.0.5", "10.0.0.7, 10.0.0.9") == "10.0.0.7"


def test_proxy_headers_off_uses_peer() -> None:
    ip = client_ip_from_parts(
        "10.0.0.5",
        "6.6.6.6",
        trust_proxy_headers=False,
        trusted_proxy_ips=GATEWAY,
        is_prod=True,
    )
    assert ip == "10.0.0.5"


def test_no_allowlist_prod_uses_peer_dev_uses_rightmost() -> None:
    assert _resolve("10.0.0.5", "6.6.6.6, 203.0.113.7", trusted=[]) == "10.0.0.5"
    assert (
        _resolve("10.0.0.5", "6.6.6.6, 203.0.113.7", trusted=[], prod=False)
        == "203.0.113.7"
    )


def test_ipv6_and_port_forms_are_normalised() -> None:
    assert _resolve("10.0.0.5", "[2001:db8::1]:443") == "2001:db8::1"
    assert _resolve("10.0.0.5", "203.0.113.7:51234") == "203.0.113.7"


def test_get_client_ip_reads_request_and_config() -> None:
    req = SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.5"),
        headers={"x-forwarded-for": "6.6.6.6, 203.0.113.7"},
    )
    cfg = SimpleNamespace(
        trust_proxy_headers=True, trusted_proxy_ips=GATEWAY, is_prod=True
    )
    assert get_client_ip(req, config=cfg) == "203.0.113.7"


def test_settings_reject_invalid_trusted_proxy_cidr() -> None:
    from pydantic import ValidationError

    from sandloom.core.config.settings import Settings

    with pytest.raises(ValidationError, match="TRUSTED_PROXY_IPS"):
        Settings(
            DATABASE_URL="postgresql+asyncpg://u:p@h:5432/d",
            CACHE_URL="redis://h:6379/0",
            BROKER_URL="redis://h:6379/1",
            JWT_SECRET="x" * 40,
            TRUSTED_PROXY_IPS=["not-a-cidr"],
        )


class _FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def get(self, key: str) -> str | None:
        v = self.store.get(key)
        return None if v is None else str(v)


def _req(ip: str = "203.0.113.7") -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host=ip),
        headers={},
        url=SimpleNamespace(path="/auth/login"),
    )


@pytest.mark.asyncio
async def test_sliding_window_blocks_boundary_burst() -> None:
    """A fixed window would allow 2x the limit straddling the boundary."""
    fake = _FakeCache()
    limiter = rate_limiter(10, 60)
    cfg = SimpleNamespace(
        is_prod=True, rate_limit_enabled=False, trust_proxy_headers=False
    )
    with (
        patch("sandloom.core.ingress.rate_limit.settings", cfg),
        patch("sandloom.core.ingress.rate_limit.cache", return_value=fake),
    ):
        # 10 requests at the very end of window 16 (t=1019.9 .. window 960-1019)
        with patch("sandloom.core.ingress.rate_limit.time.time", return_value=1019.9):
            for _ in range(10):
                await limiter(_req())
        # Right after the boundary the previous window still weighs ~100%.
        with patch("sandloom.core.ingress.rate_limit.time.time", return_value=1020.5):
            with pytest.raises(HTTPException) as exc:
                await limiter(_req())
            assert exc.value.status_code == 429
        # Once the previous window has fully slid out, traffic is allowed again.
        with patch("sandloom.core.ingress.rate_limit.time.time", return_value=1079.9):
            await limiter(_req())
