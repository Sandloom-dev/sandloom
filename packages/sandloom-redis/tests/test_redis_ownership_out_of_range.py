"""A claim of a Redis index past the server's ``databases`` says what to start.

A stock ``redis:7`` has 16 indexes; the default xdist layout puts worker N at
``index + 16 * (N + 1)``, so a run on it aborted with redis-py's bare
``DB index is out of range``. ``claim_redis`` turns that reply (and only that
one) into a refusal naming the ``--databases`` count.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from _redis_required import cache_url_or_unavailable, require_redis
from redis.exceptions import ResponseError
from sandloom_redis.testing.ownership import claim_redis

from sandloom.core.testing import ownership


async def test_an_index_past_the_servers_databases_names_the_databases_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = cache_url_or_unavailable()
    await require_redis(url)
    monkeypatch.delenv(ownership.TAKEOVER_ENV, raising=False)
    parts = urlsplit(url)
    # No Redis has this many indexes (the server's ceiling is ``databases``).
    far = urlunsplit(parts._replace(path="/100000", query=""))
    with pytest.raises(ownership.TestResourceOwnershipError) as caught:
        claim_redis(far, owner="out-of-range-test")
    text = str(caught.value)
    assert "--databases 256" in text
    assert "fewer database indexes" in text
    assert "/100000" in text


class _FailingClient:
    def __init__(self, reply: str) -> None:
        self._reply = reply

    def get(self, key: str) -> None:
        raise ResponseError(self._reply)

    def close(self) -> None:
        return None


@pytest.mark.parametrize(
    "reply", ["ERR invalid password", "NOAUTH Authentication required."]
)
def test_any_other_server_error_still_raises_as_it_is(
    monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    import redis

    def from_url(*args: Any, **kwargs: Any) -> _FailingClient:
        return _FailingClient(reply)

    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    with pytest.raises(ResponseError, match=reply.split()[0]):
        claim_redis("redis://127.0.0.1:1/0", owner="other-error-test")


def test_the_refusal_never_echoes_the_url_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import redis

    def from_url(*args: Any, **kwargs: Any) -> _FailingClient:
        return _FailingClient("ERR DB index is out of range")

    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    with pytest.raises(ownership.TestResourceOwnershipError) as caught:
        claim_redis("redis://user:s3cret-pw@127.0.0.1:1/99", owner="secret-test")
    assert "s3cret-pw" not in str(caught.value)
    assert "/99" in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "redis://127.0.0.1:1/99?password=q-secret",
        "redis://127.0.0.1:1/99?db=99&username=u&password=q-secret",
        "redis://127.0.0.1:1/99#q-secret",
    ],
)
def test_the_refusal_never_echoes_the_query_or_fragment(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    import redis

    def from_url(*args: Any, **kwargs: Any) -> _FailingClient:
        return _FailingClient("ERR DB index is out of range")

    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    with pytest.raises(ownership.TestResourceOwnershipError) as caught:
        claim_redis(url, owner="query-secret-test")
    assert "q-secret" not in str(caught.value)
    assert "127.0.0.1:1/99" in str(caught.value)
