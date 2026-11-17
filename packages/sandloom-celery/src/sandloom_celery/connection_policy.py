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
#: A Redis Sentinel master name: no space or control character, bounded.
_MASTER_NAME = re.compile(r"[A-Za-z0-9._:-]{1,64}")
#: An AWS region (``us-east-1``, ``us-gov-west-1``, ``cn-north-1``): kombu's SQS
#: transport builds the endpoint host from it.
_AWS_REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]{1,2}")
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
#: What ``predefined_queues`` of one queue may carry (``kombu.transport.SQS``).
_PREDEFINED_QUEUE_KEYS = frozenset(
    {
        "url",
        "access_key_id",
        "secret_access_key",
        "region",
        "backoff_policy",
        "backoff_tasks",
    }
)
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
    "gcs_bucket",
    "gcs_project",
    "cache_backend",
    "cache_backend_options",
)

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
    return [chunk.strip() for chunk in (url or "").split(";") if chunk.strip()]


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


def validate_connection(
    broker: str | None,
    backend: str | None,
    master_name: str | None,
    backend_master_name: str | None,
) -> None:
    """Refuse (``ValueError``, naming the option, never a value) an instance whose
    urls or masters cannot be connected safely.

    * a ``rediss://`` url without ``ssl_cert_reqs`` (or with a value kombu does not
      know): kombu then connects with ``CERT_NONE`` and only logs a warning, and
      Celery's result backend refuses the same url, so both refuse it here;
    * ``;``-separated urls of different schemes: kombu switches the transport with
      the url, and the transport options are allowed per scheme;
    * a ``sentinel://`` url without its master, or a master without a sentinel url.
    """
    for option, url, master_option, master in (
        ("broker", broker, "master_name", master_name),
        ("backend", backend, "backend_master_name", backend_master_name),
    ):
        schemes = {scheme_of(chunk) for chunk in _chunks(url)}
        if len(schemes) > 1:
            raise ValueError(f"the {option} url mixes schemes: use one scheme")
        for chunk in _chunks(url):
            _check_cert_reqs(option, chunk)
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


def _check_cert_reqs(option: str, chunk: str) -> None:
    if scheme_of(chunk) != "rediss":
        return
    values = parse_qs(urlsplit(chunk).query).get("ssl_cert_reqs", [])
    if not values or values[-1] not in _CERT_REQS:
        raise ValueError(
            f"the {option} url is a rediss:// url with no valid ssl_cert_reqs: add "
            "?ssl_cert_reqs=required to verify the server's certificate "
            "(without it kombu connects with no certificate check; CERT_NONE and "
            "CERT_OPTIONAL are accepted when stated)"
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
    options: object, allowed: frozenset[str], master_name: str | None
) -> bool:
    """Whether a ``*_transport_options`` value is a mapping of ``allowed`` keys.

    ``master_name`` is the one key whose value ``INFRA`` states: it is accepted
    only when it equals ``master_name`` (the option of the instance), so a conf
    cannot name another master. ``region``, ``queue_name_prefix`` and
    ``predefined_queues`` (SQS) are checked for their shape.
    """
    if not isinstance(options, Mapping):
        return False
    for key, value in options.items():
        if key == "master_name":
            safe = master_name is not None and value == master_name
        else:
            safe = key in allowed and _value_is_safe(key, value)
        if not safe:
            return False
    return True


def _value_is_safe(key: str, value: object) -> bool:
    """The shape of the values that name an AWS region, a queue or a queue map."""
    if key == "region":
        return _is_region(value)
    if key == "queue_name_prefix":
        return _is_queue_prefix(value)
    if key == "predefined_queues":
        return _predefined_queues_are_safe(value)
    return True


def _is_region(value: object) -> bool:
    return isinstance(value, str) and _AWS_REGION.fullmatch(value) is not None


def _is_queue_prefix(value: object) -> bool:
    return isinstance(value, str) and _QUEUE_NAME_PREFIX.fullmatch(value) is not None


def _predefined_queues_are_safe(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    for queue in value.values():
        if not isinstance(queue, Mapping) or not set(queue) <= _PREDEFINED_QUEUE_KEYS:
            return False
        region = queue.get("region")
        if region is not None and not _is_region(region):
            return False
        url = queue.get("url")
        if url is not None and not isinstance(url, str):
            return False
    return True


def infra_transport_options(
    existing: object, master_name: str | None
) -> Mapping[str, object] | None:
    """``existing`` (a conf's transport options) with the instance's sentinel master
    laid under it: a conf that names another master stays visible to the check
    (and is refused), never silently replaced. ``None`` when nothing is to add or
    ``existing`` is not a mapping (the check refuses that)."""
    if master_name is None or not isinstance(existing, Mapping | type(None)):
        return None
    return {"master_name": master_name, **(existing or {})}
