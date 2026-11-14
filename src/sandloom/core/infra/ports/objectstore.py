"""The ``objectstore`` capability (module-facing, public; spec section 9).

Key-addressed objects in one configured store. Major 2: the shape of major 1,
with the shared error hierarchy (``sandloom.core.infra.errors``) in place of
``FileNotFoundError`` and the ``ValueError`` subclasses, and ``url`` gated by
``presigned_get``.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Mapping
from typing import ClassVar, Protocol, TypeAlias

from sandloom.core.infra.errors import InvalidArgument, Unsupported
from sandloom.core.infra.ports._base import CapabilitySpec, InfraProvider

#: The longest key, in UTF-8 bytes.
MAX_OBJECT_KEY_BYTES = 1024


def validate_object_key(key: str) -> None:
    """Validate a portable relative key without rewriting it.

    Raises :class:`~sandloom.core.infra.errors.InvalidArgument` for a key that
    is empty, absolute, has an empty or dot segment, a backslash or NUL, is not
    valid UTF-8 or is longer than 1024 bytes.
    """
    try:
        encoded = key.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidArgument(
            "Object key must be valid UTF-8", capability="objectstore"
        ) from exc
    if (
        any(part in {"", ".", ".."} for part in key.split("/"))
        or "\\" in key
        or "\x00" in key
        or len(encoded) > MAX_OBJECT_KEY_BYTES
    ):
        raise InvalidArgument("Invalid object key", capability="objectstore")


class ObjectReader(Protocol):
    """Async input that returns up to size bytes and signals EOF with b''."""

    async def read(self, size: int, /) -> bytes: ...


#: What ``put`` accepts: bytes, or a reader whose ``read(size)`` returns up to
#: ``size`` bytes and ``b""`` at the end (a reader whose ``read()`` takes no
#: size is no longer accepted, spec 9).
ObjectContent: TypeAlias = bytes | ObjectReader


async def iter_object_chunks(
    data: ObjectContent, chunk_size: int
) -> AsyncIterator[bytes]:
    """Read an upload, bytes or a sized reader, in chunks of ``chunk_size``."""
    if isinstance(data, (bytes, bytearray)):
        yield bytes(data)
        return
    while chunk := await data.read(chunk_size):
        yield chunk


class ObjectStore(Protocol):
    """Key-addressed objects in one configured store.

    Keys are relative UTF-8 strings with nonempty slash-separated segments,
    at most 1024 bytes, with dot segments, backslashes and NUL excluded.
    Deployments using a filesystem also need filenames and key combinations
    representable on that filesystem.

    A complete put publishes one object and returns its key. A source read
    failure or size refusal preserves any previous object. Get returns bytes
    and raises ``NotFound`` when the backend reports a missing key. Delete
    succeeds when the key is absent. In a versioned store it removes the
    current object from ordinary reads. Older versions follow backend policy.

    Errors (``sandloom.sdk.infra``): ``put`` raises ``InvalidArgument`` (the
    key), ``TooLarge``, ``PermissionDenied``, ``Unavailable``, ``Timeout``;
    ``get`` raises ``InvalidArgument``, ``NotFound``, ``TooLarge`` (over the
    buffered read limit), ``PermissionDenied``, ``Unavailable``, ``Timeout``;
    ``delete`` raises ``InvalidArgument``, ``PermissionDenied``,
    ``Unavailable``; ``url`` raises ``InvalidArgument`` and
    ``Unsupported("presigned_get")``.

    Content type and metadata are available when the driver advertises
    ``metadata``. A driver with ``presigned_get`` can issue a time-limited
    retrieval link without checking that the object exists; without it ``url``
    raises ``Unsupported("presigned_get")``.
    """

    async def put(
        self,
        key: str,
        data: ObjectContent,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> str: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    async def url(self, key: str) -> str: ...


class ObjectStoreProvider(InfraProvider, ObjectStore):
    """Blob/file storage capability (e.g. local FS, S3, GCS).

    The contract is *key-addressed*: callers supply a logical object ``key`` and
    the object's *content* (bytes or an async byte stream) plus optional
    ``content_type``/``metadata`` — never a filesystem path. The driver maps the
    key to its own namespace safely, so a malicious key can never be treated as
    a host path (no traversal, no symlink escape). This makes S3/GCS/local FS
    interchangeable behind the same surface.
    """

    capability: ClassVar[str] = "objectstore"

    @abc.abstractmethod
    async def put(
        self,
        key: str,
        data: ObjectContent,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        """Store ``data`` (bytes or an async-readable stream) under ``key``.

        Returns the logical key. The object is published after the upload completes.
        """

    @abc.abstractmethod
    async def get(self, key: str) -> bytes:
        """Return the bytes stored under ``key`` (``NotFound`` if absent)."""

    @abc.abstractmethod
    async def delete(self, key: str) -> None:
        """Remove the current object. An absent key succeeds."""

    async def url(self, key: str) -> str:
        """A time-limited retrieval link for ``key`` (feature ``presigned_get``).

        The default validates the key and raises
        ``Unsupported("presigned_get")``: a driver that declares the feature
        overrides it.
        """
        validate_object_key(key)
        raise Unsupported("presigned_get", capability="objectstore", instance=self.name)


OBJECTSTORE_CAPABILITY = CapabilitySpec(
    name="objectstore",
    # Modules see the shape of v1; drivers owe the errors of major 2.
    version="v1",
    major=2,
    provider_contract=ObjectStoreProvider,
    feature_kinds={
        # Content type and metadata are stored and read back.
        "metadata": "behaviour",
        # ``url`` issues a time-limited link; without it ``url`` raises.
        "presigned_get": "behaviour",
    },
    description="Blob/file storage (key-addressed).",
)


__all__ = [
    "MAX_OBJECT_KEY_BYTES",
    "OBJECTSTORE_CAPABILITY",
    "ObjectContent",
    "ObjectReader",
    "ObjectStore",
    "ObjectStoreProvider",
    "iter_object_chunks",
    "validate_object_key",
]
