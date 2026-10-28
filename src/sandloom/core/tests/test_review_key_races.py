"""Failed key publication must not delete a concurrent writer's replacement."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sandloom.core.cli import keys


def test_key_rollback_preserves_replacement_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = os.open

    def raced_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        if Path(path).name == "public":
            replacement = tmp_path / "replacement"
            replacement.write_text("other-writer-key")
            os.replace(replacement, tmp_path / "private")
            raise FileExistsError("another writer won public filename")
        return real_open(path, flags, mode)

    monkeypatch.setattr(keys.os, "open", raced_open)
    with pytest.raises(FileExistsError):
        keys._write_key_files(tmp_path, {"private": "our-key", "public": "our-public"})
    assert (tmp_path / "private").read_text() == "other-writer-key"


def test_key_rollback_removes_only_unchanged_owned_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = os.open

    def raced_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        if Path(path).name == "public":
            (tmp_path / "public").write_text("other-writer-public")
            raise FileExistsError("another writer won public filename")
        return real_open(path, flags, mode)

    monkeypatch.setattr(keys.os, "open", raced_open)
    with pytest.raises(FileExistsError):
        keys._write_key_files(tmp_path, {"private": "our-key", "public": "our-public"})
    assert not (tmp_path / "private").exists()
    assert (tmp_path / "public").read_text() == "other-writer-public"
