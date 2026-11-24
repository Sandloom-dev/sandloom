"""``sandloom add consumer``: the view keeps the channel's required fields, so the
scaffolded test hands ``deliver`` a payload the producer's schema accepts, whether
``add event`` or ``add consumer`` ran first.

The check is the one ``deliver`` makes (SANDLOOM-R004): the payload of the event the
generated test builds is validated against ``<app>/shared/events/<channel>.json``.
"""

from __future__ import annotations

import ast
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


def _scaffolded_payload(root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The payload the generated test of the ``deals`` consumer passes to
    ``deliver``: the view class of ``consumers.py`` and the ``event = ...`` line of
    its test, both run (in a module of their own: pydantic resolves a view's
    annotations through ``sys.modules``)."""
    module = root / "probe" / "modules" / "deals"
    consumers = ast.parse((module / "consumers.py").read_text(encoding="utf-8"))
    test = ast.parse(
        (module / "tests" / "test_on_contact_created.py").read_text(encoding="utf-8")
    )
    view = next(
        node
        for node in consumers.body
        if isinstance(node, ast.ClassDef) and node.name == "ContactCreated"
    )
    imports = [
        node
        for tree in (consumers, test)
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.level == 0
    ]
    scaffold = types.ModuleType("probe_deals_scaffold")
    monkeypatch.setitem(sys.modules, scaffold.__name__, scaffold)
    namespace = scaffold.__dict__
    program = ast.Module([*imports, view], type_ignores=[])
    exec(compile(program, "<consumers.py>", "exec"), namespace)  # noqa: S102 - the generated view class
    call = next(
        node.value
        for node in ast.walk(test)
        if isinstance(node, ast.Assign)
        and [t.id for t in node.targets if isinstance(t, ast.Name)] == ["event"]
    )
    event = eval(compile(ast.Expression(call), "<test>", "eval"), namespace)  # noqa: S307 - the generated event line
    payload: dict[str, Any] = event.model_dump(mode="json", by_alias=True)
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


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ([], ["contact_id", "nickname", "score", "tenant_id"]),
        (["nickname:str"], ["contact_id", "nickname", "tenant_id"]),
        (["score:int"], ["contact_id", "score", "tenant_id"]),
        (["extra:str"], ["contact_id", "tenant_id", "extra"]),
    ],
)
def test_field_narrows_the_optional_fields_and_adds_its_own(
    tmp_path: Path, fields: list[str], expected: list[str]
) -> None:
    """The required fields of the producer class always stay; the optional ones
    (a default) are the producer's with no ``--field``, else only the named ones."""
    root, where = _app(tmp_path)
    _producer(
        root,
        "    contact_id: UUID\n    nickname: str = ''\n    score: int = 0\n"
        "    tenant_id: UUID\n",
    )
    add.add_consumer("deals", CHANNEL, where, fields)
    assert _view_fields(root) == expected


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
                    "nickname": {"type": "string"},
                    "tenant_id": {"type": "string", "format": "uuid"},
                },
                "required": ["contact_id", "tenant_id"],
            }
        ),
        encoding="utf-8",
    )
    add.add_consumer("deals", CHANNEL, where, ["nickname:str"])
    view = _view(root)
    assert "    contact_id: UUID\n    nickname: str\n    tenant_id: UUID\n" in view
    assert "# unverified" not in view
