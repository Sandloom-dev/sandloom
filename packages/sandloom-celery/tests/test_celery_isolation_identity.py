"""``isolation_identity`` names the broker without its credentials, and a broker
url ``urlsplit`` cannot read never has its text (a password) in the error."""

from __future__ import annotations

import pytest
from celery import Celery
from sandloom_celery.taskqueue import CeleryTaskQueueProvider

_SECRET = "S3CRETpw"


def _provider(broker: str | None) -> CeleryTaskQueueProvider:
    app = Celery("identity", broker="memory://", set_as_current=False)
    options = {} if broker is None else {"broker": broker}
    return CeleryTaskQueueProvider(name="probe", driver="celery", options=options, app=app)


async def test_the_identity_is_the_broker_without_its_credentials() -> None:
    identity = await _provider("redis://user:pw@h:6379/2").isolation_identity()
    assert identity == "celery:redis://h:6379/2"


async def test_no_broker_option_has_no_identity() -> None:
    assert await _provider(None).isolation_identity() is None


@pytest.mark.parametrize("char", [chr(c) for c in (0xFF1A, 0xFF0F, 0xFF20, 0xFF03, 0xFF1F)])
async def test_an_unreadable_broker_url_never_echoes_its_password(char: str) -> None:
    # Built with a readable url: the options model's own check of an unreadable
    # one is the connection policy's (a separate change); this is the identity.
    provider = _provider("redis://h:6379/0")
    provider.options = {"broker": f"redis://user:{_SECRET}@host{char}x:6379/1"}
    with pytest.raises(ValueError, match="cannot be parsed") as caught:
        await provider.isolation_identity()
    assert _SECRET not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
