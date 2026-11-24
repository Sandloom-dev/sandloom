"""``sandloom add consumer``: the view keeps the channel's required fields, so the
scaffolded test hands ``deliver`` a payload the producer's schema accepts, whether
``add event`` or ``add consumer`` ran first.

The check is the one ``deliver`` makes (SANDLOOM-R004): the payload of the event the
generated test builds is validated against ``<app>/shared/events/<channel>.json``.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from sandloom.core.cli import add
from sandloom.core.cli.project import generate_project
from sandloom.core.tests._root_support import redis_free_infra_env

CHANNEL = "contacts.contact.created.v1"


@pytest.fixture(autouse=True)
def _generated_app_children_run_without_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """``run_generate`` boots the generated app in a child, whose own selection
    names the Redis drivers (the ``kernel-without-redis`` lane has none)."""
    for key, value in redis_free_infra_env().items():
        monkeypatch.setenv(key, value)
    yield


def _app(tmp_path: Path, *, tenant_scoped: bool = True) -> tuple[Path, add.Layout]:
    """A new app with the producer module ``contacts`` and the consumer ``deals``."""
    generate_project("probe", tmp_path, sample_module="notes")
    root = tmp_path / "probe"
    where = add.layout(root, tenant_scoped=tenant_scoped)
    add.add_module("contacts", where)
    add.add_module("deals", where)
    return root, where


def _view(root: Path) -> str:
    return (root / "probe" / "modules" / "deals" / "consumers.py").read_text()


def _view_fields(root: Path) -> list[str]:
    """The field names of the ``ContactCreated`` view, in order."""
    tree = ast.parse(_view(root))
    view = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ContactCreated"
    )
    return [
        stmt.target.id
        for stmt in view.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    ]


_PACKAGE = "probe_deals"


def _scaffolded_payload(root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The payload the generated test of the ``deals`` consumer passes to
    ``deliver``. The module's own ``consumers.py`` and ``tests/`` are imported as the
    package ``probe_deals`` (its relative import resolves), and the generated test
    function runs with ``deliver`` replaced by one that keeps the event."""
    seen: list[Any] = []

    async def deliver(event: Any, **_: Any) -> int:
        seen.append(event)
        return 1  # the consumers the real deliver reports

    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(root / "probe" / "modules" / "deals")]
    monkeypatch.setitem(sys.modules, _PACKAGE, package)
    importlib.invalidate_caches()
    try:
        scaffold = importlib.import_module(f"{_PACKAGE}.tests.test_on_contact_created")
        monkeypatch.setattr(scaffold, "deliver", deliver)
        asyncio.run(
            scaffold.test_on_contact_created_applies_a_redelivered_event_once(
                None, None
            )
        )
    finally:
        for name in [m for m in sys.modules if m.startswith(f"{_PACKAGE}.")]:
            del sys.modules[name]
    assert len(seen) == 1
    payload: dict[str, Any] = seen[0].model_dump(mode="json", by_alias=True)
    return payload


def _refusals(root: Path, payload: dict[str, Any]) -> list[str]:
    """What the channel's exported schema says against ``payload`` (empty: accepted)."""
    exported = root / "probe" / "shared" / "events" / f"{CHANNEL}.json"
    schema = json.loads(exported.read_text(encoding="utf-8"))
    return [
        e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(payload)
    ]


def _generate(root: Path) -> None:
    code, output = add.run_generate(root)
    assert code == 0, output
    assert "C025" not in output, output  # the view agrees with its producer


def test_a_view_with_field_keeps_the_producers_required_tenant_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dogfood run: ``add event contacts ContactCreated --field name:str``, then
    ``add consumer deals <channel> --field name:str``. The view used to be ``name``
    alone and the generated test failed ``"tenant_id" is a required property``."""
    root, where = _app(tmp_path)
    add.add_event("contacts", "ContactCreated", where, ["name:str"])
    add.add_consumer("deals", CHANNEL, where, ["name:str"])
    view = _view(root)
    assert "    name: str\n    tenant_id: UUID\n" in view
    assert "# unverified" not in view  # the producer was found
    _generate(root)
    payload = _scaffolded_payload(root, monkeypatch)
    assert set(payload) == {"name", "tenant_id"}
    assert _refusals(root, payload) == []


def test_a_consumer_added_before_its_event_gets_the_tenant_id_add_event_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, where = _app(tmp_path)
    add.add_consumer("deals", CHANNEL, where, ["name:str"])
    view = _view(root)
    assert "# unverified: no producer schema was found" in view
    assert "    name: str\n    tenant_id: UUID\n" in view
    add.add_event("contacts", "ContactCreated", where, ["name:str"])
    _generate(root)
    assert _refusals(root, _scaffolded_payload(root, monkeypatch)) == []


def test_a_tenant_id_named_by_the_author_is_not_added_twice(tmp_path: Path) -> None:
    root, where = _app(tmp_path)
    add.add_consumer("deals", CHANNEL, where, ["tenant_id:UUID", "name:str"])
    assert _view(root).count("tenant_id") == 1


def test_an_app_without_tenants_gets_no_tenant_id_in_either_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-tenant``: the consumer first (no schema to read) and the event first
    (the schema is read) both give the view ``name`` alone, and its test event
    passes the tenant-less schema."""
    root, where = _app(tmp_path, tenant_scoped=False)
    add.add_consumer("deals", CHANNEL, where, ["name:str"])
    assert "tenant_id" not in _view(root)
    add.add_event("contacts", "ContactCreated", where, ["name:str"])
    _generate(root)
    payload = _scaffolded_payload(root, monkeypatch)
    assert payload.keys() == {"name"}
    assert _refusals(root, payload) == []
    # The producer found in the tree: the view takes its fields and adds none.
    (root / "probe" / "modules" / "deals" / "consumers.py").unlink()
    add.add_consumer("deals", CHANNEL, where, ["name:str"])
    assert "tenant_id" not in _view(root)


def test_the_cli_takes_no_tenant_for_a_consumer(tmp_path: Path) -> None:
    root, _ = _app(tmp_path)
    base = ["--root", str(root), "consumer", "deals", CHANNEL, "--field", "name:str"]
    assert add.main([*base, "--no-tenant"]) == 0
    assert "tenant_id" not in _view(root)
    (root / "probe" / "modules" / "deals" / "consumers.py").unlink()
    assert add.main(base) == 0
    assert "    name: str\n    tenant_id: UUID\n" in _view(root)


def _producer(root: Path, body: str) -> None:
    """A hand-written producer class in ``contacts/events.py``."""
    events = root / "probe" / "modules" / "contacts" / "events.py"
    events.write_text(
        events.read_text(encoding="utf-8")
        + f'\n\nclass ContactCreated(Event, channel="{CHANNEL}"):\n{body}',
        encoding="utf-8",
    )


@pytest.mark.parametrize("fields", [[], ["nickname:str"], ["score:int"]])
def test_a_producer_class_keeps_all_its_fields_whatever_field_names(
    tmp_path: Path, fields: list[str]
) -> None:
    """The channel's exported schema lists every field of a producer class as
    required (a defaulted one is always published), so the test event of a view
    that left one out is refused: ``--field`` cannot narrow a producer class."""
    root, where = _app(tmp_path)
    _producer(
        root,
        "    contact_id: UUID\n    nickname: str = ''\n    score: int = 0\n"
        "    tenant_id: UUID\n",
    )
    add.add_consumer("deals", CHANNEL, where, fields)
    assert _view_fields(root) == ["contact_id", "nickname", "score", "tenant_id"]


def test_a_defaulted_field_is_written_with_the_producers_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A view field with no default is SANDLOOM-C025 ("give it a default"), and
    the schema requires it: the view keeps the field with the producer's default."""
    root, where = _app(tmp_path)
    add.add_event("contacts", "ContactCreated", where, ["nickname:str", "score:int"])
    events = root / "probe" / "modules" / "contacts" / "events.py"
    text = events.read_text(encoding="utf-8")
    assert "    nickname: str\n    score: int\n" in text
    events.write_text(
        text.replace(
            "    nickname: str\n    score: int\n",
            "    nickname: str = 'anon'\n    score: int = -1\n",
        ),
        encoding="utf-8",
    )
    add.add_consumer("deals", CHANNEL, where, ["nickname:str"])
    view = _view(root)
    assert "    nickname: str = 'anon'\n    score: int = -1\n" in view
    _generate(root)  # no C025: the view agrees with its producer
    assert _refusals(root, _scaffolded_payload(root, monkeypatch)) == []


@pytest.mark.parametrize("fields", [["extra:str"], ["nickname:str", "extra:str"]])
def test_a_field_the_producer_does_not_send_is_refused(
    tmp_path: Path, fields: list[str]
) -> None:
    """``--field extra:str`` would make ``extra`` required in the view, so every
    other consumer test of the channel (and every real event) fails. The refusal
    names the producer's fields and writes nothing."""
    root, where = _app(tmp_path)
    _producer(
        root, "    contact_id: UUID\n    nickname: str = ''\n    tenant_id: UUID\n"
    )
    with pytest.raises(add.GeneratorError, match=r"does not send") as refused:
        add.add_consumer("deals", CHANNEL, where, fields)
    assert "contact_id, nickname, tenant_id" in str(refused.value)
    assert "extra" in str(refused.value)
    assert not (root / "probe" / "modules" / "deals" / "consumers.py").exists()


def test_the_cli_refuses_a_field_the_producer_does_not_send(tmp_path: Path) -> None:
    root, where = _app(tmp_path)
    add.add_event("contacts", "ContactCreated", where, ["name:str"])
    args = ["--root", str(root), "consumer", "deals", CHANNEL, "--field", "extra:str"]
    assert add.main(args) != 0
    assert not (root / "probe" / "modules" / "deals" / "consumers.py").exists()


@pytest.mark.parametrize("fields", [[], ["nickname:str"]])
def test_a_computed_default_of_a_producer_class_is_refused(
    tmp_path: Path, fields: list[str]
) -> None:
    """A default such as ``str()`` cannot be copied into a view, yet the schema
    requires the field and a view field without a default is C025: refused,
    nothing written, rather than a view the check then rejects."""
    root, where = _app(tmp_path)
    _producer(
        root, "    contact_id: UUID\n    nickname: str = str()\n    tenant_id: UUID\n"
    )
    with pytest.raises(add.GeneratorError, match="nickname a computed default"):
        add.add_consumer("deals", CHANNEL, where, fields)
    assert not (root / "probe" / "modules" / "deals" / "consumers.py").exists()


def test_a_view_of_an_exported_schema_keeps_its_required_properties(
    tmp_path: Path,
) -> None:
    """A producer in another repository: only ``<app>/shared/events/<channel>.json``
    is here, and its ``required`` list is what the view must keep."""
    root, where = _app(tmp_path)
    assert where.shared_dir is not None
    exported = where.shared_dir / "events" / f"{CHANNEL}.json"
    exported.parent.mkdir(parents=True, exist_ok=True)
    exported.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "string", "format": "uuid"},
                    "nickname": {"type": "string", "default": ""},
                    "note": {"type": "string", "default": ""},
                    "tenant_id": {"type": "string", "format": "uuid"},
                },
                "required": ["contact_id", "tenant_id"],
            }
        ),
        encoding="utf-8",
    )
    add.add_consumer("deals", CHANNEL, where, ["nickname:str"])
    view = _view(root)
    assert "    contact_id: UUID\n    nickname: str = ''\n    tenant_id: UUID\n" in view
    assert "note" not in view  # not required, not named: narrowed away
    assert "# unverified" not in view


def test_an_exported_optional_property_keeps_its_schema_default(
    tmp_path: Path,
) -> None:
    """The schema's ``default`` is the view's default; an optional property with
    none (a computed default upstream) is left out, and naming it is refused."""
    root, where = _app(tmp_path)
    assert where.shared_dir is not None
    exported = where.shared_dir / "events" / f"{CHANNEL}.json"
    exported.parent.mkdir(parents=True, exist_ok=True)
    exported.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "string", "format": "uuid"},
                    "nickname": {"type": "string", "default": "anon"},
                    "vip": {"type": "boolean", "default": False},
                    "created": {"type": "string"},
                    "tenant_id": {"type": "string", "format": "uuid"},
                },
                "required": ["contact_id", "tenant_id"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(add.GeneratorError, match="computes its default"):
        add.add_consumer("deals", CHANNEL, where, ["created:str"])
    add.add_consumer("deals", CHANNEL, where, [])
    view = _view(root)
    assert "    nickname: str = 'anon'\n    vip: bool = False\n" in view
    assert "created: " not in view
