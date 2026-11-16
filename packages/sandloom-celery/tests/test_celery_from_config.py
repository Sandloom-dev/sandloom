"""``CeleryTaskQueueProvider.from_config`` builds ``cls``: a subclass gets itself back."""

from __future__ import annotations

import pytest
from sandloom_celery import worker as celery_worker
from sandloom_celery.taskqueue import CeleryTaskQueueProvider, create_celery_taskqueue
from sandloom_celery.worker import ForeignCeleryConfError

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


_BROKER = "redis://user:infra-secret@127.0.0.1:1/9"
_BACKEND = "redis://user:infra-backend-secret@127.0.0.1:1/8"
_STRAY = "redis://user:stray-secret@127.0.0.1:1/7"
_CELERY_ENV = (
    "CELERY_BROKER_URL",
    "CELERY_BROKER_READ_URL",
    "CELERY_BROKER_WRITE_URL",
    "CELERY_RESULT_BACKEND",
)


@pytest.fixture
def _clean_celery_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient Celery variable, and no process-wide app left by another test."""
    for name in _CELERY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(celery_worker._default, "app", None)
    monkeypatch.setattr(celery_worker._default, "urls", None)


def _instance(name: str) -> InstanceConfig:
    return InstanceConfig(
        capability="taskqueue",
        name=name,
        driver="celery",
        options={"broker": _BROKER, "backend": _BACKEND},
    )


@pytest.mark.usefixtures("_clean_celery_env")
@pytest.mark.parametrize("name", ["default", "aux"])
@pytest.mark.parametrize("variable", _CELERY_ENV)
def test_a_celery_variable_other_than_infra_is_refused_on_the_enqueue_side(
    monkeypatch: pytest.MonkeyPatch, name: str, variable: str
) -> None:
    """Celery's ``app.conf`` prefers ``CELERY_BROKER_URL`` and the others over the
    ``broker=`` the app was built with: a stray one would silently redirect every
    publish. The refusal names the option and the instance, never a url."""
    provider = create_celery_taskqueue(_instance(name))
    monkeypatch.setenv(variable, _STRAY)
    with pytest.raises(ForeignCeleryConfError) as refused:
        provider._app  # noqa: B018
    text = str(refused.value)
    assert f"taskqueue.{name}." in text
    for secret in ("stray-secret", "infra-secret", "infra-backend-secret"):
        assert secret not in text
    assert provider._built_app is None


@pytest.mark.usefixtures("_clean_celery_env")
async def test_a_refused_app_is_not_an_outage_of_the_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal is no ``Unavailable``: nothing was published, and the kernel
    must not keep a dedupe key pending or retry as for an outage."""
    provider = create_celery_taskqueue(_instance("aux"))
    monkeypatch.setenv("CELERY_BROKER_URL", _STRAY)
    with pytest.raises(ForeignCeleryConfError):
        await provider.send("probe.task", b"{}", task_id="t-1")


@pytest.mark.usefixtures("_clean_celery_env")
@pytest.mark.parametrize("name", ["default", "aux"])
def test_celery_variables_equal_to_infra_or_blank_are_accepted(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The CLI mirrors ``-b`` into ``CELERY_BROKER_URL``: what INFRA states
    passes, and a blank value is unset to Celery."""
    provider = create_celery_taskqueue(_instance(name))
    monkeypatch.setenv("CELERY_BROKER_URL", _BROKER)
    monkeypatch.setenv("CELERY_BROKER_READ_URL", "")
    monkeypatch.setenv("CELERY_RESULT_BACKEND", _BACKEND)
    assert provider._app.conf.broker_url == _BROKER
    assert provider._app.conf.result_backend == _BACKEND
