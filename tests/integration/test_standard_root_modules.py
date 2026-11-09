"""Both composition roots compose the same default module set (KB-08 review).

ADR-0012 decision 3: the image and the shipped profiles boot
``sandloom_standard.main``, a framework checkout's development root is
``sandloom.main``. A checkout with ``LOCAL_MODULES`` (modules no installed
distribution registers) must compose them under either root, else a module
added with ``sandloom add module`` runs locally and is missing from the
deploy root. Each root is imported fresh with the kernel factory replaced by
a recorder, so no app is built and the set each passes is compared directly.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import sandloom.local_modules as local_modules
import sandloom.sdk.app as sdk_app
from sandloom.core.descriptors.module_names import installed_modules

LEAN_ROOT = "sandloom.main"
STANDARD_ROOT = "sandloom_standard.main"


def _import_root(
    root: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModuleType, list[str]]:
    """The root imported fresh, and the ``default_modules`` it passed to the
    kernel factory."""
    calls: list[dict[str, Any]] = []

    def record(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(state=SimpleNamespace(core=None))

    monkeypatch.setattr(sdk_app, "create_app", record)
    # The import rebinds the parent's attribute too: restore both afterwards.
    parent_name, _, child = root.rpartition(".")
    parent = importlib.import_module(parent_name)
    monkeypatch.setattr(parent, child, getattr(parent, child, None), raising=False)
    # setitem records what sys.modules held (or that it held nothing) and
    # undoes to exactly that: a delitem of an absent root records nothing and
    # a later delitem of the imported one would put this test's stub back.
    monkeypatch.setitem(sys.modules, root, ModuleType(root))
    del sys.modules[root]  # imported fresh
    module: ModuleType = importlib.import_module(root)
    del sys.modules[root]  # the next import builds it again
    (call,) = calls
    assert module.app is not None
    return module, list(call["default_modules"])


def test_both_roots_compose_installed_and_local_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a local module, the standard root's default set equals the lean
    root's: every installed module, sorted, then ``LOCAL_MODULES``."""
    monkeypatch.setattr(local_modules, "LOCAL_MODULES", ["kb_local_probe"])
    installed = sorted({m.name for m in installed_modules()})
    _, lean = _import_root(LEAN_ROOT, monkeypatch)
    _, standard = _import_root(STANDARD_ROOT, monkeypatch)
    assert lean == [*installed, "kb_local_probe"]
    assert standard == lean


def test_standard_modules_reads_local_modules_when_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``standard_modules()`` reads ``LOCAL_MODULES`` when called, as the lean
    root does, and never lists an installed module twice."""
    standard, _ = _import_root(STANDARD_ROOT, monkeypatch)
    installed = sorted({m.name for m in installed_modules()})
    assert installed, "no installed module: the comparison would be vacuous"
    monkeypatch.setattr(local_modules, "LOCAL_MODULES", [installed[0], "kb_two"])
    assert standard.standard_modules() == [*installed, "kb_two"]
    assert standard.standard_modules() == local_modules.default_modules()
