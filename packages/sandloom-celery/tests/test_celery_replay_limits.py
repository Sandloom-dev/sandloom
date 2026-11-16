"""The Celery driver's enqueue-dedupe TTL equals the kernel's shared bound.

The driver imports no kernel module, so it states the 24 h record TTL as its own
default; the kernel's ``sandloom.core.config.replay_limits`` is the bound every
other dedupe record derives from (``sandloom.core.tests.test_replay_limits``).
"""

from __future__ import annotations

from sandloom_celery.taskqueue import CeleryTaskQueueOptions

import sandloom.core.config.replay_limits as replay_limits


def test_the_enqueue_dedupe_ttl_is_the_kernels_record_ttl() -> None:
    default = CeleryTaskQueueOptions.model_fields["dedupe_ttl_seconds"].default
    assert default == replay_limits.REPLAY_RECORD_TTL_SECONDS
