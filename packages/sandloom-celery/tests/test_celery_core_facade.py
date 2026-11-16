"""``CoreFacade.taskqueue()`` resolves the Celery driver through the registry."""

from __future__ import annotations

from fastapi import FastAPI

from sandloom.core.config.settings import settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.config import load_infra_config
from sandloom.core.infra.contracts import TaskQueueProvider
from sandloom.core.schema.registry import InMemorySchemaRegistry


def test_the_taskqueue_accessor_returns_the_celery_provider() -> None:
    core = CoreFacade(app=FastAPI())
    core.set_infra(
        build_infra_registry(
            InMemorySchemaRegistry(), config=load_infra_config(settings)
        )
    )
    provider = core.taskqueue()
    assert isinstance(provider, TaskQueueProvider)
    assert provider.driver == "celery"
