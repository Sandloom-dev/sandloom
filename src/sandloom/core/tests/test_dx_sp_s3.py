"""Typed saga step arguments (dx-sp-s3-typed-args).

The stub generator writes one ``TypedDict`` per command method, named from the
contract version and the method, holding the signature without the idempotency
parameter. A saga step's ``args`` function annotated with it is checked by
mypy; at runtime it stays a dict, so the boot check and the key injection are
the ones they were.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from pydantic import BaseModel

from sandloom.core.cli import consumer_copies, generated
from sandloom.core.cli.generated import args_name, render_stub
from sandloom.core.module_api import contract_schema
from sandloom.sdk.contracts import contract_method

REPO = Path(__file__).resolve().parents[4]
CONTRACT = "sdkgen.ledger.v1"
PROTOCOL = "SdkgenLedgerV1"


class Ledger:
    """Two commands (one with an optional parameter, one with only its key),
    a snake_case command, and a read."""

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def charge(
        cls,
        customer_id: uuid.UUID,
        amount: int,
        memo: str | None = None,
        *,
        operation_key: str,
    ) -> int:
        return amount

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def ping(cls, operation_key: str) -> None:
        return None

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def refund_charge(
        cls, customer_id: uuid.UUID, charge_key: str, operation_key: str
    ) -> int:
        return 0

    @classmethod
    @contract_method
    async def balance(cls, customer_id: uuid.UUID) -> int:
        return 0


class ReadsOnly:
    @classmethod
    @contract_method
    async def balance(cls, customer_id: uuid.UUID) -> int:
        return 0


#: What the generator writes after the reference declaration for ``Ledger``.
EXPECTED_ARGS = '''
class SdkgenLedgerV1ChargeArgs(TypedDict):
    """Arguments of ``charge``, without ``operation_key``."""

    customer_id: UUID
    amount: int
    memo: NotRequired[str | None]


class SdkgenLedgerV1PingArgs(TypedDict):
    """Arguments of ``ping``, without ``operation_key``."""


class SdkgenLedgerV1RefundChargeArgs(TypedDict):
    """Arguments of ``refund_charge``, without ``operation_key``."""

    customer_id: UUID
    charge_key: str
'''


def test_the_stub_writes_one_typeddict_per_command_without_the_key() -> None:
    stub = render_stub(CONTRACT, Ledger)
    declaration = (
        f"{CONTRACT.upper().replace('.', '_')}: ContractRef[{PROTOCOL}] = "
        f'ContractRef(\n    "{CONTRACT}", {PROTOCOL}\n)\n'
    )
    # the whole tail is pinned: names, order (by method), docstring, optional keys
    assert stub.endswith(declaration + "\n" + EXPECTED_ARGS)
    assert "from typing import NotRequired, Protocol, TypedDict\n" in stub
    # the read (balance) gets none; the key never appears in a class of its own
    assert "BalanceArgs" not in stub
    assert "    operation_key" not in EXPECTED_ARGS


def test_a_stub_without_a_command_is_what_it_was() -> None:
    stub = render_stub("sdkgen.reads.v1", ReadsOnly)
    assert "TypedDict" not in stub and "NotRequired" not in stub
    assert "from typing import Protocol\n" in stub


def test_the_names_follow_the_contract_version_and_the_method() -> None:
    assert args_name("PaymentsChargesV1", "charge") == "PaymentsChargesV1ChargeArgs"
    assert args_name("PaymentsChargesV1", "refund_charge").endswith("RefundChargeArgs")
    # a new contract version gets new names: no clash between v1 and v2
    assert args_name("PaymentsChargesV2", "charge") != args_name(
        "PaymentsChargesV1", "charge"
    )


def test_the_typeddict_holds_exactly_the_signature_minus_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Imports as the stub of an app does, and its keys are the parameters of
    the provider's method but the idempotency one (so a drift of either side
    fails here)."""
    (tmp_path / "sdkgen_ledger_v1.py").write_text(render_stub(CONTRACT, Ledger))
    monkeypatch.syspath_prepend(str(tmp_path))
    stub = importlib.import_module("sdkgen_ledger_v1")
    try:
        for name, runtime in contract_schema(Ledger).methods.items():
            if runtime.kind != "command":
                assert not hasattr(stub, args_name(PROTOCOL, name))
                continue
            args = getattr(stub, args_name(PROTOCOL, name))
            expected = [p for p in runtime.signature.parameters if p != "operation_key"]
            assert list(args.__annotations__) == expected, name
            # a dict at runtime: the saga's boot check reads a mapping
            assert args(**dict.fromkeys(expected)) == dict.fromkeys(expected)
    finally:
        sys.modules.pop("sdkgen_ledger_v1", None)


class Clash:
    """``a_b`` and ``aB`` both become ``...ABArgs``, ``ok`` is unaffected."""

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def a_b(cls, operation_key: str) -> None:
        return None

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def aB(cls, operation_key: str) -> None:
        return None

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def ok(cls, n: int, operation_key: str) -> None:
        return None


def test_two_commands_with_one_class_name_get_no_class_but_the_stub_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second would replace the first, so neither gets a class: a comment
    stands in its place, the Protocol methods and the other commands'
    classes are still generated, and the stub imports."""
    stub = render_stub("sdkgen.clash.v1", Clash)
    contract_hint = "rename the method in a new major (sandloom generate --new-major"
    assert (
        "# no SdkgenClashV1ABArgs for aB: clashes with method a_b; "
        f"{contract_hint} sdkgen.clash.v1)\n"
    ) in stub
    assert (
        "# no SdkgenClashV1ABArgs for a_b: clashes with method aB; "
        f"{contract_hint} sdkgen.clash.v1)\n"
    ) in stub
    assert "class SdkgenClashV1ABArgs" not in stub
    assert "async def a_b(" in stub and "async def aB(" in stub
    assert "class SdkgenClashV1OkArgs(TypedDict):" in stub
    (tmp_path / "sdkgen_clash_v1.py").write_text(stub)
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        module = importlib.import_module("sdkgen_clash_v1")
        assert hasattr(module, "SdkgenClashV1OkArgs")
        assert not hasattr(module, "SdkgenClashV1ABArgs")
    finally:
        sys.modules.pop("sdkgen_clash_v1", None)
    # without the clash the commands render as before
    assert "# no " not in render_stub(CONTRACT, Ledger)


def test_a_stub_with_a_skipped_class_is_ruff_formatted(tmp_path: Path) -> None:
    """The comment between and after the classes is what ``ruff format`` writes."""
    path = tmp_path / "sdkgen_clash_v1.py"
    path.write_text(render_stub("sdkgen.clash.v1", Clash))
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", "--no-cache", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_skipped_class_is_warned_once_naming_the_pair_and_the_fix() -> None:
    stub = render_stub("sdkgen.clash.v1", Clash)
    warnings = generated.args_clash_warnings(stub + stub)  # two consumers' stubs
    assert warnings == [
        "warning: contract sdkgen.clash.v1: no SdkgenClashV1ABArgs for aB: clashes "
        "with method a_b; its saga args stay a Mapping. Rename the method in a new "
        "major (sandloom generate --new-major sdkgen.clash.v1)"
    ]
    assert generated.args_clash_warnings(render_stub(CONTRACT, Ledger)) == []


def test_generate_prints_the_warning_and_keeps_its_exit_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub = render_stub("sdkgen.clash.v1", Clash)
    files = [
        generated.Generated(Path("a/sdkgen_clash_v1.py"), stub, "SANDLOOM-T007"),
        generated.Generated(Path("b/sdkgen_clash_v1.py"), stub, "SANDLOOM-T007"),
    ]
    generated._warn_args_clashes(files, generated_ok=True)
    err = capsys.readouterr().err
    assert err.count("warning: contract sdkgen.clash.v1: no SdkgenClashV1ABArgs") == 1
    generated._warn_args_clashes(files, generated_ok=False)
    assert capsys.readouterr().err == ""


def test_a_command_class_taking_a_name_of_the_stub_gets_no_class() -> None:
    """The Protocol and the access atoms already hold names of the stub."""
    runtimes = contract_schema(Ledger).methods
    imports = generated._Imports()
    reserved = [args_name(PROTOCOL, "charge")]
    blocks = generated._render_args_types(
        CONTRACT, PROTOCOL, runtimes, reserved, imports, None
    )
    commands = [r for r in runtimes.values() if r.kind == "command"]
    assert len(blocks) == len(commands)
    comments = [b for b in blocks if b.startswith("# no ")]
    assert comments == [
        "# no SdkgenLedgerV1ChargeArgs for charge: clashes with another name of "
        "this stub; rename the method in a new major "
        f"(sandloom generate --new-major {CONTRACT})\n"
    ]
    # without the clash every command renders its class
    plain = generated._render_args_types(
        CONTRACT, PROTOCOL, runtimes, [], imports, None
    )
    assert not [b for b in plain if b.startswith("# no ")]


class SdkgenDtoV1PutArgs(BaseModel):
    """A DTO that takes the name the ``put`` command's TypedDict would get."""

    value: int


def test_a_command_class_may_not_shadow_a_dto_the_stub_imports() -> None:
    """A DTO named in a signature is imported into the stub: a TypedDict of
    that name would shadow it, so that command gets no class (a comment)."""

    class Dto:
        @classmethod
        @contract_method(command=True, idempotency_key="operation_key")
        async def put(cls, body: SdkgenDtoV1PutArgs, operation_key: str) -> None:
            return None

    stub = render_stub("sdkgen.dto.v1", Dto)
    assert "# no SdkgenDtoV1PutArgs for put: clashes with another name of" in stub
    assert "class SdkgenDtoV1PutArgs(TypedDict)" not in stub
    assert "SdkgenDtoV1PutArgs" in stub  # the imported DTO stays

    class Other:
        @classmethod
        @contract_method(command=True, idempotency_key="operation_key")
        async def put(cls, body: SdkgenDtoV1PutArgs, operation_key: str) -> None:
            return None

        @classmethod
        @contract_method(command=True, idempotency_key="operation_key")
        async def put_other(cls, body: int, operation_key: str) -> None:
            return None

    # the same DTO under a contract whose names differ renders
    assert "class SdkgenOtherV1PutArgs(TypedDict):" in render_stub(
        "sdkgen.other.v1", Other
    )


def test_a_class_line_over_the_limit_wraps_as_ruff_format_does() -> None:
    """``class Name(TypedDict):`` over 88 columns becomes the base on its own
    line, no trailing comma: a rendered stub equals ``ruff format``'s output."""

    class Long:
        @classmethod
        @contract_method(command=True, idempotency_key="operation_key")
        async def charge_the_customer_for_the_whole_order(
            cls, amount: int, operation_key: str
        ) -> None:
            return None

    stub = render_stub("sdkgen.averylongcontractnamethatgoeson.v1", Long)
    name = (
        "SdkgenAverylongcontractnamethatgoesonV1ChargeTheCustomerForTheWholeOrderArgs"
    )
    assert len(f"class {name}(TypedDict):") > 88
    assert f"\n\nclass {name}(\n    TypedDict\n):\n" in stub
    assert f"class {name}(TypedDict)" not in stub
    # a short name stays on one line
    assert "class SdkgenLedgerV1PingArgs(TypedDict):\n" in render_stub(CONTRACT, Ledger)


def test_the_generator_format_moved_so_an_old_stub_reads_as_a_generator_change() -> (
    None
):
    """A stub of format 2 against the same contract in format 3 is explained as
    a generator change, not as a hand edit."""
    assert consumer_copies.GENERATOR_FORMAT == 3
    lines = ["contract: sdkgen.ledger.v1  fingerprint: abc"]
    body = '"""x"""\n'
    old = consumer_copies.with_provenance(body, lines).replace("format 3", "format 2")
    new = consumer_copies.with_provenance(body + "# more\n", lines)
    why = consumer_copies.explain_diff(old, new)
    assert "generator change (format 2 -> 3)" in why


_GOOD = """
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from sandloom.sdk import Contract
from sandloom.sdk.sagas import SagaContext, step
from sdkgen_ledger_v1 import (
    SDKGEN_LEDGER_V1,
    SdkgenLedgerV1ChargeArgs,
    SdkgenLedgerV1RefundChargeArgs,
)


class Enroll(BaseModel):
    customer_id: UUID
    price: int


ledger = Contract(SDKGEN_LEDGER_V1)


def charge_args(c: SagaContext[Enroll]) -> SdkgenLedgerV1ChargeArgs:
    return {"customer_id": c.input.customer_id, "amount": c.input.price}


def charge_with_memo(c: SagaContext[Enroll]) -> SdkgenLedgerV1ChargeArgs:
    return {"customer_id": c.input.customer_id, "amount": 1, "memo": "m"}


def refund_args(c: SagaContext[Enroll]) -> SdkgenLedgerV1RefundChargeArgs:
    return {"customer_id": c.input.customer_id, "charge_key": c.key("charge")}


CHARGE = step(
    "charge", call=ledger.charge, args=charge_args, compensate=ledger.refund_charge,
    compensate_args=refund_args,
)
INLINE = step(
    "inline", call=ledger.charge,
    args=lambda c: {"customer_id": c.input.customer_id, "amount": c.input.price},
)
"""

_BAD = """
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from sandloom.sdk.sagas import SagaContext
from sdkgen_ledger_v1 import SdkgenLedgerV1ChargeArgs


class Enroll(BaseModel):
    customer_id: UUID
    price: int


def misspelled_key(c: SagaContext[Enroll]) -> SdkgenLedgerV1ChargeArgs:
    return {"customer_id": c.input.customer_id, "amount": 1, "mem": "m"}  # L1


def wrong_type(c: SagaContext[Enroll]) -> SdkgenLedgerV1ChargeArgs:
    return {"customer_id": c.input.customer_id, "amount": "10"}  # L2


def missing_key(c: SagaContext[Enroll]) -> SdkgenLedgerV1ChargeArgs:
    return {"customer_id": c.input.customer_id}  # L3


def passes_the_idempotency_key(c: SagaContext[Enroll]) -> SdkgenLedgerV1ChargeArgs:
    return {  # L4
        "customer_id": c.input.customer_id,
        "amount": 1,
        "operation_key": "k",
    }
"""


@pytest.mark.slow
def test_mypy_checks_the_keys_and_types_of_an_args_function(tmp_path: Path) -> None:
    """The generated stub, as mypy reads it: a correct args function (named,
    optional key, compensation, a lambda) passes ``--strict``; a misspelled key,
    a wrong type, a missing key and the idempotency key each fail, on their
    own line."""
    (tmp_path / "sdkgen_ledger_v1.py").write_text(render_stub(CONTRACT, Ledger))
    (tmp_path / "good.py").write_text(_GOOD)
    (tmp_path / "bad.py").write_text(_BAD)
    config = REPO / "tests" / "typing" / "mypy.ini"
    assert config.is_file(), config
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(config),
            "--no-incremental",
            "--no-error-summary",
            str(tmp_path / "good.py"),
            str(tmp_path / "bad.py"),
        ],
        cwd=REPO,
        env={**os.environ, "MYPYPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    out = proc.stdout + proc.stderr
    errors = [line for line in out.splitlines() if ": error:" in line]
    assert not [e for e in errors if "good.py" in e], out
    bad = {
        int(line.split(":")[1]): line
        for line in errors
        if line.startswith(str(tmp_path / "bad.py"))
    }
    source = _BAD.splitlines()
    marked = {
        n + 1: text.rsplit("# ", 1)[1] for n, text in enumerate(source) if "# L" in text
    }
    # every marked line fails, with the error of its kind
    assert len(marked) == 4
    kinds = {
        "L1": 'Extra key "mem"',
        "L2": 'TypedDict item "amount" has type "int"',
        "L3": 'Missing key "amount"',
        "L4": 'Extra key "operation_key"',
    }
    assert set(kinds) == set(marked.values())
    for line_no, label in marked.items():
        assert kinds[label] in bad.get(line_no, ""), (label, out)
