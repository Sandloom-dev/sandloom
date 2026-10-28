"""Object storage, request-size caps, API docs and notifications."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ServicesConfig(BaseModel):
    """Object storage, request-size caps, API docs and notifications."""

    # Storage
    storage_type: Literal["local", "s3", "gcs", "yc"] = Field(
        default="local", alias="STORAGE_TYPE"
    )
    storage_path: str = Field(default="./data", alias="STORAGE_PATH")  # local
    # Upload cap enforced by Storage.save (0 = unlimited)
    max_upload_size_bytes: int = Field(
        default=50 * 1024 * 1024, alias="MAX_UPLOAD_SIZE_BYTES"
    )
    # Global request-body cap (Content-Length) rejected with 413. 0 = fall back
    # to max_upload_size_bytes so large multipart uploads still pass.
    max_request_body_bytes: int = Field(default=0, alias="MAX_REQUEST_BODY_BYTES")

    # Expose interactive API docs (/docs, /redoc, /openapi.json). Default: on
    # outside production, off in production unless explicitly enabled.
    docs_enabled: bool | None = Field(default=None, alias="DOCS_ENABLED")

    # Notifications: default "log" delivers by structured log (zero-dependency,
    # safe everywhere); "smtp" sends email via the SMTP_* settings below.
    notifier_backend: Literal["log", "smtp"] = Field(
        default="log", alias="NOTIFIER_BACKEND"
    )
    # In prod the log driver advertises no delivery features (a module that
    # requires e.g. features={"email"} fails at startup instead of logging
    # mail nobody receives); SANDLOOM_ACCEPTED_RISKS "log_notifier_in_prod" restores
    # them (demo stacks).
    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=25, alias="SMTP_PORT")
    smtp_username: str | None = Field(default=None, alias="SMTP_USERNAME")
    smtp_password: str | None = Field(default=None, alias="SMTP_PASSWORD")
    smtp_from: str | None = Field(default=None, alias="SMTP_FROM")
    smtp_use_tls: bool = Field(default=True, alias="SMTP_USE_TLS")

    # S3 / GCS / YC (optional; required if the matching storage_type is selected)
    s3_bucket: str | None = Field(default=None, alias="S3_BUCKET")
    s3_region: str | None = Field(default=None, alias="S3_REGION")
    s3_access_key_id: str | None = Field(default=None, alias="S3_ACCESS_KEY_ID")
    s3_secret_access_key: str | None = Field(default=None, alias="S3_SECRET_ACCESS_KEY")
