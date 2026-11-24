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

from sandloom.core.cli import consumer_copies, generated
from sandloom.core.cli.dto_copies import CopyError
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


def test_two_commands_with_one_class_name_are_refused() -> None:
    """``a_b`` and ``aB`` both become ``...AbArgs``: the second would replace
    the first, so the generator refuses instead of writing one of them."""

    class Clash:
        @classmethod
        @contract_method(command=True, idempotency_key="operation_key")
        async def a_b(cls, operation_key: str) -> None:
            return None

        @classmethod
        @contract_method(command=True, idempotency_key="operation_key")
        async def aB(cls, operation_key: str) -> None:
            return None

    with pytest.raises(CopyError) as raised:
        render_stub("sdkgen.clash.v1", Clash)
    assert "SdkgenClashV1ABArgs" in str(raised.value)
    assert "rename the command method" in str(raised.value)


def test_a_command_class_may_not_take_the_name_of_another_class_of_the_stub() -> None:
    """The Protocol and the access atoms already hold names of the stub."""
    runtimes = contract_schema(Ledger).methods
    imports = generated._Imports()
    reserved = [args_name(PROTOCOL, "charge")]
    with pytest.raises(CopyError):
        generated._render_args_types(
            CONTRACT, PROTOCOL, runtimes, reserved, imports, None
        )
    # without the clash the same commands render
    assert len(
        generated._render_args_types(CONTRACT, PROTOCOL, runtimes, [], imports, None)
    ) == len([r for r in runtimes.values() if r.kind == "command"])


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
    return {
        "customer_id": c.input.customer_id,
        "amount": 1,
        "operation_key": "k",  # L4
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
