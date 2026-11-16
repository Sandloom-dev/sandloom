"""An app refused by the conf check never becomes ``celery.current_app``.

``Celery(...)`` makes the app it builds current by default, so an app refused
for a foreign broker or backend would stay the process's current app (what
``@shared_task`` and Celery's own commands reach). The driver builds without
making it current and makes it so once the check has passed.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from celery import Celery
from celery._state import get_current_app
from sandloom_celery import worker as celery_worker
from sandloom_celery.taskqueue import create_celery_taskqueue
from sandloom_celery.worker import ForeignCeleryConfError

from sandloom.core.config.settings import settings
from sandloom.core.infra.config import InstanceConfig

_BROKER = "redis://user:infra-secret@127.0.0.1:1/9"
_STRAY = "redis://user:stray-secret@127.0.0.1:1/7"
_CELERY_ENV = (
    "CELERY_BROKER_URL",
    "CELERY_BROKER_READ_URL",
    "CELERY_BROKER_WRITE_URL",
    "CELERY_RESULT_BACKEND",
    "CELERY_CONFIG_MODULE",
)


@pytest.fixture
def current(monkeypatch: pytest.MonkeyPatch) -> Iterator[Celery]:
    """A known current app, no ambient Celery variable, no process-wide app."""
    for name in _CELERY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(celery_worker._default, "app", None)
    monkeypatch.setattr(celery_worker._default, "urls", None)
    previous = get_current_app()
    sentinel = Celery("sentinel", broker="memory://", set_as_current=False)
    sentinel.set_current()
    try:
        yield sentinel
    finally:
        previous.set_current()


def _instance(name: str) -> InstanceConfig:
    return InstanceConfig(
        capability="taskqueue", name=name, driver="celery", options={"broker": _BROKER}
    )


@pytest.mark.parametrize("name", ["default", "aux"])
def test_a_refused_provider_app_is_not_current(
    monkeypatch: pytest.MonkeyPatch, current: Celery, name: str
) -> None:
    provider = create_celery_taskqueue(_instance(name))
    monkeypatch.setenv("CELERY_BROKER_URL", _STRAY)
    with pytest.raises(ForeignCeleryConfError):
        provider._app  # noqa: B018
    assert get_current_app() is current


@pytest.mark.parametrize("name", ["default", "aux"])
def test_an_accepted_provider_app_becomes_current(current: Celery, name: str) -> None:
    app = create_celery_taskqueue(_instance(name))._app
    assert get_current_app() is app is not current


def test_a_refused_importable_app_is_not_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, current: Celery
) -> None:
    monkeypatch.setattr(
        settings,
        "infra",
        {"taskqueue": {"default": {"driver": "celery", "broker": _BROKER}}},
    )
    (tmp_path / "refused_current_config.py").write_text(
        'broker_write_url = "redis://user:foreign-secret@127.0.0.1:1/5"\n'
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "refused_current_config", raising=False)
    monkeypatch.setenv("CELERY_CONFIG_MODULE", "refused_current_config")
    with pytest.raises(AttributeError, match="broker_write_url"):
        celery_worker.app  # noqa: B018
    assert get_current_app() is current


def test_an_accepted_importable_app_becomes_current(
    monkeypatch: pytest.MonkeyPatch, current: Celery
) -> None:
    monkeypatch.setattr(
        settings,
        "infra",
        {"taskqueue": {"default": {"driver": "celery", "broker": _BROKER}}},
    )
    app = celery_worker.app
    assert get_current_app() is app is not current
