"""The log redactor masks every setting ``sandloom config dump`` masks.

Regression for ``TWOFA_ENCRYPTION_KEY``: ``config dump`` redacted it while the
log redactor (:func:`guardrails.is_sensitive_key`) let it through in clear.
Every field of ``Settings`` is enumerated, so a new secret setting is covered
without editing this test; the secrets the bundled modules declare are listed.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import SecretStr

from sandloom.core.cli import config as config_cli
from sandloom.core.config.settings import Settings
from sandloom.core.observability import guardrails
from sandloom.core.observability.logging import _redact_record

_SAMPLE = "s3cr3t-value"


def _setting_names() -> list[str]:
    """Env names (alias, upper-cased) of every ``Settings`` field."""
    return sorted(
        str(field.alias or name).upper()
        for name, field in Settings.model_fields.items()
    )


def _dump_masks(name: str) -> bool:
    """Whether ``config dump`` redacts a plain string value of setting ``name``."""
    return config_cli.redact_value(name, _SAMPLE) != _SAMPLE


#: Secrets that modules declare (ADR-0015) rather than ``Settings``: the same
#: two masks must cover them (``config dump`` enumerates declarations from KB-55).
_MODULE_DECLARED_SECRETS = (
    "TWOFA_ENCRYPTION_KEY",
    "GOOGLE_CLIENT_SECRET",
    "GITHUB_CLIENT_SECRET",
    "MICROSOFT_CLIENT_SECRET",
    "LINKEDIN_CLIENT_SECRET",
    "TELEGRAM_BOT_TOKEN",
)
SECRET_SETTINGS = [
    n for n in [*_setting_names(), *_MODULE_DECLARED_SECRETS] if _dump_masks(n)
]


def test_secret_settings_are_enumerated() -> None:
    """The enumeration finds the known secrets, the 2FA key among them."""
    for name in (
        "TWOFA_ENCRYPTION_KEY",
        "JWT_SECRET",
        "STRIPE_SECRET_KEY",
        "SENTRY_DSN",
        "S3_ACCESS_KEY_ID",
    ):
        assert name in SECRET_SETTINGS


@pytest.mark.parametrize("name", SECRET_SETTINGS)
def test_log_redactor_masks_every_dump_secret(name: str) -> None:
    """Key check, dict redaction and a log record extra all mask ``name``."""
    for key in (name, name.lower()):
        assert guardrails.is_sensitive_key(key), key
        assert guardrails.redact({key: _SAMPLE}) == {key: "***"}
        record = logging.makeLogRecord({"msg": "boot", key: _SAMPLE})
        _redact_record(record)
        assert getattr(record, key) == "***"


@pytest.mark.parametrize("name", SECRET_SETTINGS)
def test_dump_masks_every_secret_setting(name: str) -> None:
    """``config dump`` masks the setting as a plain string and as a SecretStr."""
    assert config_cli.redact_value(name, _SAMPLE) == config_cli.REDACTED
    assert config_cli.redact_value(name, SecretStr(_SAMPLE)) == config_cli.REDACTED


@pytest.mark.parametrize("key", ["operation_key", "idempotency_key", "dedupe_key"])
def test_lookup_keys_stay_readable(key: str) -> None:
    """Identifier keys in audit data and log extras are not credentials."""
    assert not guardrails.is_sensitive_key(key)
    assert guardrails.redact({key: "op-1"}) == {key: "op-1"}
