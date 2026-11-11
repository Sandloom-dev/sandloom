"""kb-poc-contracts proof 11 (B4 trigger b): render every bundled provider's
stubs and DTO copies with ONE generator format under the installed pydantic,
formatted by the installed ruff, and print a digest per file.

    <venv>/bin/python -I p11_render.py
"""

import hashlib
import importlib.metadata as md
import os
import tempfile
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "kb-poc-contracts-not-a-secret-0123456789")
import sandloom.core.cli.consumer_copies as cc  # noqa: E402
import sandloom.core.cli.dto_copies as dc  # noqa: E402
import sandloom.core.cli.generated as gen  # noqa: E402

print("pydantic", md.version("pydantic"), "pydantic-core", md.version("pydantic-core"), "ruff", (md.version("ruff") if __import__("importlib.util").util.find_spec("ruff") else "absent"))
work = Path(tempfile.mkdtemp())
LINE = os.environ.get("LINE_LENGTH", "88")
(work / "pyproject.toml").write_text(f"[tool.ruff]\nline-length = {LINE}\n")
total = hashlib.sha256()
for pkg in ("sandloom_billing", "sandloom_identity", "sandloom_tenancy"):
    contracts = cc.provider_contracts(pkg)
    short = pkg.removeprefix("sandloom_")
    resolver = dc.Resolver(pkg, f"app.shared.contracts.{short}_dto", ("sandloom.shared",))
    for name, impl in sorted(contracts.items()):
        gen.render_stub(name, impl, resolver)
    out: dict[str, str] = {}
    for name, impl in sorted(contracts.items()):
        out[f"{gen.stub_names(name)[0]}.py"] = gen.render_stub(name, impl, resolver)
    if resolver.copied:
        path = work / f"{short}_dto.py"
        out[path.name] = dc.ruff_formatted(dc.render_module(short, resolver), path, cwd=work)
    for file, text in sorted(out.items()):
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        total.update(f"{file}:{digest}".encode())
        print(f"  {file:42} {digest} ({len(text)} bytes)")
print("all files:", total.hexdigest()[:16])
