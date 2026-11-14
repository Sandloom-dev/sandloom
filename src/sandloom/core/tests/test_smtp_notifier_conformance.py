"""The ``smtp`` notifier against a real SMTP server (spec 10).

``smtp`` is ``beta`` only when ``NotifierConformance`` passes in CI against a
real SMTP server container: ``.github/workflows/notification-smtp.yml`` starts
mailpit and sets ``SANDLOOM_SMTP_TEST_HOST``, ``SANDLOOM_SMTP_TEST_PORT`` and
``SANDLOOM_SMTP_TEST_API`` (mailpit's HTTP API, which the read-back test uses).
An in-process SMTP server does not count, so without them the kit tests skip;
the identity and error-mapping tests that need no server always run.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
import urllib.parse
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from sandloom.core.drivers.smtp_notifier import SmtpNotifierProvider
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import Notification
from sandloom.core.infra.errors import Unavailable
from sandloom.driverkit.testing import NotifierConformance

_HOST = os.environ.get("SANDLOOM_SMTP_TEST_HOST", "")
_PORT = int(os.environ.get("SANDLOOM_SMTP_TEST_PORT", "1025"))
_API = os.environ.get("SANDLOOM_SMTP_TEST_API", "").rstrip("/")

needs_server = pytest.mark.skipif(
    not (_HOST and _API),
    reason="set SANDLOOM_SMTP_TEST_HOST and SANDLOOM_SMTP_TEST_API to a mailpit "
    "container (.github/workflows/notification-smtp.yml)",
)


def _smtp(host: str, port: int, *, name: str = "default", **options: Any) -> Any:
    return SmtpNotifierProvider.from_config(
        InstanceConfig(
            "notification",
            name,
            "smtp",
            {
                "host": host,
                "port": port,
                "from_addr": "sandloom@example.com",
                **options,
            },
        )
    )


def _closed_port() -> int:
    """A local port nothing listens on: the connection is refused."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@needs_server
class TestSmtpNotifier(NotifierConformance):
    recipients = {**NotifierConformance.recipients, "email": "kit@example.com"}

    @pytest.fixture
    def make_provider(self) -> Callable[[], SmtpNotifierProvider]:
        return lambda: _smtp(_HOST, _PORT)

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], SmtpNotifierProvider]:
        # Nothing listens there, and the credentials carry the kit's secret: an
        # error or a health detail that shows it fails the kit.
        port = _closed_port()
        return lambda: _smtp(
            "127.0.0.1",
            port,
            username=self.leak_marker,
            password=self.leak_marker,
        )


def _api_get(path: str) -> dict[str, Any]:
    target = urllib.parse.urlsplit(_API)
    connection = http.client.HTTPConnection(
        target.hostname or "127.0.0.1", target.port or 80, timeout=5
    )
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        assert response.status == 200, f"mailpit answered {response.status}"
        return json.loads(response.read())  # type: ignore[no-any-return]
    finally:
        connection.close()


@needs_server
async def test_the_accepted_message_is_read_back_through_mailpit() -> None:
    """``send`` returned, so the server accepted the message: mailpit holds it,
    with the recipient, the subject and the body the notification carried."""
    subject = f"sandloom-smtp-{uuid.uuid4().hex}"
    provider = _smtp(_HOST, _PORT)
    await provider.aopen()
    try:
        await provider.send(
            Notification(
                channel="email",
                recipient="readback@example.com",
                subject=subject,
                body="read back through the HTTP API",
            )
        )
    finally:
        await provider.aclose()
    query = urllib.parse.urlencode({"query": f'subject:"{subject}"'})
    found: list[dict[str, Any]] = []
    for _ in range(20):
        found = (await asyncio.to_thread(_api_get, f"/api/v1/search?{query}"))[
            "messages"
        ]
        if found:
            break
        await asyncio.sleep(0.25)
    assert len(found) == 1
    summary = found[0]
    assert [to["Address"] for to in summary["To"]] == ["readback@example.com"]
    assert summary["From"]["Address"] == "sandloom@example.com"
    message = await asyncio.to_thread(_api_get, f"/api/v1/message/{summary['ID']}")
    assert "read back through the HTTP API" in message["Text"]


async def test_the_identity_names_the_server_and_no_credential() -> None:
    first = _smtp("Mail.Example.COM", 2525, password="s3cret", username="u")
    renamed = _smtp("mail.example.com", 2525, name="other")
    assert await first.isolation_identity() == "smtp:mail.example.com:2525"
    assert await renamed.isolation_identity() == await first.isolation_identity()
    assert "s3cret" not in str(await first.isolation_identity())
    assert await _smtp("mail.example.com", 25).isolation_identity() != (
        await first.isolation_identity()
    )


async def test_a_refused_connection_is_unavailable_and_keeps_the_secret_out() -> None:
    provider = _smtp(
        "127.0.0.1", _closed_port(), username="user", password="sandloom-secret-pw"
    )
    with pytest.raises(Unavailable) as raised:
        await provider.send(
            Notification(
                channel="email", recipient="a@example.com", subject="s", body="b"
            )
        )
    assert "sandloom-secret-pw" not in str(raised.value)
