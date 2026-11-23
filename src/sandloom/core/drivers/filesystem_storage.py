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
from sandloom.core.infra.errors import (
    InfraError,
    InvalidArgument,
    NotFound,
    PermissionDenied,
    TooLarge,
    Unavailable,
)
from sandloom.core.infra.ports.objectstore import (
    ObjectContent,
    iter_object_chunks,
    validate_object_key,
)

_CHUNK_SIZE = 1024 * 1024  # 1 MiB


def _storage_error(exc: OSError) -> InfraError:
    """The contract's error for a filesystem failure (spec 9). The message is
    the error type only: ``str(exc)`` carries the host path."""
    message = f"local storage failed: {type(exc).__name__}"
    if isinstance(exc, PermissionError):
        return PermissionDenied(message, capability="objectstore")
    return Unavailable(message, capability="objectstore")


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
        self.type = (
            storage_type
            if storage_type is not None
            else (settings.storage_type or "local")
        )
        self.path = path if path is not None else settings.storage_path
        self.max_upload_size_bytes = (
            max_upload_size_bytes
            if max_upload_size_bytes is not None
            else settings.max_upload_size_bytes
        )
        if self.type == "local":
            if not self.path:
                raise ValueError(
                    "STORAGE_PATH is required for the local object store "
                    "(INFRA.objectstore.<name>.path or STORAGE_PATH)"
                )
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
            raise InvalidArgument(
                f"Filename escapes the storage root: {filename!r}",
                capability="objectstore",
            )
        if candidate == base:
            raise InvalidArgument(
                "Filename resolves to the storage root", capability="objectstore"
            )
        return candidate

    async def save(self, file: ObjectContent, filename: str) -> str:
        """Stream to a temporary file and publish it atomically when complete.

        A filesystem failure raises ``PermissionDenied`` or ``Unavailable``
        naming only its type; an error of the source itself propagates."""
        if self.type != "local":
            raise NotImplementedError(f"Storage type '{self.type}' not implemented.")
        full_path = self._resolve(filename)
        parent = os.path.dirname(full_path)
        limit = getattr(self, "max_upload_size_bytes", 0) or 0
        written = 0
        try:
            os.makedirs(parent, exist_ok=True)
            fd, temporary_path = mkstemp(dir=parent, prefix=".sandloom-upload-")
        except OSError as exc:
            raise _storage_error(exc) from exc
        try:
            with os.fdopen(fd, "wb") as target:
                async for chunk in iter_object_chunks(file, _CHUNK_SIZE):
                    written += len(chunk)
                    if limit and written > limit:
                        raise TooLarge(
                            f"Upload exceeds {limit} bytes", capability="objectstore"
                        )
                    write = asyncio.create_task(asyncio.to_thread(target.write, chunk))
                    try:
                        await asyncio.shield(write)
                    except asyncio.CancelledError:
                        # The thread still owns target until the write completes.
                        await write
                        raise
                    except OSError as exc:
                        raise _storage_error(exc) from exc
                try:
                    await asyncio.to_thread(target.flush)
                except OSError as exc:
                    raise _storage_error(exc) from exc
            try:
                os.replace(temporary_path, full_path)
            except OSError as exc:
                raise _storage_error(exc) from exc
        finally:
            with suppress(OSError):
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
        except PermissionError as exc:
            # Some filesystems return EPERM when unlink is attempted on a directory.
            if not await asyncio.to_thread(os.path.isdir, full_path):
                raise _storage_error(exc) from exc
        except OSError as exc:
            raise _storage_error(exc) from exc

    async def read(self, filename: str) -> bytes:
        """Return the stored object's bytes (rejecting traversal/symlink escape).

        The contract returns the whole object, so the read is bounded by the
        same MAX_UPLOAD_SIZE_BYTES that caps puts (0 disables it): an object
        placed out of band, or stored before the limit was lowered, raises
        :class:`~sandloom.core.infra.errors.TooLarge` instead of being loaded
        into memory. A missing object raises
        :class:`~sandloom.core.infra.errors.NotFound`.
        """
        if self.type != "local":
            raise NotImplementedError(f"Storage type '{self.type}' not implemented.")
        full_path = self._resolve(filename)
        limit = getattr(self, "max_upload_size_bytes", 0) or 0
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = await asyncio.to_thread(os.open, full_path, flags)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise NotFound(filename, capability="objectstore") from exc
        except OSError as exc:
            raise _storage_error(exc) from exc
        try:
            fh = os.fdopen(fd, "rb")
        except IsADirectoryError as exc:
            os.close(fd)
            raise NotFound(filename, capability="objectstore") from exc
        try:
            if not limit:
                data = await asyncio.to_thread(fh.read)
            else:
                # One byte past the limit detects a file that grows after open.
                data = await asyncio.to_thread(fh.read, limit + 1)
        except IsADirectoryError as exc:
            raise NotFound(filename, capability="objectstore") from exc
        except OSError as exc:
            raise _storage_error(exc) from exc
        finally:
            await asyncio.to_thread(fh.close)
        if limit and len(data) > limit:
            raise TooLarge(
                f"Stored object exceeds {limit} bytes (MAX_UPLOAD_SIZE_BYTES)",
                capability="objectstore",
            )
        return data
