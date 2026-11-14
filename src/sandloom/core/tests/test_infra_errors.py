"""The shared infrastructure error hierarchy (ADR-0016, spec 2.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

import sandloom.core.infra.contracts
import sandloom.driverkit
import sandloom.sdk.infra
from sandloom.core.drivers.local_objectstore import create_local_objectstore
from sandloom.core.infra import errors
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import (
    InfraError,
    InvalidArgument,
    NotFound,
    Timeout,
    TooLarge,
    Unavailable,
    Unsupported,
)
from sandloom.core.infra.registry import FeatureUnsupportedError

_ERRORS = tuple(getattr(errors, name) for name in errors.__all__)


def test_every_error_is_an_infra_error_and_no_builtin_exception() -> None:
    for error in _ERRORS:
        assert issubclass(error, InfraError)
        builtins = {base for base in error.__mro__ if base.__module__ == "builtins"}
        assert builtins == {Exception, BaseException, object}, error


def test_only_transient_errors_are_retryable() -> None:
    retryable = {error.__name__ for error in _ERRORS if error.retryable}
    assert retryable == {"Unavailable", "Timeout"}


def test_an_error_names_its_capability_and_instance() -> None:
    exc = Unavailable("down", capability="cache", instance="sessions")
    assert (exc.capability, exc.instance, str(exc)) == ("cache", "sessions", "down")
    refused = Unsupported("cas", capability="cache", instance="sessions")
    assert refused.feature == "cas"
    assert "cas" in str(refused) and "cache/sessions" in str(refused)


def test_the_call_time_error_is_not_the_load_time_refusal() -> None:
    assert not issubclass(Unsupported, FeatureUnsupportedError)
    assert not issubclass(FeatureUnsupportedError, InfraError)


def test_both_doors_export_the_same_classes() -> None:
    for name in errors.__all__:
        assert getattr(sandloom.sdk.infra, name) is getattr(errors, name)
        assert getattr(sandloom.driverkit, name) is getattr(errors, name)
        assert name in sandloom.sdk.infra.__all__
        assert name in sandloom.driverkit.__all__


def test_the_replaced_objectstore_errors_are_gone() -> None:
    for gone in ("InvalidObjectKeyError", "UploadTooLargeError", "ObjectTooLargeError"):
        assert not hasattr(sandloom.driverkit, gone)
        assert not hasattr(sandloom.core.infra.contracts, gone)


@pytest.mark.asyncio
async def test_the_local_object_store_raises_the_contract_errors(
    tmp_path: Path,
) -> None:
    store = create_local_objectstore(
        InstanceConfig(
            "objectstore",
            "files",
            "local",
            {"path": str(tmp_path), "max_upload_size_bytes": 4},
        )
    )
    with pytest.raises(NotFound) as missing:
        await store.get("absent/object.bin")
    assert missing.value.capability == "objectstore"
    await store.put("dir/object.bin", b"abc")
    with pytest.raises(NotFound):
        await store.get("dir")  # a prefix is not an object
    with pytest.raises(NotFound):
        await store.get("dir/object.bin/below")  # a path through a file
    with pytest.raises(InvalidArgument):
        await store.get("../escape")
    with pytest.raises(TooLarge):
        await store.put("dir/big.bin", b"too large")
    with pytest.raises(Unsupported) as refused:
        await store.url("dir/object.bin")
    assert refused.value.feature == "presigned_get"
    with pytest.raises(InvalidArgument):
        await store.url("a//b")


def test_timeout_and_unavailable_are_distinct() -> None:
    assert not issubclass(Timeout, Unavailable)
    assert not issubclass(Unavailable, Timeout)
