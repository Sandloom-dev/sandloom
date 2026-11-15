"""Fixtures of the sandloom-celery package tests (the root ``conftest.py`` seeds
``settings`` from ``.env.test`` / ``SANDLOOM_TEST_*`` as for every suite)."""

from __future__ import annotations

import os

import pytest
from _celery_broker_guard import broker_required, require_broker

from sandloom.core.config.settings import settings


@pytest.fixture(scope="session")
def real_broker() -> str:
    """The URL of the real test broker; fails or skips where it is down (see
    ``_celery_broker_guard``). Asked once per process."""
    require_broker(settings.broker_url, required=broker_required(os.environ))
    return settings.broker_url
