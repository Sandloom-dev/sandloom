"""kb-poc-contracts proof 7: the generator reads an installed, UNCOMPOSED
provider's declarations (``sandloom_tenancy.contracts``) and renders the
consumer copy, recording what that import does: environment reads, file and
network I/O, and process-global registrations.

    <venv with sandloom + sandloom-tenancy>/bin/python -I p7_uncomposed.py
"""

from __future__ import annotations

import builtins
import os
import socket
import sys
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "kb-poc-contracts-not-a-secret-0123456789")

# Kernel imports the generator itself needs (before the measurement).
import sandloom.core.cli.consumer_copies as cc  # noqa: E402
import sandloom.core.cli.dto_copies as dc  # noqa: E402
import sandloom.core.cli.generated as gen  # noqa: E402
from sandloom.core.descriptors import contract_refs  # noqa: E402

env_reads: list[str] = []


class _RecordingEnviron(type(os.environ)):  # type: ignore[misc]
    def __getitem__(self, key):  # type: ignore[no-untyped-def]
        env_reads.append(str(key))
        return super().__getitem__(key)

    def get(self, key, default=None):  # type: ignore[no-untyped-def]
        env_reads.append(str(key))
        return super().get(key, default)

    def __contains__(self, key):  # type: ignore[no-untyped-def]
        env_reads.append(str(key))
        return super().__contains__(key)

    def __iter__(self):  # type: ignore[no-untyped-def]
        env_reads.append("<iterate all>")
        return super().__iter__()


os.environ.__class__ = _RecordingEnviron

opened: list[str] = []
_open = builtins.open
prefixes = tuple({sys.prefix, sys.base_prefix, sys.exec_prefix})


def _recording_open(file, *a, **k):  # type: ignore[no-untyped-def]
    path = str(file)
    if not path.startswith(prefixes):
        opened.append(path)
    return _open(file, *a, **k)


builtins.open = _recording_open
connects: list[object] = []
_connect = socket.socket.connect
socket.socket.connect = lambda self, addr: (connects.append(addr), _connect(self, addr))[1]  # type: ignore[method-assign]


def _globals() -> dict[str, int]:
    out = {"dto copies registered": sum(len(v) for v in contract_refs._COPIES.values())}
    try:
        from sandloom.core.orm import Base  # type: ignore[attr-defined]

        out["ORM tables (Base.metadata)"] = len(Base.metadata.tables)
    except Exception:  # noqa: BLE001
        pass
    from sandloom.core.config.settings import settings_loaded

    out["settings loaded"] = int(settings_loaded())
    out["sandloom_tenancy modules imported"] = sum(
        1 for m in sys.modules if m.startswith("sandloom_tenancy")
    )
    return out


before = _globals()
provider = cc.find_uncomposed("tenancy.membership.v1", None)
after = _globals()
print("composed app exists:", any(m == "sandloom.main" for m in sys.modules))
print(
    "provider found:",
    provider and f"{provider.distribution} {provider.impl.__module__}.{provider.impl.__qualname__}",
)
print("env reads during the provider import:", sorted(set(env_reads)) or "none")
print("files opened outside the interpreter/venv:", opened or "none")
print("network connects:", connects or "none")
for key in sorted(set(before) | set(after)):
    print(f"  {key}: {before.get(key)} -> {after.get(key)}")
# Render what generate would write for billing, from the uncomposed class.
resolver = dc.Resolver(
    provider_package="sandloom_tenancy",
    dto_package="sandloom_billing._generated.contracts.tenancy_dto",
    shared_packages=("sandloom.shared",),
)
assert provider is not None
stub = gen.render_stub("tenancy.membership.v1", provider.impl, resolver)
doc = cc.expectation_document(provider)
print("rendered stub:", len(stub), "bytes; copied DTOs:", [c.__qualname__ for c in resolver.copied])
print("expectation fingerprint:", doc["fingerprint"])
ref = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if ref is not None:
    meta, body = cc.strip_header(ref.read_text())
    print("equal to the committed stub body:", body == cc._strip_old_header(stub), f"(committed fingerprint {meta.get('fingerprint')})")
