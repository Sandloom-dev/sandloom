"""argon2id work-factor resolution (pure; no settings / argon2 import).

Shared by :mod:`sandloom.core.config.settings` (the startup floor check) and
:mod:`sandloom.core.auth.hashing` (the hasher). Resolution order:

1. ``PASSWORD_HASH_PROFILE`` picks a base (``strong`` — the default, today's
   m=64 MiB / t=3 / p=4 — or ``owasp_min``; ``custom`` has no base and needs
   all three explicit values);
2. each explicit ``PASSWORD_HASH_TIME_COST`` / ``PASSWORD_HASH_MEMORY_KIB`` /
   ``PASSWORD_HASH_PARALLELISM`` overrides its field of the base.

The OWASP floor is the Password Storage Cheat Sheet's argon2id minimum: "a
minimum configuration of 19 MiB of memory, an iteration count of 2, and 1
degree of parallelism", with the equivalent (equal-defence) configurations
listed in :data:`OWASP_ARGON2ID_MINIMUMS`
(https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
A configuration meets the floor when it is at least as strong as ONE of them
in BOTH memory and iterations (parallelism >= 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PasswordHashProfile = Literal["strong", "owasp_min", "custom"]


@dataclass(frozen=True)
class PasswordHashParams:
    """argon2id cost parameters (salt / digest length are fixed elsewhere)."""

    time_cost: int
    memory_kib: int
    parallelism: int

    def describe(self) -> str:
        return f"m={self.memory_kib},t={self.time_cost},p={self.parallelism}"


#: Named presets. ``strong`` is the framework's historical default (the former
#: passlib/argon2-cffi parameters, ``$argon2id$v=19$m=65536,t=3,p=4``).
PASSWORD_HASH_PROFILES: dict[str, PasswordHashParams] = {
    "strong": PasswordHashParams(time_cost=3, memory_kib=65536, parallelism=4),
    "owasp_min": PasswordHashParams(time_cost=2, memory_kib=19456, parallelism=1),
}

#: OWASP Password Storage Cheat Sheet argon2id minimums as ``(memory KiB, t)``
#: at p=1 — equal levels of defence trading RAM against CPU time.
OWASP_ARGON2ID_MINIMUMS: tuple[tuple[int, int], ...] = (
    (47104, 1),
    (19456, 2),
    (12288, 3),
    (9216, 4),
    (7168, 5),
)


def resolve_password_hash_params(
    profile: str = "strong",
    *,
    time_cost: int | None = None,
    memory_kib: int | None = None,
    parallelism: int | None = None,
) -> PasswordHashParams:
    """Preset + explicit overrides -> effective parameters.

    ``ValueError`` for an unknown profile or ``custom`` without all three
    explicit values.
    """
    if profile == "custom":
        missing = [
            name
            for name, value in (
                ("PASSWORD_HASH_TIME_COST", time_cost),
                ("PASSWORD_HASH_MEMORY_KIB", memory_kib),
                ("PASSWORD_HASH_PARALLELISM", parallelism),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                "PASSWORD_HASH_PROFILE=custom requires "
                f"{', '.join(missing)}; fix: set them, or pick a preset "
                "(strong / owasp_min) and override only what differs"
            )
        assert time_cost is not None and memory_kib is not None  # noqa: S101 - type narrowing of an invariant
        assert parallelism is not None  # noqa: S101 - type narrowing of an invariant
        return PasswordHashParams(time_cost, memory_kib, parallelism)
    base = PASSWORD_HASH_PROFILES.get(profile)
    if base is None:
        raise ValueError(
            f"unknown PASSWORD_HASH_PROFILE={profile!r}; "
            "fix: use strong, owasp_min or custom"
        )
    return PasswordHashParams(
        time_cost=base.time_cost if time_cost is None else time_cost,
        memory_kib=base.memory_kib if memory_kib is None else memory_kib,
        parallelism=base.parallelism if parallelism is None else parallelism,
    )


def meets_owasp_minimum(params: PasswordHashParams) -> bool:
    """True when ``params`` are at least one OWASP argon2id minimum."""
    if params.parallelism < 1:
        return False
    return any(
        params.memory_kib >= mem and params.time_cost >= t
        for mem, t in OWASP_ARGON2ID_MINIMUMS
    )


__all__ = [
    "OWASP_ARGON2ID_MINIMUMS",
    "PASSWORD_HASH_PROFILES",
    "PasswordHashParams",
    "PasswordHashProfile",
    "meets_owasp_minimum",
    "resolve_password_hash_params",
]
