"""A url a person configured never reaches an error with its password.

``urllib.parse.urlsplit`` raises a ``ValueError`` that carries the whole netloc
(``user:password``) when the netloc holds a character that NFKC-normalises to
``:`` ``/`` ``?`` ``#`` or ``@`` (U+FF1A, say). Each public entry point below parses
such a url and must fail with the fixed message of
:func:`sandloom.core.url_split.split_url` (or its own fixed words), never with the
password, and with no chained error that a traceback would print.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest

from sandloom.core import app_factory
from sandloom.core.cli.dev.docker import DockerUnavailable, docker_host
from sandloom.core.config.settings import settings
from sandloom.core.contract_testing import split_env
from sandloom.core.url_split import redact, split_url

_SECRET = "pw-secret"
#: ``U+FF1A`` (fullwidth colon) NFKC-normalises to ``:``, so ``urlsplit`` refuses
#: the netloc and prints it.
_BAD_NETLOC = f"u:{_SECRET}{chr(0xFF1A)}x@127.0.0.1:8000"


def _split_env(url: str) -> object:
    return split_env({"main": ["users"]}, {"main": url}, issuer="main")


def _docker_host(url: str) -> object:
    return docker_host({"DOCKER_HOST": url})


@pytest.mark.parametrize(
    ("call", "scheme"),
    [
        pytest.param(split_url, "http", id="split_url"),
        pytest.param(_split_env, "http", id="contract_testing.split_env"),
        pytest.param(_docker_host, "ssh", id="cli.dev.docker.docker_host"),
    ],
)
def test_an_unparsable_url_fails_without_its_password(
    call: Callable[[str], object], scheme: str
) -> None:
    with pytest.raises((ValueError, DockerUnavailable)) as raised:
        call(f"{scheme}://{_BAD_NETLOC}")
    error = raised.value
    assert _SECRET not in str(error)
    assert _SECRET not in repr(error)
    assert error.__cause__ is None
    assert error.__suppress_context__


def test_redact_names_no_part_of_an_unparsable_url() -> None:
    assert redact(f"http://{_BAD_NETLOC}") == "<unparseable url>"


def test_a_parsable_process_url_without_a_scheme_is_refused_without_its_password() -> (
    None
):
    """The refusal of ``split_env`` for a well-formed but non-http url used to echo
    the url with its password (``{url!r}``)."""
    with pytest.raises(ValueError, match="invalid process HTTP URL") as raised:
        _split_env(f"ftp://u:{_SECRET}@127.0.0.1:8000")
    assert _SECRET not in str(raised.value)


def test_a_process_url_with_no_scheme_at_all_is_refused_without_its_password() -> None:
    """``user:pw@host:port`` has no ``://``: ``redact`` must not echo it."""
    with pytest.raises(ValueError, match="invalid process HTTP URL") as raised:
        _split_env(f"test_user:{_SECRET}@127.0.0.1:8000")
    assert _SECRET not in str(raised.value)


@pytest.mark.asyncio
async def test_the_unreachable_database_warning_names_no_part_of_an_unparsable_url(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Down:
        def capability(self, name: str, instance: str | None = None) -> object:
            raise RuntimeError("down")

    monkeypatch.setattr(settings, "database_url", f"postgresql://{_BAD_NETLOC}/app")
    with caplog.at_level(logging.WARNING):
        await app_factory.report_unreachable_database(_Down())  # type: ignore[arg-type]
    (record,) = caplog.records
    assert _SECRET not in record.getMessage()
    assert record.exc_info is None
