"""Fixtures of the ``sandloom.sdk`` tests (Public API 1.0, hand-off H2).

``l1_modules/`` holds fixture business modules written only with L1
declarations (``sdkfix_notes``, ``sdkfix_stats``) and their shared stub
package; it is put on ``sys.path`` so they import like application modules.
The harness fixtures come from ``sandloom.sdk.testing.fixtures``; the recording
that ``published()`` reads is switched on per test, like the plugin does.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandloom.sdk.testing.fixtures import *  # noqa: F403

L1_MODULES = Path(__file__).resolve().parent / "l1_modules"
if str(L1_MODULES) not in sys.path:
    sys.path.insert(0, str(L1_MODULES))


@pytest.fixture(autouse=True)
def _sdk_recording() -> Iterator[None]:
    from sandloom.sdk._decl import _recording

    _recording.start()
    try:
        yield
    finally:
        _recording.stop()
