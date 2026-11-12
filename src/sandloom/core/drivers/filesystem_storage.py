"""Filesystem object-store backend (driver-internal).

The concrete ``Storage`` implementation lives here, inside the driver layer —
it is constructed only by :mod:`sandloom.core.drivers.local_objectstore`.
Business modules reach object storage through the ``objectstore`` capability
provider, never this class.
"""

import asyncio
import os
from contextlib import suppress
from tempfile import mkstemp

from sandloom.core.config.settings import settings
from sandloom.core.infra.contracts import (
    InvalidObjectKeyError as UnsafeStoragePathError,
)
from sandloom.core.infra.contracts import (
    ObjectContent,
    iter_object_chunks,
    validate_object_key,
)
from sandloom.core.infra.contracts import (
    ObjectTooLargeError as ObjectTooLargeError,
)
from sandloom.core.infra.contracts import (
    UploadTooLargeError as UploadTooLargeError,
)

_CHUNK_SIZE = 1024 * 1024  # 1 MiB


class Storage:
    def __init__(
        self,
        *,
        storage_type: str | None = None,
        path: str | None = None,
        max_upload_size_bytes: int | None = None,
    ) -> None:
        # Defaults come from settings so `Storage()` keeps its historic
        # behavior; explicit kwargs let the infra layer build named instances
        # (e.g. a separate "exports" object store) without touching globals.
        self.type = storage_type if storage_type is not None else settings.storage_type
        self.path = path if path is not None else settings.storage_path
        self.max_upload_size_bytes = (
            max_upload_size_bytes
            if max_upload_size_bytes is not None
            else settings.max_upload_size_bytes
        )
        if self.type == "local":
            os.makedirs(self.path, exist_ok=True)

    def _resolve(self, filename: str) -> str:
        """Resolve filename inside the storage root, rejecting escapes.

        Blocks absolute paths, `..` segments, and symlink escapes by
        comparing realpaths.
        """
        validate_object_key(filename)
        base = os.path.realpath(self.path)
        candidate = os.path.realpath(os.path.join(base, filename))
        if os.path.commonpath((base, candidate)) != base:
            raise UnsafeStoragePathError(
                f"Filename escapes the storage root: {filename!r}"
            )
        if candidate == base:
            raise UnsafeStoragePathError("Filename resolves to the storage root")
        return candidate

    async def save(self, file: ObjectContent, filename: str) -> str:
        """Stream to a temporary file and publish it atomically when complete."""
        if self.type != "local":
            raise NotImplementedError(f"Storage type '{self.type}' not implemented.")
        full_path = self._resolve(filename)
        parent = os.path.dirname(full_path)
        os.makedirs(parent, exist_ok=True)
        limit = getattr(self, "max_upload_size_bytes", 0) or 0
        written = 0
        fd, temporary_path = mkstemp(dir=parent, prefix=".sandloom-upload-")
        try:
            with os.fdopen(fd, "wb") as target:
                async for chunk in iter_object_chunks(file, _CHUNK_SIZE):
                    written += len(chunk)
                    if limit and written > limit:
                        raise UploadTooLargeError(f"Upload exceeds {limit} bytes")
                    write = asyncio.create_task(asyncio.to_thread(target.write, chunk))
                    try:
                        await asyncio.shield(write)
                    except asyncio.CancelledError:
                        # The thread still owns target until the write completes.
                        await write
                        raise
            os.replace(temporary_path, full_path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary_path)
        return full_path

    async def delete(self, filename: str) -> None:
        if self.type != "local":
            raise NotImplementedError(f"Storage type '{self.type}' not implemented.")
        full_path = self._resolve(filename)
        try:
            await asyncio.to_thread(os.remove, full_path)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            return
        except PermissionError:
            # Some filesystems return EPERM when unlink is attempted on a directory.
            if not await asyncio.to_thread(os.path.isdir, full_path):
                raise

    async def read(self, filename: str) -> bytes:
        """Return the stored object's bytes (rejecting traversal/symlink escape).

        The contract returns the whole object, so the read is bounded by the
        same MAX_UPLOAD_SIZE_BYTES that caps puts (0 disables it): an object
        placed out of band, or stored before the limit was lowered, raises
        :class:`ObjectTooLargeError` instead of being loaded into memory.
        """
        if self.type != "local":
            raise NotImplementedError(f"Storage type '{self.type}' not implemented.")
        full_path = self._resolve(filename)
        limit = getattr(self, "max_upload_size_bytes", 0) or 0
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = await asyncio.to_thread(os.open, full_path, flags)
        except NotADirectoryError as exc:
            raise FileNotFoundError(filename) from exc
        try:
            fh = os.fdopen(fd, "rb")
        except IsADirectoryError as exc:
            os.close(fd)
            raise FileNotFoundError(filename) from exc
        try:
            if not limit:
                return await asyncio.to_thread(fh.read)
            # One byte past the limit detects a file that grows after open.
            data = await asyncio.to_thread(fh.read, limit + 1)
            if len(data) > limit:
                raise ObjectTooLargeError(
                    f"Stored object exceeds {limit} bytes (MAX_UPLOAD_SIZE_BYTES)"
                )
            return data
        finally:
            await asyncio.to_thread(fh.close)
