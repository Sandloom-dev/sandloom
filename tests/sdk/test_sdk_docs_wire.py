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


def test_a_contract_prints_each_method_with_its_semantics(
    capsys: pytest.CaptureFixture[str],
) -> None:
    by_name = _out(capsys, "billing.credits.v1")
    assert _out(capsys, "BILLING_CREDITS_V1") == by_name
    assert (
        "from <your_module>._generated.contracts.billing_credits_v1 "
        "import BILLING_CREDITS_V1" in by_name
    )
    assert "provider: sandloom_billing" in by_name
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
