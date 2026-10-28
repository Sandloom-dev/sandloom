"""Stored responses: what a succeeded record keeps and how it is replayed.

:func:`success_record` turns a captured response into a terminal record,
:func:`stored_response` / :func:`stored_response_body` turn one back into a
response, and :class:`_ReplayResponse` (rendered by :func:`_replay_handler`)
is how a guard answers a retry with the original result
(``Idempotent-Replay: true``).
"""

from __future__ import annotations

import base64
import logging
from typing import Any, cast

import orjson
from fastapi import Request, Response

from sandloom.core.idempotency.fingerprint import (
    _SKIP_REPLAY_HEADERS,
    IdempotencyReservation,
)

logger = logging.getLogger("sandloom.core.idempotency")


class _ReplayResponse(Exception):
    """Signal: replay a previously stored successful response for this key."""

    def __init__(self, stored: dict[str, Any]) -> None:
        super().__init__("idempotency replay")
        self.stored = stored


#: Record field of a JSON body stored as raw JSON (see :func:`success_record`).
_JSON_BODY = "json_body"


def stored_response_body(stored: dict[str, Any]) -> bytes | None:
    """The replayable body of a stored response, or ``None`` (status only).

    Reads both record formats: a JSON body stored as raw JSON (``json_body``;
    written only when re-serialising it is byte-exact) and the base64 ``body``
    of every other response and of records written before that format.
    """
    if _JSON_BODY in stored:
        return orjson.dumps(stored[_JSON_BODY])
    if stored.get("truncated"):
        return None
    return base64.b64decode(stored.get("body", "") or "")


def _response_from_stored(stored: dict[str, Any]) -> Response:
    replayable = stored_response_body(stored)
    body = b"" if replayable is None else replayable
    headers = {
        k: v
        for k, v in (stored.get("headers") or {}).items()
        if k.lower() not in _SKIP_REPLAY_HEADERS
    }
    headers["Idempotent-Replay"] = "true"
    if replayable is None:
        # Original body was not stored (too large, incomplete, or withheld
        # from an anonymous caller); we replay status only.
        headers["Idempotent-Replay-Truncated"] = "true"
    return Response(
        content=body,
        status_code=int(stored.get("status", 200)),
        headers=headers,
        media_type=stored.get("media_type"),
    )


async def _replay_handler(_request: Request, exc: Exception) -> Response:
    """Error handler that turns a replay signal into the stored response."""
    return _response_from_stored(cast(_ReplayResponse, exc).stored)


def success_record(
    reservation: IdempotencyReservation,
    *,
    status_code: int,
    body: bytes,
    headers: dict[str, str],
    media_type: str | None,
    replayable: bool = True,
) -> dict[str, Any]:
    """Build the terminal ``succeeded`` record for ``reservation``.

    A JSON body is stored as raw JSON (``orjson.Fragment``: no base64, ~25%
    smaller on the security cache) when parsing and re-serialising it gives
    back the very same bytes — the replay is then byte-identical; any other
    body (non-JSON, big integers, duplicate keys, other float spellings) is
    stored base64 as before. Such a record also says ``truncated``: a reader
    that predates the format (rolling upgrade) replays it status-only — never
    re-executes it — while current readers replay the body.
    """
    return {
        "state": "succeeded",
        "fp": reservation.fingerprint,
        "response": stored_response(
            status_code=status_code,
            body=body,
            headers=headers,
            media_type=media_type,
            replayable=replayable and not reservation.anonymous,
        ),
    }


def stored_response(
    *,
    status_code: int,
    body: bytes,
    headers: dict[str, str],
    media_type: str | None,
    replayable: bool = True,
) -> dict[str, Any]:
    """The stored form of one response (shared by the cache and durable forms).

    ``replayable=False`` keeps the status only (``truncated``).
    """
    if replayable:
        if _json_round_trips(body, media_type):
            stored: dict[str, Any] = {
                "status": status_code,
                _JSON_BODY: orjson.Fragment(body),
                "body": "",
                "headers": headers,
                "media_type": media_type,
                "truncated": True,
            }
        else:
            stored = {
                "status": status_code,
                "body": base64.b64encode(body).decode(),
                "headers": headers,
                "media_type": media_type,
            }
    else:
        stored = {
            "status": status_code,
            "body": "",
            "headers": {},
            "media_type": None,
            "truncated": True,
        }
    return stored


def _json_round_trips(body: bytes, media_type: str | None) -> bool:
    if not body or media_type is None:
        return False
    if media_type != "application/json" and not media_type.endswith("+json"):
        return False
    try:
        return orjson.dumps(orjson.loads(body)) == body
    except (orjson.JSONDecodeError, orjson.JSONEncodeError, TypeError, ValueError):
        return False
