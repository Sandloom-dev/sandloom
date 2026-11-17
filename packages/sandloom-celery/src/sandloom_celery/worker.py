"""The Celery worker host of the ``celery`` taskqueue driver (``sandloom-celery``).

Everything Celery-specific about running tasks lives here, behind the port
(:class:`~sandloom.driverkit.WorkerHost`): the kernel names no Celery object.

* :class:`CeleryWorkerHost` — ``run_worker`` registers the kernel's task table on
  the Celery app and runs ``celery worker`` over it; ``run_scheduler`` runs
  ``celery beat`` only while the process holds the lease the kernel resolved;
  ``check`` reads the worker's heartbeat file.
* Celery bootsteps and signals: the heartbeat step (liveness probe, no broker
  round trip), ``worker_init`` (:meth:`WorkerBoot.before_fork`),
  ``worker_process_init`` / ``worker_process_shutdown``
  (:meth:`WorkerBoot.on_process_start` / ``on_process_stop``) and ``beat_init``
  (the lease wrapper of the scheduler).
* :func:`get_default_app`, the sole builder of the process-wide ``default``
  Celery app, which the provider (``sandloom_celery.taskqueue``) and this host
  share.

**The importable Celery app.** ``celery -A sandloom_celery.worker
inspect|call|status`` and Flower use the module attribute ``app`` (also
``celery`` and ``celery_app``): the ``default`` app, built lazily from the
options of the ``default`` taskqueue instance (``INFRA.taskqueue.default.broker``,
read through ``Settings`` by :mod:`sandloom.driverkit.process_config`; no result
backend unless the instance states one), with no composition root and no task
registered. A missing broker is an ``AttributeError`` naming that option, and so
is a ``CELERY_BROKER_URL`` / ``CELERY_RESULT_BACKEND`` in the environment that
differs from ``INFRA`` (Celery's CLI writes them from ``-b`` / ``BROKER_URL`` and
``app.conf`` prefers them). The finished ``app.conf`` (``broker_url``,
``broker_read_url``, ``broker_write_url``, ``result_backend``) is compared with
``INFRA`` too: a config module or the default loader can set them. It is the
driver's own interface, not a shim.

**Process options.** The port carries the log level and the scheduler's state
file (``state_file``: Celery beat's ``--schedule``, default ``celerybeat-schedule``
in the working directory); the Celery-specific sizing knobs
``WORKER_MAX_MEMORY_PER_CHILD`` and ``WORKER_MAX_TASKS_PER_CHILD`` and the
liveness file ``WORKER_HEARTBEAT_FILE`` are read here.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, Literal, NoReturn

import orjson

from sandloom.driverkit import (
    DEFAULT_INSTANCE,
    FencedLease,
    HealthResult,
    InfraError,
    InvalidArgument,
    Lease,
    LeaseStore,
    PeriodicSpec,
    TaskTable,
    Timeout,
    TooLarge,
    Unavailable,
    WorkerBoot,
)
from sandloom.driverkit.periodic import (
    CronSchedule,
    PeriodicSlot,
    SlotClaim,
    check_slot_lease_name,
    claim_periodic_slot,
    periodic_slot,
    slot_lease_name,
)
from sandloom_celery.connection_policy import (
    BACKEND_TRANSPORT_OPTIONS,
    BROKER_TRANSPORT_OPTIONS,
    FOREIGN_BACKEND_KEYS,
    FOREIGN_BACKEND_NAMES,
    FOREIGN_CONNECTION_KEYS,
    NO_QUEUE_PIN,
    SAFE_DATABASE_ENGINE_OPTIONS,
    SAFE_FAILOVER_STRATEGIES,
    ConnectionOptions,
    backend_name_is_foreign,
    backend_transport_keys,
    broker_transport_keys,
    infra_transport_options,
    transport_options_are_safe,
)

logger = logging.getLogger(__name__)

#: Recycle a prefork child above this resident size (KiB) after its task.
WORKER_MAX_MEMORY_ENV = "WORKER_MAX_MEMORY_PER_CHILD"
#: Recycle a prefork child after this many tasks.
WORKER_MAX_TASKS_ENV = "WORKER_MAX_TASKS_PER_CHILD"
#: Liveness heartbeat file (``sandloom worker check``).
HEARTBEAT_FILE_ENV = "WORKER_HEARTBEAT_FILE"
DEFAULT_HEARTBEAT_FILE = "/tmp/sandloom-worker-heartbeat"  # noqa: S108 - default liveness file; WORKER_HEARTBEAT_FILE overrides
HEARTBEAT_INTERVAL_SECONDS = 10.0
#: The pools of the ``worker_pool`` option (the first is the default): ``prefork``
#: forks child processes, ``solo`` runs the messages in the worker process
#: itself (inside Celery's event loop), ``threads`` on a thread pool of it.
WorkerPool = Literal["prefork", "solo", "threads"]
#: The operator's worker concurrency (the kernel resolves it; ``solo`` runs one
#: message at a time, so an explicit value above 1 is refused there).
WORKER_CONCURRENCY_ENV = "WORKER_CONCURRENCY"
#: The option the importable app reads its broker from.
_BROKER_OPTION = "taskqueue.default.broker"
#: Celery CLI global options whose click ``envvar`` (``APP``, ``BROKER_URL``,
#: ``RESULT_BACKEND``, ``LOADER``, ``CONFIG_MODULE``) must not reach a host run.
#: ``BROKER_URL`` is no setting of this driver, but the Celery CLI reads it: a stray
#: one in the environment must not replace the broker the instance states.
_CLI_ENV_OPTIONS = frozenset({"app", "broker", "result_backend", "loader", "config"})
#: Names ``app.conf`` / the loader read from ``os.environ`` on every use; the
#: host's app and options decide, so they are hidden for the run.
_CONF_ENV = (
    "CELERY_APP",
    "CELERY_BROKER_URL",
    "CELERY_BROKER_READ_URL",
    "CELERY_BROKER_WRITE_URL",
    "CELERY_RESULT_BACKEND",
    "CELERY_LOADER",
    "CELERY_CONFIG_MODULE",
)

#: Where ``app.conf`` reads the broker and the result backend from the
#: environment before it reads the app's own (``celery/app/utils.py``); the
#: importable app refuses a value other than ``INFRA``'s.
_BROKER_ENV = ("CELERY_BROKER_URL", "CELERY_BROKER_READ_URL", "CELERY_BROKER_WRITE_URL")
_BACKEND_ENV = "CELERY_RESULT_BACKEND"


class _DefaultApp:
    """The process-wide ``default`` Celery app, built lazily, and the connection
    (:class:`~sandloom_celery.connection_policy.ConnectionOptions`: broker,
    backend and sentinel masters) it was built from.

    A holder, never a module global: ``celery -A sandloom_celery.worker`` falls
    back to scanning ``vars(module)`` for any ``Celery`` instance once ``app`` and
    ``celery`` raise (``celery.app.utils.find_app``), so an app kept in a module
    attribute would be returned unchecked after the foreign-conf refusal, with its
    ``_sandloom_importable`` mark unset. No module-level name of this file holds a
    Celery instance."""

    app: Any | None = None
    connection: ConnectionOptions | None = None


_default = _DefaultApp()


def _celery_module() -> Any:
    """``celery``, imported where a command needs it: ``sandloom worker check``
    and a publish-only process import none of it."""
    return importlib.import_module("celery")


def _signals() -> Any:
    return importlib.import_module("celery.signals")


def _apply_framework_defaults(app: Any) -> Any:
    """Framework Celery defaults (shared by the enqueue side and the worker).

    ``task_ignore_result``: tasks store no result unless they opt out with
    ``@shared_task(ignore_result=False)`` — nothing in the framework reads
    task results, and storing each one costs a backend write + TTL key.
    """
    app.conf.task_ignore_result = True
    return app


def _apply_connection_options(app: Any, connection: ConnectionOptions) -> Any:
    """Hand the options ``INFRA`` states (the sentinel masters, TLS of an SQS
    endpoint) to kombu and Celery, which read them only from the transport
    options: laid under what the conf already carries, so a conf naming another
    value stays visible to the conf check (and is refused) instead of being
    replaced."""
    for option, stated in (
        (BROKER_TRANSPORT_OPTIONS, connection.broker_stated_options()),
        (BACKEND_TRANSPORT_OPTIONS, connection.backend_stated_options()),
    ):
        if not stated:
            # Reading the conf loads the config module: left to the first real
            # access, as before, when there is nothing to state.
            continue
        conf = app.conf
        merged = infra_transport_options(conf.get(option), stated)
        if merged is not None:
            conf[option] = merged
    return app


def get_default_app(connection: ConnectionOptions) -> Any:
    """Return the process-wide ``default`` Celery app (built once, from the
    ``default`` instance's connection options)."""
    if _default.app is None:
        _default.app = _new_default_app(connection)
        _default.connection = connection
    return _default.app


def _new_default_app(connection: ConnectionOptions) -> Any:
    # Not current yet: ``Celery(...)`` would make it ``celery.current_app`` at
    # construction, and an app refused by the conf check would stay current. The
    # caller makes it current (``app.set_current()``) once the check has passed.
    return _apply_connection_options(
        _apply_framework_defaults(
            _celery_module().Celery(
                "project",
                broker=connection.broker,
                backend=connection.backend,
                set_as_current=False,
            )
        ),
        connection,
    )


def default_app_matches(connection: ConnectionOptions) -> bool:
    """Whether the ``default`` instance may use the process-wide app: it is not
    built yet, or was built from this connection."""
    return _default.app is None or _default.connection == connection


def build_named_app(name: str, connection: ConnectionOptions) -> Any:
    """A Celery app of a named instance (its own broker and namespace). Not
    current, and never made so: the thread's ``celery.current_app`` (what an
    unbound ``@shared_task`` publishes through) stays the ``default`` instance's
    app, or whatever the process set."""
    return _apply_connection_options(
        _apply_framework_defaults(
            _celery_module().Celery(
                f"sandloom-{name}",
                broker=connection.broker,
                backend=connection.backend,
                set_as_current=False,
            )
        ),
        connection,
    )


def __getattr__(name: str) -> Any:
    # ``celery -A sandloom_celery.worker`` looks up ``app`` (or
    # ``celery``); building lazily keeps a plain import free of side effects.
    if name in {"app", "celery", "celery_app"}:
        if _default.app is not None and _default.connection is not None:
            # Built first by the provider (or by an earlier lookup): its options
            # are the ones it was built from, and the final conf is checked
            # against them just the same.
            app = _default.app
            _refuse_foreign_conf(app, _default.connection)
        else:
            connection = _importable_app_connection()
            # Built in a local and stored only once every check passed: a refused
            # app is neither the process-wide one nor reachable from the module,
            # and (built not current) never ``celery.current_app`` either.
            app = _new_default_app(connection)
            _refuse_foreign_conf(app, connection)
            _default.app = app
            _default.connection = connection
        # Current only after every step that can fail (the refusal's signal
        # wiring imports celery.signals): nothing half-set stays current.
        _refuse_unhosted_runs(app)
        app.set_current()
        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _refuse_foreign_conf(app: Any, connection: ConnectionOptions) -> None:
    """Refuse an importable app whose final ``app.conf`` names another broker or
    result backend than ``INFRA`` states.

    The environment check of :func:`_importable_app_connection` covers the variables
    Celery's CLI writes; the rest of what feeds ``app.conf`` it does not see: a
    config module (``celery --config``, ``CELERY_CONFIG_MODULE``), the default
    loader (``--loader default`` imports ``celeryconfig.py`` from the working
    directory) can still set ``broker_read_url``, ``broker_write_url`` and
    ``result_backend``. So the finished conf is read (which loads the config
    module) and compared: ``inspect``, ``call``, ``control`` and Flower would
    otherwise send to, or read from, a broker the operator did not configure.
    Celery falls back to ``broker_url`` for a read / write url left unset, and
    stores no backend when none is set. The message names the option, never its
    value: it is a URL, with a password."""
    option = _foreign_conf_option(app, connection)
    if option is not None:
        raise AttributeError(
            f"the importable Celery app's {option} "
            f"{_foreign_conf_reason(option, 'default')}"
        )


class ForeignCeleryConfError(RuntimeError):
    """The Celery app of a ``taskqueue`` instance resolves a broker or result
    backend other than the instance's ``INFRA`` options: a ``CELERY_*``
    environment variable, a config module or a loader set it, and Celery prefers
    those over the url the app was built with. Publishing would go to a broker
    the operator did not configure. Not a transient fault: nothing was sent."""


def refuse_foreign_app_conf(
    app: Any, connection: ConnectionOptions, instance: str
) -> None:
    """Refuse (:class:`ForeignCeleryConfError`) an app whose final ``app.conf``
    names another broker or result backend than the instance states: the enqueue
    side's twin of the importable app's check, for the app a provider publishes
    through. The message names the option and the instance, never a url: it
    carries a password."""
    option = _foreign_conf_option(app, connection)
    if option is not None:
        raise ForeignCeleryConfError(
            f"the Celery app of taskqueue instance {instance!r} resolves a {option} "
            f"that {_foreign_conf_reason(option, instance)}"
        )


# ``broker_use_ssl`` is the connection's TLS switch, and Celery hands its value to
# kombu as ``ssl=``. A dict is merged into the redis connection parameters AFTER
# the url: ``{"host": ..., "port": ..., "password": ..., "virtual_host": ...}``
# redirects the connection with the INFRA url untouched, and ``{"ssl_cert_reqs":
# ssl.CERT_NONE}`` turns certificate verification off for a ``rediss://`` url that
# states none. ``True`` is no safer: kombu then skips the TLS default of the
# ``rediss://`` scheme, fails to merge ``True`` into the parameters, swallows the
# error and connects in plain text, sending the url's password in the clear. Only
# absent, ``None`` or ``False`` (Celery's own default) is accepted, tested by
# identity; TLS comes from the scheme and the query of the INFRA url.
_BROKER_SSL_KEY = "broker_use_ssl"

# ``broker_transport_options`` and ``result_backend_transport_options`` are laid
# over the connection parameters by kombu's amqp transport
# (``pyamqp.establish_connection`` builds ``dict({host, userid, password, ssl,
# ...}, **transport_options)``), so a key there redirects an ``amqp://`` broker or
# turns its certificate checks off with the INFRA url untouched; the sentinel
# transport takes ``master_name`` and ``sentinel_kwargs`` from them, and the SQS
# one its region, queue map, ``is_secure`` and ``port``. An allow-list per scheme
# (``sandloom_celery.connection_policy``), not a deny-list: only keys that tune a
# connection without moving it are accepted, and the sentinel master only as the
# INFRA option states it. Any other key is refused, named only by the option that
# holds it.
_TRANSPORT_OPTIONS_KEYS = (BROKER_TRANSPORT_OPTIONS, BACKEND_TRANSPORT_OPTIONS)

# ``broker_failover_strategy`` is a name (``round-robin``, ``shuffle``) or a
# callable that yields the urls kombu switches to; ``database_engine_options`` is
# laid over the SQLAlchemy result backend's ``create_engine`` call
# (``connect_args`` / ``creator`` name the host). Both have an accepted shape.
_FAILOVER_KEY = "broker_failover_strategy"
_ENGINE_OPTIONS_KEY = "database_engine_options"


def _foreign_conf_reason(option: str, instance: str) -> str:
    """The shared tail of both refusals: why ``option`` is not accepted and what
    to do. One text for the importable app and the provider's app; it names the
    option and the INFRA option, never a value: a url carries a password."""
    if option == _BROKER_SSL_KEY:
        return (
            "is set to something but None or False: a dict is merged into the "
            f"broker connection after the url of taskqueue.{instance}.broker (host, "
            "port, password and ssl_cert_reqs included), and True makes a "
            "rediss:// connection fall back to plain text: a config module "
            "(`celery --config`, CELERY_CONFIG_MODULE) or a loader (`--loader "
            "default` reads celeryconfig.py) set it. Remove it, or state the TLS "
            "options in the query of the INFRA url"
        )
    if option in _TRANSPORT_OPTIONS_KEYS:
        return (
            "is not a mapping of tuning options, or holds a key that is none: "
            "kombu lays transport options over the connection built from the "
            f"url of taskqueue.{instance}.broker or backend (host, credentials "
            "and TLS of an amqp:// broker, the endpoint of an SQS one), so only "
            "the timeouts, keep-alive, pool size, key prefix, retry, visibility "
            "and queue-map options of the url's scheme are accepted "
            "(docs/modules/background/tasks.md lists them); the master of a "
            f"sentinel:// url is the INFRA option taskqueue.{instance}.master_name "
            "(backend_master_name for a backend), not a transport option: a "
            "config module (`celery --config`, CELERY_CONFIG_MODULE) or a "
            "loader (`--loader default` reads celeryconfig.py) set it. Remove "
            "the other keys, or state the connection in the INFRA url"
        )
    if option == _FAILOVER_KEY:
        return (
            "is neither unset, 'round-robin' nor 'shuffle': a callable yields the "
            f"urls kombu switches to, besides the one of taskqueue.{instance}.broker: "
            "a config module (`celery --config`, CELERY_CONFIG_MODULE) or a "
            "loader (`--loader default` reads celeryconfig.py) set it. Remove "
            "that setting, or state the broker in the INFRA url"
        )
    if option == _ENGINE_OPTIONS_KEY:
        return (
            "holds a key that is no pool setting (pool_pre_ping, pool_recycle, "
            "pool_size, max_overflow, pool_timeout): connect_args and creator reach "
            "the database driver's connect call and can name another host than the "
            f"url of taskqueue.{instance}.backend: a config module (`celery "
            "--config`, CELERY_CONFIG_MODULE) or a loader (`--loader default` "
            "reads celeryconfig.py) set it. Remove the other keys"
        )
    if option in FOREIGN_BACKEND_NAMES:
        return (
            "is set to something but its Celery default: it names the container, "
            "database or collection the result backend of "
            f"taskqueue.{instance}.backend stores to: a config module (`celery "
            "--config`, CELERY_CONFIG_MODULE) or a loader (`--loader default` "
            "reads celeryconfig.py) set it. Remove that setting, or state it in "
            "the INFRA url"
        )
    if option in FOREIGN_BACKEND_KEYS:
        return (
            f"is set, and INFRA states the result backend, whole, in the url of "
            f"taskqueue.{instance}.backend: a config module (`celery --config`, "
            "CELERY_CONFIG_MODULE) or a loader (`--loader default` reads "
            "celeryconfig.py) set it. Remove that setting, or state it in the "
            "INFRA url"
        )
    if option in FOREIGN_CONNECTION_KEYS:
        return (
            f"is set, and INFRA states the whole connection in the url of "
            f"taskqueue.{instance}.broker: a config module (`celery --config`, "
            "CELERY_CONFIG_MODULE) or a loader (`--loader default` reads "
            "celeryconfig.py) set it. Remove that setting, or state it in the "
            "INFRA url"
        )
    infra = "backend" if option == "result_backend" else "broker"
    return (
        f"is not what the INFRA option taskqueue.{instance}.{infra} states: a "
        "CELERY_BROKER_URL / CELERY_BROKER_READ_URL / CELERY_BROKER_WRITE_URL / "
        "CELERY_RESULT_BACKEND environment variable, a config module (`celery "
        "--config`, CELERY_CONFIG_MODULE) or a loader (`--loader default` reads "
        "celeryconfig.py) set it. Remove that setting, or state it in INFRA"
    )


def _is_unset(value: object) -> bool:
    """A conf value Celery treats as not set: ``None``, blank, or an empty
    mapping / list (the default of some dict-valued backend settings)."""
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, Mapping | list | tuple | set | frozenset):
        return not value
    return value is None


def _foreign_connection_setting(conf: Any) -> str | None:
    """The first connection part or result-backend setting of the conf that is set
    at all (or, for a ``FOREIGN_BACKEND_NAMES`` one, set to a non-default)."""
    for option in (*FOREIGN_CONNECTION_KEYS, *FOREIGN_BACKEND_KEYS):
        if not _is_unset(conf.get(option)):
            return option
    for option in FOREIGN_BACKEND_NAMES:
        if backend_name_is_foreign(option, conf.get(option)):
            return option
    return None


def _foreign_conf_option(app: Any, connection: ConnectionOptions) -> str | None:
    """The first of the app's final ``broker_url``, ``broker_read_url``,
    ``broker_write_url`` and ``result_backend`` that is not what ``connection``
    states, or of the connection parts of ``FOREIGN_CONNECTION_KEYS`` and the
    result-backend keys of ``FOREIGN_BACKEND_KEYS`` that is set at all, or a
    ``FOREIGN_BACKEND_NAMES`` setting that is not its default, or
    ``broker_use_ssl`` when it is anything but ``None`` / ``False``, or a
    ``broker_failover_strategy`` / ``database_engine_options`` of a shape that
    moves the connection, or a transport-options key outside the scheme's
    allow-list (:func:`_foreign_transport_option`); ``None`` when none."""
    conf = app.conf
    # Attribute access, not ``conf.get``: only the attribute applies the
    # ``broker_url`` fallback of a read / write url left unset.
    final = {
        "broker_url": (conf.broker_url, connection.broker),
        "broker_read_url": (conf.broker_read_url, connection.broker),
        "broker_write_url": (conf.broker_write_url, connection.broker),
        "result_backend": (conf.result_backend, connection.backend),
    }
    for option, (found, expected) in final.items():
        if isinstance(found, str):
            found = found.strip()
        if found != expected:
            return option
    foreign = _foreign_connection_setting(conf)
    if foreign is not None:
        return foreign
    # An allow-list, by identity: absent, None or False; never True (plain text on
    # a rediss:// url), a dict, or anything else.
    ssl_switch = conf.get(_BROKER_SSL_KEY)
    if ssl_switch is not None and ssl_switch is not False:
        return _BROKER_SSL_KEY
    strategy = conf.get(_FAILOVER_KEY)
    if strategy is not None and not (
        isinstance(strategy, str) and strategy in SAFE_FAILOVER_STRATEGIES
    ):
        return _FAILOVER_KEY
    engine = conf.get(_ENGINE_OPTIONS_KEY)
    if engine is not None and not (
        isinstance(engine, Mapping) and set(engine) <= SAFE_DATABASE_ENGINE_OPTIONS
    ):
        return _ENGINE_OPTIONS_KEY
    return _foreign_transport_option(conf, connection)


def _foreign_transport_option(conf: Any, connection: ConnectionOptions) -> str | None:
    """The first of :data:`_TRANSPORT_OPTIONS_KEYS` that is set to something but a
    mapping of the keys the scheme of its url allows (the sentinel master only as
    ``connection`` states it); ``None`` when none."""
    for option, allowed, stated, pin in (
        (
            BROKER_TRANSPORT_OPTIONS,
            broker_transport_keys(connection.broker),
            connection.broker_stated_options(),
            connection.queue_pin(),
        ),
        (
            BACKEND_TRANSPORT_OPTIONS,
            backend_transport_keys(connection.backend),
            connection.backend_stated_options(),
            NO_QUEUE_PIN,
        ),
    ):
        options = conf.get(option)
        if options is not None and not transport_options_are_safe(
            options, allowed, stated, pin
        ):
            return option
    return None


def _importable_app_connection() -> ConnectionOptions:
    """The connection of the importable app: the options of the ``default``
    taskqueue instance (``broker``, ``backend``, the sentinel masters and the SQS
    account), as the application resolves them (``INFRA`` through ``Settings``; no
    environment variable of this driver). The backend is ``None`` unless the
    instance states one.

    Celery's own CLI would replace them: ``celery -b`` (envvar ``BROKER_URL``) and
    ``--result-backend`` (``RESULT_BACKEND``) write ``CELERY_BROKER_URL`` and
    ``CELERY_RESULT_BACKEND`` into the environment before ``-A`` is resolved, and
    ``app.conf`` prefers those (and ``CELERY_BROKER_READ_URL`` /
    ``CELERY_BROKER_WRITE_URL``) over the app's own ``broker=``. So the importable
    app, which ``inspect``, ``call``, ``control`` and Flower use, refuses (an
    ``AttributeError`` naming the variable, never its value) a variable that
    differs from what ``INFRA`` states: it would send to, or read from, a broker
    the operator did not configure."""
    # Imported here: it pulls the kernel's infra registry, which a plain
    # ``import sandloom_celery.worker`` (the provider, the worker host) must not.
    from sandloom.driverkit.process_config import process_instance

    instance = process_instance("taskqueue", DEFAULT_INSTANCE)
    if instance is None or instance.driver != "celery":
        selected = "none" if instance is None else repr(instance.driver)
        raise AttributeError(
            "the importable Celery app reads the broker of the default taskqueue "
            f"instance, which must run the celery driver (here: {selected}): set "
            f"the INFRA option {_BROKER_OPTION} and select the celery driver"
        )
    broker = instance.options.get("broker")
    if not isinstance(broker, str) or not broker.strip():
        raise AttributeError(
            f"the importable Celery app needs the INFRA option {_BROKER_OPTION} "
            "(the broker of the default taskqueue instance)"
        )
    backend = instance.options.get("backend")
    if backend is not None and not isinstance(backend, str):
        raise AttributeError("the INFRA option taskqueue.default.backend must be a url")
    # The sentinel masters and the SQS account: optional string options.
    master_name, backend_master_name, sqs_account_id = (
        _optional_string_option(instance.options, option)
        for option in ("master_name", "backend_master_name", "sqs_account_id")
    )
    broker = broker.strip()
    backend = backend.strip() if backend else None
    # The urls and masters passed ``CeleryTaskQueueOptions`` (``process_instance``
    # validates the instance against the driver's schema, which runs
    # ``validate_connection``): an unverified ``rediss://`` url or a ``sentinel://``
    # one without its master never gets here.
    for key in _BROKER_ENV:
        _refuse_foreign_env(key, broker, f"the INFRA option {_BROKER_OPTION}")
    _refuse_foreign_env(
        _BACKEND_ENV, backend, "the INFRA option taskqueue.default.backend"
    )
    return ConnectionOptions(
        broker, backend, master_name, backend_master_name, sqs_account_id
    )


def _optional_string_option(options: Mapping[str, object], name: str) -> str | None:
    """The INFRA option ``name`` of the default taskqueue instance: a string, or
    ``None`` when unset."""
    value = options.get(name)
    if value is not None and not isinstance(value, str):
        raise AttributeError(
            f"the INFRA option taskqueue.default.{name} must be a string"
        )
    return value


def _refuse_foreign_env(key: str, stated: str | None, option: str) -> None:
    """Refuse a Celery environment variable that is set and not what ``option``
    states (``stated``, ``None`` for an option left out). Celery treats a blank
    value as unset, as does this. The message names the variable, never its
    value: it is a URL, with a password."""
    value = os.environ.get(key)
    if value and value != stated:
        raise AttributeError(
            f"{key} is set and is not {option}: Celery would use it instead of "
            "the configured one. Unset it (a `celery -b` / `--result-backend` "
            "option or a BROKER_URL / RESULT_BACKEND variable writes it), or "
            f"set it to what {option} states"
        )


def _refuse(message: str) -> NoReturn:
    """Stop the process with exit code 1. ``SystemExit`` is not an ``Exception``,
    so Celery's signal dispatch lets it through."""
    logger.critical("%s", message)
    raise SystemExit(1)


_refusal_connected = False


def _refuse_unhosted_runs(app: Any) -> Any:
    """Mark ``app`` as the importable one and make ``celery -A
    sandloom_celery.worker worker|beat`` refuse to start.

    The importable app registers no task (the task table is built by
    ``sandloom worker`` / ``sandloom beat``): a worker on it would consume
    messages and drop them as unknown tasks, a beat would run without the
    lease. A host run (:meth:`CeleryWorkerHost.run_worker` /
    :meth:`CeleryWorkerHost.run_scheduler`) marks its app hosted and passes.
    """
    global _refusal_connected
    app._sandloom_importable = True
    if not _refusal_connected:
        signals = _signals()
        signals.worker_init.connect(_refuse_worker_init, weak=False)
        signals.beat_init.connect(_refuse_beat_init, weak=False)
        _refusal_connected = True
    return app


def _is_unhosted_importable(sender: Any) -> bool:
    app = getattr(sender, "app", None)
    return (
        app is not None
        and getattr(app, "_sandloom_importable", False)
        and not getattr(app, "_sandloom_hosted", False)
    )


def _refuse_worker_init(sender: Any = None, **_: Any) -> None:
    if _is_unhosted_importable(sender):
        _refuse(
            "the importable Celery app runs no tasks (the task table is built "
            "by the kernel): start the worker with `sandloom worker`"
        )


def _refuse_beat_init(sender: Any = None, **_: Any) -> None:
    if _is_unhosted_importable(sender):
        _refuse(
            "the importable Celery app has no beat lease: "
            "start the scheduler with `sandloom beat`"
        )


# ---------------------------------------------------------------- app settings


def _positive_int_env(env: Any, key: str) -> int | None:
    raw = (env.get(key) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise InvalidArgument(
            f"{key} must be a positive integer, got {raw!r}", capability="taskqueue"
        )
    return value


def configure_worker_sizing(
    app: Any, *, concurrency: int | None, env: dict[str, str] | None = None
) -> None:
    """Apply the worker's sizing to the Celery app's config.

    - ``worker_concurrency``: ``concurrency`` (the kernel resolves it: the
      ``--concurrency`` flag, ``WORKER_CONCURRENCY`` or the CPU quota);
    - ``worker_max_memory_per_child`` (KiB): ``WORKER_MAX_MEMORY_PER_CHILD``,
      recycling a child that grew past it after its current task;
    - ``worker_max_tasks_per_child``: ``WORKER_MAX_TASKS_PER_CHILD``.
    """
    source = os.environ if env is None else env
    conf = app.conf
    if concurrency is not None:
        conf.worker_concurrency = concurrency
    max_mem = _positive_int_env(source, WORKER_MAX_MEMORY_ENV)
    if max_mem is not None:
        conf.worker_max_memory_per_child = max_mem
    max_tasks = _positive_int_env(source, WORKER_MAX_TASKS_ENV)
    if max_tasks is not None:
        conf.worker_max_tasks_per_child = max_tasks


def refuse_unusable_pool(pool: WorkerPool, env: dict[str, str] | None = None) -> None:
    """Refuse, before anything boots, a ``worker_pool`` the process cannot run
    as configured (:class:`InvalidArgument`, naming the option or setting):

    - ``prefork`` where billiard does not start its children with ``fork``
      (macOS: it spawns, and a spawned child never sees the boot-registered
      task table): set ``worker_pool`` to ``solo`` or ``threads``;
    - ``solo`` / ``threads`` with ``WORKER_MAX_MEMORY_PER_CHILD`` or
      ``WORKER_MAX_TASKS_PER_CHILD`` set: they recycle prefork children, and
      those pools have none;
    - ``solo`` with ``WORKER_CONCURRENCY`` above 1: it runs one message at a time.
    """
    source = os.environ if env is None else env
    if pool == "prefork":
        method = importlib.import_module("billiard").get_start_method()
        if method != "fork":
            raise InvalidArgument(
                "worker_pool 'prefork' needs billiard to fork its children, and "
                f"here it starts them with {method!r} (macOS): set worker_pool to "
                "'solo' or 'threads' in the taskqueue instance's options (INFRA), "
                "or run the worker on Linux",
                capability="taskqueue",
            )
        return
    for key in (WORKER_MAX_MEMORY_ENV, WORKER_MAX_TASKS_ENV):
        if _positive_int_env(source, key) is not None:
            raise InvalidArgument(
                f"{key} recycles prefork children and worker_pool {pool!r} has "
                "none: unset it or set worker_pool to 'prefork'",
                capability="taskqueue",
            )
    if pool == "solo":
        concurrency = _positive_int_env(source, WORKER_CONCURRENCY_ENV)
        if concurrency is not None and concurrency > 1:
            raise InvalidArgument(
                f"{WORKER_CONCURRENCY_ENV}={concurrency} but worker_pool 'solo' "
                "runs one message at a time: unset it, or set worker_pool to "
                "'threads' or 'prefork'",
                capability="taskqueue",
            )


def configure_worker_reliability(app: Any) -> None:
    """Framework delivery defaults of the worker's Celery app.

    - ``task_ignore_result=True``: a task stores no result — the framework
      never reads results, and each stored one is a backend write + TTL key;
    - ``worker_prefetch_multiplier=1``: a child reserves one message at a
      time, so a long task does not strand a prefetched backlog behind it (and
      a crashed child loses at most its own message's ack).

    ``acks_late`` stays OFF globally (Celery's at-most-once default: a task is
    acked when it starts). An IDEMPOTENT task opts in to at-least-once with
    ``@task(at_least_once=True)``: it is registered ``acks_late`` and
    ``reject_on_worker_lost``, acked only after it finished, and redelivered if
    the worker process dies mid-task — only safe when running it twice is
    harmless.
    """
    conf = app.conf
    conf.task_ignore_result = True
    conf.worker_prefetch_multiplier = 1


# ------------------------------------------------------------------- heartbeat


def heartbeat_path(env: dict[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return (source.get(HEARTBEAT_FILE_ENV) or "").strip() or DEFAULT_HEARTBEAT_FILE


def _touch(target: Path) -> None:
    try:
        target.touch()
    except OSError:  # pragma: no cover - read-only fs
        logger.warning("[worker] cannot touch heartbeat %s", target)


class _HeartbeatThread:
    """A daemon thread that calls ``beat`` every ``interval`` seconds until
    stopped (the heartbeat of a pool that runs messages inside the timer's
    loop)."""

    def __init__(self, beat: Callable[[], None], interval: float) -> None:
        self._beat = beat
        self._interval = interval
        self._halt = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="sandloom-heartbeat", daemon=True
        )

    def _run(self) -> None:
        while not self._halt.wait(self._interval):
            self._beat()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        self._thread.join(timeout=self._interval + 1)

    def is_alive(self) -> bool:
        return self._thread.is_alive()


def install_heartbeat(
    app: Any,
    path: str | None = None,
    *,
    thread: bool = False,
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
) -> None:
    """Touch a heartbeat file from the worker's own timer (liveness probe).

    The timer runs in the worker's main process event machinery, so the file
    goes stale when the worker is wedged or dead — not merely when a child is
    busy. ``sandloom worker check`` checks its age without booting the app (no
    broker round-trip, no second copy of the composition root inside the
    container's memory limit, unlike ``celery inspect ping``).

    ``thread=True`` (the ``solo`` and ``threads`` pools) touches the file from a
    daemon thread instead: ``solo`` runs a message inside the same event loop
    as the timer, so a task longer than the check window would starve the timer
    and ``sandloom worker check`` would declare a busy worker dead. The price:
    the file no longer shows a loop wedged by something other than a task.
    """
    bootsteps = importlib.import_module("celery.bootsteps")

    if getattr(app, "_sandloom_heartbeat_installed", False):
        return  # one heartbeat step per app, however often it is assembled
    target = Path(path or heartbeat_path())

    step_base: Any = bootsteps.StartStopStep

    class _Heartbeat(step_base):  # type: ignore[misc]
        requires = {("celery.worker.components:Timer")}  # noqa: RUF012 - celery bootstep API

        def __init__(self, parent: Any, **kwargs: Any) -> None:
            super().__init__(parent, **kwargs)
            self.tref: Any = None
            self.stopped = False
            self.ticker: _HeartbeatThread | None = None

        def _beat(self, *_: Any) -> None:
            if not self.stopped:
                _touch(target)

        def start(self, parent: Any) -> None:
            self._beat()
            if thread:
                self.ticker = _HeartbeatThread(self._beat, interval)
                self.ticker.start()
            else:
                self.tref = parent.timer.call_repeatedly(
                    interval, self._beat, (), priority=10
                )

        def stop(self, parent: Any) -> None:
            self.stopped = True
            if self.ticker is not None:
                self.ticker.stop()
            if self.tref is not None:
                self.tref.cancel()
            target.unlink(missing_ok=True)

    app.steps["worker"].add(_Heartbeat)
    app._sandloom_heartbeat_installed = True


def heartbeat_is_fresh(
    path: str | None = None,
    *,
    max_age: float,
    now: float | None = None,
) -> bool:
    """True when the heartbeat file exists and was touched within ``max_age``."""
    try:
        mtime = os.path.getmtime(path or heartbeat_path())
    except OSError:
        return False
    return ((time.time() if now is None else now) - mtime) <= max_age


# ------------------------------------------------------------------ task table


def _cron_fields(cron: str) -> dict[str, str]:
    minute, hour, day_of_month, month_of_year, day_of_week = cron.split()
    return {
        "minute": minute,
        "hour": hour,
        "day_of_month": day_of_month,
        "month_of_year": month_of_year,
        "day_of_week": day_of_week,
    }


def _celery_task_for(name: str, run: Callable[..., None]) -> Any:
    """The Celery task body of the declared task ``name``: its message's
    keyword arguments are the payload object, which ``run`` (the boot's
    ``run``) turns back into the payload model. Bound, for the message's id."""

    def body(self: Any, **payload: Any) -> None:
        run(name, orjson.dumps(payload), task_id=str(self.request.id or ""))

    # The body keeps its own fixed identifier: Celery builds the task signature
    # with exec() from __name__, so a name derived from the declared one breaks
    # on a segment that is not an identifier. The declared name reaches Celery
    # only as ``name=``.
    return body


def register_task_table(app: Any, table: TaskTable, run: Callable[..., None]) -> None:
    """Register ``table`` on the Celery ``app``: one task per entry (queue
    routing, ``acks_late`` and ``reject_on_worker_lost`` for ``at_least_once``)
    and the beat schedule of the periodic entries. A message runs
    ``run(name, payload, task_id=...)`` (``WorkerBoot.run``)."""
    routes: dict[str, dict[str, str]] = dict(app.conf.task_routes or {})
    for name, spec in table.tasks.items():
        app.task(
            name=name,
            bind=True,
            # Only this app's: a shared task joins every app finalized later.
            shared=False,
            acks_late=spec.at_least_once,
            reject_on_worker_lost=spec.at_least_once,
            ignore_result=True,
        )(_celery_task_for(name, run))
        if spec.queue != "default":
            routes[name] = {"queue": spec.queue}
    if routes:
        app.conf.task_routes = routes
    periodic = tuple(table.periodic)
    if periodic:
        app.on_after_finalize.connect(
            functools.partial(_add_periodic_tasks, periodic=periodic), weak=False
        )


def _add_periodic_tasks(
    sender: Any, *, periodic: Sequence[PeriodicSpec], **_: Any
) -> None:
    # Celery loads only where the beat is assembled (a module-level import would not).
    crontab = importlib.import_module("celery.schedules").crontab

    for spec in periodic:
        sender.add_periodic_task(
            crontab(**_cron_fields(spec.cron)),
            sender.signature(spec.task),
            name=spec.id,
        )


# ------------------------------------------------------------------ cron slots
#
# The scheduler deduplicates a run by (periodic id, slot) (spec 8.2). The slot
# math and the claim of a slot are the driverkit's (sandloom.driverkit.periodic);
# this is the adapter from a Celery ``crontab``.

_ALL_DAYS_OF_MONTH = frozenset(range(1, 32))
_ALL_DAYS_OF_WEEK = frozenset(range(7))


def cron_schedule_of(schedule: Any) -> CronSchedule | None:
    """The :class:`CronSchedule` of a ``celery.schedules.crontab`` (the zone is
    ``schedule.tz``), or ``None`` for one it cannot name a slot of: a
    day-of-month and a day-of-week that are both restricted (Celery ands them,
    a ``CronSchedule`` reads one)."""
    days_of_month = frozenset(schedule.day_of_month)
    days_of_week = frozenset(schedule.day_of_week)
    day_of_month_any = days_of_month == _ALL_DAYS_OF_MONTH
    day_of_week_any = days_of_week == _ALL_DAYS_OF_WEEK
    if not day_of_month_any and not day_of_week_any:
        return None
    minutes = frozenset(schedule.minute)
    hours = frozenset(schedule.hour)
    months = frozenset(schedule.month_of_year)
    expression = " ".join(
        ",".join(str(value) for value in sorted(values))
        for values in (minutes, hours, days_of_month, months, days_of_week)
    )
    return CronSchedule(
        expression=expression,
        minutes=minutes,
        hours=hours,
        days_of_month=days_of_month,
        months=months,
        days_of_week=days_of_week,
        day_of_month_any=day_of_month_any,
        day_of_week_any=day_of_week_any,
    )


_NOT_SENT_REASON = {
    "taken": "another scheduler sent it",
    "not_held": "the lease is not held",
    "refused": "the lease store refused the claim",
}


def _nothing_published(exc: BaseException) -> bool:
    """Is ``exc`` (or its cause) a refusal before anything reached the broker
    (``InvalidArgument``, ``TooLarge``)? Any other failure of a send, a broker
    timeout included, may have been published (spec 8.2): the claim stays."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, InvalidArgument | TooLarge):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


# ------------------------------------------------------------------ beat lease


class BeatLease:
    """The scheduler's lease: a synchronous facade over the kernel's
    :class:`~sandloom.driverkit.FencedLease` (held, not inherited).

    Beat's scheduler loop is synchronous; the lease's store operations run on
    the runner's loop (the boot's ``run_coroutine``: the persistent loop of the
    process, where the store is also disposed).
    """

    def __init__(
        self,
        store: LeaseStore,
        name: str,
        *,
        ttl_seconds: int,
        runner: Callable[[Coroutine[Any, Any, Any]], Any],
        clock: Callable[[], float] = time.monotonic,
        identity: str | None = None,
    ) -> None:
        self._identity = identity or f"{socket.gethostname()}:{os.getpid()}"
        self._holder = FencedLease(
            store,
            name,
            ttl_seconds=ttl_seconds,
            clock=clock,
            identity=self._identity,
            label="[beat]",
        )
        self._run = runner

    @property
    def name(self) -> str:
        return self._holder.name

    @property
    def held(self) -> bool:
        return self._holder.held

    @property
    def token(self) -> str | None:
        return self._holder.token

    @property
    def fence(self) -> int | None:
        return self._holder.fence

    @property
    def renew_interval(self) -> float:
        return self._holder.renew_interval

    def next_check_in(self) -> float:
        """Seconds until :meth:`ensure` has work to do (renew or retry)."""
        return self._holder.next_check_in()

    def ensure(self) -> bool:
        """Acquire or renew as due; ``True`` while this process may schedule."""
        fast = self._holder.fast_path()
        if fast is not None:
            return fast
        return bool(self._run(self._holder.ensure_async()))

    def open(self) -> None:
        """Open the store, so a backend that refuses to serve fails now, with
        its own message, instead of leaving beat on standby for good. A store
        that is only unreachable is retried by the holder."""
        try:
            opener = getattr(self._holder.store, "aopen", None)
            if opener is not None:
                self._run(opener())
        except (Unavailable, Timeout) as exc:
            logger.warning(
                "[beat] lease backend not reachable yet (%s); retrying",
                type(exc).__name__,
            )

    def release(self) -> None:
        """Release our own lease (best-effort) so a standby takes over at once."""
        if self.token is not None:
            self._run(self._holder.release_async())

    def claim_slot(
        self,
        periodic_id: str,
        schedule: CronSchedule,
        zone: tzinfo,
        slot: PeriodicSlot,
    ) -> SlotClaim:
        """Claim the run of ``periodic_id`` for ``slot`` (spec 8.2: a run is
        deduplicated by ``(id, slot)`` under the lease's fence).

        The claim is one lease name per periodic id
        (:func:`~sandloom.driverkit.periodic.slot_lease_name`), granted for the
        slot's lifetime: a second scheduler that held the scheduler lease for
        the same slot (after a takeover, or after a send that blocked past the
        lease TTL) is refused. Only a holder claims (``not_held`` otherwise: it
        must not send). A store that does not answer lets the slot through
        (``unknown``: ``periodic`` promises at least one run, a run may
        repeat); any other refusal sends nothing (``refused``).
        """
        if not self.ensure():
            return SlotClaim("not_held")
        name = slot_lease_name(self.name, periodic_id, schedule, zone)
        return self._run(  # type: ignore[no-any-return]
            claim_periodic_slot(
                self._holder.store,
                name,
                slot,
                holder=self._identity,
                fence=self.fence,
                limit_seconds=self.renew_interval,
            )
        )

    def release_slot(self, claim: SlotClaim) -> None:
        """Give a claimed slot back (best-effort): nothing was published, so
        another scheduler may send it."""
        if claim.lease is None:
            return
        lease = claim.lease
        try:
            self._run(self._release_slot_async(lease))
        except (InfraError, OSError):  # the claim expires with its slot anyway
            logger.warning("[beat] could not release the claim of slot %s", lease.name)

    async def _release_slot_async(self, lease: Lease) -> None:
        await asyncio.wait_for(
            self._holder.store.release(lease), timeout=self.renew_interval
        )


class LeasedScheduler:
    """Wrap a Celery beat scheduler: it ticks only while the lease is held, and
    sends each cron slot of a periodic task once (``(id, slot)`` dedupe under
    the lease, :meth:`BeatLease.claim_slot`).

    ``wall_clock`` is the current moment (aware); tests pass a fake one.
    """

    def __init__(
        self,
        inner: Any,
        lease: BeatLease,
        *,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._inner = inner
        self.lease = lease
        self._wall_clock = wall_clock
        send = getattr(inner, "apply_async", None)
        if send is not None:
            # Celery's beat sends a due entry through ``apply_async`` of its own
            # scheduler: the one place every send passes.
            self._send = send
            inner.apply_async = self._apply_async

    def _apply_async(
        self, entry: Any, producer: Any = None, advance: bool = True, **kwargs: Any
    ) -> Any:
        crontab = importlib.import_module("celery.schedules").crontab
        schedule: Any = getattr(entry, "schedule", None)
        cron = cron_schedule_of(schedule) if isinstance(schedule, crontab) else None
        slot = None
        if cron is not None:
            moment = self._wall_clock() if self._wall_clock else datetime.now(UTC)
            slot = periodic_slot(cron, schedule.tz, moment)
        if cron is None or slot is None:  # nothing to name a slot by
            return self._send(entry, producer, advance, **kwargs)
        claim = self.lease.claim_slot(entry.name, cron, schedule.tz, slot)
        if not claim.may_send:
            if advance:
                self._inner.reserve(entry)
            logger.warning(
                "[beat] %s slot %s not sent: %s",
                entry.name,
                slot.key,
                _NOT_SENT_REASON.get(claim.state, claim.state),
            )
            return None
        try:
            return self._send(entry, producer, advance, **kwargs)
        except Exception as exc:
            # A refusal before the broker saw anything frees the slot; any
            # other failure may have been published, so the claim stays.
            if _nothing_published(exc):
                self.lease.release_slot(claim)
            raise

    def tick(self, *args: Any, **kwargs: Any) -> float:
        if not self.lease.ensure():
            return self.lease.next_check_in()
        interval = self._inner.tick(*args, **kwargs)
        if interval and interval > 0:
            return min(float(interval), self.lease.next_check_in())
        return float(interval or 0)

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            self.lease.release()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def install_beat_lease(app: Any, lease: BeatLease) -> Callable[..., None]:
    """Wrap every beat scheduler of ``app`` in :class:`LeasedScheduler`.

    Connected to ``beat_init`` (sent by ``beat.Service.start`` before the first
    tick). Returns the receiver, for :func:`remove_beat_lease`.
    """
    signals = _signals()
    # Bound here: the identity check below narrows ``app`` inside the receiver.
    beat_clock = app.now

    def _on_beat_init(sender: Any = None, **_: Any) -> None:
        if sender is None or getattr(sender, "app", None) is not app:
            # A scheduler on another app would tick WITHOUT the lease: never run.
            _refuse(
                "the beat scheduler is not the host's app, so it would run "
                f"without the singleton lease {lease.name}: refusing to schedule"
            )
        scheduler = sender.scheduler
        if isinstance(scheduler, LeasedScheduler):
            return
        # The slot of a send is read on the clock the beat itself ticks on
        # (``app.now``), never a second one: with two clocks a tick due in one
        # minute could claim the slot of the next.
        sender.scheduler = LeasedScheduler(scheduler, lease, wall_clock=beat_clock)
        logger.info("[beat] singleton lease %s installed", lease.name)

    signals.beat_init.connect(_on_beat_init, weak=False)
    return _on_beat_init


def remove_beat_lease(receiver: Callable[..., None]) -> None:
    _signals().beat_init.disconnect(receiver)


# ------------------------------------------------------------------------ host


def _loglevel(requested: str) -> str:
    level = (requested or "INFO").strip().upper()
    return (
        level if level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO"
    )


def _process_hooks(
    signals: Any, boot: WorkerBoot, *, forks: bool
) -> list[tuple[Any, Callable[..., None]]]:
    """The ``(signal, receiver)`` pairs that call the boot's process hooks.

    ``prefork`` children announce themselves (``worker_process_init`` /
    ``worker_process_shutdown``). ``solo`` and ``threads`` run the messages in
    the worker process and send neither, so the host starts that process on
    ``worker_init`` and stops it on ``worker_shutdown``; no process is forked
    there, so ``before_fork`` (the kernel's GC freeze that children share) is
    not called."""

    def on_worker_init(**_: Any) -> None:
        if forks:
            boot.before_fork()
        else:
            boot.on_process_start()

    def on_process_init(**_: Any) -> None:
        boot.on_process_start()

    def on_process_shutdown(**_: Any) -> None:
        boot.on_process_stop()

    connected: list[tuple[Any, Callable[..., None]]] = [
        (signals.worker_init, on_worker_init),
        (signals.worker_process_init, on_process_init),
        (signals.worker_process_shutdown, on_process_shutdown),
    ]
    if not forks:
        connected.append((signals.worker_shutdown, on_process_shutdown))
    return connected


def _run_celery(app: Any, argv: Sequence[str]) -> int:
    """Run one Celery command (``["worker", ...]``, ``["beat", ...]``) bound to
    ``app`` and return its exit code.

    ``Celery.start()`` calls the same CLI but discards its return value, so a
    worker that died would exit 0 and its container would never restart.
    """
    celery_cli = importlib.import_module("celery.bin.celery").celery
    # The environment must not choose the app, the broker or the backend of a
    # process whose lease name and task table come from the host's app.
    # Celery reads it two ways: the CLI's global options (``APP``, ``BROKER_URL``,
    # ``RESULT_BACKEND``, ``LOADER``, ``CONFIG_MODULE``, as click ``envvar``) and
    # ``app.conf`` itself (``CELERY_BROKER_URL`` and the other ``CELERY_*``
    # names, on every read). The first is switched off on the command, the
    # second removed from ``os.environ`` for the run: Celery reads that mapping
    # directly, so a copy would not be seen.
    cli_options = [p for p in celery_cli.params if p.name in _CLI_ENV_OPTIONS]
    saved_envvars = {p.name: p.envvar for p in cli_options}
    saved_environ = {k: os.environ.pop(k) for k in _CONF_ENV if k in os.environ}
    args = list(argv)
    # The host's own broker and backend, passed explicitly (they win over every
    # default; the CLI mirrors them into ``CELERY_BROKER_URL`` for the run).
    broker = app.conf.broker_url
    if broker:
        args = ["--broker", str(broker), *args]
    backend = app.conf.result_backend
    if backend:
        args = ["--result-backend", str(backend), *args]
    for p in cli_options:
        p.envvar = None
    celery_cli.params[0].default = app
    try:
        # standalone_mode=False: click returns the exit code (``ctx.exit(code)``)
        # instead of calling sys.exit.
        code = celery_cli.main(args=args, standalone_mode=False)
    finally:
        celery_cli.params[0].default = None
        for p in cli_options:
            p.envvar = saved_envvars[p.name]
        for key in _CONF_ENV:
            os.environ.pop(key, None)
        os.environ.update(saved_environ)
    return code if isinstance(code, int) else 0


class CeleryWorkerHost:
    """The worker host of one Celery app (``CeleryTaskQueueProvider.worker_host``)."""

    def __init__(
        self, app_factory: Callable[[], Any], *, worker_pool: WorkerPool = "prefork"
    ) -> None:
        self._app_factory = app_factory
        self._worker_pool: WorkerPool = worker_pool

    @property
    def _app(self) -> Any:
        """The Celery app, resolved when a command needs it: ``check`` never
        does, so the container probe imports no Celery."""
        return self._app_factory()

    def run_worker(
        self,
        boot: WorkerBoot,
        *,
        queues: Sequence[str] | None = None,
        concurrency: int | None = None,
        loglevel: str = "INFO",
    ) -> int:
        pool = self._worker_pool
        # Refused before the kernel boots anything.
        refuse_unusable_pool(pool)
        signals = _signals()
        app = self._app
        app._sandloom_hosted = True
        register_task_table(app, boot.boot(), boot.run)
        # A solo pool runs one message at a time: Celery ignores a larger value.
        sized = None if pool == "solo" else concurrency
        configure_worker_sizing(app, concurrency=sized)
        configure_worker_reliability(app)
        install_heartbeat(app, thread=pool != "prefork")

        connected = _process_hooks(signals, boot, forks=pool == "prefork")
        for signal, receiver in connected:
            signal.connect(receiver, weak=False)
        argv = ["worker", f"--loglevel={_loglevel(loglevel)}", f"--pool={pool}"]
        if queues:
            argv.append(f"--queues={','.join(queues)}")
        if sized is not None:
            argv.append(f"--concurrency={sized}")
        try:
            return _run_celery(app, argv)
        finally:
            for signal, receiver in connected:
                signal.disconnect(receiver)

    def run_scheduler(
        self,
        boot: WorkerBoot,
        *,
        lease: LeaseStore,
        lease_name: str,
        lease_ttl_seconds: int = 30,
        loglevel: str = "INFO",
        state_file: str | None = None,
    ) -> int:
        try:
            check_slot_lease_name(lease_name)
        except ValueError as exc:
            # A name no store accepts for a slot would switch the dedupe off.
            logger.critical("[beat] refusing to schedule: %s", exc)
            return 1
        app = self._app
        app._sandloom_hosted = True
        register_task_table(app, boot.boot(), boot.run)
        configure_worker_reliability(app)
        beat_lease = BeatLease(
            lease, lease_name, ttl_seconds=lease_ttl_seconds, runner=boot.run_coroutine
        )
        try:
            beat_lease.open()
        except InfraError as exc:
            # A backend that refuses to serve (not merely unreachable) would
            # leave the scheduler on standby for good: stop before the first tick.
            logger.critical("[beat] refusing to schedule: %s", exc)
            return 1
        receiver = install_beat_lease(app, beat_lease)
        argv = ["beat", f"--loglevel={_loglevel(loglevel)}"]
        if state_file:
            argv.append(f"--schedule={state_file}")
        try:
            return _run_celery(app, argv)
        finally:
            remove_beat_lease(receiver)
            try:
                boot.on_process_stop()
            except Exception:  # best-effort teardown of the process's pools
                logger.warning("[beat] process teardown failed", exc_info=True)

    def check(self, *, timeout_seconds: float) -> HealthResult:
        """``ok`` when the worker's heartbeat file was touched within
        ``timeout_seconds``. Boots nothing and asks no broker; the details
        name the heartbeat file, never the broker."""
        path = heartbeat_path()
        if heartbeat_is_fresh(path, max_age=timeout_seconds):
            return HealthResult(status="ok", details={"heartbeat": path})
        return HealthResult(
            status="fail",
            details={"error": f"worker heartbeat {path} missing or stale"},
        )


__all__ = [
    "BeatLease",
    "CeleryWorkerHost",
    "ForeignCeleryConfError",
    "LeasedScheduler",
    "WorkerPool",
    "build_named_app",
    "configure_worker_reliability",
    "configure_worker_sizing",
    "default_app_matches",
    "get_default_app",
    "heartbeat_is_fresh",
    "heartbeat_path",
    "install_beat_lease",
    "install_heartbeat",
    "refuse_foreign_app_conf",
    "refuse_unusable_pool",
    "register_task_table",
    "remove_beat_lease",
]
