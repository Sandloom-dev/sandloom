"""``POST /api/v1/orgs``: ``slug`` is optional and derived from ``name``."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_tenancy.exceptions import TenancyConflictError
from sandloom_tenancy.schemas import OrganizationCreateIn
from sandloom_tenancy.schemas.organization import NO_DERIVED_SLUG, derive_slug
from sandloom_tenancy.services import OrganizationService


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Acme Robotics, Inc.", "acme-robotics-inc"),
        ("  My   workspace  ", "my-workspace"),
        ("--Q3 / R&D--", "q3-r-d"),
        ("Ünïcode Straße 42", "n-code-stra-e-42"),
        ("ab", "ab"),
        ("x" * 150, "x" * 100),
        (("a" * 99) + " b", "a" * 99),  # the cut lands on a '-': trimmed again
    ],
)
def test_the_slug_is_derived_from_the_name(name: str, slug: str) -> None:
    assert derive_slug(name) == slug


@pytest.mark.parametrize("name", ["!!!", "日本語", "a", " - "])
def test_a_name_with_nothing_to_derive_from_has_no_slug(name: str) -> None:
    assert derive_slug(name) is None
    with pytest.raises(ValidationError) as refused:
        OrganizationCreateIn(name=name)
    assert NO_DERIVED_SLUG in str(refused.value)
    assert OrganizationCreateIn(name=name, slug="chosen-slug").slug == "chosen-slug"


def test_an_explicit_slug_is_still_validated() -> None:
    with pytest.raises(ValidationError):
        OrganizationCreateIn(name="Acme", slug="Not A Slug")


async def test_creating_without_a_slug_derives_it(
    async_client: AsyncClient, user_a: Any
) -> None:
    _, headers = user_a
    name = f"Derived {uuid.uuid4().hex[:8]} Co."
    resp = await async_client.post("/api/v1/orgs", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["slug"] == f"derived-{name.split()[1]}-co"


async def test_an_explicit_slug_wins_over_the_name(
    async_client: AsyncClient, user_a: Any
) -> None:
    _, headers = user_a
    slug = f"chosen-{uuid.uuid4().hex[:8]}"
    resp = await async_client.post(
        "/api/v1/orgs", json={"name": "Whatever Name", "slug": slug}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["slug"] == slug


async def test_a_taken_derived_slug_is_409_and_says_to_pass_slug(
    async_client: AsyncClient, user_a: Any, user_b: Any
) -> None:
    _, headers_a = user_a
    _, headers_b = user_b
    name = f"Twin {uuid.uuid4().hex[:8]}"
    first = await async_client.post(
        "/api/v1/orgs", json={"name": name}, headers=headers_a
    )
    assert first.status_code == 201, first.text

    second = await async_client.post(
        "/api/v1/orgs", json={"name": name}, headers=headers_b
    )

    assert second.status_code == 409, second.text
    assert "derived from the name" in second.json()["detail"]
    assert "pass slug" in second.json()["detail"]


async def test_a_taken_explicit_slug_keeps_its_own_message(
    async_client: AsyncClient, user_a: Any, org_with_owner: Any
) -> None:
    _, headers = user_a
    slug = org_with_owner["slug"]
    resp = await async_client.post(
        "/api/v1/orgs", json={"name": "Other", "slug": slug}, headers=headers
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == f"Organization slug '{slug}' is already taken"


@pytest.mark.parametrize("name", ["!!!", "A", "日本語"])
async def test_a_name_that_yields_no_valid_slug_is_422(
    async_client: AsyncClient, user_a: Any, name: str
) -> None:
    """A name of no ASCII letters or digits, or of one character, has no slug of
    the two-character minimum: the 422 says to pass one."""
    _, headers = user_a
    resp = await async_client.post("/api/v1/orgs", json={"name": name}, headers=headers)
    assert resp.status_code == 422, resp.text
    assert NO_DERIVED_SLUG in resp.text


async def test_a_dto_built_without_validation_is_refused_by_the_service(
    test_db: AsyncSession,
) -> None:
    unvalidated = OrganizationCreateIn.model_construct(name="A", slug=None)
    with pytest.raises(TenancyConflictError, match="pass an explicit slug"):
        await OrganizationService.create(unvalidated, owner_id=uuid.uuid4(), db=test_db)
