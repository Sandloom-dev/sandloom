"""Public API 1.0 migration scanner (spec §2.1 rule 2, Appendix C).

Every existing capability keeps a public home in ``sandloom.sdk``. The name table
is Appendix C of ``tests/fixtures/specs/public-api-1.0-spec.md`` (one row per name
today: its 1.0 home and status). This script checks the table against the
code, in three directions:

1. **Unmapped kernel names.** Every public name of today's public modules
   (``public_api`` of ``architecture_rules.yml`` plus ``sandloom.shared.authz``,
   ``sandloom.shared.guards.*``, ``sandloom.core.testing``, ``sandloom.core.contract_testing``)
   has a row.
2. **Unmapped kernel imports.** Every ``from sandloom.core… / sandloom.shared… import``
   in the bundled modules (identity, tenancy, billing) and ``examples/``
   (tests included) names a row (contract DTO packages excepted).
3. **Homes exist.** Every row with a home in ``sandloom.sdk`` is exported there
   (``__all__``), new names included (H0: typed stubs); a name ADR-0009
   removes is exported nowhere in ``sandloom.sdk``.

Usage (from the repository root)::

    uv run python tools/packaging/public_api_migration.py          # check; exit 1 on a finding
    uv run python tools/packaging/public_api_migration.py --list   # print the resolved table

``tests/architecture/test_public_api_migration.py`` runs it in the suite
until the migration is complete (spec §2.1).
"""

from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND
SPEC = REPO / "tests" / "fixtures" / "specs" / "public-api-1.0-spec.md"
RULES = BACKEND / "architecture_rules.yml"

#: Today's public kernel modules beyond ``public_api`` (spec Appendix C).
EXTRA_PUBLIC = (
    "sandloom.shared.authz",
    "sandloom.shared.guards.auth",
    "sandloom.shared.guards.platform",
    "sandloom.shared.subject",
    "sandloom.shared.membership",
    "sandloom.core.testing",
    "sandloom.core.contract_testing",
)

#: Kernel implementations of the §2.3 NEW names, exported by today's public
#: kernel modules from hand-off H1 (WS-A) so the bundled modules can use them
#: until they migrate to ``sandloom.sdk`` (WS-C); each row names its 1.0 home.
#: Not Appendix C rows (they did not exist at d89189b6).
H1_KERNEL_NAMES: dict[str, str] = {
    "core.di.public": "sandloom.sdk.web",
    "core.di.no_idempotency": "sandloom.sdk.web",
    "core.di.replay_safe": "sandloom.sdk.web",
    "core.di.sensitive": "sandloom.sdk.web",
    "core.di.operation_intent": "sandloom.sdk.web",
    "core.di.writes": "sandloom.sdk.web",
    "core.di.allow_multi_db_write": "sandloom.sdk.web",
    "core.di.at_commit": "sandloom.sdk.web",
    "core.di.external_call": "sandloom.sdk.web",
    "core.di.release_check": "sandloom.sdk.web",
    "core.di.ResolvedAccount": "sandloom.sdk.web",
    "core.di.db": "sandloom.sdk.data",
    "core.di.objectstore": "sandloom.sdk.infra",
    "core.di.taskqueue": "sandloom.sdk.infra",
    "core.di.capability": "sandloom.sdk.infra",
    "core.di.sensitive_effect": "sandloom.sdk.effects",
    "core.di.mark_reauthenticated": "sandloom.sdk.effects",
    "core.di.request_operation_key": "sandloom.sdk.web",
    "core.di.SERVICE_PRINCIPAL_ID": "sandloom.sdk.security",
    "core.di.has_internal_access": "sandloom.sdk.security",
    "core.orm.MoneyLedger": "sandloom.sdk.data",
    # The id and timestamp idiom (2026-10-27, ledger C-SDK-06).
    "core.orm.new_id": "sandloom.sdk.data",
    "core.orm.CreatedAt": "sandloom.sdk.data",
    "core.orm.UpdatedAt": "sandloom.sdk.data",
    "shared.authz.Account": "sandloom.sdk.authz",
    "core.di.current_service": "sandloom.sdk.web",
    "core.di.authorizes": "sandloom.sdk.web",
    # The kernel home of ContractRef (WS-A dbd87f6f): generated contract
    # stubs import it from there, sandloom.sdk.contracts re-exports it.
    "core.descriptors.contract_refs.ContractRef": "sandloom.sdk.contracts",
}
#: Kernel names that moved out of ``sandloom.shared.schemas`` (gone) on
#: 2026-10-28, and the generated DTO copies' marker: kept where they are, no
#: ``sandloom.sdk`` home (the DTOs themselves are generated copies in
#: ``sandloom.shared.contracts``).
MOVED_KERNEL_NAMES: dict[str, str] = {
    "shared.subject.UserContext": "sandloom.shared.subject (the token subject)",
    "shared.membership.MemberEventAction": "sandloom.shared.membership (consumer rule)",
    "shared.membership.member_event_action": "sandloom.shared.membership (consumer rule)",
    "shared.membership.member_event_version": "sandloom.shared.membership (consumer rule)",
    "shared.membership.refreshed_state_wins": "sandloom.shared.membership (consumer rule)",
    "core.descriptors.contract_refs.dto_copy": "generated DTO copies (kernel home)",
}
#: H1 kernel names whose ``sandloom.sdk`` home has another name (``<module>.<name>``).
H1_RENAMED_KERNEL_NAMES: dict[str, str] = {
    "core.di.get_service": "sandloom.sdk.contracts.service",
}

#: Packages whose imports are contract DTOs / shared schemas: their home is
#: the DTO package itself (spec §2.1 rule 1), not ``sandloom.sdk``.
DTO_PACKAGES = (
    "sandloom.shared.contracts",
    "sandloom.shared.events",
)

#: Trees whose kernel imports are scanned (spec §2.1 rule 2 (b)).
SCANNED_TREES = (
    BACKEND / "src" / "sandloom" / "identity",
    BACKEND / "src" / "sandloom" / "tenancy",
    BACKEND / "src" / "sandloom" / "billing",
    REPO / "examples",
)

_ROW = re.compile(
    r"^\| `([^`]+)`(?: \((dup|imported directly)\))? \| (.*?) \| (\w+) \|"
)
_SDK_TARGET = re.compile(r"sandloom\.sdk(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


@dataclass(frozen=True)
class Row:
    today: str  # "core.di.DbSession" / "shared.authz.Role" / "core.testing.fixtures.*"
    home: str  # the "1.0 home" cell as written
    status: str  # kept | renamed | replaced | changed | adr0009
    note: str  # "", "dup" or "imported directly"

    @property
    def name(self) -> str:
        return self.today.rsplit(".", 1)[1]

    def targets(self) -> list[tuple[str, str]]:
        """(sdk module, exported name) pairs this row requires."""
        if self.status == "adr0009":
            return []
        found = _SDK_TARGET.findall(self.home)
        if not found:
            return []  # DTO package or a ModuleAPI method (checked separately)
        if self.status in ("kept", "changed"):
            return [(found[0], self.name)]
        # renamed / replaced: each target is <module>.<new name>
        return [(t.rpartition(".")[0], t.rpartition(".")[2]) for t in found]


def parse_table(spec: Path = SPEC) -> list[Row]:
    text = spec.read_text(encoding="utf-8")
    section = text.split("## Appendix C: generated capability table", 1)[1]
    section = section.split("\n## ", 1)[0]
    rows: list[Row] = []
    for line in section.splitlines():
        m = _ROW.match(line)
        if m:
            rows.append(
                Row(m.group(1), m.group(3).strip(), m.group(4), m.group(2) or "")
            )
    return rows


def _key(module: str, name: str) -> str:
    """Table key of ``module.name`` (``sandloom.core.di`` -> ``core.di``)."""
    return f"{module.removeprefix('sandloom.')}.{name}"


def _public_modules() -> list[str]:
    yaml = importlib.import_module("yaml")
    data: dict[str, Any] = yaml.safe_load(RULES.read_text()) or {}
    modules = [
        m for m in data.get("public_api", []) or [] if m.startswith("sandloom.core")
    ]
    return sorted(dict.fromkeys([*modules, *EXTRA_PUBLIC]))


def _public_names(module: Any) -> list[str]:
    explicit = getattr(module, "__all__", None)
    if isinstance(explicit, (list, tuple)):
        names = {str(x) for x in explicit}
    else:
        # No __all__: the names the module defines itself (not its imports).
        names = {
            n
            for n in dir(module)
            if getattr(getattr(module, n), "__module__", module.__name__)
            == module.__name__
        }
    return sorted(
        n
        for n in names
        if not n.startswith("_") and not inspect.ismodule(getattr(module, n, None))
    )


def unmapped_public_names(keys: set[str]) -> list[str]:
    return [
        _key(mod_name, name)
        for mod_name in _public_modules()
        for name in _public_names(importlib.import_module(mod_name))
        if _key(mod_name, name) not in keys
    ]


def _py_files(trees: Iterable[Path]) -> Iterator[Path]:
    for tree in trees:
        if tree.exists():
            yield from sorted(p for p in tree.rglob("*.py") if ".venv" not in p.parts)


def _kernel(module: str) -> bool:
    return module.startswith(
        ("sandloom.core", "sandloom.shared")
    ) and not module.startswith(DTO_PACKAGES)


def _node_keys(node: ast.AST) -> list[str]:
    """Table keys of one import statement (kernel imports only)."""
    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        if _kernel(node.module):
            return [_key(node.module, alias.name) for alias in node.names]
    elif isinstance(node, ast.Import):
        return [
            alias.name.removeprefix("sandloom.") + ".<module>"
            for alias in node.names
            if _kernel(alias.name)
        ]
    return []


def kernel_imports(trees: Iterable[Path] = SCANNED_TREES) -> dict[str, set[str]]:
    """``table key -> files`` for every kernel import in ``trees``."""
    found: dict[str, set[str]] = {}
    for path in _py_files(trees):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = str(path.relative_to(REPO))
        for node in ast.walk(tree):
            for key in _node_keys(node):
                found.setdefault(key, set()).add(rel)
    return found


def unmapped_imports(keys: set[str]) -> dict[str, set[str]]:
    return {
        key: files for key, files in sorted(kernel_imports().items()) if key not in keys
    }


def _sdk_exports(module: str) -> set[str] | None:
    try:
        mod = importlib.import_module(module)
    except ImportError:
        return None
    return {str(n) for n in getattr(mod, "__all__", ())}


def missing_homes(rows: list[Row]) -> list[str]:
    problems: list[str] = []
    for row in rows:
        if row.status != "adr0009" and "ModuleAPI." in row.home:
            attr = row.home.split("ModuleAPI.", 1)[1].split()[0]
            api = importlib.import_module("sandloom.sdk.module").ModuleAPI
            if not hasattr(api, attr):
                problems.append(f"{row.today}: ModuleAPI.{attr} missing")
            continue
        for module, name in row.targets():
            exports = _sdk_exports(module)
            if exports is None:
                problems.append(f"{row.today}: home module {module} missing")
            elif name == "*":
                source = importlib.import_module("sandloom." + row.today[: -len(".*")])
                wanted = set(getattr(source, "__all__", ()))
                if not wanted <= exports:
                    problems.append(
                        f"{row.today}: {module} lacks {sorted(wanted - exports)}"
                    )
            elif name not in exports:
                problems.append(f"{row.today}: {module}.{name} not exported")
    return problems


def removed_still_exported(rows: list[Row]) -> list[str]:
    removed = {row.name for row in rows if row.status == "adr0009"}
    sdk = importlib.import_module("sandloom.sdk")
    return [
        f"{info}.{name}: removed by ADR-0009, must not be public"
        for info in _sdk_modules(sdk)
        for name in sorted(removed & (_sdk_exports(info) or set()))
    ]


def _sdk_modules(sdk: Any) -> list[str]:
    pkgutil = importlib.import_module("pkgutil")
    return ["sandloom.sdk"] + [
        info.name
        for info in pkgutil.walk_packages(sdk.__path__, prefix="sandloom.sdk.")
        if not any(part.startswith("_") for part in info.name.split("."))
    ]


def run() -> list[str]:
    rows = (
        parse_table()
        + [
            Row(today, home, "kept", "new (H1)")
            for today, home in H1_KERNEL_NAMES.items()
        ]
        + [
            Row(today, home, "renamed", "new (H1)")
            for today, home in H1_RENAMED_KERNEL_NAMES.items()
        ]
        + [
            Row(today, home, "kept", "moved (2026-10-28)")
            for today, home in MOVED_KERNEL_NAMES.items()
        ]
    )
    keys = {row.today for row in rows}
    findings: list[str] = []
    findings += [f"unmapped public name: {k}" for k in unmapped_public_names(keys)]
    for key, files in unmapped_imports(keys).items():
        findings.append(
            f"unmapped kernel import: {key} ({', '.join(sorted(files)[:3])})"
        )
    findings += [f"missing home: {p}" for p in missing_homes(rows)]
    findings += [f"removed name exported: {p}" for p in removed_still_exported(rows)]
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--list", action="store_true", help="print the resolved table")
    args = parser.parse_args(argv)
    if args.list:
        for row in parse_table():
            targets = ", ".join(f"{m}.{n}" for m, n in row.targets()) or row.home
            print(f"{row.today:60} {row.status:9} {targets}")
        return 0
    findings = run()
    for line in findings:
        print(line)
    rows = parse_table()
    print(
        f"{len(rows)} table rows; {len(findings)} finding(s)"
        + ("" if findings else ": every name has a public home")
    )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
