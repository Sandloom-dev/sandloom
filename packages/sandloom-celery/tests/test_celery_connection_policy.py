"""The connection policy of a ``celery`` taskqueue instance
(``sandloom_celery.connection_policy``): what ``INFRA`` may state for TLS and the
sentinel master, refused at startup and in the importable app alike."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sandloom_celery import worker as celery_worker
from sandloom_celery.connection_policy import (
    ConnectionOptions,
    broker_transport_keys,
    scheme_of,
    transport_options_are_safe,
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


_HOSTLESS = "sqs://KEY:SECRET@"
_ACCOUNT = "123456789012"


def _queues_are_safe(
    url: str,
    broker: str = _HOSTLESS,
    account: str | None = None,
    **options: object,
) -> bool:
    """The conf check of an SQS broker (what ``worker`` runs) for a conf whose
    ``predefined_queues`` holds one queue at ``url``."""
    connection = ConnectionOptions(broker, sqs_account_id=account)
    return transport_options_are_safe(
        {"predefined_queues": {"q": {"url": url}}, **options},
        broker_transport_keys(connection.broker),
        connection.broker_stated_options(),
        connection.queue_pin(),
    )


@pytest.mark.parametrize(
    "url",
    [
        f"https://sqs.eu-west-1.amazonaws.com/{_ACCOUNT}/q",
        f"https://sqs.eu-west-1.amazonaws.com/{_ACCOUNT}/q.fifo",
        f"https://sqs.cn-north-1.amazonaws.com.cn/{_ACCOUNT}/q",
        f"https://sqs.us-gov-west-1.amazonaws.com/{_ACCOUNT}/q",
        # FIPS (botocore ``endpoints.json``: ``sqs-fips.<region>.amazonaws.com``)
        f"https://sqs-fips.us-east-1.amazonaws.com/{_ACCOUNT}/q",
        # VPC interface endpoint, regional and zonal DNS names (AWS PrivateLink guide)
        f"https://vpce-0a1b2c3d4e5f67890-abcd1234.sqs.eu-west-1.vpce.amazonaws.com/{_ACCOUNT}/q",
        f"https://vpce-0a1b2c3d4e5f67890-abcd1234-eu-west-1a.sqs.eu-west-1.vpce.amazonaws.com/{_ACCOUNT}/q",
    ],
)
def test_an_aws_sqs_queue_url_is_accepted(url: str) -> None:
    assert _queues_are_safe(url)


@pytest.mark.parametrize(
    "url",
    [
        f"http://sqs.eu-west-1.amazonaws.com/{_ACCOUNT}/q",
        f"https://sqs.eu-west-1.amazonaws.com.stray.example/{_ACCOUNT}/q",
        f"https://stray.example/sqs.eu-west-1.amazonaws.com/{_ACCOUNT}/q",
        f"https://sqs.eu-west-1.amazonaws.com:8443/{_ACCOUNT}/q",
        f"https://u:p@sqs.eu-west-1.amazonaws.com/{_ACCOUNT}/q",
        f"https://sqs.eu-west-1.amazonaws.com/{_ACCOUNT}/q\n",
        f"https://stray.sqs.eu-west-1.amazonaws.com/{_ACCOUNT}/q",
        f"https://sqs-fips.eu-west-1.amazonaws.com.stray.example/{_ACCOUNT}/q",
        # the China regions have no FIPS endpoint
        f"https://sqs-fips.cn-north-1.amazonaws.com.cn/{_ACCOUNT}/q",
        # a trailing dot, the legacy and the dual-stack names are none of the forms
        f"https://sqs.eu-west-1.amazonaws.com./{_ACCOUNT}/q",
        f"https://eu-west-1.queue.amazonaws.com/{_ACCOUNT}/q",
        f"https://sqs.eu-west-1.api.aws/{_ACCOUNT}/q",
        # a VPC endpoint of another service, another suffix, or a dot in its label
        f"https://vpce-0a1b2c3d-abcd1234.s3.eu-west-1.vpce.amazonaws.com/{_ACCOUNT}/q",
        f"https://vpce-0a1b2c3d-abcd1234.sqs.eu-west-1.vpce.stray.example/{_ACCOUNT}/q",
        f"https://vpce-0a1b2c3d.stray.example.sqs.eu-west-1.vpce.amazonaws.com/{_ACCOUNT}/q",
        "https://sqs.eu-west-1.amazonaws.com/1/q",
    ],
)
def test_a_queue_url_that_is_not_an_aws_https_sqs_url_is_refused(url: str) -> None:
    assert not _queues_are_safe(url)


@pytest.mark.parametrize(
    ("broker", "regions"),
    [
        ("sqs://K:S@sqs.eu-west-1.amazonaws.com", {"eu-west-1"}),
        ("sqs://K:S@sqs-fips.us-east-1.amazonaws.com", {"us-east-1"}),
        ("sqs://K:S@sqs.cn-north-1.amazonaws.com.cn", {"cn-north-1"}),
        (
            "sqs://K:S@vpce-0a1b2c3d-abcd1234-eu-west-1a.sqs.eu-west-1.vpce.amazonaws.com",
            {"eu-west-1"},
        ),
        (
            "sqs://K:S@sqs.eu-west-1.amazonaws.com;sqs://K:S@sqs.us-east-1.amazonaws.com",
            {"eu-west-1", "us-east-1"},
        ),
        # an emulator or a proxy names no region, and neither does a url without a host
        ("sqs://K:S@localhost:4566", set()),
        ("sqs://K:S@", set()),
        ("sqs://K:S@sqs.eu-west-1.amazonaws.com.stray.example", set()),
        ("redis://sqs.eu-west-1.amazonaws.com", set()),
    ],
)
def test_the_regions_infra_states_come_from_the_aws_hosts_of_an_sqs_url(
    broker: str, regions: set[str]
) -> None:
    assert ConnectionOptions(broker).queue_pin().regions == regions


@pytest.mark.parametrize(
    "host",
    [
        "sqs.eu-west-1.amazonaws.com",
        "sqs-fips.us-east-1.amazonaws.com",
        "sqs.cn-north-1.amazonaws.com.cn",
        "vpce-0a1b2c3d-abcd1234.sqs.eu-west-1.vpce.amazonaws.com",
        # an emulator or a proxy: no AWS domain, no region, nothing to pin
        "localhost:4566",
        "elasticmq.internal",
        "sqs.eu-west-1.amazonaws.com.stray.example",
        # no host at all
        "",
    ],
)
def test_an_sqs_host_that_names_a_region_or_no_aws_domain_is_accepted(
    host: str,
) -> None:
    validate_connection(ConnectionOptions(f"sqs://K:S@{host}"))


@pytest.mark.parametrize(
    "broker",
    [
        "redis://sqs.eu-west-1.api.aws",
        "redis://sqs.eu-west-1.amazonaws.com.",
        "amqp://u:p@queue.amazonaws.com//",
    ],
)
def test_the_sqs_host_checks_read_only_an_sqs_url(broker: str) -> None:
    """Another scheme has no region pin: its host is not this check's business."""
    validate_connection(ConnectionOptions(broker))


@pytest.mark.parametrize(
    "host",
    [
        # a trailing dot: the host is the same, its region is not read
        "sqs.eu-west-1.amazonaws.com.",
        "elasticmq.internal.",
        # the legacy and the dual-stack names
        "eu-west-1.queue.amazonaws.com",
        "queue.amazonaws.com",
        "sqs.eu-west-1.api.aws",
        # no FIPS endpoint in China
        "sqs-fips.cn-north-1.amazonaws.com.cn",
        # an AWS domain of no SQS form
        "stray.eu-west-1.amazonaws.com",
        "sqs.eu-west-1.vpce.amazonaws.com",
        # kombu decodes these into the real AWS host: the raw text names no region
        "sqs.us-east-1.amazonaws%2Ecom",
        "sqs.us-east-1.amazonaws.com%2E",
        "sqs.us-east-1.amazonaws。com",
        "sqs.us-east-1.amazonaws.com。",
        "elasticmq%2Einternal",
    ],
)
def test_an_infra_sqs_host_that_sets_no_region_pin_is_refused(host: str) -> None:
    """The queue-url pin reads the region out of the INFRA host: a host of the AWS
    domains that it cannot read would leave the pin silently off."""
    good = "sqs://K:S@sqs.us-east-1.amazonaws.com"
    for broker in (
        f"sqs://KEY:infra-secret@{host}",
        f"{good};sqs://KEY:infra-secret@{host}",
    ):
        with pytest.raises(ValueError, match="SQS host") as refused:
            validate_connection(ConnectionOptions(broker))
        assert "infra-secret" not in str(refused.value)
        with pytest.raises(ValidationError, match="SQS host"):
            CeleryTaskQueueOptions.model_validate({"broker": broker})


@pytest.mark.parametrize(
    "host", ["sqs.us-east-1.amazonaws%2Ecom", "sqs.us-east-1.amazonaws。com"]
)
def test_the_queue_pin_never_reads_an_encoded_host_as_no_region(host: str) -> None:
    """A pin built from an encoded host would be empty, and any region would pass."""
    with pytest.raises(ValueError, match="SQS host"):
        ConnectionOptions(f"sqs://K:S@{host}").queue_pin()


def test_the_sqs_hosts_of_one_broker_url_name_one_region() -> None:
    """With hosts of two regions the pin would hold both, and the conf region and a
    queue url could each take a different one."""
    broker = (
        "sqs://KEY:infra-secret@sqs.eu-west-1.amazonaws.com;"
        "sqs://KEY:infra-secret@sqs-fips.us-east-1.amazonaws.com"
    )
    with pytest.raises(ValueError, match="more than one AWS region") as refused:
        validate_connection(ConnectionOptions(broker))
    assert "infra-secret" not in str(refused.value)
    with pytest.raises(ValidationError, match="more than one AWS region"):
        CeleryTaskQueueOptions.model_validate({"broker": broker})
    # two hosts of one region (a regional and a VPC endpoint) pass
    validate_connection(
        ConnectionOptions(
            "sqs://K:S@sqs.eu-west-1.amazonaws.com;"
            "sqs://K:S@vpce-0a1b-abcd1234.sqs.eu-west-1.vpce.amazonaws.com"
        )
    )


def test_a_queue_url_is_pinned_to_the_region_of_the_infra_host() -> None:
    host = "sqs://K:S@sqs.eu-west-1.amazonaws.com"
    assert _queues_are_safe(f"https://sqs.eu-west-1.amazonaws.com/{_ACCOUNT}/q", host)
    # a queue of the same account in another region
    assert not _queues_are_safe(
        f"https://sqs.us-east-1.amazonaws.com/{_ACCOUNT}/q", host
    )
    # FIPS and VPC endpoint urls of the INFRA region pass, of another one do not
    for form in (
        "sqs-fips.eu-west-1.amazonaws.com",
        "vpce-0a1b-abcd1234.sqs.eu-west-1.vpce.amazonaws.com",
    ):
        assert _queues_are_safe(f"https://{form}/{_ACCOUNT}/q", host)
        assert not _queues_are_safe(
            f"https://{form.replace('eu-west-1', 'us-east-1')}/{_ACCOUNT}/q", host
        )


def test_a_queue_url_follows_the_conf_region_when_infra_names_none() -> None:
    url = f"https://sqs.eu-west-1.amazonaws.com/{_ACCOUNT}/q"
    assert _queues_are_safe(url, region="eu-west-1")
    assert not _queues_are_safe(url, region="us-east-1")
    # no region anywhere: kombu takes it from the environment, which is not read
    assert _queues_are_safe(url)


def test_the_conf_region_must_be_the_region_of_the_infra_host() -> None:
    url = f"https://sqs.eu-west-1.amazonaws.com/{_ACCOUNT}/q"
    host = "sqs://K:S@sqs.eu-west-1.amazonaws.com"
    assert _queues_are_safe(url, host, region="eu-west-1")
    assert not _queues_are_safe(url, host, region="us-east-1")


def test_a_queue_url_is_pinned_to_the_account_infra_states() -> None:
    mine = f"https://sqs.eu-west-1.amazonaws.com/{_ACCOUNT}/q"
    other = "https://sqs.eu-west-1.amazonaws.com/999999999999/q"
    assert _queues_are_safe(mine, account=_ACCOUNT)
    assert not _queues_are_safe(other, account=_ACCOUNT)
    # not stated: any account passes
    assert _queues_are_safe(other)
    vpce = f"https://vpce-0a1b-abcd1234.sqs.eu-west-1.vpce.amazonaws.com/{_ACCOUNT}/q"
    assert _queues_are_safe(vpce, account=_ACCOUNT)
    assert not _queues_are_safe(vpce, account="999999999999")


@pytest.mark.parametrize(
    "options",
    [
        {"broker": "redis://127.0.0.1/0", "sqs_account_id": _ACCOUNT},
        {"broker": _HOSTLESS, "sqs_account_id": "12345"},
        {"broker": _HOSTLESS, "sqs_account_id": f"{_ACCOUNT}\n"},
        {"broker": _HOSTLESS, "sqs_account_id": "stray-account"},
    ],
)
def test_the_sqs_account_is_twelve_digits_and_belongs_to_an_sqs_broker(
    options: dict[str, str],
) -> None:
    with pytest.raises(ValidationError, match="sqs_account_id") as refused:
        CeleryTaskQueueOptions.model_validate(options)
    assert "KEY:SECRET" not in str(refused.value)


def test_an_sqs_broker_takes_the_account_of_infra() -> None:
    options = CeleryTaskQueueOptions.model_validate(
        {"broker": _HOSTLESS, "sqs_account_id": _ACCOUNT}
    )
    assert options.sqs_account_id == _ACCOUNT


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


def test_the_importable_app_takes_the_sqs_account_from_infra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _infra(monkeypatch, broker=_HOSTLESS, sqs_account_id=_ACCOUNT)
    assert celery_worker.app is not None
    assert celery_worker._default.connection == ConnectionOptions(
        _HOSTLESS, None, None, None, _ACCOUNT
    )


def test_the_importable_app_refuses_a_non_string_sqs_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _infra(monkeypatch, broker=_HOSTLESS, sqs_account_id=7)
    with pytest.raises(ConfigSchemaError, match="sqs_account_id"):
        celery_worker.app  # noqa: B018


#: A netloc character that NFKC-normalizes to a url delimiter makes ``urlsplit``
#: raise a ``ValueError`` that carries the whole netloc, password included.
#: The fullwidth forms of ``:`` ``@`` ``/`` ``?`` ``#``.
_COLON, _AT, _SLASH, _QUESTION, _HASH = (
    chr(c) for c in (0xFF1A, 0xFF20, 0xFF0F, 0xFF1F, 0xFF03)
)
_UNPARSABLE = [
    f"redis://u:pw-secret{_COLON}x@127.0.0.1:6379/0",
    f"redis://u:pw-secret{_AT}x@127.0.0.1:6379/0",
    f"rediss://:pw-secret{_SLASH}x@127.0.0.1:6380/0?ssl_cert_reqs=none",
    f"sqs://AKIA:pw-secret{_QUESTION}x@sqs.us-east-1.amazonaws.com/",
    f"amqp://guest:pw-secret{_HASH}x@127.0.0.1//",
    f"redis://127.0.0.1/0;redis://u:pw-secret{_COLON}x@127.0.0.2/0",
]


def _assert_fixed_message(refused: ValueError) -> None:
    assert "pw-secret" not in str(refused)
    assert _COLON not in str(refused)
    assert "cannot be parsed" in str(refused)
    assert refused.__cause__ is None
    assert refused.__suppress_context__


@pytest.mark.parametrize("url", _UNPARSABLE[:5])
def test_the_scheme_of_an_unparsable_url_raises_a_fixed_message(url: str) -> None:
    with pytest.raises(ValueError) as refused:
        scheme_of(url)
    _assert_fixed_message(refused.value)
    with pytest.raises(ValueError) as refused:
        broker_transport_keys(url)
    _assert_fixed_message(refused.value)


@pytest.mark.parametrize("url", _UNPARSABLE)
def test_validate_connection_refuses_an_unparsable_url_with_a_fixed_message(
    url: str,
) -> None:
    for connection in (
        ConnectionOptions(url),
        ConnectionOptions(_HOSTLESS, url),
    ):
        with pytest.raises(ValueError) as refused:
            validate_connection(connection)
        _assert_fixed_message(refused.value)


_BAD_SQS = f"sqs://AKIA:pw-secret{_COLON}x@sqs.us-east-1.amazonaws.com/"


@pytest.mark.parametrize(
    "broker",
    [_BAD_SQS, f"sqs:///;{_BAD_SQS}", f"sqs://sqs.us-east-1.amazonaws.com/;{_BAD_SQS}"],
    ids=["first", "hostless-first", "second"],
)
def test_the_sqs_host_reads_of_an_unparsable_url_raise_a_fixed_message(
    broker: str,
) -> None:
    for call in (
        lambda: ConnectionOptions(broker).queue_pin(),
        lambda: validate_connection(ConnectionOptions(broker)),
    ):
        with pytest.raises(ValueError) as refused:
            call()
        _assert_fixed_message(refused.value)


def test_the_stated_options_of_an_unparsable_sqs_url_raise_a_fixed_message() -> None:
    with pytest.raises(ValueError) as refused:
        ConnectionOptions(f"sqs:///;{_BAD_SQS}").broker_stated_options()
    _assert_fixed_message(refused.value)


@pytest.mark.parametrize("url", _UNPARSABLE)
def test_an_unparsable_url_never_reaches_a_validation_error(url: str) -> None:
    with pytest.raises(ValidationError) as refused:
        create_celery_taskqueue(
            InstanceConfig(
                capability="taskqueue",
                name="aux",
                driver="celery",
                options={"broker": url},
            )
        )
    assert "pw-secret" not in str(refused.value)
    assert "cannot be parsed" in str(refused.value)


def test_an_unparsable_backend_never_reaches_a_validation_error() -> None:
    url = f"redis://u:pw-secret{_COLON}x@127.0.0.1:6379/1"
    with pytest.raises(ValidationError) as refused:
        create_celery_taskqueue(
            InstanceConfig(
                capability="taskqueue",
                name="aux",
                driver="celery",
                options={"broker": "redis://127.0.0.1:6379/0", "backend": url},
            )
        )
    assert "pw-secret" not in str(refused.value)
    assert "cannot be parsed" in str(refused.value)


def test_the_importable_app_never_echoes_an_unparsable_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _infra(monkeypatch, broker=_UNPARSABLE[0])
    with pytest.raises(Exception) as refused:
        celery_worker.app  # noqa: B018
    assert "pw-secret" not in str(refused.value)
    assert "cannot be parsed" in str(refused.value)


def test_a_plain_ipv6_broker_still_parses() -> None:
    validate_connection(ConnectionOptions("redis://[::1]:6379/0"))
    assert scheme_of("redis://[::1]:6379/0") == "redis"
