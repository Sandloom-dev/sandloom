"""Shared value types and checks of the configuration layer."""

from __future__ import annotations

import math
from enum import Enum
from typing import Any
from urllib.parse import urlparse


class Environment(str, Enum):
    dev = "dev"
    test = "test"
    prod = "prod"


WEAK_SECRETS = frozenset(
    {"changeme", "change-me", "secret", "password", "jwt_secret", "test"}
)
# Substrings that mark a template/placeholder value (e.g. the generated
# ``change_me_in_production_use_openssl_rand_hex_32``) — never acceptable
# as a production secret however long it is.
PLACEHOLDER_MARKERS = (
    "change_me",
    "change-me",
    "changeme",
    "example",
    "placeholder",
    "replace_me",
    "replace-me",
    "replaceme",
    "your_secret",
    "your-secret",
    "dummy",
    "not_a_real",
    "not-a-real",
    "xxxxxxxx",
)


def secret_problem(value: str, *, min_length: int = 32) -> str | None:
    """Why ``value`` is not a production-grade secret (``None`` if fine)."""
    if len(value) < min_length:
        return f"too short for production (>= {min_length} characters)"
    low = value.lower()
    if any(marker in low for marker in PLACEHOLDER_MARKERS):
        return "a placeholder/template value"
    if low in WEAK_SECRETS or len(set(value)) < 10:
        return "too predictable for production"
    return None


#: Keys a ``SERVICE_BINDINGS`` entry may carry. Anything else (e.g. a typo such
#: as ``"requried"``) is rejected at startup instead of being silently ignored.
SERVICE_BINDING_KEYS = frozenset(
    {
        "base_url",
        "url",
        "timeout_seconds",
        "required",
        "critical",
        "service_id",
    }
)


def validate_service_binding(contract: str, binding: object) -> dict[str, object]:
    """Validate one ``SERVICE_BINDINGS`` entry (raises ``ValueError``)."""
    where = f"SERVICE_BINDINGS[{contract!r}]"
    if not contract or not isinstance(contract, str):
        raise ValueError("SERVICE_BINDINGS keys must be non-empty contract names")
    if not isinstance(binding, dict):
        raise ValueError(f"{where} must be an object, got {type(binding).__name__}")
    unknown = sorted(set(binding) - SERVICE_BINDING_KEYS)
    if unknown:
        raise ValueError(
            f"{where} has unknown key(s) {unknown}; allowed: "
            f"{sorted(SERVICE_BINDING_KEYS)}"
        )
    url = binding.get("base_url") or binding.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"{where} needs a base_url (e.g. http://tenancy:2222)")
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{where}.base_url must be an absolute http(s):// URL")
    timeout = binding.get("timeout_seconds")
    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError(f"{where}.timeout_seconds must be a positive finite number")
    for flag in ("required", "critical"):
        if flag in binding and not isinstance(binding[flag], bool):
            raise ValueError(f"{where}.{flag} must be true or false")
    provider = binding.get("service_id")
    if provider is not None and (not isinstance(provider, str) or not provider):
        raise ValueError(f"{where}.service_id must be a non-empty string")
    return binding


#: Prefix of every configuration refusal, at load (``Settings``) or at boot
#: (:func:`sandloom.core.app_factory.enforce_configuration`).
INVALID_CONFIGURATION = "[core.config] Invalid configuration: "

#: ``PRODUCTION_PROFILE`` values (:mod:`sandloom.core.production_profile` enforces
#: the strict one).
STANDARD = "standard"
MULTI_TENANT_STRICT = "multi_tenant_strict"
PROFILES = (STANDARD, MULTI_TENANT_STRICT)


def strict_profile_active(cfg: Any) -> bool:
    """``multi_tenant_strict`` declared AND ``ENVIRONMENT=prod``."""
    return getattr(cfg, "production_profile", STANDARD) == MULTI_TENANT_STRICT and bool(
        getattr(cfg, "is_prod", False)
    )
