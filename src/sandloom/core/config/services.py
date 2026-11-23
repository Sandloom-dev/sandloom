"""Object storage, request-size caps, API docs and notifications."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ServicesConfig(BaseModel):
    """Object storage, request-size caps, API docs and notifications."""

    # Storage
    storage_type: Literal["local", "s3", "gcs", "yc"] | None = Field(
        default=None,
        alias="STORAGE_TYPE",
        description=(
            "Default storage driver selector. Unset, the driver the app's "
            "composition or INFRA selects stands, else local files, which ship "
            "ready to use; set, it replaces the composition's choice (with a "
            "warning) and other selections require a matching installed and "
            "configured driver."
        ),
    )
    storage_path: str = Field(
        default="./data",
        alias="STORAGE_PATH",
        description=("Filesystem root used by local object storage."),
    )  # local
    # Upload cap enforced by Storage.save (0 = unlimited)
    max_upload_size_bytes: int = Field(
        default=50 * 1024 * 1024,
        alias="MAX_UPLOAD_SIZE_BYTES",
        description=(
            "Maximum bytes accepted by Storage.save; zero disables this upload cap."
        ),
    )
    # Global request-body cap (Content-Length) rejected with 413. 0 = fall back
    # to max_upload_size_bytes so large multipart uploads still pass.
    max_request_body_bytes: int = Field(
        default=0,
        alias="MAX_REQUEST_BODY_BYTES",
        description=(
            "Global Content-Length request-body cap in bytes; zero falls back to "
            "MAX_UPLOAD_SIZE_BYTES."
        ),
    )

    # Expose interactive API docs (/docs, /redoc, /openapi.json). Default: on
    # outside production, off in production unless explicitly enabled.
    docs_enabled: bool | None = Field(
        default=None,
        alias="DOCS_ENABLED",
        description=(
            "Expose /docs, /redoc and /openapi.json; unset enables them outside "
            "production and disables them in production."
        ),
    )

    # Notifications: default "log" delivers by structured log (zero-dependency,
    # safe everywhere); "smtp" sends email through the options of
    # INFRA.notification.default.
    notifier_backend: Literal["log", "smtp"] | None = Field(
        default=None,
        alias="NOTIFIER_BACKEND",
        description=(
            "Default notifier driver. Unset, the driver the app's composition or "
            "INFRA selects stands, else log; set, it replaces the composition's "
            "choice (with a warning). log records delivery metadata without "
            "message contents; smtp sends email with the options of "
            "INFRA.notification.default."
        ),
    )
    notifier_log_links: bool = Field(
        default=False,
        alias="NOTIFIER_LOG_LINKS",
        description=(
            "The log notifier also logs the links an email carries (verification, "
            "password reset), marked [dev only], so a dev app without SMTP can "
            "follow them. Refused with ENVIRONMENT=prod unless "
            "SANDLOOM_ACCEPTED_RISKS lists log_links_in_prod."
        ),
    )
    # In prod the log driver advertises no delivery features (a module that
    # requires e.g. features={"email"} fails at startup instead of logging
    # mail nobody receives); SANDLOOM_ACCEPTED_RISKS "log_notifier_in_prod" restores
    # them (demo stacks).
    # The options of the ``smtp`` notifier (host, port, credentials, ``use_tls``)
    # and of the ``s3`` object store (bucket, region, keys) are driver options of
    # their INFRA instances (INFRA.notification.default, INFRA.objectstore.default):
    # no settings of their own.
