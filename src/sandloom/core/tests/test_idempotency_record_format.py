"""Stored idempotency responses: raw-JSON bodies, base64 fallback, old records.

A JSON body is stored as raw JSON (no base64) only when re-serialising it is
byte-exact; every reader accepts both formats, so records written before the
change (and by not-yet-upgraded instances) keep replaying during a rolling
upgrade, and an old reader never re-executes a new-format record.
"""

from __future__ import annotations

import base64
from typing import Any

import orjson
import pytest

from sandloom.core.idempotency import fingerprint as idempotency_fingerprint
from sandloom.core.idempotency import replay as idempotency_replay


def _reservation(
    anonymous: bool = False,
) -> idempotency_fingerprint.IdempotencyReservation:
    return idempotency_fingerprint.IdempotencyReservation(
        full_key="idem:fmt:k",
        fingerprint="fp",
        ttl_seconds=60,
        pending_value="p",
        anonymous=anonymous,
    )


def _stored(record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """What the cache holds and what a reader gets back (``reserve_key``)."""
    raw = orjson.dumps(record).decode()
    parsed = idempotency_fingerprint._parse_record(raw)
    assert parsed["state"] == "succeeded"
    return raw, parsed["response"]


def _record(body: bytes, media_type: str | None = "application/json") -> Any:
    return idempotency_replay.success_record(
        _reservation(),
        status_code=201,
        body=body,
        headers={"content-type": media_type or "", "x-custom": "1"},
        media_type=media_type,
    )


@pytest.mark.parametrize(
    "body",
    [
        b'{"id":"5f0e","amount":12,"ok":true,"tags":["a","b"],"n":null}',
        b'{"name":"\xc3\xa9t\xc3\xa9 \\"quoted\\""}',
        b"[1,2.5,-3]",
        b"null",
    ],
)
def test_json_body_is_stored_raw_and_replays_byte_identical(body: bytes) -> None:
    raw, stored = _stored(_record(body))
    assert body.decode() in raw  # verbatim, not base64
    assert base64.b64encode(body).decode() not in raw
    response = idempotency_replay._response_from_stored(stored)
    assert response.body == body
    assert response.status_code == 201
    assert "idempotent-replay-truncated" not in response.headers
    assert idempotency_replay.stored_response_body(stored) == body


def test_raw_json_record_is_smaller_than_base64() -> None:
    body = orjson.dumps({"items": [{"id": i, "name": f"item-{i}"} for i in range(50)]})
    raw_new, _ = _stored(_record(body))
    old = {
        "state": "succeeded",
        "fp": "fp",
        "response": {
            "status": 201,
            "body": base64.b64encode(body).decode(),
            "headers": {"content-type": "application/json", "x-custom": "1"},
            "media_type": "application/json",
        },
    }
    assert len(raw_new) < 0.8 * len(orjson.dumps(old))


@pytest.mark.parametrize(
    ("body", "media_type"),
    [
        (b'{"big":123456789012345678901234567890}', "application/json"),  # > 64 bit
        (b'{"f":1e-07}', "application/json"),  # orjson spells it 1e-7
        (b'{"a":1,"a":2}', "application/json"),  # duplicate keys
        (b'{"a": 1}', "application/json"),  # whitespace
        (b"not json", "application/json"),
        (b'{"a":1}', "text/plain"),  # not a JSON media type
        (b"\x82\xa1a\x01", "application/msgpack"),  # RPC result
        (b"", "application/json"),
    ],
)
def test_other_bodies_keep_the_base64_format(body: bytes, media_type: str) -> None:
    _, stored = _stored(_record(body, media_type))
    assert idempotency_replay._JSON_BODY not in stored
    assert stored["body"] == base64.b64encode(body).decode()
    assert idempotency_replay._response_from_stored(stored).body == body


def test_a_record_written_before_the_change_still_replays() -> None:
    body = b'{"id":"abc","amount":12}'
    old_raw = orjson.dumps(
        {
            "state": "succeeded",
            "fp": "fp",
            "response": {
                "status": 201,
                "body": base64.b64encode(body).decode(),
                "headers": {"content-type": "application/json", "x-custom": "1"},
                "media_type": "application/json",
            },
        }
    ).decode()
    stored = idempotency_fingerprint._parse_record(old_raw)["response"]
    response = idempotency_replay._response_from_stored(stored)
    assert response.body == body
    assert response.status_code == 201
    assert response.headers["x-custom"] == "1"
    assert response.headers["idempotent-replay"] == "true"
    assert "idempotent-replay-truncated" not in response.headers
    # An old truncated (status-only) record stays status-only.
    old_truncated = {"status": 200, "body": "", "headers": {}, "truncated": True}
    assert idempotency_replay.stored_response_body(old_truncated) is None
    replay = idempotency_replay._response_from_stored(old_truncated)
    assert replay.body == b""
    assert replay.headers["idempotent-replay-truncated"] == "true"


def test_an_old_reader_replays_a_new_record_status_only_never_re_executes() -> None:
    """The pre-change reader: base64 ``body`` + ``truncated`` → status only."""
    _, stored = _stored(_record(b'{"id":"x"}'))
    assert stored["truncated"] is True and stored["body"] == ""
    assert base64.b64decode(stored["body"]) == b""
    assert stored["status"] == 201


def test_anonymous_and_unreplayable_records_are_unchanged() -> None:
    for record in (
        idempotency_replay.success_record(
            _reservation(anonymous=True),
            status_code=200,
            body=b'{"a":1}',
            headers={},
            media_type="application/json",
        ),
        idempotency_replay.success_record(
            _reservation(),
            status_code=200,
            body=b'{"a":1}',
            headers={},
            media_type="application/json",
            replayable=False,
        ),
    ):
        _, stored = _stored(record)
        assert stored == {
            "status": 200,
            "body": "",
            "headers": {},
            "media_type": None,
            "truncated": True,
        }
        assert idempotency_replay.stored_response_body(stored) is None
