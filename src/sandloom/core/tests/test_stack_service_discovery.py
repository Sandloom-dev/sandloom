"""How the kernel CLI finds the package of the standard stack's server.

The one package registers ``redis`` in the ``sandloom.stack`` group
(:mod:`sandloom.core.cli.stack_service`). Two packages that register it are
refused (never the first one in metadata order, silently), a package that
cannot load is one line (never a traceback), and a package of before the group
existed is told to upgrade, not to install. Any other error from a package
still raises: only the failures of a broken install are the person's to fix.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core.cli import stack_service as stack


class _Entry:
    def __init__(self, dist: str | None, load: Any) -> None:
        self.name = stack.STANDARD_DRIVER
        self.value = f"{dist}.stack:STACK_SERVICE"
        self.dist = SimpleNamespace(name=dist) if dist else None
        self._load = load

    def load(self) -> Any:
        return self._load()


def _install(monkeypatch: pytest.MonkeyPatch, *entries: _Entry) -> None:
    """Discovery from scratch (it caches) over exactly ``entries``."""
    monkeypatch.setattr(stack, "_discover", stack._discover.__wrapped__)
    monkeypatch.setattr(
        stack.metadata,
        "entry_points",
        lambda: SimpleNamespace(select=lambda group: list(entries)),
    )


def test_one_package_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    service = object()
    _install(monkeypatch, _Entry("sandloom-redis", lambda: service))
    assert stack.load() is service


def test_two_packages_are_refused_naming_both(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        _Entry("sandloom-redis", lambda: object()),
        _Entry("a-fork", lambda: object()),
    )
    with pytest.raises(stack.StackServiceError) as refused:
        stack.load()
    message = str(refused.value)
    assert "sandloom-redis" in message and "a-fork" in message
    assert "\n" not in message


def test_a_package_that_cannot_load_is_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken() -> Any:
        raise ImportError("No module named 'redis'")

    _install(monkeypatch, _Entry("sandloom-redis", broken))
    with pytest.raises(stack.StackServiceError) as refused:
        stack.load()
    message = str(refused.value)
    assert "sandloom-redis" in message and "No module named 'redis'" in message
    assert "\n" not in message


def test_another_error_of_a_package_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def bug() -> Any:
        raise RuntimeError("a bug of the package, not a broken install")

    _install(monkeypatch, _Entry("sandloom-redis", bug))
    with pytest.raises(RuntimeError, match="a bug of the package"):
        stack.load()


def test_an_installed_package_without_the_entry_point_is_told_to_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch)
    monkeypatch.setattr(stack.metadata, "distribution", lambda name: object())
    with pytest.raises(stack.StackServiceError) as refused:
        stack.load()
    message = str(refused.value)
    assert "upgrade" in message and stack.STANDARD_PACKAGE in message
    assert "install `sandloom[redis]`" not in message


def test_no_package_at_all_is_told_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)

    def absent(name: str) -> Any:
        raise stack.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(stack.metadata, "distribution", absent)
    with pytest.raises(stack.StackServiceError, match=r"install `sandloom\[redis\]`"):
        stack.load()
