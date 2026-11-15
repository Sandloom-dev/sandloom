"""Fixtures of the sandloom-celery package tests (the root ``conftest.py`` seeds
``settings`` from ``.env.test`` / ``SANDLOOM_TEST_*`` as for every suite).

The host-group tests (``test_taskqueue_kit_host.py``) also need ``sandloom-redis``
(the lease store of the two schedulers: the ``dev`` group installs it, the wheel
does not depend on it) and the test cache at ``settings.cache_url``: the
``real_cache`` fixture probes it under the same rules as ``real_broker`` (an error
under ``SANDLOOM_CELERY_REQUIRED=1``, a skip elsewhere).
"""

from __future__ import annotations

import os

import pytest
import sandloom_celery
from _celery_broker_guard import broker_required, require_broker, source_tree_gap

from sandloom.core.config.settings import settings


@pytest.fixture(scope="session", autouse=True)
def _tests_run_against_the_built_wheel() -> None:
    """Where the lane is required (``SANDLOOM_CELERY_REQUIRED=1``) the package
    must be the installed wheel, not the source tree (see ``source_tree_gap``)."""
    gap = source_tree_gap(
        sandloom_celery.__file__, required=broker_required(os.environ)
    )
    if gap:
        pytest.fail(gap, pytrace=False)


@pytest.fixture(scope="session")
def real_broker() -> str:
    """The URL of the real test broker; fails or skips where it is down (see
    ``_celery_broker_guard``). Asked once per process."""
    require_broker(settings.broker_url, required=broker_required(os.environ))
    return settings.broker_url


@pytest.fixture(scope="session")
def real_cache() -> str:
    """The URL of the real test cache (the lease store of the host groups); fails
    or skips where it is down, by the rules of ``real_broker``."""
    require_broker(
        settings.cache_url, required=broker_required(os.environ), what="cache"
    )
    return settings.cache_url
