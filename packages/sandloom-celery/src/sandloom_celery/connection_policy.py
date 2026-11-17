"""What a ``celery`` taskqueue instance's connection is made of, and what a Celery
conf may add to it.

``INFRA.taskqueue.<name>`` states the connection of an instance whole: the
``broker`` and ``backend`` urls, and for a ``sentinel://`` url the ``master_name``
and ``backend_master_name`` options. A Celery conf (a config module, ``celery
--config``, the default loader's ``celeryconfig.py``) reaches kombu, Celery and
redis-py next to those urls, and several of its keys win over what the url says.
This module is the policy of that boundary, in one place: the checks of the
instance's own options (:func:`validate_connection`), the transport options a
conf may carry for each scheme (:func:`broker_transport_keys`,
:func:`backend_transport_keys`, :func:`transport_options_are_safe`) and the
result-backend settings that carry a connection (:data:`FOREIGN_BACKEND_KEYS`).
``design/infra-drivers/celery-conf-audit.md`` is the audit behind every entry,
key by key, with the line of the kombu / Celery / redis-py source that reads it.

Messages name the option and never a value: a url carries a password.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

#: Schemes of the kombu Redis transports (the ``rediss`` one speaks TLS).
REDIS_SCHEMES = frozenset({"redis", "rediss"})
SENTINEL_SCHEME = "sentinel"
#: Schemes of kombu's py-amqp transport (``amqps`` is its TLS variant).
AMQP_SCHEMES = frozenset({"amqp", "amqps", "pyamqp"})
SQS_SCHEMES = frozenset({"sqs"})

#: How kombu and Celery's redis backend spell ``ssl_cert_reqs`` in a url query
#: (``kombu.utils.url.parse_ssl_cert_reqs``, ``celery.backends.redis``).
_CERT_REQS = frozenset(
    {"CERT_REQUIRED", "CERT_OPTIONAL", "CERT_NONE", "required", "optional", "none"}
)
#: ``ssl_cert_reqs`` values that ask for no certificate check at all.
_NO_VERIFY = frozenset({"CERT_NONE", "none"})
#: ``ssl_check_hostname`` values that switch the hostname check on (kombu and
#: redis-py read anything but ``false`` / ``0`` as true: only these are accepted).
_TRUE = frozenset({"true", "1", "yes", "on"})
#: A Redis Sentinel master name: no space or control character, bounded.
_MASTER_NAME = re.compile(r"[A-Za-z0-9._:-]{1,64}")
#: An AWS region (``us-east-1``, ``us-gov-west-1``, ``cn-north-1``): kombu's SQS
#: transport builds the endpoint host from it.
_REGION = r"[a-z]{2}(?:-[a-z]+)+-[0-9]{1,2}"
_AWS_REGION = re.compile(_REGION)
#: The 12 digits of an AWS account id.
_ACCOUNT_ID = re.compile(r"[0-9]{12}")
#: An AWS SQS host, named for its region (botocore's ``endpoints.json`` and the
#: PrivateLink guide): ``sqs.<region>.amazonaws.com`` (``.amazonaws.com.cn`` in
#: China), the FIPS ``sqs-fips.<region>.amazonaws.com`` (the China regions have no
#: FIPS endpoint) and the interface endpoint of a VPC,
#: ``vpce-<id>-<suffix>.sqs.<region>.vpce.amazonaws.com`` (the zonal name adds
#: ``-<zone>`` to the label). Nothing else: no port, userinfo, trailing dot or other
#: domain. Exactly one of the three region groups matches.
_SQS_HOST = (
    rf"(?:sqs\.(?P<region>{_REGION})\.amazonaws\.com(?:\.cn)?"
    rf"|sqs-fips\.(?P<fips_region>{_REGION})\.amazonaws\.com"
    rf"|vpce-[a-z0-9-]{{1,60}}\.sqs\.(?P<vpce_region>{_REGION})"
    r"\.vpce\.amazonaws\.com(?:\.cn)?)"
)
_SQS_HOST_RE = re.compile(_SQS_HOST)
#: Domains of AWS hosts: an SQS host under one of them that :data:`_SQS_HOST` does not
#: spell (a trailing dot, the legacy ``<region>.queue.amazonaws.com``, the dual-stack
#: ``sqs.<region>.api.aws``, a FIPS name in China) names no region, so the queue-url
#: pin would be off: an ``INFRA`` broker url with one is refused.
_AWS_DOMAINS = (".amazonaws.com", ".amazonaws.com.cn", ".api.aws")
#: The prefix of SQS queue names (queue names allow letters, digits, ``-``, ``_``).
_QUEUE_NAME_PREFIX = re.compile(r"[A-Za-z0-9_-]{0,64}")

# Transport options of every scheme: how a connection is retried and polled, which
# moves nothing. ``visibility_timeout`` is read by every virtual transport (redis,
# SQS, ...) and ignored by the rest.
_GENERIC = frozenset(
    {
        "visibility_timeout",
        "polling_interval",
        "retry_policy",
        "max_retries",
        "interval_start",
        "interval_step",
        "interval_max",
    }
)
# ``kombu.transport.redis.Channel.from_transport_options`` (and the virtual
# channel's): socket and pool tuning, key prefixes and the redelivery bookkeeping.
# None of them names a host, a credential or TLS: those come from the url.
_REDIS = _GENERIC | {
    "sep",
    "global_keyprefix",
    "priority_steps",
    "queue_order_strategy",
    "socket_timeout",
    "socket_connect_timeout",
    "socket_keepalive",
    "socket_keepalive_options",
    "max_connections",
    "health_check_interval",
    "retry_on_timeout",
    "client_name",
    "fanout_prefix",
    "fanout_patterns",
    "ack_emulation",
    "unacked_key",
    "unacked_index_key",
    "unacked_mutex_key",
    "unacked_mutex_expire",
    "unacked_restore_limit",
}
# py-amqp's ``Connection`` takes the transport options as keyword arguments laid over
# ``host`` / ``userid`` / ``password`` / ``ssl`` / ``virtual_host``: only the ones
# that tune the socket and the channel are allowed.
_AMQP = _GENERIC | {
    "confirm_publish",
    "read_timeout",
    "write_timeout",
    "socket_settings",
    "channel_max",
    "frame_max",
}
# ``kombu.transport.SQS``: the region and the queue map are how an SQS broker is
# addressed (the url carries the credentials and, optionally, an endpoint host).
_SQS = _GENERIC | {
    "wait_time_seconds",
    "region",
    "queue_name_prefix",
    "predefined_queues",
}
#: What ``predefined_queues`` of one queue may carry (``kombu.transport.SQS``): the
#: queue url and the backoff tuning. ``access_key_id``, ``secret_access_key`` and
#: ``region`` of a queue replace the credentials of the INFRA url and the region of
#: the transport (``SQS.py:769-781``), so tasks would go to another AWS account.
_PREDEFINED_QUEUE_KEYS = frozenset({"url", "backoff_policy", "backoff_tasks"})
#: The url of an SQS queue (``https://<SQS host>/<account>/<name>``, the host as
#: :data:`_SQS_HOST` spells it): TLS, an AWS host, nothing else.
_SQS_QUEUE_URL = re.compile(
    rf"https://(?P<host>{_SQS_HOST})/(?P<account>[0-9]{{12}})"
    r"/[A-Za-z0-9_-]{1,80}(?:\.fifo)?"
)


def _host_region(host: str | None) -> str | None:
    """The region an AWS SQS host names, ``None`` for any other host (the one place
    that reads a region out of a host)."""
    match = _SQS_HOST_RE.fullmatch(host) if host else None
    if match is None:
        return None
    return match["region"] or match["fips_region"] or match["vpce_region"]


@dataclass(frozen=True)
class QueuePin:
    """What a ``predefined_queues`` url and the SQS ``region`` must match: the
    regions the ``INFRA`` broker url's AWS hosts name (none when the url has no AWS
    host: the conf's own ``region`` is the pin then) and the account ``INFRA``
    states (``None``: not pinned)."""

    regions: frozenset[str] = frozenset()
    account: str | None = None


NO_QUEUE_PIN = QueuePin()
#: Read from ``result_backend_transport_options`` by Celery's redis backend
#: (``retry_policy``, ``result_chord_ordered``) and by any backend
#: (``global_keyprefix``, ``celery.backends.base``).
_BACKEND_ANY = frozenset({"result_chord_ordered", "global_keyprefix", "retry_policy"})
#: The sentinel transports read these from the transport options; ``master_name``
#: comes from ``INFRA`` (:class:`ConnectionOptions`), never from the conf.
_SENTINEL_ONLY = frozenset({"min_other_sentinels"})

# What the result backends take from the conf where their url omits the part, or
# lets the conf override it (``celery.backends.*``): redis host, port, database,
# credentials, TLS options and credential provider; the settings dicts of the
# MongoDB, Couchbase and ArangoDB backends; Cassandra's servers, port, keyspace
# and authentication; S3's endpoint, region and credentials; GCS's bucket and
# project; the cache backend's url; the SQLAlchemy backend's url. INFRA states the
# whole backend in its url, so none of them may be set at all.
FOREIGN_BACKEND_KEYS = (
    "redis_host",
    "redis_port",
    "redis_db",
    "redis_username",
    "redis_password",
    "redis_backend_use_ssl",
    "redis_backend_credential_provider",
    "database_url",
    "mongodb_backend_settings",
    "couchbase_backend_settings",
    "arangodb_backend_settings",
    "cassandra_servers",
    "cassandra_bundle_path",
    "cassandra_port",
    "cassandra_keyspace",
    "cassandra_table",
    "cassandra_auth_provider",
    "cassandra_auth_kwargs",
    "cassandra_options",
    "s3_endpoint_url",
    "s3_region",
    "s3_access_key_id",
    "s3_secret_access_key",
    "s3_bucket",
    "s3_base_path",
    "gcs_bucket",
    "gcs_project",
    "gcs_base_path",
    "firestore_project",
    "dynamodb_endpoint_url",
    "azureblockblob_base_path",
    "cache_backend",
    "cache_backend_options",
)

#: Backend settings that name a container, database or collection and have a
#: default of their own (``celery.app.defaults``): they pass at their default and
#: are refused with any other value, which would store results elsewhere.
FOREIGN_BACKEND_NAMES = {
    "azureblockblob_container_name": "celery",
    "cosmosdbsql_database_name": "celerydb",
    "cosmosdbsql_collection_name": "celerycol",
}

# Connection parts Celery hands to kombu next to the url, where they win over what
# the url omits (``broker_transport`` and ``broker_host`` even over what it states):
# a config module setting one redirects the publish (``broker_transport = "memory"``
# loses every task into an in-process queue) with the url itself untouched. INFRA
# states the whole connection in the url, so none of them may be set at all.
FOREIGN_CONNECTION_KEYS = (
    "broker_transport",
    "broker_host",
    "broker_port",
    "broker_vhost",
    "broker_user",
    "broker_password",
)

#: The conf options that carry transport options, one per role.
BROKER_TRANSPORT_OPTIONS = "broker_transport_options"
BACKEND_TRANSPORT_OPTIONS = "result_backend_transport_options"

#: ``database_engine_options`` has a non-empty default (``pool_pre_ping``,
#: ``pool_recycle``) and its ``connect_args`` / ``creator`` reach the DBAPI
#: connect call, which can name another host: only the pool tuning is allowed.
SAFE_DATABASE_ENGINE_OPTIONS = frozenset(
    {"pool_pre_ping", "pool_recycle", "pool_size", "max_overflow", "pool_timeout"}
)
#: ``broker_failover_strategy`` names how kombu cycles the hosts of the url; a
#: callable yields urls of its own choosing.
SAFE_FAILOVER_STRATEGIES = frozenset({"round-robin", "shuffle"})


def scheme_of(url: str | None) -> str:
    """The transport a kombu / Celery url selects: the scheme of its first ``;``
    chunk, lower-cased, before a ``+`` (``sqla+mysql://`` is ``sqla``); ``""`` for
    no url."""
    if not url:
        return ""
    return urlsplit(url.split(";")[0].strip()).scheme.lower().split("+")[0]


def _chunks(url: str | None) -> list[str]:
    """The ``;``-separated urls as kombu splits them: raw, never stripped (a blank
    url is no url: the caller refuses a missing broker by its own message)."""
    return url.split(";") if url and url.strip() else []


def _sqs_host(chunk: str) -> str | None:
    """The host of one chunk of an ``sqs://`` broker url, ``None`` for none.

    Refused (``ValueError``, never the url): a host with a percent escape or a
    non-ASCII character. Kombu hands the host to botocore decoded (``%2E`` is a dot,
    U+3002 an ideographic full stop that IDNA reads as one), so such a host names a
    real AWS endpoint while the region pin, reading the raw text, would see none.
    """
    host = urlsplit(chunk).hostname
    if host and ("%" in host or not host.isascii()):
        raise ValueError(
            "the broker url has an SQS host with a percent escape or a non-ASCII "
            "character: write the host in plain ASCII"
        )
    return host


def _has_host(url: str | None) -> bool:
    return any(urlsplit(chunk).hostname for chunk in _chunks(url))


@dataclass(frozen=True)
class ConnectionOptions:
    """The connection of one instance, as ``INFRA`` states it: what the Celery app
    is built from and what the conf is compared with."""

    broker: str
    backend: str | None = None
    #: The Redis master of a ``sentinel://`` ``broker`` (kombu reads it only from
    #: the transport options).
    master_name: str | None = None
    #: The same, for a ``sentinel://`` ``backend``.
    backend_master_name: str | None = None
    #: The AWS account of the queues of an ``sqs://`` ``broker`` (12 digits): a
    #: ``predefined_queues`` url of a conf must belong to it. ``None``: not pinned.
    sqs_account_id: str | None = None

    def queue_pin(self) -> QueuePin:
        """What the SQS queue urls and ``region`` of a conf must match: the regions
        the AWS hosts of an ``sqs://`` ``broker`` url name, and the account. A host
        that :func:`validate_connection` refuses for its spelling raises here too
        (``ValueError``): its region is not read, so no pin could be built."""
        if scheme_of(self.broker) not in SQS_SCHEMES:
            return NO_QUEUE_PIN
        regions = {
            region
            for chunk in _chunks(self.broker)
            if (region := _host_region(_sqs_host(chunk)))
        }
        return QueuePin(frozenset(regions), self.sqs_account_id)

    def broker_stated_options(self) -> Mapping[str, object]:
        """The ``broker_transport_options`` that ``INFRA`` states: the sentinel
        master, and TLS for an ``sqs://`` url with a host (kombu builds the
        endpoint ``http://host`` unless ``is_secure`` is true, ``SQS.py:923-934``).
        A conf may repeat them, never change them."""
        stated: dict[str, object] = {}
        if self.master_name is not None:
            stated["master_name"] = self.master_name
        if scheme_of(self.broker) in SQS_SCHEMES and _has_host(self.broker):
            stated["is_secure"] = True
        return stated

    def backend_stated_options(self) -> Mapping[str, object]:
        """The ``result_backend_transport_options`` that ``INFRA`` states."""
        if self.backend_master_name is None:
            return {}
        return {"master_name": self.backend_master_name}


def validate_connection(connection: ConnectionOptions) -> None:
    """Refuse (``ValueError``, naming the option, never a value) an instance whose
    urls or masters cannot be connected safely.

    * a ``rediss://`` url without ``ssl_cert_reqs`` (or with a value kombu does not
      know): kombu then connects with ``CERT_NONE`` and only logs a warning, and
      Celery's result backend refuses the same url, so both refuse it here; and
      one that verifies the certificate without ``ssl_check_hostname=true``
      (redis-py checks no name by default, so any certificate of the CA passes);
    * ``;``-separated urls of different schemes, or with an empty or space-padded
      chunk (kombu splits on ``;`` without stripping and a padded chunk fails
      with the password in the error): kombu switches the transport with the url,
      and the transport options are allowed per scheme;
    * a ``sentinel://`` url without its master, or a master without a sentinel url;
    * an ``sqs_account_id`` that is not 12 digits, or one with a broker that is not
      an ``sqs://`` url;
    * an ``sqs://`` broker url whose AWS host names no region the queue-url pin can
      read (:func:`_check_sqs_hosts`).
    """
    _check_sqs_account(connection)
    _check_sqs_hosts(connection)
    for option, url, master_option, master in (
        ("broker", connection.broker, "master_name", connection.master_name),
        (
            "backend",
            connection.backend,
            "backend_master_name",
            connection.backend_master_name,
        ),
    ):
        chunks = _chunks(url)
        if any(not chunk or chunk != chunk.strip() for chunk in chunks):
            raise ValueError(
                f"the {option} url has an empty chunk or a space around a ';' or "
                "an end: remove it"
            )
        schemes = {scheme_of(chunk) for chunk in chunks}
        if len(schemes) > 1:
            raise ValueError(f"the {option} url mixes schemes: use one scheme")
        for chunk in chunks:
            _check_tls(option, chunk)
        sentinel = schemes == {SENTINEL_SCHEME}
        if sentinel and master is None:
            raise ValueError(
                f"the {option} url is a sentinel:// url: set the option "
                f"{master_option} (the name of the Redis master)"
            )
        if not sentinel and master is not None:
            raise ValueError(
                f"the option {master_option} is for a sentinel:// {option} url"
            )
        if master is not None and not _MASTER_NAME.fullmatch(master):
            raise ValueError(
                f"the option {master_option} is 1 to 64 letters, digits, "
                "'.', '_', ':' or '-'"
            )


def _check_sqs_account(connection: ConnectionOptions) -> None:
    account = connection.sqs_account_id
    if account is None:
        return
    if scheme_of(connection.broker) not in SQS_SCHEMES:
        raise ValueError("the option sqs_account_id is for an sqs:// broker url")
    if not _ACCOUNT_ID.fullmatch(account):
        raise ValueError("the option sqs_account_id is the 12 digits of an AWS account")


def _check_sqs_hosts(connection: ConnectionOptions) -> None:
    """Refuse an ``sqs://`` broker url with a host under an AWS domain that
    :data:`_SQS_HOST` does not spell, or with a trailing dot: the region of such a
    host is not read, so a ``predefined_queues`` url would not be pinned to it. An
    emulator or a proxy host (no AWS domain) is not refused: it names no region."""
    if scheme_of(connection.broker) not in SQS_SCHEMES:
        return
    for chunk in _chunks(connection.broker):
        host = _sqs_host(chunk)
        if not host or _host_region(host):
            continue
        if host.endswith(".") or ("." + host).endswith(_AWS_DOMAINS):
            raise ValueError(
                "the broker url has an SQS host the driver cannot read a region "
                "from: use sqs.<region>.amazonaws.com, sqs-fips.<region>."
                "amazonaws.com or a vpce-<id>.sqs.<region>.vpce.amazonaws.com "
                "interface endpoint (no trailing dot, no <region>.queue."
                "amazonaws.com, no sqs.<region>.api.aws)"
            )


def _check_tls(option: str, chunk: str) -> None:
    if scheme_of(chunk) != "rediss":
        return
    query = parse_qs(urlsplit(chunk).query)
    reqs = query.get("ssl_cert_reqs", [])
    if not reqs or reqs[-1] not in _CERT_REQS:
        raise ValueError(
            f"the {option} url is a rediss:// url with no valid ssl_cert_reqs: add "
            "?ssl_cert_reqs=required&ssl_check_hostname=true to verify the server's "
            "certificate and its hostname (without ssl_cert_reqs kombu connects "
            "with no check; none states that none is wanted)"
        )
    if reqs[-1] in _NO_VERIFY:
        return
    hostname = query.get("ssl_check_hostname", [])
    if not hostname or hostname[-1].lower() not in _TRUE:
        raise ValueError(
            f"the {option} url is a rediss:// url that checks the certificate but "
            "not the hostname of the server: add &ssl_check_hostname=true (redis-py "
            "does not check the name by default, so any certificate of the CA passes)"
        )


def broker_transport_keys(url: str | None) -> frozenset[str]:
    """The ``broker_transport_options`` keys a conf may carry for ``url``'s scheme
    (the sentinel master excepted: see :func:`transport_options_are_safe`)."""
    scheme = scheme_of(url)
    if scheme == SENTINEL_SCHEME:
        return _REDIS | _SENTINEL_ONLY
    if scheme in REDIS_SCHEMES:
        return _REDIS
    if scheme in AMQP_SCHEMES:
        return _AMQP
    if scheme in SQS_SCHEMES:
        return _SQS
    return _GENERIC


def backend_transport_keys(url: str | None) -> frozenset[str]:
    """The ``result_backend_transport_options`` keys a conf may carry for the
    backend ``url``'s scheme: the redis backend's own, and the broker's redis
    tuning keys that it ignores (accepted since they move nothing)."""
    scheme = scheme_of(url)
    if scheme == SENTINEL_SCHEME:
        return _REDIS | _SENTINEL_ONLY | _BACKEND_ANY
    if scheme in REDIS_SCHEMES:
        return _REDIS | _BACKEND_ANY
    return _GENERIC | _BACKEND_ANY


def transport_options_are_safe(
    options: object,
    allowed: frozenset[str],
    stated: Mapping[str, object],
    pin: QueuePin = NO_QUEUE_PIN,
) -> bool:
    """Whether a ``*_transport_options`` value is a mapping of ``allowed`` keys.

    The keys of ``stated`` (:meth:`ConnectionOptions.broker_stated_options`) are the
    ones whose value ``INFRA`` states: each is accepted only with that value, so a
    conf cannot name another master or turn TLS off. ``region``,
    ``queue_name_prefix`` and ``predefined_queues`` (SQS) are checked for their
    shape and against ``pin`` (:meth:`ConnectionOptions.queue_pin`): the region of
    the ``INFRA`` url's AWS host (else the conf's own ``region``) and the account
    ``INFRA`` states.
    """
    if not isinstance(options, Mapping):
        return False
    conf_region = options.get("region")
    if not pin.regions and isinstance(conf_region, str) and _is_region(conf_region):
        # INFRA names no region: the conf's own is the pin (it equals ``region``
        # below, which then passes by its shape alone).
        pin = QueuePin(frozenset({conf_region}), pin.account)
    for key, value in options.items():
        if key in stated:
            safe = value == stated[key]
        else:
            safe = key in allowed and _value_is_safe(key, value, pin)
        if not safe:
            return False
    return True


def _value_is_safe(key: str, value: object, pin: QueuePin) -> bool:
    """The shape of the values that name an AWS region, a queue or a queue map:
    the region and the queue urls against ``pin``."""
    if key == "region":
        return _is_region(value) and (not pin.regions or value in pin.regions)
    if key == "queue_name_prefix":
        return _is_queue_prefix(value)
    if key == "predefined_queues":
        return _predefined_queues_are_safe(value, pin)
    return True


def _is_region(value: object) -> bool:
    return isinstance(value, str) and _AWS_REGION.fullmatch(value) is not None


def _is_queue_prefix(value: object) -> bool:
    return isinstance(value, str) and _QUEUE_NAME_PREFIX.fullmatch(value) is not None


def _queue_url_is_pinned(url: object, pin: QueuePin) -> bool:
    """Whether ``url`` is an SQS queue url in a region of ``pin`` (any when it has
    none) and in its account (any when it names none)."""
    match = _SQS_QUEUE_URL.fullmatch(url) if isinstance(url, str) else None
    if match is None:
        return False
    if pin.regions and _host_region(match["host"]) not in pin.regions:
        return False
    return pin.account is None or match["account"] == pin.account


def _predefined_queues_are_safe(value: object, pin: QueuePin) -> bool:
    if not isinstance(value, Mapping):
        return False
    for queue in value.values():
        if not isinstance(queue, Mapping) or not set(queue) <= _PREDEFINED_QUEUE_KEYS:
            return False
        if not _queue_url_is_pinned(queue.get("url"), pin):
            return False
        policy = queue.get("backoff_policy")
        if policy is not None and not isinstance(policy, Mapping):
            return False
        tasks = queue.get("backoff_tasks")
        if tasks is not None and not (
            isinstance(tasks, list | tuple) and all(isinstance(t, str) for t in tasks)
        ):
            return False
    return True


def infra_transport_options(
    existing: object, stated: Mapping[str, object]
) -> Mapping[str, object] | None:
    """``existing`` (a conf's transport options) with the options ``INFRA`` states
    laid under it: a conf that names another value stays visible to the check
    (and is refused), never silently replaced. ``None`` when nothing is to add or
    ``existing`` is not a mapping (the check refuses that)."""
    if not stated or not isinstance(existing, Mapping | type(None)):
        return None
    return {**stated, **(existing or {})}


def backend_name_is_foreign(option: str, value: object) -> bool:
    """Whether a result-backend setting of :data:`FOREIGN_BACKEND_NAMES` is set to
    anything but its default (or left unset)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return False
    return value != FOREIGN_BACKEND_NAMES[option]
