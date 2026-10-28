"""Shortlink business logic.

Demonstrates the framework's infrastructure-agnostic surface in a tiny app:
the request's session for persistence, the operation-based cache provider
(``cache()``) for a hot code->URL lookup and live click counter, and a typed
``LinkCreated`` event published through the transactional outbox. No auth, no
other modules.
"""

import secrets
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.infra import cache  # sandloom: advanced operation-based cache

from ..events import LinkCreated
from ..models import Shortlink
from ..schemas import LinkCreateIn
from .operations import link_target_by_code

_CODE_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_URL_TTL_SECONDS = 3600


def _gen_code(length: int = 7) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def _url_key(code: str) -> str:
    return f"links:url:{code}"


def _clicks_key(code: str) -> str:
    return f"links:clicks:{code}"


class LinkService:
    @classmethod
    async def create(cls, data: LinkCreateIn, db: AsyncSession) -> Shortlink:
        code = data.custom_code or _gen_code()
        link = Shortlink(code=code, target_url=data.target_url)
        db.add(link)
        try:
            await db.flush()
        except IntegrityError as exc:
            # Code collision (custom or — very rarely — a generated one).
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"short code '{code}' is already taken",
            ) from exc
        await db.refresh(link)
        await LinkCreated(link_id=link.id, code=link.code).publish()
        return link

    @classmethod
    async def resolve(cls, code: str, reads: Any) -> str:
        """Return the target URL for ``code`` and bump the live click counter.

        ``reads`` is the route's ``PlannedReads``: a cache hit touches no
        database at all (the read-only scope opens no transaction until a
        declared read runs); a miss is one declared read.
        """
        c = cache()
        cached = await c.get(_url_key(code))
        if cached is not None:
            target = cached
        else:
            link = await reads.one(link_target_by_code, code=code)
            if link is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="short code not found"
                )
            target = link.target_url
            await c.set(_url_key(code), target, ttl_seconds=_URL_TTL_SECONDS)
        await c.incr(_clicks_key(code))
        return target

    @classmethod
    async def get_or_404(cls, link_id: UUID, db: AsyncSession) -> Shortlink:
        result = await db.execute(select(Shortlink).where(Shortlink.id == link_id))
        link = result.scalar_one_or_none()
        if link is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="link not found"
            )
        return link

    @classmethod
    async def clicks(cls, code: str) -> int:
        raw = await cache().get(_clicks_key(code))
        return int(raw) if raw is not None else 0
