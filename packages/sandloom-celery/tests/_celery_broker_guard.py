"""The real-broker guard of the sandloom-celery package tests.

The tests that need a broker (the kit's host groups, the probe of a reachable
broker) call :func:`require_broker`. Where nothing answers, the run fails when
``SANDLOOM_CELERY_REQUIRED=1`` (the ``celery-drivers`` lane sets it: a broker
that did not start must not turn into green tests that ran nothing) and skips
with the reason otherwise (a laptop without Redis). The lane also needs the
broker to be a real one: where the variable is set, a broker URL whose scheme is
not a network transport (``memory://``, ``filesystem://``) is an error too, since
a broker that cannot be down proves nothing about the driver. The name follows
``SANDLOOM_<PACKAGE>_REQUIRED`` (``SANDLOOM_REDIS_REQUIRED`` of sandloom-redis).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from celery import Celery
from kombu.exceptions import OperationalError  # type: ignore[import-untyped]

REQUIRED_ENV = "SANDLOOM_CELERY_REQUIRED"

#: The broker schemes of a transport that talks to a server. Anything else
#: (``memory``, ``filesystem``, ``sqla``...) runs inside the test process.
NETWORK_SCHEMES = frozenset({"redis", "rediss", "amqp", "amqps", "sentinel"})

#: Seconds the one connection attempt may take.
_CONNECT_SECONDS = 3.0


def broker_required(environ: Mapping[str, str]) -> bool:
    """Whether ``environ`` makes an unreachable broker an error: only ``1``."""
    return environ.get(REQUIRED_ENV) == "1"


def source_tree_gap(module_file: str, *, required: bool) -> str | None:
    """The reason the lane may not run, or ``None``: where ``required`` the
    package under test must be the installed wheel (a ``site-packages`` path),
    not the source tree an editable install points at. The lane exists to test
    the wheel's entry point and contents; a source-tree run would be green for a
    wheel that is broken."""
    if not required or "site-packages" in Path(module_file).resolve().parts:
        return None
    return (
        f"sandloom_celery is imported from the source tree, not the built wheel "
        f"({REQUIRED_ENV}=1 tests the wheel: install it with "
        f"`uv pip install --reinstall --no-deps <wheel>`)"
    )


def require_broker(url: str, *, required: bool, what: str = "broker") -> None:
    """Return when the server at ``url`` accepts a connection; otherwise fail
    (``required``) or skip. Where ``required``, a URL that is not a network
    transport fails too. ``what`` names the server in the messages (``broker``,
    ``cache``), which carry no URL: it can carry a password."""
    scheme = urlsplit(url).scheme.lower()
    if required and scheme not in NETWORK_SCHEMES:
        pytest.fail(
            f"the test {what} scheme {scheme!r} is not a network transport "
            f"({sorted(NETWORK_SCHEMES)}); {REQUIRED_ENV}=1 needs a real broker",
            pytrace=False,
        )
    connection = Celery("sandloom-celery-tests", broker=url).connection_for_write(
        connect_timeout=_CONNECT_SECONDS
    )
    try:
        connection.ensure_connection(
            max_retries=1,
            interval_start=0,
            interval_step=0,
            interval_max=0,
            timeout=_CONNECT_SECONDS,
        )
    except (OperationalError, OSError) as exc:
        reason = f"the test {what} does not answer ({type(exc).__name__})"
        if required:
            pytest.fail(f"{reason}; {REQUIRED_ENV}=1 makes this an error")
        pytest.skip(f"{reason}; {REQUIRED_ENV}=1 would make this an error")
    finally:
        connection.release()
