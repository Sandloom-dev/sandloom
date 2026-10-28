"""Synthetic accounts exercising the shipped identity/tenancy/billing APIs.

Passwords and tokens stay in memory. Email verification is a fixture SQL step,
as in CI's microservices smoke; no external SMTP or payment API is contacted.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

import check
from run import STATE


def request(method, path, jwt=None, body=None):
    headers = {"Content-Type": "application/json"}
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    req = urllib.request.Request(
        check.BASE + path,
        method=method,
        headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with check.HTTP.open(req, timeout=12) as response:
            return response.status, check.response_body(response)
    except urllib.error.HTTPError as exc:
        return exc.code, check.response_body(exc)
    except (OSError, urllib.error.URLError):
        return 0, None


@dataclass
class Subject:
    email: str
    password: str
    jwt: str
    user_id: str
    org_id: str
    foreign_org: str = ""
    logged_in_at: float = 0
    refresh_token: str | None = None

    def login(self):
        status, body = request(
            "POST",
            "/auth/login",
            body={
                "email": self.email,
                "password": self.password,
            },
        )
        if status == 200:
            self.jwt = body["access_token"]
            self.refresh_token = body["refresh_token"]
            self.logged_in_at = time.monotonic()
        return status

    def refresh(self):
        # An ambiguous exchange may already have rotated this token. Never
        # replay it: recover with a fresh login on the next attempt instead.
        if self.refresh_token is None:
            return self.login()
        refresh_token, self.refresh_token = self.refresh_token, None
        status, body = request(
            "POST", "/auth/refresh", body={"refresh_token": refresh_token}
        )
        if status == 200:
            self.jwt = body["access_token"]
            self.refresh_token = body["refresh_token"]
            self.logged_in_at = time.monotonic()
        return status


def prepare(count=4):
    saved = STATE / "real-subjects.json"
    if saved.exists():
        subjects = [Subject(**data) for data in json.loads(saved.read_text())][:count]
        assert len(subjects) == count, "fixture has fewer subjects than requested"
        for subject in subjects:
            assert subject.login() == 200, "fixture login failed"
        return subjects
    subjects = []
    for _ in range(count):
        email = f"qualification-{uuid.uuid4().hex}@example.com"
        password = "Q7!" + secrets.token_urlsafe(24)
        status, _ = request(
            "POST",
            "/auth/register",
            body={
                "email": email,
                "password": password,
                "full_name": "Qualification",
            },
        )
        assert status == 202, ("register", status)
        # Generated ASCII hex email, never operator-supplied SQL input.
        user_id = check.sql(
            "UPDATE users SET is_verified=true,email_verified_at=now() "
            f"WHERE email='{email}' RETURNING id"
        ).splitlines()[0]
        user_id = str(uuid.UUID(user_id))
        subject = Subject(email, password, "", user_id, "")
        assert subject.login() == 200, "initial login failed"
        status, body = request(
            "POST",
            "/api/v1/orgs",
            subject.jwt,
            {
                "name": "Qualification",
                "slug": "qualification-" + uuid.uuid4().hex,
            },
        )
        assert status == 201, ("create organization", status)
        subject.org_id = str(uuid.UUID(body["id"]))
        subjects.append(subject)
    for i, subject in enumerate(subjects):
        subject.foreign_org = subjects[(i + 1) % len(subjects)].org_id
        assert (
            request(
                "GET", f"/api/v1/billing/credits?org_id={subject.org_id}", subject.jwt
            )[0]
            == 200
        )
        assert request(
            "GET", f"/api/v1/billing/credits?org_id={subject.foreign_org}", subject.jwt
        )[0] in (403, 404)
    fd = os.open(saved, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(
            [
                {
                    key: getattr(s, key)
                    for key in ("email", "password", "user_id", "org_id", "foreign_org")
                }
                | {"jwt": ""}
                for s in subjects
            ],
            stream,
        )
    return subjects
