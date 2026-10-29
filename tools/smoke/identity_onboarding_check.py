#!/usr/bin/env python3
"""Exercise the documented identity + tenancy onboarding through real SMTP.

Run against a disposable local application with identity/tenancy and Mailpit:
    python3 tools/smoke/identity_onboarding_check.py \
        --base-url http://127.0.0.1:8000 --mailpit-url http://127.0.0.1:8025

Creates a uniquely named synthetic user and organization. Requires the app's
SMTP notifier to deliver to this inbox; tokens and credentials are never printed.
Only loopback HTTP endpoints are accepted. Mailpit's public API is documented at
https://mailpit.axllent.org/docs/api-v1/.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class OnboardingError(Exception):
    pass


def local_url(value: str) -> str:
    parts = urllib.parse.urlsplit(value)
    if (
        parts.scheme != "http"
        or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise argparse.ArgumentTypeError("use a loopback HTTP origin without a path")
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, base: str) -> None:
        self.base = base
        # Local synthetic credentials must not go through a configured proxy.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        body: dict[str, Any] | None = None,
        token: str | None = None,
        tenant_id: str | None = None,
    ) -> Any:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if tenant_id is not None:
            headers["X-Tenant-Id"] = tenant_id
        request = urllib.request.Request(  # noqa: S310 - both origins are validated as loopback HTTP; redirects/proxies are disabled
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            response = self.opener.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            if response.status != status:
                raise OnboardingError(
                    f"{method} {path.split('?', 1)[0]}: "
                    f"expected {status}, got {response.status}"
                )
            data = response.read(4 * 1024 * 1024)
        return json.loads(data) if data else None


def verification_token(inbox: API, email: str, timeout: float) -> str:
    query = urllib.parse.urlencode({"query": f"to:{email}"})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = inbox.request("GET", f"/api/v1/search?{query}")
        for summary in found.get("messages", []):
            message_id = urllib.parse.quote(summary["ID"], safe="")
            message = inbox.request("GET", f"/api/v1/message/{message_id}")
            text = html.unescape(message.get("Text", "") + message.get("HTML", ""))
            for url in re.findall(r"https?://[^\s<>\"']+", text):
                parts = urllib.parse.urlsplit(url)
                if parts.path.endswith("/auth/verify-email"):
                    tokens = urllib.parse.parse_qs(parts.query).get("token", [])
                    if len(tokens) == 1:
                        return tokens[0]
        time.sleep(0.2)
    raise OnboardingError("verification email did not reach the configured local inbox")


def check(api: API, inbox: API, timeout: float, module: str) -> None:
    nonce = secrets.token_hex(8)
    email = f"onboarding-{nonce}@example.com"
    credentials = {"email": email, "password": f"Smoke-{nonce}-Passw0rd!"}
    api.request(
        "POST",
        "/auth/register",
        status=202,
        body={**credentials, "full_name": "Onboarding Smoke"},
    )
    api.request("POST", "/auth/login", status=403, body=credentials)
    verification = verification_token(inbox, email, timeout)
    api.request("POST", "/auth/verify-email", status=204, body={"token": verification})
    pair = api.request("POST", "/auth/login", body=credentials)
    user = api.request("GET", "/auth/me", token=pair["access_token"])
    organization = api.request(
        "POST",
        "/api/v1/orgs",
        status=201,
        token=pair["access_token"],
        body={"name": "Onboarding workspace", "slug": f"onboarding-{nonce}"},
    )
    if organization["owner_id"] != user["id"]:
        raise OnboardingError("the new user is not the organization's owner")
    path = f"/api/v1/orgs/{organization['id']}"
    api.request("GET", path, token=pair["access_token"])
    items = f"/api/v1/{module}/items"
    api.request(
        "GET",
        items,
        status=403,
        token=pair["access_token"],
        tenant_id=organization["id"],
    )
    refreshed = api.request(
        "POST", "/auth/refresh", body={"refresh_token": pair["refresh_token"]}
    )
    if refreshed["refresh_token"] == pair["refresh_token"]:
        raise OnboardingError("refresh did not rotate the token pair")
    api.request("GET", path, token=refreshed["access_token"])
    listed = api.request(
        "GET", items, token=refreshed["access_token"], tenant_id=organization["id"]
    )
    if listed != []:
        raise OnboardingError("the newly created organization's item list is not empty")
    print("identity onboarding: OK (SMTP, verification, login, organization, refresh)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", type=local_url, default="http://127.0.0.1:8000")
    parser.add_argument(
        "--mailpit-url", type=local_url, default="http://127.0.0.1:8025"
    )
    parser.add_argument("--mail-timeout", type=float, default=20)
    parser.add_argument("--module", default="notes", help="generated sample module")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", args.module):
        parser.error("--module must be a generated snake_case module name")
    try:
        check(API(args.base_url), API(args.mailpit_url), args.mail_timeout, args.module)
    except (OnboardingError, urllib.error.URLError, ValueError, KeyError) as error:
        print(
            f"identity onboarding: FAILED ({type(error).__name__}: {error})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
