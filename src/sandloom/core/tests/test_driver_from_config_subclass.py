"""``from_config`` builds ``cls``: a driver subclass gets its own class back.

The kernel's in-tree drivers used to return the base class through a ``cast``;
a subclass that reused ``from_config`` silently lost its own methods.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sandloom.core.drivers.local_objectstore import (
    LocalObjectStoreProvider,
    create_local_objectstore,
)
from sandloom.core.drivers.log_notifier import (
    LogNotifierProvider,
    create_log_notifier,
    get_default_notifier,
)
from sandloom.core.drivers.smtp_notifier import (
    SmtpNotifierProvider,
    create_smtp_notifier,
)
from sandloom.core.infra.config import DEFAULT_INSTANCE, InstanceConfig


class _Log(LogNotifierProvider):
    pass


class _Smtp(SmtpNotifierProvider):
    pass


class _Store(LocalObjectStoreProvider):
    pass


def test_a_log_notifier_subclass_builds_itself() -> None:
    named = InstanceConfig("notification", "ops", "log")
    built = _Log.from_config(named)
    assert type(built) is _Log
    assert (built.name, built.driver) == ("ops", "log")
    assert type(LogNotifierProvider.from_config(named)) is LogNotifierProvider
    assert type(create_log_notifier(named)) is LogNotifierProvider


def test_the_default_log_notifier_stays_the_process_wide_one_for_the_base_class() -> (
    None
):
    default = InstanceConfig("notification", DEFAULT_INSTANCE, "log")
    assert LogNotifierProvider.from_config(default) is get_default_notifier()
    # a subclass is not the shared base instance: it is built, as itself
    built = _Log.from_config(default)
    assert type(built) is _Log
    assert built is not get_default_notifier()


def test_an_smtp_notifier_subclass_builds_itself() -> None:
    config = InstanceConfig("notification", "mail", "smtp", {"host": "smtp.example"})
    assert type(_Smtp.from_config(config)) is _Smtp
    assert type(SmtpNotifierProvider.from_config(config)) is SmtpNotifierProvider
    assert type(create_smtp_notifier(config)) is SmtpNotifierProvider


def test_the_smtp_build_still_refuses_an_empty_host_for_a_subclass() -> None:
    with pytest.raises(ValueError, match="non-empty host"):
        _Smtp.from_config(InstanceConfig("notification", "mail", "smtp", {}))


def test_a_local_objectstore_subclass_builds_itself(tmp_path: Path) -> None:
    config = InstanceConfig("objectstore", "files", "local", {"path": str(tmp_path)})
    assert type(_Store.from_config(config)) is _Store
    assert (
        type(LocalObjectStoreProvider.from_config(config)) is LocalObjectStoreProvider
    )
    assert type(create_local_objectstore(config)) is LocalObjectStoreProvider
