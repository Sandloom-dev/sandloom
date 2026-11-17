"""``CeleryTaskQueueProvider.from_config`` builds ``cls``: a subclass gets itself back."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from sandloom_celery import worker as celery_worker
from sandloom_celery.connection_policy import (
    FOREIGN_BACKEND_KEYS,
    FOREIGN_BACKEND_NAMES,
)
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
    monkeypatch.setattr(celery_worker._default, "connection", None)


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


@pytest.fixture
def _config_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[[str], None]:
    """A Celery config module on ``sys.path``, named by ``CELERY_CONFIG_MODULE``
    (what ``celery --config`` writes): the argument is its body."""

    def write(body: str) -> None:
        (tmp_path / "foreign_provider_celery_config.py").write_text(body)
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.delitem(
            sys.modules, "foreign_provider_celery_config", raising=False
        )
        monkeypatch.setenv("CELERY_CONFIG_MODULE", "foreign_provider_celery_config")

    return write


_CONNECTION_REPROS = {
    # a config module that sinks every publish into an in-process queue
    "broker_transport": 'broker_transport = "memory"\n',
    # the INFRA url below omits port, database and password: these fill them in
    "broker_port": "broker_port = 6399\n",
    "broker_vhost": 'broker_vhost = "7"\n',
    "broker_user": 'broker_user = "stray-user"\n',
    "broker_password": 'broker_password = "stray-pw"\n',
    "broker_host": 'broker_host = "stray-host"\n',
    # kombu merges a ``broker_use_ssl`` dict into the connection AFTER the url:
    # host, port, password and database of INFRA lose to it
    "broker_use_ssl": (
        "broker_use_ssl = {'host': 'stray-host', 'port': 6399, "
        "'password': 'stray-pw', 'virtual_host': '7'}\n"
    ),
    # ... and ``ssl_cert_reqs`` turns certificate verification off for a rediss:// url
    "broker_use_ssl#cert_none": (
        "import ssl\nbroker_use_ssl = {'ssl_cert_reqs': ssl.CERT_NONE}\n"
    ),
    # the redis result backend fills what its url omits from these
    "redis_host": 'redis_host = "stray-host"\n',
    "redis_port": "redis_port = 6399\n",
    "redis_db": "redis_db = 7\n",
    "redis_username": 'redis_username = "stray-user"\n',
    "redis_password": 'redis_password = "stray-pw"\n',
    "redis_backend_use_ssl": "redis_backend_use_ssl = {'ssl_cert_reqs': 'none'}\n",
    "redis_backend_credential_provider": (
        'redis_backend_credential_provider = "stray.provider"\n'
    ),
    # kombu cycles the hosts of the url; a callable yields urls of its own
    "broker_failover_strategy": 'broker_failover_strategy = "stray-strategy"\n',
    # the SQLAlchemy result backend's create_engine() takes connect_args
    "database_engine_options": (
        "database_engine_options = {'connect_args': {'host': 'stray-host'}}\n"
    ),
}


@pytest.mark.usefixtures("_clean_celery_env")
@pytest.mark.parametrize("name", ["default", "aux"])
@pytest.mark.parametrize("key", list(_CONNECTION_REPROS))
def test_a_config_module_cannot_redirect_the_connection_past_the_infra_url(
    _config_module: Callable[[str], None], name: str, key: str
) -> None:
    """``broker_transport`` / ``broker_port`` / ``broker_vhost`` / ``broker_user`` /
    ``broker_password`` / ``broker_host`` reach kombu next to the url and win over
    what it omits (the transport over everything): with ``broker_transport =
    "memory"`` every publish "succeeds" into an in-process queue and the task is
    lost. A ``broker_use_ssl`` dict redirects or de-verifies the connection, and
    the ``redis_*`` keys feed the result backend. Each is refused, naming the key
    and the instance, never a value."""
    _config_module(_CONNECTION_REPROS[key])
    provider = create_celery_taskqueue(
        InstanceConfig(
            capability="taskqueue",
            name=name,
            driver="celery",
            options={"broker": "redis://127.0.0.1"},
        )
    )
    with pytest.raises(ForeignCeleryConfError, match=key.split("#")[0]) as refused:
        provider._app  # noqa: B018
    text = str(refused.value)
    assert f"taskqueue.{name}." in text
    for secret in ("stray-pw", "stray-user", "stray-host", "6399", "CERT_NONE"):
        assert secret not in text
    assert provider._built_app is None


@pytest.mark.usefixtures("_clean_celery_env")
@pytest.mark.parametrize("name", ["default", "aux"])
@pytest.mark.parametrize("setting", ["True", "None", "False"])
def test_broker_use_ssl_is_accepted_as_none_or_false_only(
    _config_module: Callable[[str], None], name: str, setting: str
) -> None:
    """``broker_use_ssl = True`` makes kombu skip the TLS default of ``rediss://``,
    swallow the failed merge and connect in plain text: the url's password goes
    out in the clear. Only ``None`` and ``False`` (Celery's default) pass; the
    refusal names the key, never the url."""
    _config_module(f"broker_use_ssl = {setting}\n")
    provider = create_celery_taskqueue(
        InstanceConfig(
            capability="taskqueue",
            name=name,
            driver="celery",
            options={
                "broker": "rediss://:infra-secret@127.0.0.1:6380/0"
                "?ssl_cert_reqs=required&ssl_check_hostname=true"
            },
        )
    )
    if setting == "True":
        with pytest.raises(ForeignCeleryConfError, match="broker_use_ssl") as refused:
            provider._app  # noqa: B018
        assert "infra-secret" not in str(refused.value)
        assert provider._built_app is None
    else:
        assert provider._app.conf.broker_use_ssl is (
            None if setting == "None" else False
        )


@pytest.mark.usefixtures("_clean_celery_env")
@pytest.mark.parametrize("key", FOREIGN_BACKEND_KEYS)
def test_every_result_backend_connection_key_is_refused(
    _config_module: Callable[[str], None], key: str
) -> None:
    """The result backends complete (or override) the connection their url states
    from these keys (Celery's ``backends`` read each): redis, MongoDB, Couchbase,
    ArangoDB, Cassandra, S3, GCS, cache and SQLAlchemy ones. None may be set."""
    _config_module(f"{key} = 'stray-value'\n")
    provider = create_celery_taskqueue(_instance("aux"))
    with pytest.raises(ForeignCeleryConfError, match=key) as refused:
        provider._app  # noqa: B018
    assert "stray-value" not in str(refused.value)
    assert provider._built_app is None


@pytest.mark.usefixtures("_clean_celery_env")
def test_the_defaults_and_the_safe_shapes_of_the_backend_keys_pass(
    _config_module: Callable[[str], None],
) -> None:
    """``database_engine_options`` has a non-empty default and two dict-valued
    backend settings default to ``{}``: those, a pool setting and a named failover
    strategy are no foreign connection."""
    _config_module(
        "broker_failover_strategy = 'shuffle'\n"
        "database_engine_options = {'pool_size': 3, 'pool_pre_ping': True}\n"
        "cassandra_options = {}\ncache_backend_options = {}\n"
    )
    assert create_celery_taskqueue(_instance("aux"))._app is not None


_SENTINEL = "sentinel://:infra-secret@127.0.0.1:26379/0"
_SQS = "sqs://AKIA:infra-secret@"
_SQS_HOST = "sqs://AKIA:infra-secret@sqs.eu-west-1.amazonaws.com"
_QUEUE_URL = "https://sqs.eu-west-1.amazonaws.com/123456789012/q"
_AMQP = "amqp://u:infra-secret@127.0.0.1:1//"
_REDIS = "redis://:infra-secret@127.0.0.1:1/0"
_MASTER = {"master_name": "infra-master"}

#: ``(broker, instance options besides it, config module body, refused option)``.
_TRANSPORT_REPROS = {
    # kombu's pyamqp transport lays these over host / userid / password
    "host": (
        _AMQP,
        {},
        "broker_transport_options = {'host': '127.0.0.1:6399', "
        "'userid': 'stray-user', 'password': 'stray-pw'}\n",
        "broker_transport_options",
    ),
    # ... and ``ssl`` turns certificate verification off for amqps://
    "ssl": (
        "amqps://u:infra-secret@127.0.0.1:1//",
        {},
        "import ssl\nbroker_transport_options = {'ssl': {'cert_reqs': ssl.CERT_NONE}}\n",
        "broker_transport_options",
    ),
    # the SASL response and the authentication object carry the credentials
    "login_response": (
        _AMQP,
        {},
        "broker_transport_options = {'login_response': b'stray-pw'}\n",
        "broker_transport_options",
    ),
    # the sentinel transport takes the master of every publish from the INFRA
    # option: another one in the conf is refused ...
    "master_name": (
        _SENTINEL,
        _MASTER,
        "broker_transport_options = {'master_name': 'stray-master'}\n",
        "broker_transport_options",
    ),
    # ... and so is one for a url that is no sentinel url
    "master_name on redis": (
        _REDIS,
        {},
        "broker_transport_options = {'master_name': 'stray-master'}\n",
        "broker_transport_options",
    ),
    # the sentinel nodes' own password and TLS live here: no INFRA option for them
    "sentinel_kwargs": (
        _SENTINEL,
        _MASTER,
        "broker_transport_options = {'sentinel_kwargs': {'password': 'stray-pw'}}\n",
        "broker_transport_options",
    ),
    "backend": (
        _REDIS,
        {},
        "result_backend_transport_options = {'host': 'stray-host', "
        "'password': 'stray-pw'}\n",
        "result_backend_transport_options",
    ),
    "backend sentinel_kwargs": (
        _REDIS,
        {"backend": _SENTINEL, "backend_master_name": "infra-backend-master"},
        "result_backend_transport_options = {'sentinel_kwargs': "
        "{'password': 'stray-pw'}}\n",
        "result_backend_transport_options",
    ),
    "backend master_name": (
        _REDIS,
        {"backend": _SENTINEL, "backend_master_name": "infra-backend-master"},
        "result_backend_transport_options = {'master_name': 'stray-master'}\n",
        "result_backend_transport_options",
    ),
    "not a mapping": (
        _REDIS,
        {},
        "broker_transport_options = 'stray-pw'\n",
        "broker_transport_options",
    ),
    # a redis key means nothing to (and a key of another transport must not pass
    # for) the scheme of the url
    "redis key on amqp": (
        _AMQP,
        {},
        "broker_transport_options = {'socket_timeout': 9.0}\n",
        "broker_transport_options",
    ),
    # SQS: plain-text endpoint, another endpoint port, proxies and another
    # credential source are not tuning
    "sqs is_secure": (
        _SQS,
        {},
        "broker_transport_options = {'is_secure': False}\n",
        "broker_transport_options",
    ),
    "sqs port": (
        _SQS,
        {},
        "broker_transport_options = {'port': 6399}\n",
        "broker_transport_options",
    ),
    "sqs client-config": (
        _SQS,
        {},
        "broker_transport_options = {'client-config': {'proxies': "
        "{'https': 'http://stray-host:6399'}}}\n",
        "broker_transport_options",
    ),
    "sqs sts_role_arn": (
        _SQS,
        {},
        "broker_transport_options = {'sts_role_arn': 'stray-arn'}\n",
        "broker_transport_options",
    ),
    # the region builds the endpoint host: only a region name passes
    "sqs region": (
        _SQS,
        {},
        "broker_transport_options = {'region': 'stray-host.example/x#'}\n",
        "broker_transport_options",
    ),
    "sqs queue_name_prefix": (
        _SQS,
        {},
        "broker_transport_options = {'queue_name_prefix': 'stray/../x'}\n",
        "broker_transport_options",
    ),
    "sqs predefined_queues key": (
        _SQS,
        {},
        "broker_transport_options = {'predefined_queues': {'q': "
        "{'url': 'https://sqs.eu-west-1.amazonaws.com/1/q', 'endpoint_url': "
        "'http://stray-host'}}}\n",
        "broker_transport_options",
    ),
    # a queue's own region and credentials replace the INFRA url's and the
    # transport's (``SQS.py:769-781``): tasks would go to another AWS account
    "sqs predefined_queues region": (
        _SQS,
        {},
        "broker_transport_options = {'predefined_queues': {'q': "
        f"{{'url': '{_QUEUE_URL}', 'region': 'us-west-2'}}}}}}\n",
        "broker_transport_options",
    ),
    "sqs predefined_queues access_key_id": (
        _SQS,
        {},
        "broker_transport_options = {'predefined_queues': {'q': "
        f"{{'url': '{_QUEUE_URL}', 'access_key_id': 'AKIASTAGING'}}}}}}\n",
        "broker_transport_options",
    ),
    "sqs predefined_queues secret_access_key": (
        _SQS,
        {},
        "broker_transport_options = {'predefined_queues': {'q': "
        f"{{'url': '{_QUEUE_URL}', 'secret_access_key': 'stray-secret'}}}}}}\n",
        "broker_transport_options",
    ),
    # the queue url: TLS, an AWS host, an account and a name, nothing else
    "sqs predefined_queues plain http url": (
        _SQS,
        {},
        "broker_transport_options = {'predefined_queues': {'q': "
        "{'url': 'http://sqs.eu-west-1.amazonaws.com/123456789012/q'}}}\n",
        "broker_transport_options",
    ),
    "sqs predefined_queues foreign host url": (
        _SQS,
        {},
        "broker_transport_options = {'predefined_queues': {'q': "
        "{'url': 'https://sqs.eu-west-1.amazonaws.com.stray.example/123456789012/q'}}}\n",
        "broker_transport_options",
    ),
    "sqs predefined_queues no url": (
        _SQS,
        {},
        "broker_transport_options = {'predefined_queues': {'q': "
        "{'backoff_tasks': ['t']}}}\n",
        "broker_transport_options",
    ),
    # a queue in the INFRA host's region only, and in the INFRA account when it
    # states one: a conf shipped from another account or region cannot publish there
    "sqs predefined_queues other region than the infra host": (
        _SQS_HOST,
        {},
        "broker_transport_options = {'predefined_queues': {'q': "
        "{'url': 'https://sqs.us-east-1.amazonaws.com/123456789012/q'}}}\n",
        "broker_transport_options",
    ),
    "sqs predefined_queues other region than the conf region": (
        _SQS,
        {},
        "broker_transport_options = {'region': 'eu-west-1', 'predefined_queues': "
        "{'q': {'url': 'https://sqs.us-east-1.amazonaws.com/123456789012/q'}}}\n",
        "broker_transport_options",
    ),
    "sqs predefined_queues other account": (
        _SQS_HOST,
        {"sqs_account_id": "123456789012"},
        "broker_transport_options = {'predefined_queues': {'q': "
        "{'url': 'https://sqs.eu-west-1.amazonaws.com/999999999999/q'}}}\n",
        "broker_transport_options",
    ),
    "sqs conf region other than the infra host": (
        _SQS_HOST,
        {},
        "broker_transport_options = {'region': 'us-east-1'}\n",
        "broker_transport_options",
    ),
    # an sqs:// url with a host is https only through ``is_secure``, which INFRA
    # states: the conf may not turn it off
    "sqs host is_secure off": (
        _SQS_HOST,
        {},
        "broker_transport_options = {'is_secure': False}\n",
        "broker_transport_options",
    ),
    "sqs predefined_queues shape": (
        _SQS,
        {},
        "broker_transport_options = {'predefined_queues': 'stray-queue'}\n",
        "broker_transport_options",
    ),
}


@pytest.mark.usefixtures("_clean_celery_env")
@pytest.mark.parametrize("name", ["default", "aux"])
@pytest.mark.parametrize("case", list(_TRANSPORT_REPROS))
def test_a_transport_option_that_moves_the_connection_is_refused(
    _config_module: Callable[[str], None], name: str, case: str
) -> None:
    """An allow-list of transport options per scheme: kombu lays them over the
    connection built from the url (an ``amqp://`` host and credentials, ``ssl``, a
    sentinel ``master_name``, an SQS endpoint and region), so any key outside it is
    refused, named by its option only: no key, value, host or url reaches the
    message."""
    broker, extra, body, option = _TRANSPORT_REPROS[case]
    _config_module(body)
    provider = create_celery_taskqueue(
        InstanceConfig(
            capability="taskqueue",
            name=name,
            driver="celery",
            options={"broker": broker, **extra},
        )
    )
    with pytest.raises(ForeignCeleryConfError, match=option) as refused:
        provider._app  # noqa: B018
    text = str(refused.value)
    assert f"taskqueue.{name}." in text
    for stray in ("stray", "infra-secret", "6399", "CERT_NONE", "userid"):
        assert stray not in text
    assert provider._built_app is None


#: ``(broker, instance options besides it, config module body)``: tuning the
#: scheme's transport reads and that moves no connection.
_HARMLESS_TRANSPORT_OPTIONS = {
    "redis": (
        _REDIS,
        {},
        "broker_transport_options = {'visibility_timeout': 3600, "
        "'socket_timeout': 9.0, 'global_keyprefix': 'app:', 'max_connections': 5, "
        "'retry_on_timeout': True, 'unacked_mutex_expire': 600}\n"
        "result_backend_transport_options = {'retry_policy': "
        "{'timeout': 5.0}}\n",
    ),
    "amqp": (
        _AMQP,
        {},
        "broker_transport_options = {'confirm_publish': True, 'read_timeout': 5, "
        "'write_timeout': 5, 'socket_settings': {}, 'max_retries': 3}\n",
    ),
    "sqs": (
        _SQS,
        {},
        "broker_transport_options = {'region': 'eu-west-1', 'queue_name_prefix': "
        "'app-', 'wait_time_seconds': 20, 'visibility_timeout': 60, "
        f"'predefined_queues': {{'q': {{'url': '{_QUEUE_URL}', "
        "'backoff_policy': {1: 10}, 'backoff_tasks': ['t']}}}\n",
    ),
    # queue urls of the INFRA account and region, on the FIPS and VPC endpoint hosts
    "sqs host queues": (
        _SQS_HOST,
        {"sqs_account_id": "123456789012"},
        "broker_transport_options = {'predefined_queues': {"
        "'a': {'url': 'https://sqs-fips.eu-west-1.amazonaws.com/123456789012/a'}, "
        "'b': {'url': 'https://vpce-0a1b-abcd1234.sqs.eu-west-1.vpce.amazonaws.com"
        "/123456789012/b'}}}\n",
    ),
    # an sqs:// url with a host: INFRA states ``is_secure``, the conf may repeat it
    "sqs host": (
        _SQS_HOST,
        {},
        "broker_transport_options = {'is_secure': True, 'region': 'eu-west-1'}\n",
    ),
    "sentinel": (
        _SENTINEL,
        _MASTER,
        # the master of INFRA, stated again by the conf, and tuning
        "broker_transport_options = {'master_name': 'infra-master', "
        "'min_other_sentinels': 1, 'socket_timeout': 9.0}\n",
    ),
    "redis backend": (
        _REDIS,
        {"backend": "redis://:infra-secret@127.0.0.1:1/1"},
        "result_backend_transport_options = {'result_chord_ordered': False, "
        "'global_keyprefix': 'res:'}\n",
    ),
    "other backend": (
        _REDIS,
        {"backend": "cache+memory://"},
        "result_backend_transport_options = {'result_chord_ordered': False}\n",
    ),
}


@pytest.mark.usefixtures("_clean_celery_env")
@pytest.mark.parametrize("name", ["default", "aux"])
@pytest.mark.parametrize("case", list(_HARMLESS_TRANSPORT_OPTIONS))
def test_a_harmless_transport_option_is_accepted(
    _config_module: Callable[[str], None], name: str, case: str
) -> None:
    """The allow-list refuses the connection, not the tuning: the keys each
    transport reads to tune sockets, pools, key prefixes, retries, visibility, the
    SQS region and queue map and the result backend's ordering pass."""
    broker, extra, body = _HARMLESS_TRANSPORT_OPTIONS[case]
    _config_module(body)
    provider = create_celery_taskqueue(
        InstanceConfig(
            capability="taskqueue",
            name=name,
            driver="celery",
            options={"broker": broker, **extra},
        )
    )
    conf = provider._app.conf
    stated = "broker" if "broker_transport_options" in body else "result_backend"
    assert conf.get(f"{stated}_transport_options")


@pytest.mark.usefixtures("_clean_celery_env")
@pytest.mark.parametrize("name", ["default", "aux"])
@pytest.mark.parametrize(
    ("broker", "expected"),
    [(_SQS_HOST, {"is_secure": True}), (_SQS, None)],
)
def test_an_sqs_url_with_a_host_reaches_kombu_over_https(
    name: str, broker: str, expected: dict[str, object] | None
) -> None:
    """kombu builds ``http://<host>`` unless the transport option ``is_secure`` is
    true: INFRA lays it under the conf for an ``sqs://`` url with a host, and
    leaves an url without one (``use_ssl=True`` there) alone."""
    provider = create_celery_taskqueue(
        InstanceConfig(
            capability="taskqueue",
            name=name,
            driver="celery",
            options={"broker": broker},
        )
    )
    options = provider._app.conf.get("broker_transport_options")
    assert (options or None) == expected


@pytest.mark.usefixtures("_clean_celery_env")
@pytest.mark.parametrize("key", FOREIGN_BACKEND_NAMES)
def test_a_backend_container_or_collection_name_is_refused_unless_default(
    _config_module: Callable[[str], None], key: str
) -> None:
    """``azureblockblob_container_name`` and the Cosmos DB database and collection
    default to a name of their own: the default passes, any other names where the
    result backend stores (a refusal naming the option, not the value)."""
    _config_module(f"{key} = 'stray-value'\n")
    provider = create_celery_taskqueue(_instance("aux"))
    with pytest.raises(ForeignCeleryConfError, match=key) as refused:
        provider._app  # noqa: B018
    assert "stray-value" not in str(refused.value)
    assert provider._built_app is None


@pytest.mark.usefixtures("_clean_celery_env")
def test_the_default_backend_names_pass(
    _config_module: Callable[[str], None],
) -> None:
    _config_module(
        "".join(f"{key} = {value!r}\n" for key, value in FOREIGN_BACKEND_NAMES.items())
    )
    assert create_celery_taskqueue(_instance("aux"))._app is not None


@pytest.mark.usefixtures("_clean_celery_env")
@pytest.mark.parametrize("name", ["default", "aux"])
def test_the_sentinel_master_of_infra_reaches_kombu_with_the_confs_tuning(
    _config_module: Callable[[str], None], name: str
) -> None:
    """kombu reads ``master_name`` only from the transport options: the app is
    built with the INFRA option laid under what the conf carries, so a
    ``sentinel://`` broker connects (it failed closed before) and the conf's own
    tuning survives."""
    _config_module("broker_transport_options = {'socket_timeout': 9.0}\n")
    provider = create_celery_taskqueue(
        InstanceConfig(
            capability="taskqueue",
            name=name,
            driver="celery",
            options={
                "broker": _SENTINEL,
                "master_name": "infra-master",
                "backend": _SENTINEL,
                "backend_master_name": "infra-backend-master",
            },
        )
    )
    app = provider._app
    assert app.conf.broker_transport_options == {
        "master_name": "infra-master",
        "socket_timeout": 9.0,
    }
    assert app.conf.result_backend_transport_options == {
        "master_name": "infra-backend-master"
    }
    connection = app.connection_for_write()
    try:
        assert connection.transport_options["master_name"] == "infra-master"
    finally:
        connection.release()


@pytest.mark.usefixtures("_clean_celery_env")
async def test_a_memory_transport_never_swallows_a_publish(
    _config_module: Callable[[str], None],
) -> None:
    _config_module(_CONNECTION_REPROS["broker_transport"])
    provider = create_celery_taskqueue(
        InstanceConfig(
            capability="taskqueue",
            name="aux",
            driver="celery",
            options={"broker": "redis://127.0.0.1:6379/0"},
        )
    )
    with pytest.raises(ForeignCeleryConfError, match="broker_transport"):
        await provider.send("probe.task", b"{}", task_id="t-1")


@pytest.mark.usefixtures("_clean_celery_env")
@pytest.mark.parametrize("name", ["default", "aux"])
async def test_a_celery_variable_set_after_the_first_use_is_refused_on_the_next_send(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Celery re-reads ``CELERY_BROKER_URL`` on each ``conf.broker_url`` access and
    opens its producers lazily: the check of the first use alone would let a
    variable set later redirect the publish. Every access checks again."""
    provider = create_celery_taskqueue(_instance(name))
    assert provider._app is not None  # built and checked: nothing foreign yet
    monkeypatch.setenv("CELERY_BROKER_URL", _STRAY)
    with pytest.raises(ForeignCeleryConfError) as refused:
        await provider.send("probe.task", b"{}", task_id="t-2")
    assert "stray-secret" not in str(refused.value)
