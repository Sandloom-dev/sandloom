"""A test run whose Redis does not answer says so (SANDLOOM-R014), naming the server.

``sandloom test`` on a checkout whose Redis is down used to end in an
INTERNALERROR traceback from the allocation of the workers' database indexes.
The family now maps a Redis that does not answer onto ``Unavailable`` (an
allow-list: a connection fault, a timeout, a socket error), and the session
turns it into the one R014 line. These tests need no Redis: they point the run
at a port nothing listens on, so they run in every lane that has the client.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import NoReturn

import pytest
import redis
from sandloom_redis.testing import isolation, resources
from sandloom_redis.testing.family import FAMILY
from sandloom_redis.testing.urls import instance_using, server_of

from sandloom.core.observability.unreachable import connection_failure
from sandloom.driverkit import Unavailable
from sandloom.driverkit.testing_family import INFRA_VAR

_SECRET = "S3CRETpw"


def _dead_port() -> int:
    """A loopback port nothing listens on (bound, then released)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _infra(port: int) -> str:
    url = f"redis://:{_SECRET}@127.0.0.1:{port}/0"
    return json.dumps({"cache": {"default": {"url": url}}})


def test_a_redis_that_does_not_answer_is_unavailable_and_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _dead_port()
    monkeypatch.setenv(isolation.REDIS_STRIDE_ENV, isolation.REDIS_STRIDE_AUTO)
    with pytest.raises(Unavailable) as caught:
        FAMILY.allocate_workers({INFRA_VAR: _infra(port)}, 2)
    error = caught.value
    assert (error.capability, error.instance) == ("cache", "default")
    assert f"redis://127.0.0.1:{port}" in str(error)
    assert _SECRET not in str(error)
    assert isinstance(error.__cause__, redis.exceptions.ConnectionError | OSError)
    line = connection_failure(error)
    assert line is not None
    assert f"cannot reach the cache backend at 127.0.0.1:{port} (connection" in line
    assert f"start the cache backend at 127.0.0.1:{port} yourself)" in line
    assert _SECRET not in line and line.endswith("[SANDLOOM-R014]")


@pytest.mark.parametrize(
    "answer",
    [
        redis.exceptions.ResponseError("ERR unknown command"),
        redis.exceptions.AuthenticationError("invalid username-password pair"),
        ValueError("a bug"),
    ],
    ids=["a server that answered", "a refused login", "a bug of the harness"],
)
def test_only_a_server_that_does_not_answer_is_an_outage(
    monkeypatch: pytest.MonkeyPatch, answer: Exception
) -> None:
    """Anything else keeps its type: a refusal of the request is no outage."""

    def allocate(*args: object, **kwargs: object) -> NoReturn:
        raise answer

    monkeypatch.setenv(isolation.REDIS_STRIDE_ENV, isolation.REDIS_STRIDE_AUTO)
    monkeypatch.setattr(resources, "allocate_worker_redis", allocate)
    with pytest.raises(type(answer)) as caught:
        FAMILY.allocate_workers({INFRA_VAR: _infra(_dead_port())}, 2)
    assert caught.value is answer


def test_unreachable_errors_are_the_connection_faults_not_the_replies() -> None:
    errors = resources.unreachable_errors()
    for fault in (
        redis.exceptions.ConnectionError,
        redis.exceptions.TimeoutError,
        ConnectionRefusedError,
    ):
        assert issubclass(fault, errors)
    for reply in (redis.exceptions.ResponseError, redis.exceptions.DataError):
        assert not issubclass(reply, errors)
    # A refused login is a ConnectionError of the client, so the tuple holds it
    # and the family re-raises it by this predicate.
    login = redis.exceptions.AuthenticationError("invalid username-password pair")
    assert isinstance(login, errors) and resources.refused_login(login)
    assert not resources.refused_login(redis.exceptions.ConnectionError("refused"))
    assert not resources.refused_login(ConnectionRefusedError())


def test_the_server_and_instance_of_a_url_never_carry_a_credential() -> None:
    raw = _infra(6379)
    url = f"redis://:{_SECRET}@127.0.0.1:6379/0"
    assert instance_using(raw, url) == ("cache", "default")
    assert instance_using(raw, "redis://127.0.0.1:1/0") is None
    assert instance_using(None, url) is None
    assert server_of(url) == "redis://127.0.0.1:6379"


_APP_ENV_TEST = textwrap.dedent(
    """\
    ENVIRONMENT=test
    INFRA='{infra}'
    JWT_SECRET=dx-fresh-app-secret-0123456789abcdef0123456789
    """
)


def test_a_session_on_a_dead_redis_exits_with_the_r014_line_not_a_traceback(
    tmp_path: Path,
) -> None:
    """End to end, the way ``sandloom test`` runs it: the plugin, xdist workers,
    allocated Redis indexes, and no Redis."""
    port = _dead_port()
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = ["-p", "sandloom.core.testing.plugin"]\n'
    )
    (tmp_path / ".env.test").write_text(_APP_ENV_TEST.format(infra=_infra(port)))
    (tmp_path / "test_never.py").write_text(
        "def test_never_runs():\n    raise AssertionError('ran with no Redis')\n"
    )
    env = {
        "SANDLOOM_HOME": str(tmp_path / "home"),
        "SANDLOOM_TEST_OWNER": "dx-fresh-app-tests-probe",
        isolation.REDIS_STRIDE_ENV: isolation.REDIS_STRIDE_AUTO,
        "PATH": os.environ.get("PATH", ""),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-n", "2", "--no-cov"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == pytest.ExitCode.USAGE_ERROR, out[-3000:]
    assert "INTERNALERROR" not in out and "Traceback" not in out, out[-3000:]
    assert f"cannot reach the cache backend at 127.0.0.1:{port}" in out, out[-3000:]
    assert "[SANDLOOM-R014]" in out
    assert "ran with no Redis" not in out
    assert _SECRET not in out
