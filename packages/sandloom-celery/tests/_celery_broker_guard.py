"""The real-broker guard of the sandloom-celery package tests.

The tests that need a broker (the kit's host groups, the probe of a reachable
broker) call :func:`require_broker`. Where nothing answers, the run fails when
``SANDLOOM_CELERY_BROKER_REQUIRED=1`` (the CI lane sets it: a broker that did
not start must not turn into green tests that ran nothing) and skips with the
reason otherwise (a laptop without Redis).
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from celery import Celery
from kombu.exceptions import OperationalError  # type: ignore[import-untyped]

BROKER_REQUIRED_ENV = "SANDLOOM_CELERY_BROKER_REQUIRED"

#: Seconds the one connection attempt may take.
_CONNECT_SECONDS = 3.0


def broker_required(environ: Mapping[str, str]) -> bool:
    """Whether ``environ`` makes an unreachable broker an error: only ``1``."""
    return environ.get(BROKER_REQUIRED_ENV) == "1"


def require_broker(url: str, *, required: bool) -> None:
    """Return when the broker at ``url`` accepts a connection; otherwise fail
    (``required``) or skip. The message names no URL: it can carry a password."""
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
        reason = f"the test broker does not answer ({type(exc).__name__})"
        if required:
            pytest.fail(f"{reason}; {BROKER_REQUIRED_ENV}=1 makes this an error")
        pytest.skip(f"{reason}; {BROKER_REQUIRED_ENV}=1 would make this an error")
    finally:
        connection.release()
