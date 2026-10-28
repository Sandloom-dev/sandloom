"""Filesystem object-store backend (driver-internal).

The concrete ``Storage`` implementation lives here, inside the driver layer —
it is constructed only by :mod:`sandloom.core.drivers.local_objectstore`.
Business modules reach object storage through the ``objectstore`` capability
provider, never this class.
"""

import asyncio
import os
from typing import Any

from sandloom.core.config.settings import settings


class UnsafeStoragePathError(ValueError):
    """Filename escapes the storage root (path traversal attempt)."""


class UploadTooLargeError(ValueError):
    """Upload exceeds MAX_UPLOAD_SIZE_BYTES."""


class ObjectTooLargeError(ValueError):
    """A stored object exceeds MAX_UPLOAD_SIZE_BYTES and is not read into RAM."""


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
        base = os.path.realpath(self.path)
        candidate = os.path.realpath(os.path.join(base, filename))
        if candidate != base and not candidate.startswith(base + os.sep):
            raise UnsafeStoragePathError(
                f"Filename escapes the storage root: {filename!r}"
            )
        if candidate == base:
            raise UnsafeStoragePathError("Filename resolves to the storage root")
        return candidate

    async def save(self, file: Any, filename: str) -> str:
        """Stream the upload to disk in chunks.

        Never buffers the whole file in RAM and enforces
        MAX_UPLOAD_SIZE_BYTES (0 disables the limit). A partial file is
        removed when the upload fails or exceeds the limit.
        """
        if self.type != "local":
            raise NotImplementedError(f"Storage type '{self.type}' not implemented.")
        full_path = self._resolve(filename)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        limit = getattr(self, "max_upload_size_bytes", 0) or 0
        written = 0
        # Open with O_NOFOLLOW so the final path component cannot be a symlink:
        # closes the TOCTOU window between _resolve()'s realpath check and the
        # open() where an attacker with write access to the root could swap the
        # target for a symlink escaping it. O_CREAT|O_TRUNC keeps overwrite
        # semantics; 0o600 keeps stored blobs owner-only.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = await asyncio.to_thread(os.open, full_path, flags, 0o600)
        fh = await asyncio.to_thread(os.fdopen, fd, "wb")
        try:
            while True:
                try:
                    chunk = await file.read(_CHUNK_SIZE)
                except TypeError:
                    # File-like without sized read: single full read; the
                    # limit still applies, just after the fact.
                    chunk = await file.read()
                    if limit and len(chunk) > limit:
                        raise UploadTooLargeError(
                            f"Upload exceeds {limit} bytes"
                        ) from None
                    await asyncio.to_thread(fh.write, chunk)
                    break
                if not chunk:
                    break
                written += len(chunk)
                if limit and written > limit:
                    raise UploadTooLargeError(f"Upload exceeds {limit} bytes")
                await asyncio.to_thread(fh.write, chunk)
        except BaseException:
            await asyncio.to_thread(fh.close)
            try:
                os.remove(full_path)
            except OSError:  # pragma: no cover - best effort cleanup
                pass
            raise
        await asyncio.to_thread(fh.close)
        return full_path

    async def delete(self, filename: str) -> None:
        if self.type == "local":
            full_path = self._resolve(filename)
            if os.path.exists(full_path):  # noqa: ASYNC240 - one local stat, negligible
                await asyncio.to_thread(os.remove, full_path)
            else:
                raise FileNotFoundError(f"{filename} not found")
            return
        raise NotImplementedError(f"Storage type '{self.type}' not implemented.")

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
        fd = await asyncio.to_thread(os.open, full_path, flags)
        fh = await asyncio.to_thread(os.fdopen, fd, "rb")
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
