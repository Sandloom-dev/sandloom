"""Object storage, request-size caps, API docs and notifications."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ServicesConfig(BaseModel):
    """Object storage, request-size caps, API docs and notifications."""

    # Storage
    storage_type: Literal["local", "s3", "gcs", "yc"] = Field(
        default="local",
        alias="STORAGE_TYPE",
        description=(
            "Default storage driver selector; local files ship ready to use, "
            "while other selections require a matching installed and configured "
            "driver."
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
    # safe everywhere); "smtp" sends email via the SMTP_* settings below.
    notifier_backend: Literal["log", "smtp"] = Field(
        default="log",
        alias="NOTIFIER_BACKEND",
        description=(
            "Default notifier driver: log records delivery metadata without "
            "message contents; smtp sends email using SMTP settings."
        ),
    )
    # In prod the log driver advertises no delivery features (a module that
    # requires e.g. features={"email"} fails at startup instead of logging
    # mail nobody receives); SANDLOOM_ACCEPTED_RISKS "log_notifier_in_prod" restores
    # them (demo stacks).
    smtp_host: str | None = Field(
        default=None,
        alias="SMTP_HOST",
        description=("SMTP server hostname used by the email notifier."),
    )
    smtp_port: int = Field(
        default=25,
        alias="SMTP_PORT",
        description=("SMTP server TCP port used by the email notifier."),
    )
    smtp_username: str | None = Field(
        default=None,
        alias="SMTP_USERNAME",
        description=("Optional SMTP authentication username."),
    )
    smtp_password: str | None = Field(
        default=None,
        alias="SMTP_PASSWORD",
        description=("Optional SMTP authentication password."),
    )
    smtp_from: str | None = Field(
        default=None,
        alias="SMTP_FROM",
        description=("Sender email address used by the SMTP notifier."),
    )
    smtp_use_tls: bool = Field(
        default=True,
        alias="SMTP_USE_TLS",
        description=(
            "Upgrade SMTP with STARTTLS and certificate verification; disable only "
            "for a trusted local development inbox."
        ),
    )

    # S3 / GCS / YC (optional; required if the matching storage_type is selected)
    s3_bucket: str | None = Field(
        default=None,
        alias="S3_BUCKET",
        description=("Bucket name supplied to the S3 object-storage driver."),
    )
    s3_region: str | None = Field(
        default=None,
        alias="S3_REGION",
        description=("Region supplied to the S3 object-storage driver."),
    )
    s3_access_key_id: str | None = Field(
        default=None,
        alias="S3_ACCESS_KEY_ID",
        description=("Access-key ID supplied to the S3 object-storage driver."),
    )
    s3_secret_access_key: str | None = Field(
        default=None,
        alias="S3_SECRET_ACCESS_KEY",
        description=("Secret access key supplied to the S3 object-storage driver."),
    )
