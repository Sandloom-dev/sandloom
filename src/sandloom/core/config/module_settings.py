"""Settings a module declares (ADR-0015 decisions 1-3).

A module hands a plain pydantic model to its registration
(``define_module(..., settings=InventorySettings)``, or
``api.declare_settings(InventorySettings)`` in a hand-written ``register``).
Each field keeps its environment name as its alias
(``Field(alias="INVENTORY_RESERVE_TTL_SECONDS")``); secrets are ``SecretStr``.
Metadata sit in class attributes holding the kernel's existing dataclasses,
annotated ``ClassVar`` so pydantic leaves them alone::

    semantic: ClassVar[tuple[SemanticSetting, ...]] = (SemanticSetting(...),)
    risks: ClassVar[tuple[Risk, ...]] = (Risk("oversell_in_prod", ...),)
    removed: ClassVar[tuple[RemovedSetting, ...]] = (RemovedSetting(...),)

Production checks are ``model_validator``\\ s reading the validation context:
``info.context["environment"]`` (``"dev"`` / ``"test"`` / ``"prod"``),
``info.context["accepts_risk"](id)`` (a risk the model declares, by its local
name or as ``<module>.<name>``; ``KeyError`` for one it does not declare),
``info.context["kernel"]`` (a read-only view of the kernel ``Settings``) and
``info.context["module"]`` (the module's name, the risks' namespace).

This module is the configuration half, a leaf like the rest of
``sandloom.core.config`` (ADR-0007):

- :class:`SourceSnapshot`: one frozen read per process of the environment,
  ``.env`` (parsed once) and ``SECRETS_DIR`` (one lookup per declared alias,
  never listed), each value with its origin. The kernel ``Settings`` and every
  module model validate from the same instance (decision 2).
- :func:`validate_module_settings`: a model validated from the snapshot with
  the module's context; a failure is :class:`ModuleSettingsError`, a
  :class:`~sandloom.core.config.common.ConfigurationError` (decision 3).
- :func:`post_load_check`: the one check after the load pass: one env name
  declared twice (SANDLOOM-B036), the loaded modules' removed settings, the
  production typo check over the union of kernel and module names (never
  over a variable under the prefix of a module absent from the process),
  and the modules' semantic overrides to log.

Where the instances live and how a module reads its own
(``settings_of``) is :mod:`sandloom.core.module_api._settings`.
"""

from __future__ import annotations

import contextvars
import importlib
import json
import os
import types
import typing
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from difflib import get_close_matches
from importlib import metadata
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, DotEnvSettingsSource, NoDecode

from sandloom.core.config import accepted_risks as _risks
from sandloom.core.config import tiers as _tiers
from sandloom.core.config.accepted_risks import ModuleRiskReport, Risk
from sandloom.core.config.common import INVALID_CONFIGURATION, ConfigurationError
from sandloom.core.config.tiers import NO_OPINION, RemovedSetting, SemanticSetting

M = TypeVar("M", bound=BaseModel)

#: One environment name declared twice across the kernel and the loaded
#: modules (``sandloom explain SANDLOOM-B036``).
COLLISION_CODE = "SANDLOOM-B036"

#: The origins a value can come from, highest precedence first.
ORIGINS = ("env", "dotenv", "secrets_dir")


class ModuleSettingsError(ConfigurationError):
    """A module's settings are invalid, or the loaded set is inconsistent.

    A :class:`~sandloom.core.config.common.ConfigurationError` (a
    ``SystemExit``): the loader's per-module handler catches ``Exception``
    only, so an optional module refuses the boot like a required one
    (ADR-0015 answer #29), with the kernel's message prefix and exit 1.
    """


# ---------------------------------------------------------------- sources ----


class SourceSnapshot:
    """One frozen, case-insensitive read of env > ``.env`` > ``SECRETS_DIR``.

    The environment is copied once, ``.env`` is parsed once (with
    pydantic-settings' own reader), and ``SECRETS_DIR`` is never listed: a
    file is looked up when a declared alias asks for it, once per alias
    (``secret_lookups`` counts them). Names are compared lower-cased, as
    pydantic-settings does with ``case_sensitive=False``.
    """

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        env_file: str | os.PathLike[str] | None = ".env",
        env_file_encoding: str | None = None,
        secrets_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        source = os.environ if environ is None else environ
        self.env: dict[str, str] = {str(k).lower(): v for k, v in source.items()}
        self.dotenv: dict[str, str | None] = {}
        #: how often ``.env`` was parsed (the P7 spy: at most once).
        self.dotenv_parses = 0
        #: secrets lookups per lower-cased alias (the P7 spy: at most one each).
        self.secret_lookups: Counter[str] = Counter()
        if env_file is not None:
            path = Path(env_file).expanduser()
            if path.is_file():
                self.dotenv = _read_dotenv(path, env_file_encoding)
                self.dotenv_parses += 1
        self.secrets_dir = Path(secrets_dir).expanduser() if secrets_dir else None
        self._secrets: dict[str, str | None] = {}

    def secret(self, name: str) -> str | None:
        """The ``SECRETS_DIR`` file named ``name`` (lower or upper case), stripped."""
        key = name.lower()
        if self.secrets_dir is None:
            return None
        if key not in self._secrets:
            self.secret_lookups[key] += 1
            value: str | None = None
            for candidate in dict.fromkeys((key, key.upper(), name)):
                path = self.secrets_dir / candidate
                if path.is_file():
                    value = path.read_text(encoding="utf-8").strip()
                    break
            self._secrets[key] = value
        return self._secrets[key]

    def get(self, name: str) -> tuple[str | None, str | None]:
        """``(value, origin)`` of ``name``; ``(None, None)`` when no source has it."""
        key = name.lower()
        if key in self.env:
            return self.env[key], "env"
        value = self.dotenv.get(key)
        if value is not None:
            return value, "dotenv"
        value = self.secret(key)
        if value is not None:
            return value, "secrets_dir"
        return None, None

    def names(self) -> set[str]:
        """Upper-cased names the environment or ``.env`` supply (secrets are
        never listed)."""
        return {k.upper() for k in self.env} | {
            k.upper() for k, v in self.dotenv.items() if v is not None
        }

    def secrets_for(self, aliases: Iterable[str]) -> dict[str, str]:
        """``{lower alias: value}`` of the ``SECRETS_DIR`` files of ``aliases``."""
        out: dict[str, str] = {}
        for alias in aliases:
            value = self.secret(alias)
            if value is not None:
                out[alias.lower()] = value
        return out


def _read_dotenv(path: Path, encoding: str | None) -> dict[str, str | None]:
    """``.env`` parsed by pydantic-settings' own reader (names lower-cased)."""
    return dict(
        DotEnvSettingsSource._static_read_env_file(
            path, encoding=encoding, case_sensitive=False
        )
    )


_PROCESS_SNAPSHOT: SourceSnapshot | None = None


def set_process_snapshot(snapshot: SourceSnapshot | None) -> None:
    """Record the snapshot the process ``Settings`` were loaded from."""
    global _PROCESS_SNAPSHOT
    _PROCESS_SNAPSHOT = snapshot


def process_snapshot() -> SourceSnapshot:
    """The snapshot the process ``Settings`` were loaded from.

    Built here only when no load recorded one (a test that builds its own
    ``Settings``): the same sources, read once and kept.
    """
    global _PROCESS_SNAPSHOT
    if _PROCESS_SNAPSHOT is None:
        snapshot = SourceSnapshot()
        secrets_dir = snapshot.env.get("secrets_dir")
        if secrets_dir:
            snapshot.secrets_dir = Path(secrets_dir).expanduser()
        _PROCESS_SNAPSHOT = snapshot
    return _PROCESS_SNAPSHOT


# ----------------------------------------------------------- declarations ----


def check_declaration(model: object) -> type[BaseModel]:
    """``model`` as a declarable settings model, else ``TypeError``.

    A plain ``pydantic.BaseModel``: a ``BaseSettings`` reads the environment
    itself, a second read beside the process snapshot (decision 2).
    """
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise TypeError(f"settings= takes a pydantic BaseModel class, got {model!r}")
    if issubclass(model, BaseSettings):
        raise TypeError(
            f"{model.__name__} is a pydantic-settings BaseSettings, which reads "
            "the environment itself; declare a plain pydantic BaseModel with "
            "Field(alias='<ENV_NAME>') fields: the kernel validates it from the "
            "process's one read of the environment"
        )
    return model


def _alias(name: str, info: FieldInfo) -> str:
    alias = info.validation_alias if isinstance(info.validation_alias, str) else None
    return str(alias or info.alias or name).upper()


def env_names(model: type[BaseModel]) -> dict[str, str]:
    """``{ENV_NAME: field name}`` of a declared model."""
    return {_alias(name, info): name for name, info in model.model_fields.items()}


def kernel_env_names(kernel_cls: type[BaseModel]) -> set[str]:
    """The environment names the kernel ``Settings`` declares."""
    return set(env_names(kernel_cls))


def declared_semantic(model: type[BaseModel]) -> tuple[SemanticSetting, ...]:
    return tuple(getattr(model, "semantic", ()) or ())


def declared_risks(model: type[BaseModel]) -> tuple[Risk, ...]:
    return tuple(getattr(model, "risks", ()) or ())


def declared_removed(model: type[BaseModel]) -> tuple[RemovedSetting, ...]:
    return tuple(getattr(model, "removed", ()) or ())


@dataclass(frozen=True)
class ModuleSettingsRecord:
    """One module's validated settings, as the loader stores them."""

    #: the registering package (``sandloom_billing``), named in messages.
    package: str
    #: the module's name (``billing``): the namespace of its risk ids.
    module: str
    model: type[BaseModel]
    instance: BaseModel
    #: ``{ENV_NAME: origin}`` (``env`` / ``dotenv`` / ``secrets_dir``, or
    #: ``None`` for a default).
    origins: Mapping[str, str | None] = field(default_factory=dict)


class ModuleSettingsRegistry:
    """The validated module settings of one application (on its core)."""

    def __init__(self) -> None:
        self._records: dict[str, ModuleSettingsRecord] = {}
        self._by_model: dict[type[BaseModel], BaseModel] = {}
        #: the module-namespaced accepted-risk ids as the post-load check
        #: classified them (active, inert here); read by diagnostics and doctor.
        self.risk_report: ModuleRiskReport = ModuleRiskReport()

    def add(self, record: ModuleSettingsRecord) -> None:
        self._records[record.package] = record
        self._by_model[record.model] = record.instance

    def get(self, model: type[M]) -> M | None:
        return self._by_model.get(model)  # type: ignore[return-value]

    def records(self) -> list[ModuleSettingsRecord]:
        return list(self._records.values())


#: The records of the module registering in this context (its draft), so the
#: module's ``setup`` reads its own settings before the draft is applied.
REGISTERING: contextvars.ContextVar[list[ModuleSettingsRecord] | None] = (
    contextvars.ContextVar("sandloom_module_settings_registering", default=None)
)


# ------------------------------------------------------------- validation ----


def _is_complex(annotation: Any) -> bool:
    """A value pydantic-settings would decode as JSON (list, dict, model...)."""
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        inner, *meta = typing.get_args(annotation)
        return NoDecode not in meta and _is_complex(inner)
    if origin in (typing.Union, types.UnionType):
        return any(_is_complex(arg) for arg in typing.get_args(annotation))
    target = origin or annotation
    if not isinstance(target, type) or issubclass(target, str | bytes):
        return False
    if issubclass(target, BaseModel):
        return True
    return issubclass(target, Sequence | Mapping | set | frozenset)


def _raw_values(
    model: type[BaseModel], snapshot: SourceSnapshot
) -> tuple[dict[str, Any], dict[str, str | None]]:
    data: dict[str, Any] = {}
    origins: dict[str, str | None] = {}
    for alias, name in env_names(model).items():
        value, origin = snapshot.get(alias)
        origins[alias] = origin
        if value is None:
            continue
        info = model.model_fields[name]
        if NoDecode not in info.metadata and _is_complex(info.annotation):
            try:
                data[alias] = json.loads(value)
                continue
            except ValueError:
                pass  # pydantic reports the raw string against the type
        data[alias] = value
    return data, origins


def _reason(error: ValidationError, names: Mapping[str, str]) -> str:
    by_field = {field_name: alias for alias, field_name in names.items()}
    parts = []
    for item in error.errors(include_input=False, include_url=False):
        loc = item.get("loc") or ()
        message = str(item.get("msg", "invalid")).removeprefix("Value error, ")
        if loc:
            head = str(loc[0])
            alias = head.upper() if head.upper() in names else by_field.get(head, head)
            parts.append(f"{alias}: {message}")
        else:
            parts.append(message)
    return "; ".join(parts)


class KernelView:
    """A read-only view of the kernel ``Settings`` for production checks."""

    __slots__ = ("_settings",)

    def __init__(self, settings: Any) -> None:
        object.__setattr__(self, "_settings", settings)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_settings"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("the kernel settings are read-only in a module's checks")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("the kernel settings are read-only in a module's checks")


def environment_of(kernel: Any) -> str:
    """``dev`` / ``test`` / ``prod`` of a kernel ``Settings``."""
    env = getattr(kernel, "environment", "dev")
    return str(getattr(env, "value", env))


def _accepts_risk(
    module: str, model: type[BaseModel], accepted: frozenset[str]
) -> typing.Callable[[str], bool]:
    declared = {r.id for r in declared_risks(model)}

    def accepts_risk(risk_id: str) -> bool:
        local = risk_id.removeprefix(f"{module}.")
        if local not in declared:
            raise KeyError(
                f"module {module} declares no risk {risk_id!r} "
                f"(declared: {sorted(declared)})"
            )
        return f"{module}.{local}" in accepted

    return accepts_risk


def literal_value_problems(
    model: type[BaseModel], environ: Mapping[str, str]
) -> list[tuple[str, str]]:
    """``[(ENV_NAME, message)]`` for each declared name set in ``environ`` whose
    value its field refuses (type and constraints of the field only).

    The static gate's check of a deploy profile's literal values: a
    production check (a model validator) needs the whole environment and the
    kernel and is not run here. The message never carries the value.
    """
    data, _origins = _raw_values(model, SourceSnapshot(environ=environ, env_file=None))
    names = env_names(model)
    out: list[tuple[str, str]] = []
    for alias, value in data.items():
        info = model.model_fields[names[alias]]
        annotation: Any = info.annotation
        adapter: TypeAdapter[Any] = TypeAdapter(
            typing.Annotated[(annotation, *info.metadata)]
            if info.metadata
            else annotation
        )
        try:
            adapter.validate_python(value)
        except ValidationError as exc:
            reason = "; ".join(
                str(e.get("msg", "invalid")).removeprefix("Value error, ")
                for e in exc.errors(include_input=False, include_url=False)
            )
            out.append((alias, reason))
    return out


def validate_module_settings(
    model: type[M],
    *,
    package: str,
    module: str,
    kernel: Any,
    snapshot: SourceSnapshot | None = None,
    environment: str | None = None,
    accepted_risks: Iterable[str] | None = None,
) -> ModuleSettingsRecord:
    """Validate ``model`` from ``snapshot`` (default: the process one) with the
    module's context; :class:`ModuleSettingsError` names the setting."""
    check_declaration(model)
    snap = snapshot if snapshot is not None else process_snapshot()
    data, origins = _raw_values(model, snap)
    accepted = frozenset(
        accepted_risks
        if accepted_risks is not None
        else (getattr(kernel, "sandloom_accepted_risks", None) or ())
    )
    context = {
        "environment": environment or environment_of(kernel),
        "accepts_risk": _accepts_risk(module, model, accepted),
        "kernel": KernelView(kernel),
        "module": module,
    }
    try:
        instance = model.model_validate(data, context=context)
    except ValidationError as exc:
        reason = _reason(exc, env_names(model))
        raise ModuleSettingsError(
            f"{INVALID_CONFIGURATION}module {package}: {reason}"
        ) from None
    return ModuleSettingsRecord(package, module, model, instance, origins)


# --------------------------------------------------------- post-load check ----


@dataclass
class PostLoadOutcome:
    """What the post-load check found."""

    #: refusals: the boot stops with all of them.
    problems: list[str] = field(default_factory=list)
    #: a removed setting at its inert value: logged, the boot goes on.
    warnings: list[str] = field(default_factory=list)
    #: the modules' semantic settings weaker than their profile value, and the
    #: accepted risks a loaded module declares.
    overrides: list[_tiers.Override] = field(default_factory=list)
    #: the module-namespaced ids of ``SANDLOOM_ACCEPTED_RISKS``, classified.
    risks: ModuleRiskReport = field(default_factory=ModuleRiskReport)


def _collisions(
    records: Sequence[ModuleSettingsRecord], kernel_names: Iterable[str]
) -> tuple[dict[str, str], list[str]]:
    owners = dict.fromkeys(kernel_names, "the kernel")
    problems = []
    for record in records:
        for name in env_names(record.model):
            if name in owners:
                problems.append(
                    f"{COLLISION_CODE}: {name} is declared by {owners[name]} and "
                    f"by module {record.package}; each environment name has one "
                    "owner: rename one of them (a module's names start with its "
                    "prefix)"
                )
            else:
                owners[name] = f"module {record.package}"
    return owners, problems


def _removed(
    records: Sequence[ModuleSettingsRecord], snapshot: SourceSnapshot
) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    warnings: list[str] = []
    for record in records:
        for removed in declared_removed(record.model):
            raw, _origin = snapshot.get(removed.name)
            if raw is None:
                continue
            message, fatal = _tiers.removed_setting_problem(removed, raw)
            (problems if fatal else warnings).append(
                f"module {record.package}: {message}"
            )
    return problems, warnings


def _under_absent_module(
    key: str, loaded: Iterable[str], absent: Iterable[str]
) -> bool:
    """Whether ``key`` lies under the prefix (``<NAME>_``) of a module absent
    from the process, the longest matching prefix deciding: ``SHOP_EU_X``
    belongs to an absent ``shop_eu`` even where ``shop`` is loaded, and
    ``SHOP_X`` to a loaded ``shop`` even where a ``sho`` is absent."""
    owner_len, owner_absent = 0, False
    for names, is_absent in ((loaded, False), (absent, True)):
        for name in names:
            prefix = f"{name.upper()}_"
            if key.startswith(prefix) and len(prefix) > owner_len:
                owner_len, owner_absent = len(prefix), is_absent
    return owner_absent


#: The entry-point group of installed modules; equal to
#: ``sandloom.core.descriptors.module_names.ENTRY_POINT_GROUP``, which this
#: leaf may not import (ADR-0007).
MODULES_ENTRY_POINT_GROUP = "sandloom.modules"


def known_module_names(kernel: Any) -> set[str]:
    """The modules known in the deployment without importing one: the
    installed ``sandloom.modules`` entry points (metadata only) and the
    modules ``SANDLOOM_MODULES`` names (by name, or the last segment of the
    import path). The known-namespace rule of answer #27 (K1 and the
    composition's modules)."""
    names = {
        str(ep.name)
        for ep in metadata.entry_points().select(group=MODULES_ENTRY_POINT_GROUP)
        if ep.name
    }
    for entry in getattr(kernel, "sandloom_modules", None) or ():
        names.add(str(entry).split(":", 1)[0].rsplit(".", 1)[-1])
    return names


def _typos(
    owners: Mapping[str, str],
    records: Sequence[ModuleSettingsRecord],
    snapshot: SourceSnapshot,
    known_modules: Callable[[], Iterable[str]],
) -> list[str]:
    """Near-misses of a declared name (kernel or loaded module), in prod.

    The kernel's own check covers its names before any module loads; this
    one runs over the union, so a near-miss of a module's name refuses too.
    A variable under the prefix of a module known in the deployment
    (``known_modules``, asked only once a near-miss is found) but not loaded
    here is that module's, never a typo: an absent module's variables do not
    interfere (ADR-0015 decision 3, answer #25).
    """
    known = set(owners) | {
        r.name for record in records for r in declared_removed(record.model)
    }
    prefixes = {name.split("_", 1)[0] for name in known if "_" in name}
    names = sorted(known)
    loaded = {record.module for record in records}
    absent: set[str] | None = None
    problems = []
    for key in sorted(snapshot.names()):
        if key in known or len(key) < 8 or "_" not in key:
            continue
        if key.split("_", 1)[0] not in prefixes:
            continue
        matches = get_close_matches(key, names, n=1, cutoff=0.95)
        if not matches:
            continue
        if absent is None:
            absent = set(known_modules()) - loaded
        if _under_absent_module(key, loaded, absent):
            continue
        problems.append(
            f"Unknown setting {key}; did you mean {matches[0]} "
            f"(declared by {owners.get(matches[0], 'a removed setting')})?"
        )
    return problems


def module_overrides(
    records: Sequence[ModuleSettingsRecord], kernel: Any
) -> list[_tiers.Override]:
    """The loaded modules' semantic settings weaker than their profile value.

    A setting whose declared ``risk`` is accepted is reported as that risk,
    not as an override (as for the kernel's). ``effective`` / ``applies`` of a
    module's :class:`SemanticSetting` receive the module's settings instance.
    """
    profile = _tiers.profile_of(kernel)
    accepted = frozenset(getattr(kernel, "sandloom_accepted_risks", None) or ())
    out: list[_tiers.Override] = []
    for record in records:
        names = env_names(record.model)
        for spec in declared_semantic(record.model):
            field_name = names.get(spec.name.upper())
            if field_name is None or spec.selector or spec.risk_list:
                continue
            if spec.applies is not None and not spec.applies(record.instance):
                continue
            if spec.risk is not None:
                full = spec.risk if "." in spec.risk else f"{record.module}.{spec.risk}"
                if full in accepted:
                    continue
            value = (
                spec.effective(record.instance)
                if spec.effective is not None
                else getattr(record.instance, field_name)
            )
            expected = spec.profile.get(profile, NO_OPINION)
            if expected is not NO_OPINION and spec.weaker(value, expected):
                explicit = record.origins.get(spec.name.upper()) is not None
                out.append(
                    _tiers.Override(
                        spec.name, "weaker", value, profile, expected, explicit
                    )
                )
    return out


def installed_risk_declarations() -> dict[str, tuple[Risk, ...]]:
    """``{module name: its declared risks}`` of the installed modules.

    Imports each ``sandloom.modules`` entry point's package (importing a
    module reads no environment and does no I/O) and reads the settings model
    its ``define_module(settings=)`` recorded. Used only on the error path of
    a flat risk id, to name the namespaced replacement; a package that does
    not import is skipped.
    """
    out: dict[str, tuple[Risk, ...]] = {}
    for ep in metadata.entry_points().select(group=MODULES_ENTRY_POINT_GROUP):
        declared = _declared_risks_of(str(ep.value).split(":", 1)[0])
        if declared is not None:
            out[str(ep.name)] = declared
    return out


def _declared_risks_of(package: str) -> tuple[Risk, ...] | None:
    """The risks ``package``'s ``define_module(settings=)`` declares; ``None``
    when it declares no settings or does not import."""
    try:
        register = getattr(importlib.import_module(package), "register", None)
    except (ImportError, AttributeError, RuntimeError, SystemExit):
        return None  # a package that does not import cannot name a replacement
    model = getattr(register, "__dict__", {}).get("sandloom_settings")
    return declared_risks(model) if model is not None else None


def flat_risk_refusal(ids: Iterable[str]) -> str:
    """The ``SANDLOOM_ACCEPTED_RISKS`` refusal for unknown ``ids``, naming the
    namespaced replacement of a flat id where its module is installed."""
    given = list(ids)
    flat = [i for i in _risks.unknown_ids(given) if "." not in i]
    moved = _risks.replacement_ids(flat, installed_risk_declarations()) if flat else {}
    return _risks.unknown_ids_message(given, moved)


def _risk_report(
    records: Sequence[ModuleSettingsRecord],
    kernel: Any,
    loaded_modules: Iterable[str],
    known: Callable[[], Iterable[str]],
) -> ModuleRiskReport:
    accepted = getattr(kernel, "sandloom_accepted_risks", None) or ()
    if not any(_risks.is_module_id(i) for i in accepted):
        return ModuleRiskReport()
    declared: dict[str, tuple[Risk, ...]] = dict.fromkeys(loaded_modules, ())
    for record in records:
        declared[record.module] = declared_risks(record.model)
    return _risks.classify_module_risks(accepted, loaded=declared, known=known)


def _risk_overrides(report: ModuleRiskReport, kernel: Any) -> list[_tiers.Override]:
    """Active module risks as ``accepted_risk`` overrides (as the kernel's)."""
    profile = _tiers.profile_of(kernel)
    prod = environment_of(kernel) == "prod"
    return [
        _tiers.Override(
            f"SANDLOOM_ACCEPTED_RISKS[{risk_id}]",
            "accepted_risk",
            risk_id,
            profile,
            (),
            risk=item,
        )
        for risk_id, item in report.active
        if prod or item.outside_prod
    ]


def post_load_check(
    records: Sequence[ModuleSettingsRecord],
    kernel: Any,
    snapshot: SourceSnapshot | None = None,
    *,
    known_modules: Iterable[str] | None = None,
    loaded_modules: Iterable[str] = (),
) -> PostLoadOutcome:
    """ADR-0015 decision 3: the one check after the load pass.

    ``known_modules``: the modules known in the deployment (default
    :func:`known_module_names`); the prod typo check leaves alone a variable
    under the prefix of one this process does not load, and a risk id of one
    is inert. ``loaded_modules``: every loaded module's name, whether or not
    it declares settings (the namespaces of ``SANDLOOM_ACCEPTED_RISKS``).
    Returns what it found; the caller raises :class:`ModuleSettingsError` with
    every problem (:func:`refusal`) and logs the rest.
    """
    snap = snapshot if snapshot is not None else process_snapshot()
    ordered = sorted(records, key=lambda r: r.package)
    owners, problems = _collisions(ordered, kernel_env_names(type(kernel)))
    removed_problems, warnings = _removed(ordered, snap)
    problems += removed_problems

    def known() -> Iterable[str]:
        if known_modules is not None:
            return known_modules
        return known_module_names(kernel)

    risks = _risk_report(
        ordered, kernel, [*loaded_modules, *(r.module for r in ordered)], known
    )
    problems += risks.problems
    if environment_of(kernel) == "prod":
        problems += _typos(owners, ordered, snap, known)
    overrides = module_overrides(ordered, kernel) + _risk_overrides(risks, kernel)
    return PostLoadOutcome(problems, warnings, overrides, risks)


def refusal(problems: Sequence[str]) -> ModuleSettingsError:
    """The boot refusal for the post-load check's problems."""
    return ModuleSettingsError(INVALID_CONFIGURATION + "; ".join(problems))


__all__ = [
    "COLLISION_CODE",
    "MODULES_ENTRY_POINT_GROUP",
    "ORIGINS",
    "REGISTERING",
    "KernelView",
    "ModuleSettingsError",
    "ModuleSettingsRecord",
    "ModuleSettingsRegistry",
    "PostLoadOutcome",
    "RemovedSetting",
    "Risk",
    "SemanticSetting",
    "SourceSnapshot",
    "check_declaration",
    "declared_removed",
    "declared_risks",
    "declared_semantic",
    "env_names",
    "environment_of",
    "flat_risk_refusal",
    "installed_risk_declarations",
    "kernel_env_names",
    "known_module_names",
    "literal_value_problems",
    "module_overrides",
    "post_load_check",
    "process_snapshot",
    "refusal",
    "set_process_snapshot",
    "validate_module_settings",
]
