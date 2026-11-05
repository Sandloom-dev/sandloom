"""Integration tests: ``skip_unless_db_feature``'s marker.

The kernel's tests register it in ``src/sandloom/core/tests/conftest.py`` and
the pytest plugin registers it for applications; a run that collects only
this directory needs it too (``--strict-markers``). The autouse fixture that
applies it comes with ``sandloom.core.testing.fixtures``' star import.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "sandloom_db_feature(*features, database='default'): skip unless the "
        "database instance has the features (skip_unless_db_feature)",
    )
