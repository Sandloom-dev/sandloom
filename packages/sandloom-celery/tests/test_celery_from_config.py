"""``CeleryTaskQueueProvider.from_config`` builds ``cls``: a subclass gets itself back."""

from __future__ import annotations

import pytest
from sandloom_celery.taskqueue import CeleryTaskQueueProvider, create_celery_taskqueue

from sandloom.core.infra.config import InstanceConfig


class _Queue(CeleryTaskQueueProvider):
    pass


def _config(**options: object) -> InstanceConfig:
    return InstanceConfig(
        capability="taskqueue", name="aux", driver="celery", options=dict(options)
    )


def test_a_taskqueue_subclass_builds_itself_and_the_factory_the_base_class() -> None:
    config = _config(broker="redis://127.0.0.1:1/0")
    built = _Queue.from_config(config)
    assert type(built) is _Queue
    assert type(CeleryTaskQueueProvider.from_config(config)) is CeleryTaskQueueProvider
    assert type(create_celery_taskqueue(config)) is CeleryTaskQueueProvider


def test_the_subclass_build_still_needs_a_broker() -> None:
    with pytest.raises(ValueError, match="broker"):
        _Queue.from_config(_config())
