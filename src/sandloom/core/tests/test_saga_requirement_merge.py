"""The loader merges the contracts a module's sagas call into its
``consumes`` BY NAME (ADR-0008): never a duplicate entry per contract."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import sandloom.core.loader.plugin_loader as loader
from sandloom.core.descriptors import ContractRequirement as Req


def _merge(
    monkeypatch: pytest.MonkeyPatch, declared: list[Any], saga: list[Req]
) -> list[Any]:
    monkeypatch.setattr(loader, "pending_saga_requirements", lambda _api: saga)
    desc = SimpleNamespace(consumes=list(declared))
    loader._merge_saga_requirements(desc, api=None)  # type: ignore[arg-type]
    return desc.consumes


def test_an_undeclared_contract_is_added_once(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _merge(
        monkeypatch,
        [],
        [Req("b.x.v1", methods=("spend",)), Req("b.x.v1", methods=("reverse",))],
    )
    assert out == [Req("b.x.v1", methods=("spend", "reverse"))]


def test_a_declared_all_methods_entry_absorbs_the_sagas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = _merge(monkeypatch, ["b.x.v1"], [Req("b.x.v1", methods=("spend",))])
    assert plain == ["b.x.v1"]
    every = Req("b.x.v1", required=False, reason="r")
    assert _merge(monkeypatch, [every], [Req("b.x.v1", methods=("spend",))]) == [every]


def test_methods_union_required_or_and_the_declared_reason_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declared = Req("b.x.v1", required=False, reason="quota", methods=("get",))
    out = _merge(monkeypatch, [declared], [Req("b.x.v1", methods=("spend",))])
    assert out == [
        Req("b.x.v1", required=True, reason="quota", methods=("get", "spend"))
    ]
    # a saga calling the whole contract (methods=()) widens to all methods
    out = _merge(monkeypatch, [declared], [Req("b.x.v1", required=False)])
    assert out == [Req("b.x.v1", required=False, reason="quota", methods=())]
