"""Generate the fixture's consumer copies with the candidate generator:
shopmod (a module distribution, B1 `_generated`) and an application's
`app.shared` copy of the same provider (the unchanged app path)."""

import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1])  # a scratch copy of this fixture
sys.path.insert(0, str(root))
import sandloom.core.cli.consumer_copies as cc  # noqa: E402
import sandloom.core.cli.dto_copies as dc  # noqa: E402
import sandloom.core.cli.generated as gen  # noqa: E402

same = lambda text, path: text  # noqa: E731  (no ruff: byte-stable here)
files = cc.module_files("shopmod", root / "shopmod", root, ("sandloom.shared",), same)
for f in files:
    assert not f.problems, f.problems
    f.path.parent.mkdir(parents=True, exist_ok=True)
    for pkg in (f.path.parent.parent, f.path.parent):
        (pkg / "__init__.py").write_text("")
    f.path.write_text(f.content)
    print("wrote", f.path.relative_to(root))
# the application's copy (unchanged app path: <app>.shared.contracts)
from acme.contracts import QuotaV1Contract  # noqa: E402

shared = root / "app" / "shared" / "contracts"
shared.mkdir(parents=True, exist_ok=True)
for pkg in (root / "app", root / "app" / "shared", shared):
    (pkg / "__init__.py").write_text("")
r = dc.Resolver("acme", "app.shared.contracts.acme_dto", ("sandloom.shared",))
gen.render_stub("acme.quota.v1", QuotaV1Contract, r)
(shared / "acme_quota_v1.py").write_text(gen.render_stub("acme.quota.v1", QuotaV1Contract, r))
(shared / "acme_dto.py").write_text(dc.render_module("acme", r))
print("wrote app/shared/contracts/acme_quota_v1.py, acme_dto.py")
