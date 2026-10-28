"""Storage path-containment tests (path traversal protection)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.drivers.filesystem_storage import Storage, UnsafeStoragePathError


class _AsyncFile:
    """Minimal async file-like object (mimics UploadFile.read)."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    async def read(self) -> bytes:
        return self._buf.read()


@pytest.fixture()
def local_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Storage:
    storage = Storage.__new__(Storage)
    storage.type = "local"
    storage.path = str(tmp_path)
    return storage


@pytest.mark.asyncio
async def test_save_and_delete_inside_root(local_storage: Storage, tmp_path: Path):
    saved = await local_storage.save(_AsyncFile(b"hello"), "docs/report.txt")
    assert Path(saved).read_bytes() == b"hello"
    assert Path(saved).is_relative_to(tmp_path)

    await local_storage.delete("docs/report.txt")
    assert not Path(saved).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evil",
    [
        "../outside.txt",
        "../../etc/passwd",
        "docs/../../escape.txt",
        "/etc/passwd",
        "/tmp/abs.txt",
    ],
)
async def test_save_rejects_traversal(local_storage: Storage, evil: str):
    with pytest.raises(UnsafeStoragePathError):
        await local_storage.save(_AsyncFile(b"pwn"), evil)


@pytest.mark.asyncio
@pytest.mark.parametrize("evil", ["../x", "/etc/passwd", ""])
async def test_delete_rejects_traversal(local_storage: Storage, evil: str):
    with pytest.raises(UnsafeStoragePathError):
        await local_storage.delete(evil)


@pytest.mark.asyncio
async def test_symlink_escape_rejected(local_storage: Storage, tmp_path: Path):
    outside = tmp_path.parent / "outside-target"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(UnsafeStoragePathError):
        await local_storage.save(_AsyncFile(b"x"), "link/escape.txt")


def test_storage_has_no_dead_static_url_branch():
    # GLM review #9: Storage.get_url returned "/static/<key>", a route nothing
    # serves; the provider's url() raises FeatureUnsupportedError (the local
    # driver does not advertise a "url" feature). One consistent answer.
    assert not hasattr(Storage, "get_url")


@pytest.mark.asyncio
async def test_read_refuses_objects_over_the_size_limit(
    local_storage: Storage, tmp_path: Path
):
    # GLM review #9: puts are capped by MAX_UPLOAD_SIZE_BYTES, reads loaded the
    # whole object into memory whatever its size (an out-of-band or pre-limit
    # file). The contract returns bytes, so the guard is a bounded read.
    from sandloom.core.drivers.filesystem_storage import ObjectTooLargeError

    local_storage.max_upload_size_bytes = 8
    (tmp_path / "small.bin").write_bytes(b"x" * 8)
    (tmp_path / "big.bin").write_bytes(b"x" * 9)
    assert await local_storage.read("small.bin") == b"x" * 8
    with pytest.raises(ObjectTooLargeError):
        await local_storage.read("big.bin")
    local_storage.max_upload_size_bytes = 0  # 0 disables the limit
    assert await local_storage.read("big.bin") == b"x" * 9


def _noop(*args: Any, **kwargs: Any) -> None:  # pragma: no cover
    pass
