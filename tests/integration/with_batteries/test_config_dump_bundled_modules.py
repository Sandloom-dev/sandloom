"""``sandloom config dump`` over the bundled modules (ADR-0015 decision 8,
KB-55, scenario P11).

The dump builds the composition the way the app does, in collect mode, and
only builds it: no application is served, no lifespan runs, and no connection
is opened (a socket spy over billing's PSP client built in ``setup``, identity
and tenancy). A bare install lists no setting of a module that is not
installed (D-H4). Real processes, an empty working directory and an explicit
environment; the harness is the kernel's.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.tests.test_boot_configuration_checks import (
    _DEV_BASE,
    _cli,
    _run,
)

pytestmark = pytest.mark.slow

_BATTERIES: dict[str, Any] = {
    **_DEV_BASE,
    "SANDLOOM_COMPOSITION_ROOT": "sandloom_standard.main",
    "TWOFA_ENCRYPTION_KEY": "9f1c4e7a2b8d6035e1f94c7ab23d58e06f7b1a9c4d2e85f3071b6c9ae4d2f815",
    "STRIPE_SECRET_KEY": "sk_live_not-printed-by-the-dump",
}

#: The dump run in-process behind a spy on every way a socket can connect and
#: on the ASGI entry (a served application or its lifespan).
_SPIED_DUMP = """
import asyncio, json, socket, sys
spied = {"connect": [], "served": 0}
def _spy(name, original):
    def wrapper(self, *args, **kwargs):
        spied["connect"].append(name)
        raise OSError("socket spy: no connection while building the composition")
    return wrapper
socket.socket.connect = _spy("connect", socket.socket.connect)
socket.socket.connect_ex = _spy("connect_ex", socket.socket.connect_ex)
socket.create_connection = lambda *a, **k: (spied["connect"].append("create_connection"), (_ for _ in ()).throw(OSError("spy")))[1]
_real_create = asyncio.BaseEventLoop.create_connection
async def _create(self, *a, **k):
    spied["connect"].append("create_connection (asyncio)")
    raise OSError("socket spy")
asyncio.BaseEventLoop.create_connection = _create
from starlette.routing import Router
_call = Router.__call__
async def _served(self, *a, **k):
    spied["served"] += 1
    return await _call(self, *a, **k)
Router.__call__ = _served
sys.argv = ["sandloom", "config", "dump", "--json"]
import sandloom
code = 0
try:
    sandloom.main()
except SystemExit as exc:
    code = exc.code or 0
during = list(spied["connect"])
try:
    socket.socket().connect(("127.0.0.1", 9))  # control: the spy must see this
except OSError:
    pass
sys.stderr.write(
    "SPIED " + json.dumps({"connect": during, "served": spied["served"], "exit": code,
                           "control": len(spied["connect"])}) + "\\n"
)
"""


@pytest.mark.subprocess_probe
def test_dump_builds_the_bundled_composition_with_no_connection_and_no_lifespan(
    tmp_path: Path,
) -> None:
    """P11: the socket spy finds no connection while billing builds its PSP
    client and every module registers; nothing is served."""
    proc = _run([sys.executable, "-c", _SPIED_DUMP], _BATTERIES, tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    (line,) = [ln for ln in proc.stderr.splitlines() if ln.startswith("SPIED ")]
    spied = json.loads(line.removeprefix("SPIED "))
    assert spied == {"connect": [], "served": 0, "exit": 0, "control": 1}, spied
    rows = json.loads(proc.stdout)
    modules = {row["module"] for row in rows if "module" in row}
    assert {"billing", "identity", "tenancy"} <= modules


@pytest.mark.subprocess_probe
def test_dump_lists_the_bundled_modules_values_redacted(tmp_path: Path) -> None:
    proc = _run(_cli("config", "dump", "--json"), _BATTERIES, tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "sk_live_not-printed-by-the-dump" not in proc.stdout
    assert _BATTERIES["TWOFA_ENCRYPTION_KEY"] not in proc.stdout
    rows = {row["name"]: row for row in json.loads(proc.stdout) if "module" in row}
    assert rows["TWOFA_ENCRYPTION_KEY"] == {
        "name": "TWOFA_ENCRYPTION_KEY",
        "value": "<redacted>",
        "source": "env",
        "secret": True,
        "module": "identity",
    }
    assert rows["STRIPE_SECRET_KEY"]["value"] == "<redacted>"
    assert rows["BILLING_PROVIDER"]["source"] == "default"
    assert rows["TENANT_CLAIMS_MAX_ORGANIZATIONS"]["module"] == "tenancy"


@pytest.mark.subprocess_probe
def test_dump_diagnoses_each_bundled_module_with_invalid_settings(
    tmp_path: Path,
) -> None:
    """Billing on Stripe without its keys: diagnosed, exit 0, the other
    modules still listed (the serving processes refuse the same value, G-CONFIG-07)."""
    values = {k: v for k, v in _BATTERIES.items() if k != "STRIPE_SECRET_KEY"}
    proc = _run(
        _cli("config", "dump"), {**values, "BILLING_PROVIDER": "stripe"}, tmp_path
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "BILLING_PROVIDER=stripe requires variables" in proc.stdout
    assert "# module identity" in proc.stdout


@pytest.mark.subprocess_probe
def test_a_composition_without_a_module_lists_none_of_its_settings(
    tmp_path: Path,
) -> None:
    """D-H4: a process that loads no battery (one module that declares no
    settings) shows no module block and no battery's variable, whatever is
    set in its environment (answer #25: the variables stay inert)."""
    package = tmp_path / "pkgs" / "plain_module"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from sandloom.sdk import define_module\n"
        "register = define_module(__name__, version='0.1.0', router=None)\n"
    )
    values = {
        **_DEV_BASE,
        "SANDLOOM_MODULES": ["plain_module"],
        "PYTHONPATH": str(tmp_path / "pkgs"),
        "STRIPE_SECRET_KEY": "sk_live_not-printed-by-the-dump",
    }
    proc = _run(_cli("config", "dump", "--json"), values, tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    rows = json.loads(proc.stdout)
    assert [row for row in rows if "module" in row] == []
    assert "TWOFA_ENCRYPTION_KEY" not in {row["name"] for row in rows}
    assert "sk_live_not-printed-by-the-dump" not in proc.stdout
