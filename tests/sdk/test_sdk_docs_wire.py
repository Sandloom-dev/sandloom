"""``sandloom docs`` describes the contract stubs and event schemas an application
calls, from the installed packages alone (ledger C-SDK-04)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sandloom.sdk._prim import docs_wire
from sandloom.sdk._prim.docs import docs_main
from sandloom.sdk._prim.docs_wire import contract_stubs, event_schemas


def _out(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    assert docs_main(list(argv)) == 0, argv
    return capsys.readouterr().out


def test_an_unknown_name_points_at_the_listings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert docs_main(["nope.thing.v1"]) == 1
    assert "sandloom docs contracts" in capsys.readouterr().out


def test_an_app_shared_package_is_read_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """In an application, its own stubs and schemas (``<app>.shared``)."""
    app = tmp_path / "docsapp"
    (app / "shared" / "contracts").mkdir(parents=True)
    (app / "shared" / "events").mkdir()
    (app / "__init__.py").write_text("")
    (app / "shared" / "__init__.py").write_text("")
    (app / "shared" / "contracts" / "__init__.py").write_text("")
    (app / "shared" / "contracts" / "notes_count_v1.py").write_text(
        "from typing import Protocol\n"
        "from sandloom.core.descriptors.contract_refs import ContractRef\n\n\n"
        "class NotesCountV1(Protocol):\n"
        "    async def count(self) -> int:\n"
        '        """How many notes the tenant has."""\n\n\n'
        'NOTES_COUNT_V1: ContractRef[NotesCountV1] = ContractRef("notes.count.v1")\n'
    )
    (app / "shared" / "events" / "notes.note.created.v1.json").write_text(
        json.dumps(
            {
                "title": "NoteCreated",
                "type": "object",
                "properties": {"note_id": {"type": "string", "format": "uuid"}},
                "required": ["note_id"],
            }
        )
    )
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "docsapp"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    text = _out(capsys, "notes.count.v1")
    assert "from docsapp.shared.contracts.notes_count_v1 import NOTES_COUNT_V1" in text
    assert "How many notes the tenant has." in text
    event = _out(capsys, "notes.note.created.v1")
    assert "note_id  UUID  required" in event
    assert "no tenant_id field" in event
    # An exported file written before the marks counts as released (ADR-0017).
    assert "release: released, revision 1" in event


def test_a_contract_release_is_read_from_the_providers_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(docs_wire, "_package_dir", lambda _package: tmp_path)
    stub = docs_wire._Stub("m.a.v1", "x", "M_A_V1", provider="provider_pkg")
    snapshot = tmp_path / "contracts.snapshot.json"
    assert docs_wire._contract_release(stub) is None  # no snapshot
    body = {"m.a.v1": {"methods": {}}}
    for document, expected in (
        ({"contracts": body}, 1),  # written before the marks: released
        ({"contracts": body, "released": {}}, 0),  # a draft
        ({"contracts": body, "released": {"m.a.v1": 4}}, 4),
        ({"contracts": body, "released": {"m.a.v1": "x"}}, None),  # malformed
        ({"contracts": {}, "released": {}}, None),  # not listed
    ):
        snapshot.write_text(json.dumps(document))
        assert docs_wire._contract_release(stub) == expected, document


def test_an_exported_event_shows_its_release_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = tmp_path / "markapp"
    (app / "shared" / "events").mkdir(parents=True)
    (app / "__init__.py").write_text("")
    (app / "shared" / "__init__.py").write_text("")
    for channel, mark in (("a.draft.v1", 0), ("a.out.v1", 3), ("a.bad.v1", "x")):
        (app / "shared" / "events" / f"{channel}.json").write_text(
            json.dumps({"title": "E", "properties": {}, "x-sandloom-released": mark})
        )
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "markapp"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    assert "release: draft" in _out(capsys, "a.draft.v1")
    assert "release: released, revision 3" in _out(capsys, "a.out.v1")
    assert "release: unknown" in _out(capsys, "a.bad.v1")


def test_every_bundled_contract_method_states_its_semantics() -> None:
    """``sandloom docs <contract>`` reads the installed provider's own class: a
    caller reads the semantics there, never the provider's source."""
    missing = []
    for name, stub in contract_stubs().items():
        if stub.provider is None:
            continue
        cls = docs_wire._provider_class(stub)
        assert cls is not None, name
        if not (cls.__doc__ or "").strip():
            missing.append(f"{name}: class")
        missing += [
            f"{name}.{method}"
            for method, _args, _returns, doc in docs_wire._provider_methods(cls)
            if not doc
        ]
    assert missing == [], f"undocumented contract surface: {missing}"


def test_the_kernel_ships_no_stub_or_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside an application every contract and event comes from an installed
    provider (its ``module.snapshot.json`` and ``contracts`` module)."""
    monkeypatch.chdir(tmp_path)
    assert all(stub.provider for stub in contract_stubs().values())
    assert all("module.snapshot.json" in src for _s, src in event_schemas().values())
