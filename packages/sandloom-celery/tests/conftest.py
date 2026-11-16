"""Fixtures of the sandloom-celery package tests (the root ``conftest.py`` seeds
``settings`` from ``.env.test`` / ``SANDLOOM_TEST_*`` as for every suite).

The host-group tests (``test_taskqueue_kit_host.py``) also need ``sandloom-redis``
(the lease store of the two schedulers: the ``dev`` group installs it, the wheel
does not depend on it) and the test cache (``cache.default``'s url): the
``real_cache`` fixture probes it under the same rules as ``real_broker`` (an error
under ``SANDLOOM_CELERY_REQUIRED=1``, a skip elsewhere).
"""

from __future__ import annotations

import pytest
import sandloom_celery
from _celery_broker_guard import require_broker, source_tree_gap

from sandloom.driverkit.process_config import process_instance
from sandloom.sdk.testing import resolved_cache_url


def _broker_url() -> str:
    """The broker the test configuration gives ``taskqueue.default`` (the harness
    resource, ``SANDLOOM_TEST_BROKER_URL``). A configuration with none fails: a
    test that needs the server must not skip for want of a url."""
    instance = process_instance("taskqueue")
    url = None if instance is None else instance.options.get("broker")
    if not isinstance(url, str) or not url:
        pytest.fail("the test configuration has no taskqueue.default broker")
    return url


@pytest.fixture(scope="session", autouse=True)
def _tests_run_against_the_built_wheel() -> None:
    """Where the lane is required (``SANDLOOM_CELERY_REQUIRED=1``) the package
    must be the installed wheel, not the source tree (see ``source_tree_gap``)."""
    gap = source_tree_gap(sandloom_celery.__file__)
    if gap:
        pytest.fail(gap, pytrace=False)


@pytest.fixture(scope="session")
def real_broker() -> str:
    """The URL of the real test broker; fails or skips where it is down (see
    ``_celery_broker_guard``). Asked once per process."""
    url = _broker_url()
    require_broker(url)
    return url


@pytest.fixture(scope="session")
def real_cache() -> str:
    """The URL of the real test cache (the lease store of the host groups); fails
    or skips where it is down, by the rules of ``real_broker``."""
    url = resolved_cache_url()
    require_broker(url, what="cache")
    return url
