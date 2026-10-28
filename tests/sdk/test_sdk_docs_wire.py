"""``sandloom docs`` describes the contract stubs and event schemas an application
calls, from the installed packages alone (ledger C-SDK-04)."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

import sandloom.shared.contracts as bundled_contracts
from sandloom.sdk._prim.docs import docs_main
from sandloom.sdk._prim.docs_wire import contract_stubs, event_schemas


def _out(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    assert docs_main(list(argv)) == 0, argv
    return capsys.readouterr().out


def test_a_contract_prints_each_method_with_its_semantics(
    capsys: pytest.CaptureFixture[str],
) -> None:
    by_name = _out(capsys, "billing.credits.v1")
    assert _out(capsys, "BILLING_CREDITS_V1") == by_name
    assert (
        "from sandloom.shared.contracts.billing_credits_v1 import BILLING_CREDITS_V1"
        in by_name
    )
    assert (
        "await client.spend(tenant_type: str, tenant_id: UUID, amount: int" in by_name
    )
    for semantics in ("operation_key", "402", "409", "amount=0", "422", "403"):
        assert semantics in by_name, semantics


def test_an_event_prints_its_fields_and_the_producers_notes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    text = _out(capsys, "tenancy.member_added.v2")
    assert "tenant_id" in text and "version" in text
    assert "also on tenancy.member_added.v1" in text
    assert "tenant_from='tenant_id'" in text and "version_field='version'" in text
    assert 'class MemberAdded(Event, channel="tenancy.member_added.v2")' in text
    unversioned = _out(capsys, "billing.subscription.created.v1")
    assert "not a projection source" in unversioned


def test_the_listings_name_every_stub_and_schema(
    capsys: pytest.CaptureFixture[str],
) -> None:
    contracts = _out(capsys, "contracts")
    events = _out(capsys, "events")
    for name in ("billing.credits.v1", "tenancy.membership.v1", "identity.contact.v1"):
        assert name in contracts
    assert "tenancy.member_removed.v2" in events
    assert set(contract_stubs()) >= {"billing.entitlements.v1", "identity.security.v1"}
    assert "billing.credits.spent.v1" in event_schemas()


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


def test_every_bundled_contract_method_states_its_semantics() -> None:
    """The bundled stubs carry the providers' docstrings: a caller reads the
    semantics with ``sandloom docs <contract>``, never the provider's source."""
    directory = Path(inspect.getfile(bundled_contracts)).parent
    missing = []
    for path in sorted(directory.glob("*_v[0-9]*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and not ast.get_docstring(node):
                missing.append(f"{path.name}:{node.name}")
            if isinstance(node, ast.AsyncFunctionDef) and not ast.get_docstring(node):
                missing.append(f"{path.name}:{node.name}")
    assert missing == [], f"undocumented contract surface: {missing}"
