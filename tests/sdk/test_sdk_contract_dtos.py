"""Contract DTOs authored in the provider, copied for callers (SANDLOOM-C012).

A provider declares its DTOs in its own module; ``sandloom generate`` writes a
wire-shape copy of each one a signature names into the shared DTO package,
and the stub names the copy. Over the whole closed set of DTO shapes:

- the copy accepts and refuses exactly the payloads the provider's class
  does, and reads them to the same data (property test);
- a ``Contract(REF)`` caller receives the copy's class, equal to what the
  provider returned, on the local path and over RPC (the RPC caller with and
  without the provider's package imported);
- the provider receives its own class for a copy the caller passes;
- what the copy cannot carry is refused when ``@contract`` decorates the
  provider, naming the field path and the fix.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import FastAPI
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from sandloom.core.cli import dto_copies, generated
from sandloom.core.contract_testing import (
    in_process_rpc_client,
    rpc_proxy_for,
    rpc_settings,
)
from sandloom.core.descriptors.contract_refs import copy_origin
from sandloom.core.facade import CoreFacade
from sandloom.sdk import Contract
from sandloom.sdk._decl import contracts as _contracts
from sandloom.sdk._decl.errors import ContractDtoError

#: The closed set, validator-free: every scalar, constraint, alias kind,
#: Literal / Enum, nested and discriminated models, containers, defaults.
SCHEMAS = '''
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Color(StrEnum):
    """A colour."""

    RED = "red"
    BLUE = "blue"


class Level(IntEnum):
    LOW = 1
    HIGH = 2


class Shape(str, Enum):
    CIRCLE = "circle"
    SQUARE = "square"


class Leaf(BaseModel):
    """A frozen leaf of immutable scalars (the fast conversion path)."""

    model_config = ConfigDict(frozen=True)

    n: int = Field(ge=0, le=100)
    note: str = Field(default="", max_length=10, pattern="^[a-z]*$")
    at: datetime | None = None


class Cat(BaseModel):
    kind: Literal["cat"] = "cat"
    lives: int = 9


class Dog(BaseModel):
    kind: Literal["dog"] = "dog"
    good: bool = True


class Zoo(BaseModel):
    """Every allowed shape at once."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )

    zoo_id: UUID
    name: str = Field(min_length=1, description="the zoo's name")
    color: Color = Color.RED
    level: Level = Level.LOW
    shape: Shape | None = None
    price: Decimal = Field(default=Decimal("1.50"), max_digits=6, decimal_places=2)
    ratio: float = Field(default=0.5, ge=0, allow_inf_nan=False)
    when: datetime | None = None
    day: date | None = None
    leaves: list[Leaf] = Field(default_factory=list)
    by_name: dict[str, Leaf] = Field(default_factory=dict)
    pair: tuple[int, str] = (1, "a")
    many: tuple[int, ...] = ()
    tags: frozenset[str] = frozenset()
    mode: Literal["a", "b"] = "a"
    pet: Annotated[Cat | Dog, Field(discriminator="kind")] | None = None
    scores: list[Annotated[int, Field(ge=0)]] = []
    child: Leaf | None = None
    explicit: int = Field(default=0, alias="Explicit", strict=True)
'''

CONTRACTS = '''
from __future__ import annotations

from sandloom.sdk import contract

from .schemas import Color, Leaf, Zoo


@contract("zoo.read.v1")
class ZooRead:
    """The zoo's reads."""

    async def echo(self, zoo: Zoo) -> Zoo:
        assert type(zoo) is Zoo  # the provider receives its own class
        return zoo

    async def leaf(self, n: int) -> Leaf | None:
        return Leaf(n=n, note="x") if n else None

    async def leaves(self, n: int) -> list[Leaf]:
        return [Leaf(n=i) for i in range(n)]

    async def color(self, color: Color) -> Color:
        assert type(color) is Color
        return color
'''


class _FakeUoW:
    session: Any = None

    async def __aenter__(self) -> _FakeUoW:
        self.session = type("S", (), {"info": {}})()
        return self

    async def __aexit__(self, *_: Any) -> None:
        self.session = None


class _FakeDatabase:
    def uow_factory(self) -> Callable[[], _FakeUoW]:
        return _FakeUoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


@pytest.fixture(scope="module")
def zoo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """The provider package, its generated copies and stub, imported."""
    root = tmp_path_factory.mktemp("zoo")
    name = f"zoo_{uuid.uuid4().hex[:8]}"
    package = root / name
    (package / "shared" / "contracts").mkdir(parents=True)
    for init in (package, package / "shared", package / "shared" / "contracts"):
        (init / "__init__.py").write_text("")
    (package / "schemas.py").write_text(textwrap.dedent(SCHEMAS))
    (package / "contracts.py").write_text(textwrap.dedent(CONTRACTS))
    sys.path.insert(0, str(root))
    try:
        schemas = importlib.import_module(f"{name}.schemas")
        contracts = importlib.import_module(f"{name}.contracts")
        resolver = dto_copies.Resolver(
            provider_package=name,
            dto_package=f"{name}.shared.contracts.zoo_dto",
            shared_packages=("sandloom.shared", f"{name}.shared"),
        )
        stub = generated.render_stub("zoo.read.v1", contracts.ZooRead, resolver)
        copy = dto_copies.render_module("zoo", resolver)
        target = package / "shared" / "contracts" / "zoo_dto.py"
        target.write_text(dto_copies.ruff_formatted(copy, target, cwd=root))
        (package / "shared" / "contracts" / "zoo_read_v1.py").write_text(stub)
        copies = importlib.import_module(f"{name}.shared.contracts.zoo_dto")
        ref = importlib.import_module(f"{name}.shared.contracts.zoo_read_v1")
        yield {
            "name": name,
            "schemas": schemas,
            "contracts": contracts,
            "copies": copies,
            "ref": ref.ZOO_READ_V1,
            "copy_text": target.read_text(),
        }
    finally:
        sys.path.remove(str(root))
        for loaded in [m for m in sys.modules if m.split(".")[0] == name]:
            del sys.modules[loaded]


@pytest.fixture
def core(zoo: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> CoreFacade:
    from sandloom.core.config.settings import settings as cfg

    for key, value in rpc_settings(
        "svc", allowed_calls={"svc": ["zoo.read.v1"]}
    ).items():
        monkeypatch.setattr(cfg, key, value)
    facade = CoreFacade(app=FastAPI())
    facade.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    facade.register_service("zoo.read.v1", zoo["contracts"].ZooRead)
    return facade


def _zoo_value(schemas: ModuleType) -> BaseModel:
    s = schemas
    return s.Zoo(
        zoo_id=uuid.uuid4(),
        name="north",
        color=s.Color.BLUE,
        level=s.Level.HIGH,
        shape=s.Shape.SQUARE,
        price="12.30",
        ratio=2.5,
        when="2026-10-28T10:00:00+00:00",
        day="2026-10-28",
        leaves=[s.Leaf(n=1), s.Leaf(n=2, note="ab")],
        by_name={"x": s.Leaf(n=3)},
        pair=(2, "b"),
        many=(1, 2, 3),
        tags=frozenset({"a", "b"}),
        mode="b",
        pet=s.Dog(good=False),
        scores=[0, 7],
        child=s.Leaf(n=4),
        Explicit=5,
    )


def _same(copy: BaseModel, original: BaseModel) -> None:
    """``copy`` is the generated class and carries ``original``'s data."""
    assert copy_origin(type(copy)) == dto_copies.origin_of(type(original))
    dump = {"mode": "json", "by_alias": True}
    actual, expected = copy.model_dump(**dump), original.model_dump(**dump)
    if "tags" in expected:
        # A frozenset has no wire ordering, including after a JSON round trip.
        actual["tags"], expected["tags"] = (
            sorted(actual["tags"]),
            sorted(expected["tags"]),
        )
    assert actual == expected
    # as an RPC caller reads it: every field arrives on the wire
    assert copy.model_fields_set == set(type(copy).model_fields)


def test_every_copied_class_is_marked_and_carries_the_wire_shape(
    zoo: dict[str, Any],
) -> None:
    copies, text = zoo["copies"], zoo["copy_text"]
    for cls_name in ("Color", "Level", "Shape", "Leaf", "Cat", "Dog", "Zoo"):
        cls = getattr(copies, cls_name)
        assert copy_origin(cls) == f"{zoo['name']}.schemas:{cls_name}"
    assert text.startswith(dto_copies.HEADER)
    assert "not the methods, properties or validators" in text
    assert "class Color(StrEnum):" in text and '"""A colour."""' in text
    assert "class Shape(str, Enum):" in text
    assert 'zoo_id: UUID = Field(alias="zooId")' in text  # alias_generator resolved
    assert 'explicit: int = Field(default=0, alias="Explicit", strict=True)' in text
    assert "alias_generator" not in text
    assert 'Field(discriminator="kind")' in text
    assert "list[Annotated[int, Field(ge=0)]]" in text
    zoo_copy, zoo_own = copies.Zoo, zoo["schemas"].Zoo
    assert zoo_copy.model_json_schema(by_alias=True)["properties"].keys() == (
        zoo_own.model_json_schema(by_alias=True)["properties"].keys()
    )


def test_the_stub_names_the_copies_and_passes_its_protocol(
    zoo: dict[str, Any],
) -> None:
    ref = zoo["ref"]
    assert ref.name == "zoo.read.v1"
    assert ref.protocol.__name__ == "ZooReadV1"
    hints = ref.protocol.echo.__annotations__
    assert hints["return"] == "Zoo"
    assert sys.modules[ref.protocol.__module__].Zoo is zoo["copies"].Zoo


# --------------------------------------------------------------------------- #
# The copy validates what the provider's class validates
# --------------------------------------------------------------------------- #

_JSON = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-5, 120)
    | st.floats(allow_nan=False, allow_infinity=False, width=32)
    | st.sampled_from(
        [
            "",
            "a",
            "ab",
            "abcdefghijk",
            "Z",
            "red",
            "blue",
            "circle",
            "cat",
            "dog",
            "1.5",
            "12.345",
            "2026-10-28",
            "2026-10-28T10:00:00Z",
            "10:30:00",
            "PT90S",
            str(uuid.UUID(int=7)),
        ]
    ),
    lambda inner: (
        st.lists(inner, max_size=3)
        | st.dictionaries(
            st.sampled_from(["n", "note", "kind", "x", "lives"]), inner, max_size=3
        )
    ),
    max_leaves=6,
)
_KEYS = [
    "zooId",
    "zoo_id",
    "name",
    "color",
    "level",
    "shape",
    "price",
    "ratio",
    "when",
    "day",
    "leaves",
    "byName",
    "pair",
    "many",
    "tags",
    "mode",
    "pet",
    "scores",
    "child",
    "Explicit",
    "explicit",
    "unknown",
]


def _outcome(cls: type[BaseModel], payload: Any, *, json: bool) -> Any:
    try:
        value = (
            cls.model_validate_json(payload) if json else cls.model_validate(payload)
        )
    except ValidationError as exc:
        return ("refused", sorted(str(e["loc"]) + e["type"] for e in exc.errors()))
    return ("read", value.model_dump(mode="json", by_alias=True))


@settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(changes=st.dictionaries(st.sampled_from(_KEYS), _JSON, max_size=6))
def test_the_copy_accepts_exactly_what_the_provider_accepts(
    zoo: dict[str, Any], changes: dict[str, Any]
) -> None:
    import json

    base = _zoo_value(zoo["schemas"]).model_dump(mode="json", by_alias=True)
    payload = {**base, **changes}
    own, copy = zoo["schemas"].Zoo, zoo["copies"].Zoo
    assert _outcome(copy, payload, json=False) == _outcome(own, payload, json=False)
    text = json.dumps(payload)
    assert _outcome(copy, text, json=True) == _outcome(own, text, json=True)
    for nested in ("Leaf", "Cat", "Dog"):
        part = changes.get("child", payload)
        assert _outcome(getattr(zoo["copies"], nested), part, json=False) == (
            _outcome(getattr(zoo["schemas"], nested), part, json=False)
        )


# --------------------------------------------------------------------------- #
# Local and RPC calls give the caller the copy
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["local", "rpc", "rpc-without-provider"])
async def test_a_caller_receives_the_copy_equal_to_the_providers_value(
    zoo: dict[str, Any],
    core: CoreFacade,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    schemas, copies = zoo["schemas"], zoo["copies"]
    caller = Contract(zoo["ref"])
    async with in_process_rpc_client(core) as client:
        target: Any = core.get_service("zoo.read.v1")
        if path != "local":
            target = rpc_proxy_for(core, "zoo.read.v1", client)
        if path == "rpc-without-provider":
            # a caller process that never imported the provider's package
            monkeypatch.delitem(sys.modules, schemas.__name__)
        monkeypatch.setattr(_contracts, "service", lambda name, optional=False: target)
        original = _zoo_value(schemas)
        argument = copies.Zoo.model_validate(original.model_dump(by_alias=True))
        echoed = await caller.echo(argument)  # a copy in, the provider gets its own
        assert type(echoed) is copies.Zoo
        _same(echoed, original)
        assert type(echoed.leaves[0]) is copies.Leaf and type(echoed.pet) is copies.Dog
        assert echoed.color is copies.Color.BLUE and echoed.level is copies.Level.HIGH
        leaf = await caller.leaf(3)
        assert type(leaf) is copies.Leaf
        _same(leaf, schemas.Leaf(n=3, note="x"))
        assert await caller.leaf(0) is None
        leaves = await caller.leaves(2)
        assert [type(x) for x in leaves] == [copies.Leaf, copies.Leaf]
        assert [x.n for x in leaves] == [0, 1]
        assert await caller.color(copies.Color.RED) is copies.Color.RED
        optional = Contract(zoo["ref"], optional=True).client()
        assert optional is not None
        assert type(await optional.leaf(5)) is copies.Leaf


def test_the_fast_path_copies_a_frozen_scalar_dto_without_revalidating(
    zoo: dict[str, Any],
) -> None:
    from sandloom.sdk._decl._caller import result_converter

    convert = result_converter(zoo["ref"].protocol, "leaf")
    assert convert is not None
    leaf = zoo["schemas"].Leaf(n=9, note="ok")
    copy = convert(leaf)
    assert type(copy) is zoo["copies"].Leaf
    _same(copy, leaf)
    assert convert(copy) is copy and convert(None) is None
    # A result naming no model or Enum needs no conversion at all.
    assert result_converter(zoo["ref"].protocol, "missing") is None


# --------------------------------------------------------------------------- #
# What a copy cannot carry is refused at @contract
# --------------------------------------------------------------------------- #


def _refusal(source: str) -> str:
    namespace: dict[str, Any] = {}
    header = (
        "from typing import Annotated, Any, Generic, TypeVar\n"
        "from pydantic import BaseModel, ConfigDict, Field, field_serializer\n"
        "from pydantic import StringConstraints\n"
        "import annotated_types\n"
        "from enum import Flag\n"
        "from sandloom.sdk import contract\n"
    )
    with pytest.raises(ContractDtoError) as exc_info:
        code = compile(
            header + textwrap.dedent(source), "<dto>", "exec", dont_inherit=True
        )
        exec(code, namespace)  # noqa: S102 - test fixture source
    return str(exc_info.value)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            """
            class D(BaseModel):
                x: float
            @contract("r.d.v1")
            class P:
                async def get(self) -> D: ...
            """,
            "D.x: `float` may be NaN or infinite",
        ),
        (
            """
            class D(BaseModel):
                items: list[int] = Field(default_factory=lambda: [1])
            @contract("r.d.v1")
            class P:
                async def get(self) -> D: ...
            """,
            "D.items: default_factory='<lambda>' cannot be copied",
        ),
        (
            """
            class D(BaseModel):
                n: Annotated[int, annotated_types.Predicate(lambda v: v > 0)]
            @contract("r.d.v1")
            class P:
                async def get(self, d: D) -> int: ...
            """,
            "D.n: the constraint Predicate",
        ),
        (
            """
            class D(BaseModel):
                s: Annotated[str, StringConstraints(to_upper=True)]
            @contract("r.d.v1")
            class P:
                async def get(self) -> D: ...
            """,
            "StringConstraints transformation",
        ),
        (
            """
            T = TypeVar("T")
            class Page(BaseModel, Generic[T]):
                items: list[T]
            @contract("r.d.v1")
            class P:
                async def get(self) -> Page[int]: ...
            """,
            "is a parametrised generic DTO",
        ),
        (
            """
            class D(BaseModel):
                x: int
                @field_serializer("x")
                def _x(self, v: int) -> str:
                    return str(v)
            @contract("r.d.v1")
            class P:
                async def get(self) -> D: ...
            """,
            "has a custom serializer",
        ),
        (
            """
            class F(Flag):
                A = 1
            @contract("r.d.v1")
            class P:
                async def get(self) -> F: ...
            """,
            "is a Flag enum",
        ),
    ],
)
def test_a_dto_the_copy_cannot_carry_is_refused_with_its_path(
    source: str, expected: str
) -> None:
    message = _refusal(source)
    assert message.startswith("[SANDLOOM-C012]")
    assert expected in message, message
    assert "fix:" in message


def test_a_validator_stays_the_providers(tmp_path: Path) -> None:
    """Allowed and not copied: the copy validates the field's type only."""
    from pydantic import field_validator

    class Even(BaseModel):
        n: int

        @field_validator("n")
        @classmethod
        def _even(cls, v: int) -> int:
            if v % 2:
                raise ValueError("odd")
            return v

    Even.__qualname__ = "Even"
    resolver = dto_copies.Resolver(
        provider_package=Even.__module__,
        dto_package="probe.shared.contracts.m_dto",
        shared_packages=("sandloom.shared",),
    )
    resolver.place(Even, "Even")
    text = dto_copies.render_module("m", resolver)
    assert "class Even(BaseModel):" in text and "_even" not in text


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("populate_by_name", [False, True])
def test_separate_matching_aliases_survive_the_real_caller_conversion(
    frozen: bool, populate_by_name: bool
) -> None:
    from pydantic import ConfigDict, Field, create_model

    from sandloom.sdk._decl._caller import _build
    from sandloom.sdk._decl._event_wire import check_contract_dto

    provider = create_model(
        "Aliased",
        __module__="alias_provider.schemas",
        __config__=ConfigDict(frozen=frozen, populate_by_name=populate_by_name),
        x=(int, Field(validation_alias="X", serialization_alias="X")),
    )
    check_contract_dto(provider, "Aliased")
    resolver = dto_copies.Resolver(
        "alias_provider", "alias_shared.contracts.alias_dto", ("sandloom.shared",)
    )
    resolver.collect(provider, "Aliased")
    namespace: dict[str, Any] = {"__name__": resolver.dto_package}
    exec(dto_copies.render_module("alias", resolver), namespace)  # noqa: S102 - generated source under test
    copy = namespace["Aliased"]
    converted = _build(copy)(provider(X=3))
    assert type(converted) is copy
    assert converted.x == 3
    assert converted.model_dump(mode="json", by_alias=True) == {"X": 3}
    assert copy.model_validate({"X": 4}).x == 4
    if not populate_by_name:
        with pytest.raises(ValidationError):
            copy.model_validate({"x": 4})


def test_same_named_provider_models_are_distinct_and_order_independent() -> None:
    from pydantic import create_model

    from sandloom.sdk._decl._caller import _build
    from sandloom.sdk._decl._event_wire import check_contract_dto

    read = create_model("State", __module__="state_provider.reads", a=(int, ...))
    write = create_model("State", __module__="state_provider.writes", b=(str, ...))
    names: list[dict[str, str]] = []
    for classes in ((read, write), (write, read)):
        resolver = dto_copies.Resolver(
            "state_provider", "state_shared.contracts.state_dto", ("sandloom.shared",)
        )
        for cls in classes:
            check_contract_dto(cls, cls.__name__)
            resolver.collect(cls, cls.__name__)
        namespace: dict[str, Any] = {"__name__": resolver.dto_package}
        exec(dto_copies.render_module("state", resolver), namespace)  # noqa: S102 - generated source under test
        copies = {
            copy_origin(cls): cls
            for cls in namespace.values()
            if isinstance(cls, type) and issubclass(cls, BaseModel) and copy_origin(cls)
        }
        assert len(copies) == 2
        assert _build(copies[dto_copies.origin_of(read)])(read(a=2)).a == 2
        assert _build(copies[dto_copies.origin_of(write)])(write(b="ok")).b == "ok"
        names.append({origin: cls.__name__ for origin, cls in copies.items()})
    assert names[0] == names[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["local", "rpc"])
async def test_generated_stubs_keep_same_named_models_and_enum_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    from types import SimpleNamespace
    from typing import get_type_hints

    name = "colliding_" + uuid.uuid4().hex[:8]
    package = tmp_path / name
    shared = package / "shared" / "contracts"
    shared.mkdir(parents=True)
    (package / "schemas").mkdir()
    for folder in (package, package / "schemas", package / "shared", shared):
        (folder / "__init__.py").write_text("")
    (package / "schemas" / "reads.py").write_text(
        "from pydantic import BaseModel\nclass State(BaseModel):\n    a: int\n"
    )
    (package / "schemas" / "writes.py").write_text(
        "from pydantic import BaseModel\nclass State(BaseModel):\n    b: str\n"
    )
    (package / "schemas" / "kinds.py").write_text(
        "from enum import StrEnum\nclass State(StrEnum):\n    READY = 'ready'\n"
    )
    (package / "contracts.py").write_text(
        textwrap.dedent("""
        from sandloom.sdk import contract
        from .schemas.reads import State as ReadState
        from .schemas.writes import State as WriteState
        from .schemas.kinds import State as KindState

        @contract("collision.read.v1")
        class Read:
            async def get(self) -> ReadState:
                return ReadState(a=7)

        @contract("collision.write.v1")
        class Write:
            async def get(self) -> WriteState:
                return WriteState(b="ok")

        @contract("collision.kind.v1")
        class Kind:
            async def get(self) -> KindState:
                return KindState.READY
    """)
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        providers = importlib.import_module(name + ".contracts")
        names = ["collision.read.v1", "collision.write.v1", "collision.kind.v1"]
        implementations = dict(
            zip(names, [providers.Read, providers.Write, providers.Kind], strict=True)
        )
        entry = SimpleNamespace(
            name=name, contracts_provided=[SimpleNamespace(name=n) for n in names]
        )
        facade = SimpleNamespace(
            service_registry=SimpleNamespace(try_get=implementations.get)
        )
        files = generated._contract_files(
            entry,
            facade,
            shared.parent,
            name + ".shared",
            ("sandloom.shared", name + ".shared"),
            lambda text, _path: text,
        )
        assert all(not item.problems for item in files)
        for item in files:
            item.path.write_text(item.content)
        references = []
        for contract_name in names:
            stem, _protocol, ref = generated.stub_names(contract_name)
            module = importlib.import_module(name + ".shared.contracts." + stem)
            references.append(getattr(module, ref))
        types = [get_type_hints(ref.protocol.get)["return"] for ref in references]
        assert len(set(types)) == 3
        for key, value in rpc_settings("svc", allowed_calls={"svc": names}).items():
            from sandloom.core.config.settings import settings as cfg

            monkeypatch.setattr(cfg, key, value)
        core = CoreFacade(app=FastAPI())
        core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
        for contract_name, provider in implementations.items():
            core.register_service(contract_name, provider)
        async with in_process_rpc_client(core) as client:
            targets = {
                n: core.get_service(n)
                if path == "local"
                else rpc_proxy_for(core, n, client)
                for n in names
            }
            monkeypatch.setattr(
                _contracts, "service", lambda n, optional=False: targets[n]
            )
            results = [await Contract(ref).get() for ref in references]
        assert [type(result) for result in results] == types
        assert (
            results[0].a == 7 and results[1].b == "ok" and results[2].value == "ready"
        )
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                del sys.modules[key]
