"""A broker url a person configured never reaches an error with its password.

``urllib.parse.urlsplit`` raises a ``ValueError`` that carries the whole netloc
(``user:password``) when the netloc holds a character that NFKC-normalises to
``:`` ``/`` ``?`` ``#`` or ``@`` (U+FF1A, say). Every place of the package that
parses a broker url reads it through ``sandloom.driverkit.url_split.split_url``:
the error is a fixed message and no chained error carries the netloc.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from celery import Celery
from sandloom_celery.broker_probe import BrokerUnreachableError, probe_broker
from sandloom_celery.connection_policy import (
    ConnectionOptions,
    scheme_of,
    validate_connection,
)

from sandloom.driverkit.url_split import split_url

_SECRET = "pw-secret"
#: ``U+FF1A`` (fullwidth colon) NFKC-normalises to ``:``, so ``urlsplit`` refuses
#: the netloc and prints it.
_UNPARSABLE = f"redis://u:{_SECRET}{chr(0xFF1A)}x@127.0.0.1:6379/0"


def _chain(error: BaseException) -> list[BaseException]:
    """The errors a traceback of ``error`` prints: its cause, else its context
    unless ``from None`` suppressed it."""
    seen: list[BaseException] = []
    link: BaseException | None = error
    while link is not None and link not in seen:
        seen.append(link)
        link = link.__cause__ or (
            None if link.__suppress_context__ else link.__context__
        )
    return seen


def _assert_no_secret_anywhere(error: BaseException) -> None:
    for link in _chain(error):
        assert _SECRET not in str(link)
        assert _SECRET not in repr(link)


async def _probe(url: str) -> object:
    return await probe_broker(Celery("url-split-sites", broker=url), limit_seconds=1.0)


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(split_url, id="driverkit.split_url"),
        pytest.param(scheme_of, id="connection_policy.scheme_of"),
        pytest.param(
            lambda url: validate_connection(ConnectionOptions(url)),
            id="connection_policy.validate_connection",
        ),
    ],
)
def test_an_unparsable_broker_url_fails_with_a_fixed_message(
    call: Callable[[str], object],
) -> None:
    with pytest.raises(ValueError, match="cannot be parsed") as raised:
        call(_UNPARSABLE)
    _assert_no_secret_anywhere(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


async def test_the_broker_probe_chains_no_error_that_carries_the_password() -> None:
    """``probe_broker`` wraps any failure into ``BrokerUnreachableError`` with
    ``from exc``: a traceback prints that cause, so the cause must be the fixed
    message, not urllib's, which holds the netloc."""
    with pytest.raises(BrokerUnreachableError) as raised:
        await _probe(_UNPARSABLE)
    _assert_no_secret_anywhere(raised.value)
