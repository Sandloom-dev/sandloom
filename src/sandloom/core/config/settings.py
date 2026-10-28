"""The process configuration: one ``Settings`` composed of the domain
sections in :mod:`sandloom.core.config`, and the process-wide ``settings``.

Every field is classified in :mod:`sandloom.core.config.tiers` (semantic / ops;
``test_config_tiers`` fails on an unclassified one). Validation here is
self-contained (the config layer imports nothing above it): checks that need
key material or other kernel code run at startup
(:func:`sandloom.core.app_factory.enforce_configuration`).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
from difflib import get_close_matches
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError
from pydantic.functional_validators import field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SecretsSettingsSource,
    SettingsConfigDict,
)

from sandloom.core.config import accepted_risks as _risks
from sandloom.core.config.auth import AuthConfig
from sandloom.core.config.cache import CacheConfig
from sandloom.core.config.common import (
    INVALID_CONFIGURATION,
    Environment,
    secret_problem,
    validate_service_binding,
)
from sandloom.core.config.contracts import ContractsConfig
from sandloom.core.config.database import DatabaseConfig
from sandloom.core.config.eventing import EventingConfig
from sandloom.core.config.http import HttpConfig
from sandloom.core.config.infra import InfraConfig
from sandloom.core.config.modules import ModulesConfig
from sandloom.core.config.observability import ObservabilityConfig
from sandloom.core.config.password_params import (
    PasswordHashParams,
    meets_owasp_minimum,
    resolve_password_hash_params,
)
from sandloom.core.config.rpc import RpcConfig
from sandloom.core.config.runtime import RuntimeConfig
from sandloom.core.config.sagas import SagasConfig
from sandloom.core.config.services import ServicesConfig
from sandloom.core.config.tiers import REMOVED, removed_setting_problem
from sandloom.core.config.trust import TrustConfig


def effective_secrets_dir(settings_cls: type[BaseSettings]) -> str | None:
    """The secrets directory of ``settings_cls``: its configured
    ``secrets_dir``, else the ``SECRETS_DIR`` environment variable (read when
    settings are built, never at import)."""
    configured = settings_cls.model_config.get("secrets_dir")
    if configured:
        return str(configured)
    return os.getenv("SECRETS_DIR") or None


class Settings(
    ObservabilityConfig,
    ModulesConfig,
    HttpConfig,
    RpcConfig,
    AuthConfig,
    ServicesConfig,
    CacheConfig,
    DatabaseConfig,
    TrustConfig,
    ContractsConfig,
    InfraConfig,
    EventingConfig,
    SagasConfig,
    RuntimeConfig,
    BaseSettings,
):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
        allow_inf_nan=False,
        case_sensitive=False,
        # Docker/Kubernetes file-based secrets: when SECRETS_DIR is set (e.g.
        # /run/secrets), each setting may be supplied as a file named after the
        # field (e.g. /run/secrets/jwt_secret) instead of an env var. Keeps
        # secrets out of the process environment and compose YAML. Resolved
        # when an instance is built (settings_customise_sources), never at
        # import (spec §2.4).
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        if (
            isinstance(file_secret_settings, SecretsSettingsSource)
            and file_secret_settings.secrets_dir is None
        ):
            file_secret_settings.secrets_dir = effective_secrets_dir(settings_cls)
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    # ---- Validators ----
    @field_validator("log_level", mode="before")
    @classmethod
    def _log_level(cls, value: object) -> str:
        level = str(value).strip().upper()
        aliases = {"WARN": "WARNING", "FATAL": "CRITICAL"}
        level = aliases.get(level, level)
        if level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}:
            raise ValueError("LOG_LEVEL must be a standard Python logging level")
        return level

    @field_validator("tracing_url")
    @classmethod
    def _tracing_endpoint(cls, value: str) -> str:
        endpoint = urlsplit(value)
        if value and (
            endpoint.scheme not in {"http", "https", "grpc", "grpcs"}
            or not endpoint.hostname
        ):
            raise ValueError("TRACING_URL must be an http(s) or grpc(s) endpoint")
        return value

    @model_validator(mode="before")
    @classmethod
    def _refuse_removed_settings(cls, value: Any) -> Any:
        """A removed setting still configured: inert value warns, else fails.

        Removed settings (``sandloom.core.config.tiers.REMOVED``) are no longer
        fields, so ``extra="ignore"`` would drop them silently — and the
        behaviour the operator asked for with a non-inert value is gone.
        """
        supplied = (
            {str(k).upper(): v for k, v in value.items()}
            if isinstance(value, dict)
            else {}
        )
        for removed in REMOVED:
            raw = supplied.get(removed.name, os.environ.get(removed.name))
            if raw is None:
                continue
            problem, fatal = removed_setting_problem(removed, raw)
            if fatal:
                raise ValueError(problem)
            logging.getLogger("sandloom.settings").warning(problem)
        return value

    @field_validator("sandloom_accepted_risks", mode="before")
    @classmethod
    def _accepted_risks_list(cls, value: object) -> object:
        # JSON list, or a comma-separated string ("a,b") for env convenience.
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return json.loads(text)
            return [item.strip() for item in text.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _known_accepted_risks(self) -> Settings:
        if _risks.unknown_ids(self.sandloom_accepted_risks):
            raise ValueError(_risks.unknown_ids_message(self.sandloom_accepted_risks))
        return self

    @model_validator(mode="before")
    @classmethod
    def _reject_production_setting_typos(cls, value: Any) -> Any:
        """Catch near-miss framework names without rejecting ambient env vars."""
        if not isinstance(value, dict):
            return value
        supplied = {str(k).upper(): v for k, v in value.items()}
        environment = supplied.get("ENVIRONMENT", os.environ.get("ENVIRONMENT"))
        if environment not in ("prod", Environment.prod):
            return value
        aliases = {
            str(field.alias or name).upper() for name, field in cls.model_fields.items()
        }
        accepted = aliases | {name.upper() for name in cls.model_fields}
        prefixes = {name.split("_", 1)[0] for name in aliases if "_" in name}
        for key in sorted(set(supplied) | set(os.environ)):
            normalized = key.upper()
            if normalized in accepted or len(normalized) < 8 or "_" not in normalized:
                continue
            if normalized.split("_", 1)[0] not in prefixes:
                continue
            matches = get_close_matches(normalized, sorted(aliases), n=1, cutoff=0.95)
            if matches:
                raise ValueError(
                    f"Unknown framework setting {normalized}; "
                    f"did you mean {matches[0]}?"
                )
        return value

    @model_validator(mode="after")
    def _event_reconnect_bounds(self) -> Settings:
        if (
            self.event_streams_reconnect_max_seconds
            < self.event_streams_reconnect_base_seconds
        ):
            raise ValueError(
                "EVENT_STREAMS_RECONNECT_MAX_SECONDS must be >= "
                "EVENT_STREAMS_RECONNECT_BASE_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def _warn_production_trust_defaults(self) -> Settings:
        if self.is_prod:
            logger = logging.getLogger("sandloom.settings")
            if not self.jwt_issuer or not self.jwt_audience:
                logger.warning(
                    "Production JWT_ISSUER/JWT_AUDIENCE are not both configured; "
                    "use deployment-specific values and distinct deployment keys"
                )
            if "cors_origins" not in self.model_fields_set:
                logger.warning(
                    "Production CORS_ORIGINS uses the development default; "
                    "set the explicit browser origin allowlist"
                )
        return self

    @field_validator("service_bindings")
    @classmethod
    def _service_bindings_ok(
        cls, v: dict[str, dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        return {
            contract: validate_service_binding(contract, binding)
            for contract, binding in v.items()
        }

    @field_validator("database_url")
    @classmethod
    def _db_dsn_ok(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must look like "
                "postgresql+asyncpg://user:pass@host:port/db"
            )
        return v

    @field_validator("database_system_url")
    @classmethod
    def _system_dsn_ok(cls, v: str | None) -> str | None:
        if v and not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_SYSTEM_URL must look like "
                "postgresql+asyncpg://sandloom_system:pass@host:port/db"
            )
        return v or None

    @field_validator("gc_thresholds", mode="before")
    @classmethod
    def _gc_thresholds_ok(cls, v: Any) -> tuple[int, int, int] | None:
        """``"a,b,c"`` / ``[a, b, c]`` → three bounded ints; blank → None.

        threshold0 >= 100: 0 would DISABLE automatic collection (cycles then
        grow without bound) and tiny values collect on nearly every
        allocation. Upper bounds keep a typo (an extra zero or three) from
        turning into minutes-long young pauses or unbounded cyclic garbage.
        """
        if v is None:
            return None
        if isinstance(v, str):
            raw = v.strip()
            if not raw:
                return None
            if raw.startswith("["):
                v = json.loads(raw)
            else:
                v = [p.strip() for p in raw.split(",")]
        try:
            parts = [int(p) for p in v]
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "GC_THRESHOLDS must be three integers, e.g. '10000,10,100'"
            ) from exc
        if len(parts) != 3:
            raise ValueError(
                "GC_THRESHOLDS must be three integers, e.g. '10000,10,100'"
            )
        t0, t1, t2 = parts
        if not 100 <= t0 <= 1_000_000:
            raise ValueError(
                "GC_THRESHOLDS threshold0 must be in 100..1000000 "
                "(0 would disable automatic collection)"
            )
        if not (1 <= t1 <= 10_000 and 1 <= t2 <= 10_000):
            raise ValueError("GC_THRESHOLDS threshold1/threshold2 must be in 1..10000")
        return (t0, t1, t2)

    @field_validator("trusted_proxy_ips")
    @classmethod
    def _trusted_proxy_cidrs_ok(cls, v: list[str]) -> list[str]:
        for cidr in v:
            try:
                ipaddress.ip_network(str(cidr).strip(), strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"TRUSTED_PROXY_IPS entry {cidr!r} is not a valid IP/CIDR"
                ) from exc
        return [str(c).strip() for c in v]

    @field_validator("cache_url", "broker_url")
    @classmethod
    def _redis_dsn_ok(cls, v: str) -> str:
        if not (v.startswith("redis://") or v.startswith("rediss://")):
            raise ValueError(
                "CACHE_URL/BROKER_URL must start with redis:// or rediss://"
            )
        return v

    @model_validator(mode="after")
    def _single_tenant_requirements(self) -> Settings:
        if self.tenancy_mode == "single" and self.single_tenant_id is None:
            raise ValueError("SINGLE_TENANT_ID is required when TENANCY_MODE=single")
        return self

    @property
    def jwt_asymmetric(self) -> bool:
        """True when tokens are signed with a private key (EdDSA/RS256/ES256)."""
        return self.jwt_algorithm in ("EdDSA", "RS256", "ES256")

    @field_validator("jwt_private_key", "jwt_secret", mode="before")
    @classmethod
    def _blank_key_is_unset(cls, v: object) -> object:
        # Compose passes ``${VAR:-}`` as an empty string: that means "unset".
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("jwt_public_keys", mode="before")
    @classmethod
    def _blank_public_keys(cls, v: object) -> object:
        if isinstance(v, str):
            if not v.strip():
                return {}
            try:
                return json.loads(v)
            except ValueError as exc:
                raise ValueError(
                    'JWT_PUBLIC_KEYS must be a JSON object {"<kid>": "<PEM>"}'
                ) from exc
        return v

    @model_validator(mode="after")
    def _jwt_keys_ok(self) -> Settings:  # noqa: C901
        """Key settings must match the algorithm family (fail at load).

        Parsing the key material itself (PEM type vs algorithm, the private
        key matching its public half) needs the signing code and runs at
        startup: :func:`sandloom.core.auth.tokens.jwt_key_material_problem`.
        """
        if not self.jwt_asymmetric:
            if not self.jwt_secret:
                raise ValueError(
                    f"JWT_SECRET is required with JWT_ALGORITHM={self.jwt_algorithm} "
                    "(or choose an asymmetric algorithm: EdDSA / RS256 / ES256 "
                    "with JWT_PUBLIC_KEYS)"
                )
            if self.jwt_private_key or self.jwt_public_keys:
                raise ValueError(
                    "JWT_PRIVATE_KEY / JWT_PUBLIC_KEYS require an asymmetric "
                    f"JWT_ALGORITHM (EdDSA, RS256, ES256), not {self.jwt_algorithm}"
                )
            return self
        if self.jwt_previous_secrets:
            raise ValueError(
                "JWT_PREVIOUS_SECRETS are HMAC keys and are never accepted with "
                f"JWT_ALGORITHM={self.jwt_algorithm}; rotate by adding a kid to "
                "JWT_PUBLIC_KEYS instead"
            )
        if not self.jwt_public_keys:
            raise ValueError(
                f"JWT_ALGORITHM={self.jwt_algorithm} requires JWT_PUBLIC_KEYS "
                '({"<kid>": "<PEM public key>"}); generate a pair with '
                "`sandloom keys generate`"
            )
        for kid in self.jwt_public_keys:
            if not str(kid).strip():
                raise ValueError("JWT_PUBLIC_KEYS: a kid must be a non-empty string")
        if self.jwt_private_key:
            if not self.jwt_kid:
                raise ValueError(
                    "JWT_KID is required with JWT_PRIVATE_KEY: it is stamped on "
                    "every minted token and must name the matching JWT_PUBLIC_KEYS "
                    "entry"
                )
            if self.jwt_kid not in self.jwt_public_keys:
                raise ValueError(
                    f"JWT_KID={self.jwt_kid!r} is not in JWT_PUBLIC_KEYS: tokens "
                    "minted with it would not verify anywhere"
                )
        elif self.jwt_secret:
            raise ValueError(
                "JWT_SECRET must not be set on a verify-only process when "
                f"JWT_ALGORITHM={self.jwt_algorithm}: only the token issuer (the "
                "process holding JWT_PRIVATE_KEY) keeps a symmetric secret"
            )
        return self

    @field_validator(
        "rpc_private_key",
        "rpc_key_id",
        "rpc_service_id",
        "rpc_dh_private_key",
        "rpc_dh_key_id",
        mode="before",
    )
    @classmethod
    def _blank_rpc_value_is_unset(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator(
        "rpc_service_keys",
        "rpc_allowed_calls",
        "rpc_service_dh_keys",
        "rpc_service_dh_current_kids",
        "rpc_dh_extra_private_keys",
        mode="before",
    )
    @classmethod
    def _rpc_json_maps(cls, v: object) -> object:
        if isinstance(v, str):
            if not v.strip():
                return {}
            try:
                return json.loads(v)
            except ValueError as exc:
                raise ValueError("must be a JSON object") from exc
        return v

    @model_validator(mode="after")
    def _jwt_secret_ok(self) -> Settings:
        """Prod-strength JWT secret check.

        Uses the parsed environment (not os.getenv) so a prod flag coming from
        an .env file cannot bypass the check.
        """
        if self.environment == Environment.prod:
            if self.jwt_secret is not None:
                problem = secret_problem(self.jwt_secret)
                if problem:
                    raise ValueError(
                        f"JWT_SECRET is {problem}; generate a random one "
                        "(e.g. openssl rand -hex 32)"
                    )
            # A retired key still verifies tokens, so it must be just as strong.
            for idx, previous in enumerate(self.jwt_previous_secrets):
                problem = secret_problem(previous)
                if problem:
                    raise ValueError(
                        f"JWT_PREVIOUS_SECRETS[{idx}] is {problem}; previous "
                        "secrets must meet the same rules as JWT_SECRET"
                    )
            if self.twofa_encryption_key:
                for idx, key in enumerate(
                    k.strip() for k in self.twofa_encryption_key.split(",")
                ):
                    if key and (problem := secret_problem(key)):
                        raise ValueError(
                            f"TWOFA_ENCRYPTION_KEY[{idx}] is {problem}; generate "
                            "a random one (e.g. openssl rand -hex 32)"
                        )
        if self.jwt_secret is not None and self.jwt_secret in self.jwt_previous_secrets:
            raise ValueError(
                "JWT_PREVIOUS_SECRETS must not contain the current JWT_SECRET"
            )
        return self

    @model_validator(mode="after")
    def _internal_tokens_ok(self) -> Settings:
        """Prod-strength internal/metrics tokens (when configured)."""
        if self.environment == Environment.prod:
            for name, value in (
                ("INTERNAL_API_TOKEN", self.internal_api_token),
                ("METRICS_TOKEN", self.metrics_token),
            ):
                if value:
                    problem = secret_problem(value)
                    if problem:
                        raise ValueError(
                            f"{name} is {problem}; generate a random one "
                            "(e.g. openssl rand -hex 32)"
                        )
            if (
                self.internal_api_token
                and self.metrics_token
                and self.internal_api_token == self.metrics_token
            ):
                raise ValueError(
                    "METRICS_TOKEN must differ from INTERNAL_API_TOKEN (the "
                    "scrape-only credential must not unlock the ops endpoints)"
                )
        return self

    @model_validator(mode="after")
    def _prod_payload_cap(self) -> Settings:
        """In prod, default the event payload cap to 256 KiB.

        ``publish_event`` preflights the payload size inside the transaction;
        without a ceiling an oversized event could still be committed to the
        outbox and then fail forever at dispatch. Unless the operator set the
        env var explicitly (their choice wins). A missing / off-schema payload
        is refused in every profile, so no schema default flips here.
        """
        if (
            self.environment == Environment.prod
            and "event_payload_max_bytes" not in self.model_fields_set
        ):
            # 256 KiB is generous for id+metadata event payloads while still
            # rejecting accidental blobs before they reach the outbox.
            object.__setattr__(self, "event_payload_max_bytes", 256 * 1024)
        return self

    @model_validator(mode="after")
    def _password_hash_requirements(self) -> Settings:
        """Resolve the argon2id parameters; enforce the OWASP floor in prod."""
        params = self.password_hash_params
        if params.memory_kib < 8 * params.parallelism:
            raise ValueError(
                f"argon2 needs PASSWORD_HASH_MEMORY_KIB >= 8 * parallelism "
                f"(got {params.describe()}); fix: raise the memory or lower "
                "PASSWORD_HASH_PARALLELISM"
            )
        if (
            self.is_prod
            and not meets_owasp_minimum(params)
            and not self.accepts_risk("weak_password_hash")
        ):
            raise ValueError(
                f"password hashing parameters {params.describe()} are below the "
                "OWASP argon2id minimum (m>=19456 KiB with t>=2, or an "
                "equivalent: m=47104/t=1, m=12288/t=3, m=9216/t=4, m=7168/t=5; "
                "p>=1); fix: set PASSWORD_HASH_PROFILE to owasp_min or strong, or "
                "accept the risk explicitly: "
                'SANDLOOM_ACCEPTED_RISKS=["weak_password_hash"]'
            )
        return self

    @property
    def password_hash_params(self) -> PasswordHashParams:
        """Effective argon2id parameters (preset + explicit overrides)."""
        return resolve_password_hash_params(
            self.password_hash_profile,
            time_cost=self.password_hash_time_cost,
            memory_kib=self.password_hash_memory_kib,
            parallelism=self.password_hash_parallelism,
        )

    @model_validator(mode="after")
    def _strict_profile_defaults(self) -> Settings:
        """``multi_tenant_strict`` in prod turns fail-closed revocation on.

        Only the default the operator left unset; the profile's refusals
        (``config_problems``) need the RPC key code and run at startup
        (:func:`sandloom.core.app_factory.enforce_configuration`).
        """
        if (
            self.production_profile == "multi_tenant_strict"
            and self.is_prod
            and self.auth_revocation_fail_closed is None
        ):
            object.__setattr__(self, "auth_revocation_fail_closed", True)
        return self

    @model_validator(mode="after")
    def _billing_requirements(self) -> Settings:
        """Fail fast when Stripe is selected but its keys are missing.

        A misconfigured billing provider must not surface only at the first
        webhook/charge in production — it aborts startup instead.
        """
        if (
            self.environment == Environment.prod
            and self.billing_provider == "fake"
            and "billing_provider" in self.model_fields_set
            and not self.accepts_risk("fake_psp_in_prod")
        ):
            # Only an EXPLICIT fake selection is rejected here: the kernel names
            # no business module, so a deployment without billing keeps the
            # field's default. The billing module's critical startup check
            # rejects the implicit default when billing is actually loaded.
            raise ValueError(
                "BILLING_PROVIDER=fake is not allowed with ENVIRONMENT=prod: use "
                "BILLING_PROVIDER=stripe (with STRIPE_SECRET_KEY and "
                "STRIPE_WEBHOOK_SECRET) or, for a demo stack, "
                'SANDLOOM_ACCEPTED_RISKS=["fake_psp_in_prod"]'
            )
        if self.billing_provider == "stripe":
            missing = [
                name
                for name, val in {
                    "STRIPE_SECRET_KEY": self.stripe_secret_key,
                    "STRIPE_WEBHOOK_SECRET": self.stripe_webhook_secret,
                }.items()
                if not val
            ]
            if missing:
                raise ValueError(
                    f"BILLING_PROVIDER=stripe requires variables: {', '.join(missing)}"
                )
        return self

    @model_validator(mode="after")
    def _storage_requirements(self) -> Settings:
        if self.storage_type == "local":
            # local directory
            if not self.storage_path:
                raise ValueError("STORAGE_PATH is required when STORAGE_TYPE=local")
        elif self.storage_type == "s3":
            missing = [
                name
                for name, val in {
                    "S3_BUCKET": self.s3_bucket,
                    "S3_REGION": self.s3_region,
                    "S3_ACCESS_KEY_ID": self.s3_access_key_id,
                    "S3_SECRET_ACCESS_KEY": self.s3_secret_access_key,
                }.items()
                if not val
            ]
            if missing:
                raise ValueError(
                    f"STORAGE_TYPE=s3 requires variables: {', '.join(missing)}"
                )
        # gcs/yc can be added similarly
        return self

    # ---- Helpers ----
    @property
    def is_dev(self) -> bool:
        return self.environment == Environment.dev

    @property
    def is_prod(self) -> bool:
        return self.environment == Environment.prod

    @property
    def accepted_risks(self) -> frozenset[str]:
        """Every accepted risk id (``SANDLOOM_ACCEPTED_RISKS``)."""
        return _risks.effective(self)

    def accepts_risk(self, risk_id: str) -> bool:
        """This deployment accepts ``risk_id``
        (``sandloom.core.config.accepted_risks``).

        ``KeyError`` for an id that is not registered — a typo in a check
        never silently reads as "not accepted".
        """
        return _risks.accepts(self, risk_id)

    @property
    def is_test(self) -> bool:
        return self.environment == Environment.test

    @property
    def revocation_fail_closed(self) -> bool:
        """Whether a revocation-store outage rejects tokens (503)."""
        if self.auth_revocation_fail_closed is not None:
            return self.auth_revocation_fail_closed
        return self.is_prod

    @property
    def docs_exposed(self) -> bool:
        """Whether interactive API docs should be served."""
        if self.docs_enabled is not None:
            return self.docs_enabled
        return not self.is_prod

    @property
    def effective_max_request_body_bytes(self) -> int:
        """Body cap applied by the request-size middleware (0 = unlimited)."""
        return self.max_request_body_bytes or self.max_upload_size_bytes

    @property
    def log_as_json(self) -> bool:
        """Whether logs should be emitted as structured JSON."""
        if self.log_format == "json":
            return True
        if self.log_format == "text":
            return False
        return self.is_prod  # auto


_LOAD_LOCK = threading.Lock()


def _load_into(target: Settings) -> None:
    """Validate the environment and turn ``target`` into a loaded ``Settings``.

    An invalid configuration exits the process, as it always has: entry
    points force the load (:func:`load_settings`, also run by
    ``enforce_configuration``), so a bad environment still fails at startup.
    """
    with _LOAD_LOCK:
        if type(target) is not _UnloadedSettings:
            return  # another thread loaded it
        try:
            fresh = Settings()
        except ValidationError as e:
            # fail loudly and immediately: an invalid config is unacceptable
            raise SystemExit(f"{INVALID_CONFIGURATION}{e}") from e
        for slot in _STATE_SLOTS:
            object.__setattr__(target, slot, object.__getattribute__(fresh, slot))
        object.__setattr__(target, "__class__", Settings)


_STATE_SLOTS = (
    "__dict__",
    "__pydantic_fields_set__",
    "__pydantic_extra__",
    "__pydantic_private__",
)


class _UnloadedSettings(Settings):
    """The process ``settings`` before first use (spec §2.4: lazy settings).

    Importing the framework reads no environment and no ``.env``: the
    instance validates itself on its first attribute access and then *is* a
    plain :class:`Settings` (its class is swapped), so a loaded instance
    costs nothing extra per access and ``isinstance`` / ``type()`` behave as
    before.
    """

    def __init__(self) -> None:  # no validation, no environment read
        pass

    def __getattribute__(self, name: str) -> Any:
        _load_into(self)
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        _load_into(self)
        setattr(self, name, value)

    def __delattr__(self, name: str) -> None:
        _load_into(self)
        delattr(self, name)


#: The process configuration. Lazy: validated on first use (see
#: :class:`_UnloadedSettings`); entry points call :func:`load_settings`.
settings: Settings = _UnloadedSettings()


def load_settings() -> Settings:
    """Force the process settings now (every entry point calls it).

    Exits with the configuration error when the environment is invalid.
    """
    _load_into(settings)
    return settings


def settings_loaded() -> bool:
    """Whether the process settings have been validated (no side effect)."""
    return type(settings) is not _UnloadedSettings
