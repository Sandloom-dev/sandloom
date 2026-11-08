"""KB-49 P12: the D2b authoring form as a thin shim over the D2a runtime.

Same loader path (``define_module(settings=...)``), same snapshot, same
post-load check; only the author's surface differs::

    class CatalogSettings(ModuleSettings):
        page_size: int = setting(50, env="CATALOG_PAGE_SIZE", ge=1, le=500)
        api_key: SecretStr | None = setting(None, env="CATALOG_API_KEY")
        allow_x: bool = setting(False, env="CATALOG_ALLOW_X",
                                semantic=Semantic(why="...", weaker=True, risk="x_in_prod"))
        risks = (Risk(...),)
        removed = (RemovedSetting(...),)

        @prod_check
        def _no_x(self, ctx: CheckContext) -> str | None:
            if self.allow_x and not ctx.accepts_risk("x_in_prod"):
                return "CATALOG_ALLOW_X=true is not allowed in prod"
            return None

    CatalogSettings.current()   # the bound instance (typed)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Self

from pydantic import BaseModel, Field, ValidationInfo, model_validator

from sandloom.sdk.module import RemovedSetting, Risk, SemanticSetting, settings_of

PROFILES = ("dev", "test", "prod", "prod_strict")


@dataclass(frozen=True)
class Semantic:
    """A semantic tier marker for ``setting(semantic=...)``."""

    why: str
    #: the weaker value (the profile value is its opposite for a bool)
    weaker: object = True
    profile_value: object = False
    risk: str | None = None


@dataclass(frozen=True)
class CheckContext:
    environment: str
    accepts_risk: Callable[[str], bool]
    kernel: Any


def setting(
    default: Any,
    *,
    env: str,
    semantic: Semantic | None = None,
    shared: bool = False,
    console: bool = False,
    backup: str | None = None,
    **constraints: Any,
) -> Any:
    extra = {"sandloom": {"semantic": semantic, "shared": shared, "console": console, "backup": backup}}
    return Field(default, alias=env, json_schema_extra=extra, **constraints)  # type: ignore[arg-type]


def prod_check(fn: Callable[[Any, CheckContext], str | None]) -> Callable[[Any, CheckContext], str | None]:
    """Mark a production check: return a problem string, or ``None``.
    Runs only with ENVIRONMENT=prod; every problem is reported."""
    fn.__sandloom_prod_check__ = True  # type: ignore[attr-defined]
    return fn


class ModuleSettings(BaseModel):
    semantic: ClassVar[tuple[SemanticSetting, ...]] = ()
    risks: ClassVar[tuple[Risk, ...]] = ()
    removed: ClassVar[tuple[RemovedSetting, ...]] = ()
    shared: ClassVar[frozenset[str]] = frozenset()
    console: ClassVar[tuple[str, ...]] = ()
    backup: ClassVar[dict[str, str]] = {}

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        semantic, shared, console, backup = [], set(), [], {}
        for info in cls.model_fields.values():
            meta = (info.json_schema_extra or {}).get("sandloom") if isinstance(info.json_schema_extra, dict) else None
            if not meta:
                continue
            alias = str(info.alias)
            spec = meta.get("semantic")
            if spec is not None:
                strength = (spec.weaker, spec.profile_value) if isinstance(spec.weaker, bool) else ()
                semantic.append(
                    SemanticSetting(alias, spec.why, profile=dict.fromkeys(PROFILES, spec.profile_value), strength=strength, risk=spec.risk)
                )
            if meta.get("shared"):
                shared.add(alias)
            if meta.get("console"):
                console.append(alias)
            if meta.get("backup"):
                backup[alias] = meta["backup"]
        cls.semantic = tuple(semantic) + tuple(cls.__dict__.get("semantic", ()) or ())
        cls.shared = frozenset(shared)
        cls.console = tuple(console)
        cls.backup = backup

    @model_validator(mode="after")
    def _sandloom_prod_checks(self, info: ValidationInfo) -> Self:
        ctx = info.context or {}
        if ctx.get("environment") != "prod":
            return self
        check_ctx = CheckContext(ctx.get("environment", "dev"), ctx.get("accepts_risk", lambda _r: False), ctx.get("kernel"))
        problems = []
        for name in dir(type(self)):
            fn = getattr(type(self), name, None)
            if callable(fn) and getattr(fn, "__sandloom_prod_check__", False):
                problem = fn(self, check_ctx)
                if problem:
                    problems.append(problem)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @classmethod
    def current(cls) -> Self:
        return settings_of(cls)


__all__ = ["CheckContext", "ModuleSettings", "RemovedSetting", "Risk", "Semantic", "prod_check", "setting"]
