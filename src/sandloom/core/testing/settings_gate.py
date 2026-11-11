"""The static settings gate of ``sandloom check`` and ``sandloom test``.

ADR-0015 decision 7, owner answers #25, #26, #30. A module declares its
settings with ``define_module(settings=Model)``; the gate imports every
module of the application (importing reads no environment and does no I/O)
and reads that declaration, never a running process:

* **Collisions** (SANDLOOM-B036): one environment name declared by two
  modules or by a module and the kernel, whether or not they ever load
  together (the runtime check sees only the modules one process loads).
* **Prefix rule** (SANDLOOM-T019, answer #30): a module's names start with its
  prefix (``INVENTORY_`` for ``inventory``). A name recorded as ``legacy`` in
  the committed ``module.snapshot.json`` is grandfathered; a new unprefixed
  name fails. :func:`settings_section` writes that record
  (``sandloom generate``) and never grandfathers a name of a module that had
  no snapshot yet.
* **Tiers** (answer #26): every semantic setting names a field of its model,
  and the tier the snapshot records is the tier the code declares (a
  semantic setting cannot slip in as ops unreviewed). Modules' own tests call
  :func:`semantic_ratchet_problems` for the ratchet.
* **Topology** (answer #25): for each application process of a deploy
  profile: a module named in ``SANDLOOM_MODULES`` that is no installed
  module and no importable package; a name of a module the service does not
  load; a literal value its declaration refuses. A profile carries
  ``${VAR:?...}`` placeholders and no production secrets, so names and literal
  values are all it can be held to; the values are validated where the
  process starts.

Every problem is one line that names its code and the fix.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import sandloom.core.testing.deploy_profile as _profile
from sandloom.core.config import module_settings as _ms
from sandloom.core.config.settings import Settings
from sandloom.core.descriptors import module_names as _names
from sandloom.core.observability.guardrails import is_secret_name

#: What importing a module or reading its declaration can raise: reported as
#: a problem of that module, never swallowed.
_FAILURES = (
    ImportError,
    AttributeError,
    LookupError,
    RuntimeError,
    TypeError,
    ValueError,
    SystemExit,
)
#: Prefix, tier, topology and literal-value problems of the gate.
CODE = "SANDLOOM-T019"
#: One environment name declared twice.
COLLISION_CODE = _ms.COLLISION_CODE
#: The key of the settings record in ``module.snapshot.json``.
SNAPSHOT_KEY = "settings"
#: Where a module's snapshot lies, next to its package.
SNAPSHOT_FILE = "module.snapshot.json"


@dataclass(frozen=True)
class DeclaredModule:
    """One module's declaration, read without running it."""

    #: the module's name (``inventory``): keys its prefix and risk namespace.
    module: str
    #: its import path (``shop.modules.inventory``).
    package: str
    model: type[BaseModel]
    #: ``{ENV_NAME: field name}``.
    names: Mapping[str, str]
    #: the env names classified semantic.
    semantic: frozenset[str]
    #: the package directory (where ``module.snapshot.json`` lies), if any.
    directory: Path | None = None

    @property
    def prefix(self) -> str:
        return f"{self.module.upper()}_"

    def tier(self, name: str) -> str:
        return "semantic" if name in self.semantic else "ops"


def _declaration(package: str) -> tuple[type[BaseModel] | None, Path | None]:
    module = importlib.import_module(package)
    register = getattr(module, "register", None)
    model = getattr(register, "__dict__", {}).get("sandloom_settings")
    where = getattr(module, "__file__", None)
    return model, (Path(where).parent if where else None)


def declared_modules(
    packages: Iterable[str],
) -> tuple[list[DeclaredModule], list[str]]:
    """The settings declarations of ``packages`` (import paths), and the
    problems of reading them: a package that cannot be imported, or a model
    that declares its settings badly."""
    out: list[DeclaredModule] = []
    problems: list[str] = []
    for package in dict.fromkeys(packages):
        try:
            model, directory = _declaration(package)
        except _FAILURES as exc:
            problems.append(
                f"{package}: cannot be imported for the settings gate "
                f"({type(exc).__name__}: {exc}) ({CODE})"
            )
            continue
        if model is None:
            continue
        try:
            _ms.check_declaration(model)
        except _FAILURES as exc:
            problems.append(f"{package}: {exc} ({CODE})")
            continue
        names = _ms.env_names(model)
        semantic = frozenset(s.name.upper() for s in _ms.declared_semantic(model))
        out.append(
            DeclaredModule(
                _names.module_name(package), package, model, names, semantic, directory
            )
        )
    return out, problems


# --------------------------------------------------------------- snapshot ----


def _is_prefixed(module: str, name: str) -> bool:
    return name.startswith(f"{module.upper()}_")


def settings_section(
    model: type[BaseModel], module: str, previous: Mapping[str, Any] | None
) -> dict[str, dict[str, Any]]:
    """The ``settings`` record of ``module.snapshot.json``: each declared env
    name with its tier, and ``"legacy": true`` on an unprefixed name that is
    grandfathered.

    ``previous``: the module's committed snapshot document, ``None`` when it
    has none. A name is legacy when the previous record says so, or when the
    module had a snapshot but no settings record yet (it adopts declared
    settings for names it already had). A module without a snapshot is new:
    its unprefixed names are not grandfathered (answer #30)."""
    old = (previous or {}).get(SNAPSHOT_KEY)
    semantic = {s.name.upper() for s in _ms.declared_semantic(model)}
    section: dict[str, dict[str, Any]] = {}
    for name in sorted(_ms.env_names(model)):
        entry: dict[str, Any] = {"tier": "semantic" if name in semantic else "ops"}
        if not _is_prefixed(module, name):
            adopted = previous is not None and old is None
            if adopted or (
                isinstance(old, dict) and (old.get(name) or {}).get("legacy")
            ):
                entry["legacy"] = True
        section[name] = entry
    return section


def _recorded(directory: Path | None) -> dict[str, Any] | None:
    """The committed ``settings`` record, ``None`` when the module has no
    snapshot or its snapshot has no record."""
    if directory is None:
        return None
    path = directory / SNAPSHOT_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    record = doc.get(SNAPSHOT_KEY) if isinstance(doc, dict) else None
    return record if isinstance(record, dict) else None


# ----------------------------------------------------------- module level ----


def collision_problems(
    declared: Sequence[DeclaredModule], kernel_names: Iterable[str]
) -> list[str]:
    """A name declared by two modules, or by a module and the kernel."""
    owners: dict[str, str] = dict.fromkeys(kernel_names, "the kernel")
    problems: list[str] = []
    for item in sorted(declared, key=lambda d: d.package):
        for name in item.names:
            if name in owners:
                problems.append(
                    f"{name} is declared by {owners[name]} and by module "
                    f"{item.package}, whether or not they load together: each "
                    "environment name has one owner; rename one of them (a "
                    f"module's names start with its prefix) ({COLLISION_CODE})"
                )
            else:
                owners[name] = f"module {item.package}"
    return problems


def prefix_problems(declared: Sequence[DeclaredModule]) -> list[str]:
    """A name without its module's prefix that the committed snapshot does not
    grandfather."""
    problems: list[str] = []
    for item in declared:
        record = _recorded(item.directory)
        for name in sorted(item.names):
            if _is_prefixed(item.module, name):
                continue
            entry = (record or {}).get(name)
            if isinstance(entry, dict) and entry.get("legacy"):
                continue
            problems.append(
                f"module {item.package}: {name} does not start with "
                f"{item.prefix}: a new setting's name carries its module's "
                f"prefix, e.g. {item.prefix}{name.removeprefix(item.prefix)} "
                f"(only a name {SNAPSHOT_FILE} records as legacy is "
                f"grandfathered) ({CODE})"
            )
    return problems


def tier_problems(declared: Sequence[DeclaredModule]) -> list[str]:
    """Every semantic setting is a field of its model, and the tier the
    committed snapshot records is the tier the code declares."""
    problems: list[str] = []
    for item in declared:
        problems.extend(
            f"module {item.package}: semantic setting {name} is not a field "
            f"of {item.model.__name__}: classify a declared name ({CODE})"
            for name in sorted(item.semantic - set(item.names))
        )
        record = _recorded(item.directory)
        if record is None:
            problems.append(
                f"module {item.package}: {SNAPSHOT_FILE} records none of its "
                "settings: run `sandloom generate` and review the diff "
                f"({CODE})"
            )
            continue
        for name in sorted(item.names):
            have = (record.get(name) or {}).get("tier") if name in record else None
            if have != item.tier(name):
                problems.append(
                    f"module {item.package}: {name} is {item.tier(name)} in the "
                    f"code but {have or 'not recorded'} in {SNAPSHOT_FILE}: "
                    "run `sandloom generate` and review the tier change "
                    f"({CODE})"
                )
        problems.extend(
            f"module {item.package}: {SNAPSHOT_FILE} records {name}, which "
            f"the module no longer declares: run `sandloom generate` ({CODE})"
            for name in sorted(set(record) - set(item.names))
        )
    return problems


def semantic_ratchet_problems(
    model: type[BaseModel], *, baseline: Iterable[str], ceiling: int
) -> list[str]:
    """A module's own semantic ratchet (answer #26), for its tests.

    ``baseline``: the semantic env names reviewed so far; ``ceiling``: the
    most the module may classify semantic. A setting that left the tier must
    leave the baseline (the baseline only shrinks); a new one needs an
    ``adr`` on its :class:`SemanticSetting`; every semantic name is a field.
    """
    known = set(_ms.env_names(model))
    declared = {s.name.upper(): s for s in _ms.declared_semantic(model)}
    base = {str(n).upper() for n in baseline}
    problems = [
        f"{name} is semantic but no field of {model.__name__}"
        for name in sorted(set(declared) - known)
    ]
    problems += [
        f"{name} left the semantic tier: drop it from the baseline and lower "
        "the ceiling (the count only goes down)"
        for name in sorted(base - set(declared))
    ]
    problems += [
        f"{name} is a new semantic setting: justify it with an adr= or make "
        "it an ops setting"
        for name, spec in sorted(declared.items())
        if name not in base and not spec.adr
    ]
    if len(declared) > ceiling:
        problems.append(
            f"{len(declared)} semantic settings exceed the ceiling of {ceiling}"
        )
    return problems


# --------------------------------------------------------------- topology ----


def _known_module_names(extra: Iterable[str]) -> set[str]:
    return {m.name for m in _names.installed_modules()} | set(extra)


def _resolvable(entry: str, known: set[str]) -> bool:
    if entry in known:
        return True
    try:
        return importlib.util.find_spec(entry) is not None
    except (ImportError, ValueError):
        return False


def topology_problems(
    profile: Path,
    declared: Sequence[DeclaredModule],
    *,
    known_modules: Iterable[str] = (),
) -> list[str]:
    """The services of one compose profile against the declarations."""
    where = f"{profile.parent.name}/{profile.name}"
    compose = _profile.load_profile(profile)
    known = _known_module_names(known_modules)
    by_name = {d.module: d for d in declared}
    by_package = {d.package: d for d in declared}
    problems: list[str] = []
    for service, svc in sorted(_profile.app_processes(compose).items()):
        listed = _profile.modules_of(svc)
        loaded: set[str] = set()
        for entry in listed:
            item = by_package.get(entry) or by_name.get(entry)
            loaded.add(item.module if item else _names.module_name(entry))
            if item is None and not _resolvable(entry, known):
                near = get_close_matches(entry, sorted(known), n=1)
                hint = f"; did you mean {near[0]!r}?" if near else ""
                problems.append(
                    f"{where}: service {service!r}: SANDLOOM_MODULES names "
                    f"{entry!r}, which is neither an installed module nor an "
                    f"importable package{hint} ({CODE})"
                )
        env = {str(k): v for k, v in _profile._env(svc).items()}
        for item in sorted(declared, key=lambda d: d.package):
            if item.module in loaded:
                literal = {
                    k: str(v)
                    for k, v in env.items()
                    if k.upper() in item.names and "${" not in str(v)
                }
                for name, reason in _ms.literal_value_problems(item.model, literal):
                    shown = (
                        "<value hidden>"
                        if is_secret_name(name)
                        else repr(literal[name])
                    )
                    problems.append(
                        f"{where}: service {service!r}: {name}={shown} is refused "
                        f"by module {item.package}: {reason} ({CODE})"
                    )
                continue
            problems.extend(
                f"{where}: service {service!r} sets {key}, a setting of "
                f"module {item.package}, but loads only "
                f"{sorted(loaded) or 'no module'}: remove it, or add the "
                f"module to this service's SANDLOOM_MODULES ({CODE})"
                for key in sorted(env)
                if key.upper() in item.names
            )
    return problems


# ------------------------------------------------------------------- gate ----


def gate_problems(
    packages: Sequence[str],
    profiles: Sequence[Path] = (),
    *,
    kernel_names: Iterable[str] | None = None,
    known_modules: Iterable[str] = (),
) -> list[str]:
    """Every problem of the settings gate for the application's ``packages``.

    ``profiles``: the compose profiles to check (the chosen topology);
    ``known_modules``: names of the application's own modules, valid in a
    ``SANDLOOM_MODULES`` beside the installed ones."""
    declared, problems = declared_modules(packages)
    if kernel_names is None:
        kernel_names = _ms.kernel_env_names(Settings)
    problems += collision_problems(declared, kernel_names)
    problems += prefix_problems(declared)
    problems += tier_problems(declared)
    for profile in profiles:
        problems += topology_problems(profile, declared, known_modules=known_modules)
    return problems


__all__ = [
    "CODE",
    "COLLISION_CODE",
    "SNAPSHOT_FILE",
    "SNAPSHOT_KEY",
    "DeclaredModule",
    "collision_problems",
    "declared_modules",
    "gate_problems",
    "prefix_problems",
    "semantic_ratchet_problems",
    "settings_section",
    "tier_problems",
    "topology_problems",
]
