"""``sandloom.driverkit.process_config``: an instance's options as a process that
boots no application reads them (``celery -A sandloom_celery.worker inspect``).

A driver package imports only ``sandloom.driverkit``, so this is how it reads
``INFRA`` through ``Settings``. Lane: the no-Redis lane over
``src/sandloom/core/tests``; what it returns for the real Celery driver, and that
only the taskqueue's drivers are loaded, is pinned in the Celery package's
``test_celery_process_config.py`` (a driver is needed to answer).
"""

from __future__ import annotations

import pytest

from sandloom.driverkit.process_config import process_instance


@pytest.mark.parametrize("capability", ["cache", "database", "eventbus", "nothing"])
def test_a_capability_a_process_does_not_configure_is_refused_not_none(
    capability: str,
) -> None:
    with pytest.raises(ValueError, match=rf"taskqueue.*{capability}"):
        process_instance(capability)
