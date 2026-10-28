"""Tests for core rate_limit with a fake cache backend (atomic INCR API).

The fake is installed where the limiter resolves its cache (the module's
``cache`` accessor), so no driver internals are involved.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import HTTPException, status

from sandloom.core.descriptors import HealthResult
from sandloom.core.infra.contracts import CacheProvider
from sandloom.core.ingress.rate_limit import rate_limiter as rate_limiter_factory
from sandloom.core.testing import InMemoryCache

# Local defaults matching historical values
RATE_LIMIT = 5
RATE_PERIOD = 60
PATH = "/auth/login"


class FakeRedis(CacheProvider):
    """Records what the limiter does: INCR counters and the TTL set on the
    first hit of each counter (as the atomic INCR+EXPIRE does)."""

    def __init__(self) -> None:
        super().__init__(name="default", driver="fake", options={})
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        value = self.store.get(key)
        return None if value is None else str(value)

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:  # pragma: no cover - unused by the limiter
        self.store[key] = int(value)

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:  # pragma: no cover - unused by the limiter
        return self.store.setdefault(key, int(value)) == int(value)

    async def delete(self, key: str) -> None:  # pragma: no cover - unused
        self.store.pop(key, None)

    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        if self.store[key] == 1 and ttl_seconds:
            self.ttls[key] = ttl_seconds
        return self.store[key]

    async def health(self) -> HealthResult:  # pragma: no cover - unused
        return HealthResult(status="ok")


def _use_cache(provider: CacheProvider) -> Any:
    """Serve ``provider`` wherever the limiter resolves its cache."""
    return patch("sandloom.core.ingress.rate_limit.cache", return_value=provider)


def make_request(
    ip: str | None,
    path: str = PATH,
    headers: dict[str, str] | None = None,
):
    client = None if ip is None else SimpleNamespace(host=ip)
    return SimpleNamespace(
        client=client,
        url=SimpleNamespace(path=path),
        headers=headers or {},
    )


def _prod_settings(**overrides):
    base = {
        "is_prod": True,
        "rate_limit_enabled": False,
        "trust_proxy_headers": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _key(ip: str, window: int, path: str = PATH) -> str:
    return f"rate_limit:{ip}:{path}:{window}"


@pytest.mark.asyncio
async def test_rate_limiter_allows_first_request():
    fake = FakeRedis()
    limiter = rate_limiter_factory(RATE_LIMIT, RATE_PERIOD)

    with (
        patch("sandloom.core.ingress.rate_limit.settings", _prod_settings()),
        _use_cache(fake),
        patch("sandloom.core.ingress.rate_limit.time.time", return_value=1000),
    ):
        await limiter(make_request("192.168.1.1"))
        window = 1000 // RATE_PERIOD
        assert fake.store[_key("192.168.1.1", window)] == 1
        # TTL is set on first hit; counters live two periods so the previous
        # window stays readable for the sliding-window estimate.
        assert fake.ttls[_key("192.168.1.1", window)] == 2 * RATE_PERIOD


@pytest.mark.asyncio
async def test_rate_limiter_multiple_requests_within_limit():
    fake = FakeRedis()
    limiter = rate_limiter_factory(RATE_LIMIT, RATE_PERIOD)

    with (
        patch("sandloom.core.ingress.rate_limit.settings", _prod_settings()),
        _use_cache(fake),
        patch("sandloom.core.ingress.rate_limit.time.time", return_value=1000),
    ):
        for i in range(RATE_LIMIT):
            await limiter(make_request("192.168.1.1"))
            window = 1000 // RATE_PERIOD
            assert fake.store[_key("192.168.1.1", window)] == i + 1


@pytest.mark.asyncio
async def test_rate_limiter_exceeds_limit():
    fake = FakeRedis()
    limiter = rate_limiter_factory(RATE_LIMIT, RATE_PERIOD)

    with (
        patch("sandloom.core.ingress.rate_limit.settings", _prod_settings()),
        _use_cache(fake),
        patch("sandloom.core.ingress.rate_limit.time.time", return_value=1000),
    ):
        for _ in range(RATE_LIMIT):
            await limiter(make_request("192.168.1.1"))

        with pytest.raises(HTTPException) as exc_info:
            await limiter(make_request("192.168.1.1"))

        assert exc_info.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert "Rate limit exceeded" in exc_info.value.detail
        assert exc_info.value.headers["Retry-After"] == str(RATE_PERIOD)


@pytest.mark.asyncio
async def test_rate_limiter_different_ips_separate_limits():
    fake = FakeRedis()
    limiter = rate_limiter_factory(RATE_LIMIT, RATE_PERIOD)

    with (
        patch("sandloom.core.ingress.rate_limit.settings", _prod_settings()),
        _use_cache(fake),
        patch("sandloom.core.ingress.rate_limit.time.time", return_value=1000),
    ):
        await limiter(make_request("192.168.1.1"))
        await limiter(make_request("192.168.1.2"))

        window = 1000 // RATE_PERIOD
        assert fake.store[_key("192.168.1.1", window)] == 1
        assert fake.store[_key("192.168.1.2", window)] == 1


@pytest.mark.asyncio
async def test_rate_limiter_separate_routes_separate_limits():
    fake = FakeRedis()
    limiter = rate_limiter_factory(RATE_LIMIT, RATE_PERIOD)

    with (
        patch("sandloom.core.ingress.rate_limit.settings", _prod_settings()),
        _use_cache(fake),
        patch("sandloom.core.ingress.rate_limit.time.time", return_value=1000),
    ):
        await limiter(make_request("192.168.1.1", path="/auth/login"))
        await limiter(make_request("192.168.1.1", path="/auth/register"))

        window = 1000 // RATE_PERIOD
        assert fake.store[_key("192.168.1.1", window, "/auth/login")] == 1
        assert fake.store[_key("192.168.1.1", window, "/auth/register")] == 1


@pytest.mark.asyncio
async def test_rate_limiter_different_time_windows():
    fake = FakeRedis()
    limiter = rate_limiter_factory(RATE_LIMIT, RATE_PERIOD)

    with (
        patch("sandloom.core.ingress.rate_limit.settings", _prod_settings()),
        _use_cache(fake),
    ):
        with patch("sandloom.core.ingress.rate_limit.time.time", return_value=1000):
            await limiter(make_request("192.168.1.1"))
        with patch(
            "sandloom.core.ingress.rate_limit.time.time",
            return_value=1000 + RATE_PERIOD,
        ):
            await limiter(make_request("192.168.1.1"))

    w1 = 1000 // RATE_PERIOD
    w2 = (1000 + RATE_PERIOD) // RATE_PERIOD
    assert fake.store[_key("192.168.1.1", w1)] == 1
    assert fake.store[_key("192.168.1.1", w2)] == 1


@pytest.mark.asyncio
async def test_rate_limiter_edge_case_exactly_at_limit():
    fake = FakeRedis()
    limiter = rate_limiter_factory(RATE_LIMIT, RATE_PERIOD)

    with (
        patch("sandloom.core.ingress.rate_limit.settings", _prod_settings()),
        _use_cache(fake),
        patch("sandloom.core.ingress.rate_limit.time.time", return_value=1000),
    ):
        # Exactly RATE_LIMIT requests pass; the next one is rejected
        for _ in range(RATE_LIMIT):
            await limiter(make_request("192.168.1.1"))
        with pytest.raises(HTTPException):
            await limiter(make_request("192.168.1.1"))


@pytest.mark.asyncio
async def test_rate_limiter_handles_unknown_client():
    fake = FakeRedis()
    limiter = rate_limiter_factory(RATE_LIMIT, RATE_PERIOD)

    with (
        patch("sandloom.core.ingress.rate_limit.settings", _prod_settings()),
        _use_cache(fake),
        patch("sandloom.core.ingress.rate_limit.time.time", return_value=1000),
    ):
        await limiter(make_request(None))
        window = 1000 // RATE_PERIOD
        assert fake.store[_key("unknown", window)] == 1


@pytest.mark.asyncio
async def test_rate_limiter_uses_forwarded_for_when_trusted():
    fake = FakeRedis()
    limiter = rate_limiter_factory(RATE_LIMIT, RATE_PERIOD)

    with (
        patch(
            "sandloom.core.ingress.rate_limit.settings",
            _prod_settings(
                trust_proxy_headers=True,
                trusted_proxy_ips=["10.0.0.1"],  # the peer is an allowlisted proxy
            ),
        ),
        _use_cache(fake),
        patch("sandloom.core.ingress.rate_limit.time.time", return_value=1000),
    ):
        request = make_request(
            "10.0.0.1",  # proxy address
            headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"},
        )
        await limiter(request)
        window = 1000 // RATE_PERIOD
        # Keyed on the original client, not the proxy
        assert fake.store[_key("203.0.113.7", window)] == 1


@pytest.mark.asyncio
async def test_rate_limiter_fail_closed_when_no_trusted_proxies_in_prod():
    # TRUST_PROXY_HEADERS on but no TRUSTED_PROXY_IPS allowlist: in prod we must
    # NOT trust a spoofable XFF (any peer could rotate it to mint buckets) and
    # fall back to the immediate peer instead.
    fake = FakeRedis()
    limiter = rate_limiter_factory(RATE_LIMIT, RATE_PERIOD)

    with (
        patch(
            "sandloom.core.ingress.rate_limit.settings",
            _prod_settings(trust_proxy_headers=True),  # no trusted_proxy_ips
        ),
        _use_cache(fake),
        patch("sandloom.core.ingress.rate_limit.time.time", return_value=1000),
    ):
        request = make_request("10.0.0.1", headers={"x-forwarded-for": "6.6.6.6"})
        await limiter(request)
        window = 1000 // RATE_PERIOD
        assert fake.store[_key("10.0.0.1", window)] == 1  # peer, not spoofed XFF


@pytest.mark.asyncio
async def test_rate_limiter_ignores_forwarded_for_when_untrusted():
    fake = FakeRedis()
    limiter = rate_limiter_factory(RATE_LIMIT, RATE_PERIOD)

    with (
        patch("sandloom.core.ingress.rate_limit.settings", _prod_settings()),
        _use_cache(fake),
        patch("sandloom.core.ingress.rate_limit.time.time", return_value=1000),
    ):
        request = make_request("10.0.0.1", headers={"x-forwarded-for": "6.6.6.6"})
        await limiter(request)
        window = 1000 // RATE_PERIOD
        # Spoofed header ignored without TRUST_PROXY_HEADERS
        assert fake.store[_key("10.0.0.1", window)] == 1


@pytest.mark.asyncio
async def test_rate_limiter_bypassed_outside_prod():
    fake = FakeRedis()
    limiter = rate_limiter_factory(RATE_LIMIT, RATE_PERIOD)

    with (
        patch(
            "sandloom.core.ingress.rate_limit.settings",
            _prod_settings(is_prod=False, rate_limit_enabled=False),
        ),
        _use_cache(fake),
    ):
        for _ in range(RATE_LIMIT * 3):
            await limiter(make_request("192.168.1.1"))
        assert fake.store == {}  # Redis never touched


@pytest.mark.asyncio
async def test_rate_limiter_fails_open_on_cache_outage_by_default():
    limiter = rate_limiter_factory(RATE_LIMIT, RATE_PERIOD)
    with (
        patch("sandloom.core.ingress.rate_limit.settings", _prod_settings()),
        _use_cache(InMemoryCache(broken=True)),
        patch("sandloom.core.ingress.rate_limit.time.time", return_value=1000),
    ):
        # Default: a cache blip must not 500 every request -> allow.
        assert await limiter(make_request("192.168.1.1")) is None


@pytest.mark.asyncio
async def test_rate_limiter_fail_closed_returns_503_on_cache_outage():
    limiter = rate_limiter_factory(RATE_LIMIT, RATE_PERIOD, fail_closed=True)
    with (
        patch("sandloom.core.ingress.rate_limit.settings", _prod_settings()),
        _use_cache(InMemoryCache(broken=True)),
        patch("sandloom.core.ingress.rate_limit.time.time", return_value=1000),
    ):
        # Admission-control endpoints must not silently lift the limit.
        with pytest.raises(HTTPException) as exc_info:
            await limiter(make_request("192.168.1.1"))
        assert exc_info.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


@pytest.mark.asyncio
async def test_rate_limiter_enforced_when_explicitly_enabled():
    fake = FakeRedis()
    limiter = rate_limiter_factory(1, RATE_PERIOD)

    with (
        patch(
            "sandloom.core.ingress.rate_limit.settings",
            _prod_settings(is_prod=False, rate_limit_enabled=True),
        ),
        _use_cache(fake),
        patch("sandloom.core.ingress.rate_limit.time.time", return_value=1000),
    ):
        await limiter(make_request("192.168.1.1"))
        with pytest.raises(HTTPException):
            await limiter(make_request("192.168.1.1"))


# --------------------------------------------------------------------------- #
# GLM review #10: an opt-in global per-IP floor (unknown routes / 404 scans
# were unlimited: only routes with a rate_limiter dependency are limited).
# --------------------------------------------------------------------------- #


def _floor_app(monkeypatch: pytest.MonkeyPatch, requests: int, cache_obj: Any) -> Any:
    from fastapi import FastAPI

    from sandloom.core.config.settings import settings
    from sandloom.core.http.error_handlers import register_error_handlers
    from sandloom.core.http.middlewares import register_middlewares
    from sandloom.core.ingress import rate_limit

    monkeypatch.setattr(settings, "rate_limit_global_requests", requests)
    monkeypatch.setattr(settings, "rate_limit_global_period_seconds", 60)
    monkeypatch.setattr(rate_limit, "cache", lambda: cache_obj)
    app = FastAPI()
    register_error_handlers(app)
    register_middlewares(app)

    @app.get("/known")
    async def known() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_global_floor_is_off_by_default() -> None:
    from sandloom.core.config.settings import Settings
    from sandloom.core.http.middlewares import GlobalRateLimitMiddleware

    assert Settings.model_fields["rate_limit_global_requests"].default == 0
    from fastapi import FastAPI

    from sandloom.core.http.middlewares import register_middlewares

    app = FastAPI()
    register_middlewares(app)
    assert all(m.cls is not GlobalRateLimitMiddleware for m in app.user_middleware)


def test_global_floor_limits_unknown_routes_per_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.testclient import TestClient

    client = TestClient(_floor_app(monkeypatch, 3, InMemoryCache()))
    statuses = [client.get(f"/scan/{i}").status_code for i in range(5)]
    assert statuses == [404, 404, 404, 429, 429]
    blocked = client.get("/known")  # same IP: the floor spans every route
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Rate limit exceeded.", "code": "rate_limited"}
    assert blocked.headers["Retry-After"] == "60"
    # Probes / ops endpoints are exempt (a kubelet must never be throttled).
    assert client.get("/health").status_code == 200


def test_global_floor_fails_open_on_cache_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.testclient import TestClient

    class _Down(InMemoryCache):
        async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
            raise ConnectionError("cache down")

        async def incr_window(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("cache down")

    client = TestClient(_floor_app(monkeypatch, 1, _Down()))
    assert [client.get("/known").status_code for _ in range(3)] == [200] * 3
