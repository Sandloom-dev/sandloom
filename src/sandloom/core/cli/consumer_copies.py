"""Consumer copies owned by the consuming distribution (ADR-0013, KB-20).

A module that ships as its own distribution (a top-level module package: the
framework's modules, an ecosystem module) holds what it consumes in a
reserved subpackage of its own package::

    <module>/_generated/contracts/<x_y_vN>.py            the stub
    <module>/_generated/contracts/<provider>_dto.py       the DTO copies
    <module>/_generated/contracts/<x_y_vN>.describe.json  the expectation

The module's ``consumes`` is the single source of the set (ADR-0013 decision
2). ``sandloom generate`` plans it twice:

- **before composition**, from the declarations' AST (module-level
  ``Contract(REF)`` and ``define_module(consumes=[...])``), so a module that
  imports a stub it has not generated yet still loads; the stubs and DTO
  copies are written from the importable providers, never composed;
- **after composition**, from the composed ``consumes``: the expectations
  narrowed to the methods each entry lists. The two sets must be equal, and
  an entry with no method list is refused with a fix naming ``consumes=``.
  A module-level client the module never names again is refused (a
  ``consumes`` entry for nothing); the copies of a contract no longer
  consumed are removed (``--check`` reports them).

Every consumer file carries a provenance header whose digest covers the body
(decision 7), and every expectation the digest of its content, so a hand edit
is found without the provider. With the provider not importable (decision 5),
a committed copy whose digest verifies is kept; an edited or missing one is
refused (SANDLOOM-T018). ``sandloom check`` refuses any other file in
``_generated/`` (SANDLOOM-C032). DTO copies are
rendered in their final layout by the generator, never by the project's
formatter (decision 6). Kernel contract families (``<m>.claims.v1``,
``<m>.facts.v1``) have kernel-defined shapes and get no copy.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sandloom.core.cli.dto_copies as _dto_copies
import sandloom.core.contract_compat as _contract_compat
import sandloom.core.contract_wire as _contract_wire
import sandloom.core.descriptors.generated_provenance as _provenance
from sandloom.core.descriptors.module_names import installed_modules, module_name
from sandloom.core.module_api import contract_schema

#: Bumped only when the rendered output changes (provenance header).
GENERATOR_FORMAT = 2
GENERATED = _provenance.GENERATED
EXPECTATION_SUFFIX = _provenance.EXPECTATION_SUFFIX
#: A consumed contract's copy cannot be generated or verified.
CODE = "SANDLOOM-T018"
FIRST_LINE = _provenance.FIRST_LINE
INIT = _provenance.INIT
#: The layout of an expectation file (2: it carries its own ``digest``).
EXPECTATION_FORMAT = 2
_CONTRACT_ATTR = "__sandloom_contract__"
_SKIPPED_DIRS = frozenset({GENERATED, "tests", "migrations", "__pycache__"})
_FIX_CONSUMES = (
    "state the methods it calls in define_module(consumes=["
    'ContractRequirement("{name}", methods=("<method>", ...))])'
)


@dataclass(frozen=True)
class Stubs:
    """The stub renderer and its compatibility rule
    (:mod:`sandloom.core.cli.generated`, which imports this module)."""

    render: Callable[[str, Any, _dto_copies.Resolver], str]
    problems: Callable[[str | None, str], list[str]]


@dataclass
class Planned:
    """One file to write, compare, keep or remove (the fields of
    ``Generated``)."""

    path: Path
    content: str
    code: str
    problems: list[str] = field(default_factory=list)
    #: an orphan copy no consumed contract stands for any more: deleted by
    #: ``sandloom generate``, reported by ``--check``
    remove: bool = False


def is_kernel_family(contract: str) -> bool:
    """``<m>.claims.v1`` / ``<m>.facts.v1``: kernel-shaped, never copied."""
    parts = contract.split(".")
    return len(parts) == 3 and parts[1] in ("claims", "facts")


def stem_of(contract: str) -> str:
    return "_".join(contract.split("."))


def stem_collisions(contracts: Iterable[str]) -> dict[str, list[str]]:
    """``{stem: [contract, ...]}`` of the stems two contracts share (their
    Protocol name and constant collide too, ADR-0013 decision 8)."""
    by_stem: dict[str, list[str]] = {}
    for name in sorted(set(contracts)):
        by_stem.setdefault(stem_of(name), []).append(name)
    return {s: n for s, n in by_stem.items() if len(n) > 1}


def collision_problem(stem: str, names: Iterable[str]) -> str:
    return (
        f"stub name collision: contracts {' and '.join(sorted(names))} both map "
        f"to {stem}.py (and one Protocol and constant); rename one contract"
    )


# --------------------------------------------------------------------------- #
# Provenance header
# --------------------------------------------------------------------------- #


digest = _provenance.digest
read_header = _provenance.read_header
verify = _provenance.verify


def with_provenance(text: str, lines: Iterable[str]) -> str:
    """``text`` (a rendered stub or DTO module) under the provenance header,
    its legacy one-line header replaced; the digest covers the body."""
    legacy = _dto_copies.HEADER
    body = text[len(legacy) :] if text.startswith(legacy) else text
    head = [
        FIRST_LINE,
        *(f"# {line}" for line in lines),
        f"# generator: format {GENERATOR_FORMAT}  digest {digest(body)}",
    ]
    return "\n".join(head) + "\n" + body


def _json_format(doc: Mapping[str, Any]) -> str:
    """An expectation's generator format and its own layout's format."""
    return f"{doc.get('generator_format')}, expectation {doc.get('format')}"


def explain_diff(old: str | None, new: str) -> str:
    """Why a consumer file differs: a generator change (the contract
    fingerprint unchanged, another generator format) or a contract change;
    ``""`` for a file without provenance."""
    if old is None:
        return ""
    before: tuple[Any, Any]
    after: tuple[Any, Any]
    if old.lstrip().startswith("{") and new.lstrip().startswith("{"):
        try:
            a, b = json.loads(old), json.loads(new)
        except ValueError:
            return ""
        if not isinstance(a, dict) or not isinstance(b, dict):
            return ""
        before = (a.get("fingerprint"), _json_format(a))
        after = (b.get("fingerprint"), _json_format(b))
    else:
        meta_a, _ = read_header(old)
        meta_b, _ = read_header(new)
        if not meta_a or not meta_b:
            return ""
        before = (
            meta_a.get("fingerprint"),
            meta_a.get("format"),
        )
        after = (
            meta_b.get("fingerprint"),
            meta_b.get("format"),
        )
    if before[0] and before[0] == after[0] and before[1] != after[1]:
        return (
            f": generator change (format {before[1]} -> {after[1]}), contract "
            f"fingerprint unchanged ({before[0]})"
        )
    if before[0] != after[0]:
        return f": contract change ({before[0]} -> {after[0]})"
    return ": same contract and generator format (edited by hand?)"


# --------------------------------------------------------------------------- #
# Providers, found without composing them
# --------------------------------------------------------------------------- #


@dataclass
class Provider:
    contract: str
    impl: type
    package: str
    label: str


def _spec_name(value: Any) -> str | None:
    # ``type(value)`` first: a lazy proxy in a provider's namespace (the
    # settings object) loads itself on any attribute access, isinstance too.
    if not issubclass(type(value), type):
        return None
    spec = value.__dict__.get(_CONTRACT_ATTR)
    name = getattr(spec, "name", None)
    return name if isinstance(name, str) else None


def distribution_label(package: str) -> str:
    """The provider's distribution, else its import package (a source-tree
    or app-local provider)."""
    top = package.split(".", 1)[0]
    for module in installed_modules():
        if module.path in (package, top):
            return module.distribution
    return package


def importable_providers(
    source_root: Path | None, packages: Iterable[str] = ()
) -> dict[str, Provider]:
    """``{contract: provider}`` of every installed module, every top-level
    package of ``source_root`` and ``packages``: their ``contracts``
    submodule is imported (never composed, no ``register`` call). A contract
    two packages declare is left out (the composed app refuses it)."""
    candidates = [m.path for m in installed_modules()]
    if source_root is not None and source_root.is_dir():
        candidates += [
            child.name
            for child in sorted(source_root.iterdir())
            if (child / "__init__.py").is_file()
        ]
    candidates += list(packages)
    found: dict[str, Provider] = {}
    doubled: set[str] = set()
    for package in dict.fromkeys(candidates):
        for name, impl in _contracts_of(package).items():
            if name in found and found[name].package != package:
                doubled.add(name)
            found.setdefault(name, Provider(name, impl, package, ""))
    for name in doubled:
        del found[name]
    for provider in found.values():
        provider.label = distribution_label(provider.package)
    return found


def _contracts_of(package: str) -> dict[str, type]:
    target = f"{package}.contracts"
    try:
        if importlib.util.find_spec(target) is None:
            return {}
    except (ImportError, ValueError):
        return {}
    try:
        module = importlib.import_module(target)
    except (ImportError, RuntimeError, ValueError, TypeError, AttributeError):
        return {}  # an unimportable provider package is "absent" here
    out: dict[str, type] = {}
    for value in vars(module).values():
        name = _spec_name(value)
        if name is not None and value.__module__.startswith(package):
            out[name] = value
    return out


# --------------------------------------------------------------------------- #
# The planned set: what a module's declarations consume (AST, no import)
# --------------------------------------------------------------------------- #


@dataclass
class StaticPlan:
    """What one module's declarations say it consumes."""

    names: dict[str, str] = field(default_factory=dict)  # contract -> where
    #: generated stems the module imports (``<pkg>._generated.contracts.<x>``)
    imported: dict[str, str] = field(default_factory=dict)  # stem -> where
    problems: list[str] = field(default_factory=list)


def _module_files(directory: Path) -> list[Path]:
    return [
        path
        for path in sorted(directory.rglob("*.py"))
        if not _SKIPPED_DIRS.intersection(path.relative_to(directory).parts[:-1])
    ]


def _dotted(package: str, directory: Path, path: Path) -> str:
    rel = path.relative_to(directory).with_suffix("").parts
    parts = [package, *rel]
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _absolute(node: ast.ImportFrom, current: str, is_package: bool) -> str:
    if not node.level:
        return node.module or ""
    base = current.split(".")
    keep = len(base) - node.level + (1 if is_package else 0)
    parts = base[: max(keep, 0)]
    return ".".join([*parts, node.module] if node.module else parts)


class _File:
    """One parsed source file of the module: its imports and constants."""

    def __init__(self, package: str, directory: Path, path: Path) -> None:
        self.path = path
        self.dotted = _dotted(package, directory, path)
        self.tree = ast.parse(path.read_text(encoding="utf-8"))
        is_package = path.name == "__init__.py"
        self.imports: dict[str, str] = {}
        self.constants: dict[str, str] = {}
        for node in self.tree.body:
            if isinstance(node, ast.ImportFrom):
                base = _absolute(node, self.dotted, is_package)
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = f"{base}.{alias.name}"
            elif (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.constants[target.id] = node.value.value


class _Planner:
    """Reads the consumed contracts of one module package from its AST."""

    def __init__(
        self, package: str, directory: Path, stems: Mapping[str, list[str]]
    ) -> None:
        self.package = package
        self.directory = directory
        self.stems = stems
        self.files = {
            f.dotted: f
            for f in (_File(package, directory, p) for p in _module_files(directory))
        }
        self.plan = StaticPlan()
        self.prefix = f"{package}.{GENERATED}.contracts"

    def where(self, file: _File, node: ast.AST) -> str:
        rel = file.path.relative_to(self.directory.parent)
        return f"{rel}:{getattr(node, 'lineno', 0)}"

    def run(self) -> StaticPlan:
        clients: list[tuple[_File, str, ast.AST, str | None]] = []
        for file in self.files.values():
            self._imports(file)
            for node in file.tree.body:
                value = (
                    node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
                )
                if (
                    isinstance(value, ast.Call)
                    and _called(value) == "Contract"
                    and value.args
                ):
                    contract = self._reference(file, value.args[0], node)
                    clients += [(file, name, node, contract) for name in _targets(node)]
            for call in ast.walk(file.tree):
                if isinstance(call, ast.Call) and _called(call) == "define_module":
                    for keyword in call.keywords:
                        if keyword.arg == "consumes":
                            self._consumes(file, keyword.value)
        self._unused(clients)
        return self.plan

    def _unused(self, clients: list[tuple[_File, str, ast.AST, str | None]]) -> None:
        """A module-level ``x = Contract(REF)`` no file of the module ever
        names again: a consumes entry for nothing (ADR-0013 decision 2)."""
        if not clients:
            return
        named: set[str] = set()
        for file in self.files.values():
            named |= _names_read(file.tree)
        for file, name, node, contract in clients:
            if name in named:
                continue
            self.plan.problems.append(
                f"{self.where(file, node)}: {name} = Contract(...) is never used, "
                f"so {self.package} consumes {contract or 'a contract'} for "
                "nothing: call it, or drop the declaration"
            )

    def _imports(self, file: _File) -> None:
        for node in ast.walk(file.tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            base = _absolute(node, file.dotted, file.path.name == "__init__.py")
            if base == self.prefix:
                stems = [a.name for a in node.names]
            elif base.startswith(self.prefix + "."):
                stems = [base[len(self.prefix) + 1 :].split(".")[0]]
            else:
                continue
            for stem in stems:
                self.plan.imported.setdefault(stem, self.where(file, node))

    def _consumes(self, file: _File, value: ast.expr) -> None:
        if not isinstance(value, ast.List | ast.Tuple):
            self.plan.problems.append(
                f"{self.where(file, value)}: consumes= is not a literal list, so "
                "the generator cannot plan it before composition: write it as a "
                "list of ContractRequirement(...)"
            )
            return
        for item in value.elts:
            target = item
            if isinstance(item, ast.Call) and _called(item) == "ContractRequirement":
                if not item.args:
                    continue
                target = item.args[0]
            self._reference(file, target, item)

    def _reference(self, file: _File, node: ast.expr, at: ast.AST) -> str | None:
        name = self._resolve(file, node)
        if name is None:
            self.plan.problems.append(
                f"{self.where(file, at)}: the consumed contract cannot be read "
                "from the declaration (name it with a string literal, a module "
                "constant or a generated reference)"
            )
        elif name:
            self.plan.names.setdefault(name, self.where(file, at))
        return name

    def _resolve(self, file: _File, node: ast.expr) -> str | None:
        """The contract name an expression stands for (``""``: a stem that
        maps to no known contract, already reported)."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Attribute) and node.attr == "name":
            return self._resolve(file, node.value)
        if not isinstance(node, ast.Name):
            return None
        if node.id in file.constants:
            return file.constants[node.id]
        origin = file.imports.get(node.id)
        if origin is None:
            return None
        module, _, attr = origin.rpartition(".")
        source = self.files.get(module)
        if source is not None and attr in source.constants:
            return source.constants[attr]
        if ".contracts." in f"{module}." or module.endswith(".contracts"):
            stem = module.rsplit(".", 1)[-1]
            names = self.stems.get(stem, [])
            if len(names) == 1:
                return names[0]
            if len(names) > 1:
                self.plan.problems.append(collision_problem(stem, names))
                return ""
            self.plan.problems.append(
                f"{module}: no importable provider declares a contract with the "
                f"stub {stem!r} and no copy is committed"
            )
            return ""
        return None


def _targets(node: ast.AST) -> list[str]:
    """The plain names a module-level assignment binds."""
    if isinstance(node, ast.AnnAssign):
        return [node.target.id] if isinstance(node.target, ast.Name) else []
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    return []


def _names_read(tree: ast.AST) -> set[str]:
    """Every name a file reads, imports by name or reaches as an attribute."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.alias):
            out.add(node.name.rsplit(".", 1)[-1])
    return out


def _called(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def static_plan(
    package: str, directory: Path, stems: Mapping[str, list[str]]
) -> StaticPlan:
    """The contracts ``package``'s declarations consume, read from its AST
    (``stems`` maps a generated stub's stem to the contracts it may stand for)."""
    return _Planner(package, directory, stems).run()


def committed_contracts(out_dir: Path) -> list[str]:
    """The contracts of the expectations committed in ``out_dir``."""
    names = []
    for path in sorted(out_dir.glob(f"*{EXPECTATION_SUFFIX}")):
        try:
            names.append(str(json.loads(path.read_text(encoding="utf-8"))["contract"]))
        except (OSError, ValueError, KeyError):
            continue
    return names


# --------------------------------------------------------------------------- #
# Rendering one module's copies
# --------------------------------------------------------------------------- #


def stub_file(
    contract: str, provider: Provider, resolver: _dto_copies.Resolver, stubs: Stubs
) -> str:
    """The stub of ``contract`` as every writer writes it (provenance header)."""
    body = stubs.render(contract, provider.impl, resolver)
    impl = provider.impl
    return with_provenance(
        body,
        [
            *_contract_lines(contract, contract_schema(impl).hash),
            *_provider_lines(provider.label, f"{impl.__module__}.{impl.__qualname__}"),
        ],
    )


def _contract_lines(contract: str, fingerprint: str) -> list[str]:
    """``contract: <name>  fingerprint: <hash>``, two lines past 88 columns."""
    line = f"contract: {contract}  fingerprint: {fingerprint}"
    if len(line) + 2 <= _dto_copies.LINE:
        return [line]
    return [f"contract: {contract}", f"fingerprint: {fingerprint}"]


def _provider_lines(label: str, where: str) -> list[str]:
    """``provider: <label> (<where>)``; two lines (``provider``, ``class``)
    when one would pass the 88 columns a generated file keeps to."""
    line = f"provider: {label} ({where})"
    if len(line) + 2 <= _dto_copies.LINE:
        return [line]
    return [f"provider: {label}", f"class: {where}"]


def dto_file(
    short: str, resolver: _dto_copies.Resolver, providers: Iterable[Provider]
) -> str:
    """The DTO copies module of provider module ``short`` (provenance header,
    final layout: never the project's formatter)."""
    group = sorted(providers, key=lambda p: p.contract)
    label = group[0].label if group else "?"
    package = resolver.provider_package
    return with_provenance(
        _dto_copies.render_module(short, resolver),
        [
            *(
                line
                for p in group
                for line in _contract_lines(p.contract, contract_schema(p.impl).hash)
            ),
            *_provider_lines(label, package),
        ],
    )


def _narrowed(describe: Mapping[str, Any], methods: Iterable[str]) -> dict[str, Any]:
    wanted = set(methods)
    out = dict(describe)
    for key in ("methods", "codecs"):
        section = describe.get(key)
        if isinstance(section, Mapping):
            out[key] = {k: v for k, v in section.items() if k in wanted}
    return out


def _classes(impl: Any) -> dict[str, type]:
    """``{"<module>:<qualname>": class}`` of every model and Enum the
    provider's signatures name, nested ones included."""
    out: dict[str, type] = {}
    for runtime in contract_schema(impl).methods.values():
        for annotation in (*runtime.annotations.values(), runtime.returns):
            for cls in _contract_wire.nominal_types(annotation):
                out.setdefault(_dto_copies.origin_of(cls), cls)
    return out


def _with_extra(node: Any, classes: Mapping[str, type]) -> Any:
    """``node`` (a describe fragment) with each model's ``extra`` recorded
    when its class closes or opens it (``extra="forbid"``: a field added to
    it breaks an older copy, ADR-0013 decision 3)."""
    if isinstance(node, list):
        return [_with_extra(item, classes) for item in node]
    if not isinstance(node, dict):
        return node
    out = {k: _with_extra(v, classes) for k, v in node.items()}
    if out.get("k") == "model" and isinstance(out.get("ref"), str):
        config = getattr(classes.get(out["ref"]), "model_config", None)
        extra = config.get("extra") if isinstance(config, Mapping) else None
        if extra in ("forbid", "allow"):
            out["extra"] = str(extra)
    return out


def expectation(provider: Provider, methods: Iterable[str]) -> dict[str, Any]:
    """The ``.describe.json`` beside a stub: the provider's describe entry
    restricted to the consumed methods (decision 3; B2's forward-compatible
    shape, a ``describe`` key with room for ``dtos`` and ``docs``), with the
    digest of its content (a hand edit is found without the provider)."""
    schema = contract_schema(provider.impl)
    impl = provider.impl
    describe = schema.describe()
    doc = {
        "format": EXPECTATION_FORMAT,
        "contract": provider.contract,
        "fingerprint": schema.hash,
        "provider": {
            "distribution": provider.label,
            "class": f"{impl.__module__}.{impl.__qualname__}",
        },
        "generator_format": GENERATOR_FORMAT,
        "methods": sorted(set(methods)),
        "describe": _with_extra(_narrowed(describe, methods), _classes(impl)),
    }
    return _provenance.signed_expectation(doc)


def stub_problems(
    old: str | None, new: str, methods: Iterable[str] | None, stubs: Stubs
) -> list[str]:
    """A consumed method removed or changed in a regenerated copy (the
    methods the consumer does not list are not its business)."""
    problems = stubs.problems(old, new)
    if methods is None:
        return problems
    wanted = set(methods)
    return [p for p in problems if p.split("'")[1] in wanted]


@dataclass
class ModuleCopies:
    """Inputs of one consuming module's copies."""

    package: str
    directory: Path
    #: consumed contract -> its method list (``None``: not known yet, before
    #: composition; no expectation is written)
    uses: Mapping[str, tuple[str, ...] | None]
    providers: Mapping[str, Provider]
    stubs: Stubs
    shared_packages: tuple[str, ...] = ("sandloom.shared",)

    @property
    def out_dir(self) -> Path:
        return self.directory / GENERATED / "contracts"


def module_files(spec: ModuleCopies) -> list[Planned]:
    """Every generated file of one consuming module's copies, with its verdict."""
    out_dir = spec.out_dir
    names = sorted(n for n in spec.uses if not is_kernel_family(n))
    if not names:
        return []
    out: list[Planned] = [
        Planned(spec.directory / GENERATED / "__init__.py", INIT, CODE),
        Planned(out_dir / "__init__.py", INIT, CODE),
    ]
    for stem, same in stem_collisions(names).items():
        out.append(
            Planned(out_dir / f"{stem}.py", "", CODE, [collision_problem(stem, same)])
        )
    collided = {n for g in stem_collisions(names).values() for n in g}
    by_short: dict[str, list[Provider]] = {}
    for name in names:
        if name in collided:
            continue
        methods = spec.uses[name]
        if methods is not None and not methods:
            out.append(
                Planned(
                    out_dir / f"{stem_of(name)}{EXPECTATION_SUFFIX}",
                    "",
                    CODE,
                    [
                        f"{spec.package} consumes {name} with no method list (a "
                        "call the generator cannot resolve: .client(), getattr, a "
                        "client passed on); " + _FIX_CONSUMES.format(name=name)
                    ],
                )
            )
            continue
        provider = spec.providers.get(name)
        if provider is None:
            out += keep_committed(spec.package, name, out_dir)
            continue
        by_short.setdefault(module_name(provider.package), []).append(provider)
    for short, group in sorted(by_short.items()):
        out += _render_group(spec, short, group)
    return out


def _render_group(
    spec: ModuleCopies, short: str, group: list[Provider]
) -> list[Planned]:
    out_dir = spec.out_dir
    stem_dto = _dto_copies.dto_module_stem(short)
    resolver = _dto_copies.Resolver(
        provider_package=group[0].package,
        dto_package=f"{spec.package}.{GENERATED}.contracts.{stem_dto}",
        shared_packages=spec.shared_packages,
    )
    refused: list[str] = []
    for provider in group:  # every DTO placed before any stub is final
        try:
            stub_file(provider.contract, provider, resolver, spec.stubs)
        except _dto_copies.CopyError as exc:
            refused.append(f"{provider.contract}: {exc}")
    out: list[Planned] = []
    for provider in group:
        stem = stem_of(provider.contract)
        methods = spec.uses[provider.contract]
        path = out_dir / f"{stem}.py"
        content = (
            ""
            if refused
            else stub_file(provider.contract, provider, resolver, spec.stubs)
        )
        item = Planned(path, content, "SANDLOOM-T007", list(refused))
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if content:
            item.problems += stub_problems(current, content, methods, spec.stubs)
        out.append(item)
        if methods is not None:
            out.append(
                Planned(
                    out_dir / f"{stem}{EXPECTATION_SUFFIX}",
                    _contract_compat.render_snapshot(expectation(provider, methods)),
                    "SANDLOOM-T004",
                )
            )
    if resolver.copied and not refused:
        try:
            text = dto_file(short, resolver, group)
            problems: list[str] = []
        except _dto_copies.CopyError as exc:
            text, problems = "", [str(exc)]
        out.append(
            Planned(out_dir / f"{stem_dto}.py", text, _dto_copies.CODE, problems)
        )
    return out


def keep_committed(package: str, contract: str, out_dir: Path) -> list[Planned]:
    """No importable provider: keep the committed copy when its digest
    verifies; refuse an edited or missing one (SANDLOOM-T018)."""
    stem = stem_of(contract)
    stub = out_dir / f"{stem}.py"
    owner = contract.split(".", 1)[0]
    if not stub.is_file():
        return [
            Planned(
                stub,
                "",
                CODE,
                [
                    f"{package} consumes {contract}; its provider (module "
                    f"{owner!r}) is not importable here and no copy is committed: "
                    "install the provider as a development dependency of "
                    f"{package}, or commit the copy generated where it is installed"
                ],
            )
        ]
    out: list[Planned] = []
    expectation_path = out_dir / f"{stem}{EXPECTATION_SUFFIX}"
    paths = [stub, *_dto_files_of(stub)]
    if expectation_path.is_file():
        paths.append(expectation_path)
    for path in paths:
        text = path.read_text(encoding="utf-8")
        item = Planned(path, text, CODE)
        problem = (
            _provenance.verify_expectation(text)
            if path.name.endswith(EXPECTATION_SUFFIX)
            else verify(text)
        )
        if problem:
            item.problems.append(
                f"committed copy of {contract} (provider module {owner!r} not "
                f"importable here): {problem}; regenerate it where the provider "
                "is installed"
            )
        out.append(item)
    return out


def _dto_files_of(stub: Path) -> list[Path]:
    found: list[Path] = []
    for line in stub.read_text(encoding="utf-8").splitlines():
        if line.startswith("from ") and f".{GENERATED}.contracts." in line:
            path = stub.parent / f"{line.split()[1].rsplit('.', 1)[-1]}.py"
            if path.is_file() and path not in found:
                found.append(path)
    return found


# --------------------------------------------------------------------------- #
# A tree of module distributions
# --------------------------------------------------------------------------- #


def _stems(
    providers: Mapping[str, Provider], out_dirs: Iterable[Path]
) -> dict[str, list[str]]:
    names = set(providers)
    for out_dir in out_dirs:
        names.update(committed_contracts(out_dir))
    by_stem: dict[str, list[str]] = {}
    for name in sorted(names):
        by_stem.setdefault(stem_of(name), []).append(name)
    return by_stem


def _import_problems(
    package: str, plan: StaticPlan, wanted: Iterable[str], out_dir: Path
) -> list[Planned]:
    """An import of a generated stem no consumed contract stands for."""
    stems = {stem_of(n) for n in wanted}
    shorts = {f"{n.split('.', 1)[0]}_dto" for n in wanted}
    out: list[Planned] = []
    for stem, where in sorted(plan.imported.items()):
        if stem in stems or stem in shorts or stem == "__init__":
            continue
        out.append(
            Planned(
                out_dir / f"{stem}.py",
                "",
                CODE,
                [
                    f"{where}: {package} imports {package}.{GENERATED}.contracts."
                    f"{stem}, which no consumes entry stands for: declare the "
                    "contract (Contract(REF) at module level, or define_module("
                    "consumes=[...])), or drop the import"
                ],
            )
        )
    return out


def plan_static(
    modules: Mapping[str, Path],
    source_root: Path | None,
    stubs: Stubs,
    shared_packages: tuple[str, ...] = ("sandloom.shared",),
) -> tuple[list[Planned], dict[str, set[str]]]:
    """Before composition: the stubs and DTO copies of every module in
    ``modules`` (``{package: directory}``), from its declarations' AST and
    the importable providers; and ``{package: planned contracts}``."""
    providers = importable_providers(source_root, modules)
    stems = _stems(providers, (d / GENERATED / "contracts" for d in modules.values()))
    out: list[Planned] = []
    planned: dict[str, set[str]] = {}
    for package, directory in sorted(modules.items()):
        plan = static_plan(package, directory, stems)
        own = {n for n, p in providers.items() if p.package == package}
        names = {n for n in plan.names if not is_kernel_family(n) and n not in own}
        planned[package] = names
        out_dir = directory / GENERATED / "contracts"
        out += [
            Planned(out_dir / "__init__.py", "", CODE, [problem])
            for problem in plan.problems
        ]
        out += _import_problems(package, plan, names, out_dir)
        spec = ModuleCopies(
            package,
            directory,
            dict.fromkeys(names),
            {n: p for n, p in providers.items() if p.package != package},
            stubs,
            shared_packages,
        )
        out += module_files(spec)
    return out, planned


def plan_composed(
    modules: Mapping[str, Path],
    consumes: Mapping[str, Mapping[str, tuple[str, ...]]],
    planned: Mapping[str, set[str]],
    source_root: Path | None,
    stubs: Stubs,
    registry: Mapping[str, Provider] | None = None,
    shared_packages: tuple[str, ...] = ("sandloom.shared",),
) -> list[Planned]:
    """After composition: every file of each module's copies, expectations
    narrowed to the composed ``consumes`` (``{package: {contract:
    methods}}``); the planned set (:func:`plan_static`) must equal it."""
    providers = dict(importable_providers(source_root, modules))
    for name, provider in (registry or {}).items():
        providers.setdefault(name, provider)
    out: list[Planned] = []
    for package, directory in sorted(modules.items()):
        uses = {
            n: tuple(m)
            for n, m in consumes.get(package, {}).items()
            if not is_kernel_family(n)
        }
        own = {n for n, p in providers.items() if p.package == package}
        uses = {n: m for n, m in uses.items() if n not in own}
        out_dir = directory / GENERATED / "contracts"
        static = planned.get(package, set())
        for name in sorted(set(uses) ^ static):
            if name in uses:
                why = (
                    f"{package} consumes {name} (composed), but its declarations "
                    "do not name it where the generator plans copies before "
                    "composition (a route policy's contract, a computed name): "
                    + _FIX_CONSUMES.format(name=name)
                )
            else:
                why = (
                    f"{package}'s declarations name {name}, but the composed "
                    "consumes does not list it: drop the declaration"
                )
            out.append(Planned(out_dir / f"{stem_of(name)}.py", "", CODE, [why]))
        spec = ModuleCopies(
            package,
            directory,
            uses,
            {n: p for n, p in providers.items() if p.package != package},
            stubs,
            shared_packages,
        )
        files = module_files(spec)
        out += files + orphans(package, directory, {f.path for f in files})
    return out


def orphans(package: str, directory: Path, planned: set[Path]) -> list[Planned]:
    """The generated files in ``directory``'s ``_generated/`` that no planned
    file is any more (a contract no longer consumed): removed when the
    generator wrote them unchanged, refused when edited by hand. A file the
    generator never wrote is ``sandloom check``'s (SANDLOOM-C032)."""
    root = directory / GENERATED
    if not root.is_dir():
        return []
    out: list[Planned] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path in planned or "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        problem = _provenance.is_generated(path.name, text)
        if problem is None:
            out.append(Planned(path, "", CODE, remove=True))
        elif _written_by_generate(path.name, text):
            out.append(
                Planned(
                    path,
                    text,
                    CODE,
                    [
                        f"{package} no longer consumes the contract of this "
                        f"copy, and it was edited ({problem}): remove it"
                    ],
                )
            )
    return out


def _written_by_generate(name: str, text: str) -> bool:
    """A file the generator wrote, edited or not (its header, or an
    expectation's ``contract`` and ``generator_format`` keys)."""
    if name.endswith(EXPECTATION_SUFFIX):
        try:
            doc = json.loads(text)
        except ValueError:
            return False
        return isinstance(doc, dict) and {"contract", "generator_format"} <= set(doc)
    return text.startswith(FIRST_LINE)


__all__ = [
    "CODE",
    "EXPECTATION_FORMAT",
    "EXPECTATION_SUFFIX",
    "FIRST_LINE",
    "GENERATED",
    "GENERATOR_FORMAT",
    "ModuleCopies",
    "Planned",
    "Provider",
    "StaticPlan",
    "Stubs",
    "collision_problem",
    "committed_contracts",
    "distribution_label",
    "dto_file",
    "expectation",
    "explain_diff",
    "importable_providers",
    "is_kernel_family",
    "keep_committed",
    "module_files",
    "orphans",
    "plan_composed",
    "plan_static",
    "read_header",
    "static_plan",
    "stem_collisions",
    "stem_of",
    "stub_file",
    "verify",
    "with_provenance",
]
