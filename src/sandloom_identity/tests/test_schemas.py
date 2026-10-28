"""User schemas: e-mail validation happens on input, never again on output."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pydantic.networks
import pytest
from pydantic import ValidationError

from sandloom_identity.schemas.user import UserCreate, UserOut


def _stored_user(email: str) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid4(),
        email=email,
        full_name="Ada",
        first_name=None,
        last_name=None,
        username=None,
        is_active=True,
        is_superuser=False,
        is_verified=True,
        created_at=now,
        updated_at=now,
        last_login_at=None,
        email_verified_at=None,
    )


def test_user_out_copies_the_stored_address_without_revalidating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_validation(*_a: object, **_k: object) -> None:
        raise AssertionError("UserOut must not re-validate the stored e-mail")

    monkeypatch.setattr(pydantic.networks, "validate_email", _no_validation)
    out = UserOut.model_validate(_stored_user("Ada@Example.com"))
    assert out.email == "Ada@Example.com"


def test_user_out_schema_still_declares_an_email() -> None:
    email = UserOut.model_json_schema()["properties"]["email"]
    assert email == {"format": "email", "title": "Email", "type": "string"}


def test_input_schemas_still_validate_the_address() -> None:
    with pytest.raises(ValidationError):
        UserCreate(email="not-an-address", full_name="Ada", password="x" * 12)
