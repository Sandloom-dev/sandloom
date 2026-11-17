"""The connection policy of a ``celery`` taskqueue instance
(``sandloom_celery.connection_policy``): what ``INFRA`` may state for TLS and the
sentinel master, refused at startup and in the importable app alike."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sandloom_celery import worker as celery_worker
from sandloom_celery.connection_policy import (
    ConnectionOptions,
    scheme_of,
    validate_connection,
)
from sandloom_celery.taskqueue import CeleryTaskQueueOptions, create_celery_taskqueue

from sandloom.core.config.settings import settings
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.registry import ConfigSchemaError

_CELERY_ENV = (
    "CELERY_BROKER_URL",
    "CELERY_BROKER_READ_URL",
    "CELERY_BROKER_WRITE_URL",
    "CELERY_RESULT_BACKEND",
    "CELERY_CONFIG_MODULE",
)
_TLS = "rediss://:infra-secret@127.0.0.1:6380/0"


@pytest.fixture(autouse=True)
def _clean_celery(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _CELERY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(celery_worker._default, "app", None)
    monkeypatch.setattr(celery_worker._default, "connection", None)


@pytest.mark.parametrize(
    ("url", "scheme"),
    [
        ("redis://h:1/0", "redis"),
        ("REDISS://h:1/0", "rediss"),
        ("sentinel://a:26379;sentinel://b:26379", "sentinel"),
        ("sqla+mysql://u@h/db", "sqla"),
        ("amqps://u:p@h//", "amqps"),
        (None, ""),
        ("", ""),
    ],
)
def test_the_scheme_is_the_transport_kombu_selects(
    url: str | None, scheme: str
) -> None:
    assert scheme_of(url) == scheme


@pytest.mark.parametrize(
    "query",
    ["", "?ssl_cert_reqs=", "?ssl_ca_certs=/etc/ca.pem", "?ssl_cert_reqs=bogus"],
)
@pytest.mark.parametrize("role", ["broker", "backend"])
def test_a_rediss_url_without_a_valid_ssl_cert_reqs_is_refused(
    role: str, query: str
) -> None:
    """kombu connects a ``rediss://`` url with no ``ssl_cert_reqs`` with
    ``CERT_NONE`` (``kombu/connection.py``) and only logs a warning; Celery's result
    backend refuses the same url. Both are refused now, naming the option, never
    the url."""
    options = {"broker": "redis://127.0.0.1/0"}
    options[role] = _TLS + query
    with pytest.raises(ValidationError, match=f"the {role} url") as refused:
        CeleryTaskQueueOptions.model_validate(options)
    assert "infra-secret" not in str(refused.value)


_VERIFIED = "?ssl_cert_reqs=required&ssl_check_hostname=true"


@pytest.mark.parametrize(
    "query",
    [
        "?ssl_cert_reqs=required&ssl_check_hostname=true",
        "?ssl_cert_reqs=CERT_REQUIRED&ssl_check_hostname=TRUE",
        "?ssl_cert_reqs=optional&ssl_check_hostname=1",
        # none states that no check is wanted: no hostname to check either
        "?ssl_cert_reqs=none",
        "?ssl_cert_reqs=CERT_NONE",
    ],
)
def test_a_rediss_url_that_states_its_verification_is_accepted(query: str) -> None:
    """A stated value is the operator's decision, as for the result backend
    (``none`` is the explicit way to skip verification, which kombu then logs)."""
    options = CeleryTaskQueueOptions.model_validate({"broker": f"{_TLS}{query}"})
    assert options.broker is not None


@pytest.mark.parametrize(
    "query",
    [
        "?ssl_cert_reqs=required",
        "?ssl_cert_reqs=required&ssl_check_hostname=false",
        "?ssl_cert_reqs=optional&ssl_check_hostname=0",
        "?ssl_check_hostname=true&ssl_cert_reqs=required&ssl_check_hostname=false",
    ],
)
@pytest.mark.parametrize("role", ["broker", "backend"])
def test_a_rediss_url_that_checks_no_hostname_is_refused(role: str, query: str) -> None:
    """redis-py (< 6) defaults ``ssl_check_hostname`` to false, so
    ``ssl_cert_reqs=required`` alone accepts any certificate of the CA, for any
    name: the url must say ``ssl_check_hostname=true`` too."""
    options = {"broker": "redis://127.0.0.1/0"}
    options[role] = _TLS + query
    with pytest.raises(ValidationError, match="ssl_check_hostname") as refused:
        CeleryTaskQueueOptions.model_validate(options)
    assert f"the {role} url" in str(refused.value)
    assert "infra-secret" not in str(refused.value)


def test_every_chunk_of_a_multi_host_rediss_url_must_state_it() -> None:
    """kombu switches to the next ``;`` url on failure and parses its own query."""
    url = f"{_TLS}{_VERIFIED};{_TLS}"
    with pytest.raises(ValidationError, match="ssl_cert_reqs"):
        CeleryTaskQueueOptions.model_validate({"broker": url})
    url = f"{_TLS}{_VERIFIED};{_TLS}?ssl_cert_reqs=required"
    with pytest.raises(ValidationError, match="ssl_check_hostname"):
        CeleryTaskQueueOptions.model_validate({"broker": url})
    assert CeleryTaskQueueOptions.model_validate(
        {"broker": f"{_TLS}{_VERIFIED};{_TLS}{_VERIFIED}"}
    )


@pytest.mark.parametrize(
    "url",
    [
        "redis://:infra-secret@a:1/0; redis://:infra-secret@b:1/0",
        "redis://:infra-secret@a:1/0 ;redis://:infra-secret@b:1/0",
        " redis://:infra-secret@a:1/0",
        "redis://:infra-secret@a:1/0 ",
        "redis://:infra-secret@a:1/0;",
        ";redis://:infra-secret@a:1/0",
    ],
)
def test_a_padded_or_empty_chunk_is_refused(url: str) -> None:
    """kombu splits a url on ``;`` without stripping: a padded chunk fails over
    with the password in the error it logs."""
    with pytest.raises(ValidationError, match="empty chunk or a space") as refused:
        CeleryTaskQueueOptions.model_validate({"broker": url})
    assert "infra-secret" not in str(refused.value)


def test_a_plain_redis_url_needs_no_ssl_option() -> None:
    CeleryTaskQueueOptions.model_validate({"broker": "redis://127.0.0.1/0"})


def test_urls_of_different_schemes_are_refused() -> None:
    """kombu switches the transport with the url, and the transport options are
    allowed per scheme."""
    with pytest.raises(ValueError, match="mixes schemes"):
        validate_connection(ConnectionOptions("redis://a;amqp://b"))


@pytest.mark.parametrize(
    ("broker", "stated"),
    [
        ("sqs://KEY:SECRET@sqs.eu-west-1.amazonaws.com", {"is_secure": True}),
        ("sqs://KEY:SECRET@", {}),
        ("redis://h:1/0", {}),
    ],
)
def test_infra_states_tls_for_an_sqs_url_with_a_host(
    broker: str, stated: dict[str, object]
) -> None:
    """kombu builds ``http://<host>`` for an SQS url with a host unless
    ``is_secure`` is true (``SQS.py:923-934``), and only the transport options
    carry it: INFRA states it, a conf may repeat it and never turn it off."""
    assert ConnectionOptions(broker).broker_stated_options() == stated


@pytest.mark.parametrize("role", ["broker", "backend"])
def test_a_sentinel_url_needs_its_master(role: str) -> None:
    url = "sentinel://:infra-secret@127.0.0.1:26379/0"
    options = {"broker": "redis://127.0.0.1/0"}
    options[role] = url
    master = "master_name" if role == "broker" else "backend_master_name"
    with pytest.raises(ValidationError, match=master) as refused:
        CeleryTaskQueueOptions.model_validate(options)
    assert "infra-secret" not in str(refused.value)
    options[master] = "mymaster"
    assert CeleryTaskQueueOptions.model_validate(options)


@pytest.mark.parametrize("master", ["master_name", "backend_master_name"])
def test_a_master_without_a_sentinel_url_is_refused(master: str) -> None:
    """The option would be ignored: a refusal, not a silent no-op."""
    with pytest.raises(ValidationError, match=master):
        CeleryTaskQueueOptions.model_validate(
            {"broker": "redis://127.0.0.1/0", master: "mymaster"}
        )


@pytest.mark.parametrize("name", ["", "my master", "m" * 65, "m\nm", "a/b"])
def test_a_master_name_is_bounded(name: str) -> None:
    with pytest.raises(ValidationError):
        CeleryTaskQueueOptions.model_validate(
            {"broker": "sentinel://127.0.0.1:26379/0", "master_name": name}
        )


def test_the_provider_of_an_unverified_rediss_broker_is_refused_at_build() -> None:
    with pytest.raises(ValidationError, match="ssl_cert_reqs") as refused:
        create_celery_taskqueue(
            InstanceConfig(
                capability="taskqueue",
                name="aux",
                driver="celery",
                options={"broker": _TLS},
            )
        )
    assert "infra-secret" not in str(refused.value)


def _infra(monkeypatch: pytest.MonkeyPatch, **options: object) -> None:
    monkeypatch.setattr(
        settings,
        "infra",
        {"taskqueue": {"default": {"driver": "celery", **options}}},
    )


def test_the_importable_app_refuses_an_unverified_rediss_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _infra(monkeypatch, broker=_TLS)
    with pytest.raises(ConfigSchemaError, match="ssl_cert_reqs") as refused:
        celery_worker.app  # noqa: B018
    assert "infra-secret" not in str(refused.value)
    assert celery_worker._default.app is None


def test_the_importable_app_refuses_a_sentinel_broker_without_a_master(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _infra(monkeypatch, broker="sentinel://127.0.0.1:26379/0")
    with pytest.raises(ConfigSchemaError, match="master_name"):
        celery_worker.app  # noqa: B018
    assert celery_worker._default.app is None


def test_the_importable_app_takes_the_sentinel_master_from_infra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _infra(
        monkeypatch,
        broker="sentinel://127.0.0.1:26379/0",
        master_name="infra-master",
    )
    app = celery_worker.app
    assert app.conf.broker_transport_options == {"master_name": "infra-master"}
    assert celery_worker._default.connection == ConnectionOptions(
        "sentinel://127.0.0.1:26379/0", None, "infra-master", None
    )


def test_the_importable_app_refuses_a_non_string_master(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _infra(monkeypatch, broker="sentinel://127.0.0.1:26379/0", master_name=7)
    with pytest.raises(ConfigSchemaError, match="master_name"):
        celery_worker.app  # noqa: B018
