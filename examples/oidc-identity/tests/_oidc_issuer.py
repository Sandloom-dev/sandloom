"""A local external OIDC issuer for the oidc-identity example.

The stand-in for a third-party identity provider (Keycloak, Auth0, Entra ID):
an HTTP server on a loopback port serving OpenID discovery
(``/.well-known/openid-configuration``) and its signing keys (``/jwks``),
plus the test-side half of a sign-in: id_tokens and back-channel logout
tokens signed RS256 with a key only this object holds. It knows nothing of
Sandloom; the application's ``acme_idp`` module verifies what it signs the
way it would verify a real provider's tokens (discovery, JWKS, ``iss``,
``aud``, ``exp``, ``kid``).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

#: The ``events`` member of an OIDC back-channel logout token.
BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"


class ExternalIssuer:
    """An OIDC issuer on ``127.0.0.1:<port>``: discovery, JWKS, signed tokens.

    ``rotate()`` publishes a new signing key (the old one stays in the JWKS
    until ``retire()``), so a verifier that caches the JWKS must refetch on
    an unknown ``kid``.
    """

    def __init__(self, client_id: str = "sandloom-app") -> None:
        self.client_id = client_id
        self._keys: dict[str, rsa.RSAPrivateKey] = {}
        self._kid = ""
        self.jwks_fetches = 0
        self.rotate()
        issuer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/.well-known/openid-configuration":
                    body = issuer.discovery()
                elif self.path == "/jwks":
                    issuer.jwks_fetches += 1
                    body = issuer.jwks()
                else:
                    self.send_error(404)
                    return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> ExternalIssuer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    # -- keys ---------------------------------------------------------------
    def rotate(self) -> str:
        """Sign with a new key from now on; the ``kid`` of the previous one."""
        previous = self._kid
        self._kid = f"k-{uuid.uuid4().hex[:8]}"
        self._keys[self._kid] = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        return previous

    def retire(self, kid: str) -> None:
        """Drop ``kid`` from the published JWKS."""
        self._keys.pop(kid, None)

    def discovery(self) -> dict[str, Any]:
        """The OpenID provider metadata a relying party reads."""
        return {
            "issuer": self.url,
            "jwks_uri": f"{self.url}/jwks",
            "id_token_signing_alg_values_supported": ["RS256"],
            "backchannel_logout_supported": True,
        }

    def jwks(self) -> dict[str, Any]:
        """The published public keys."""
        keys = []
        for kid, key in self._keys.items():
            jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
            keys.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
        return {"keys": keys}

    # -- tokens -------------------------------------------------------------
    def _sign(
        self, claims: dict[str, Any], key: rsa.RSAPrivateKey | None = None
    ) -> str:
        return jwt.encode(
            claims,
            key or self._keys[self._kid],
            algorithm="RS256",
            headers={"kid": self._kid},
        )

    def id_token(
        self,
        subject: str,
        *,
        email: str,
        email_verified: bool = True,
        groups: list[str] | None = None,
        audience: str | None = None,
        issuer: str | None = None,
        lifetime: int = 300,
        key: rsa.RSAPrivateKey | None = None,
    ) -> str:
        """An id_token for ``subject`` (the overrides forge a bad one)."""
        now = int(time.time())
        return self._sign(
            {
                "iss": issuer or self.url,
                "sub": subject,
                "aud": audience or self.client_id,
                "iat": now,
                "exp": now + lifetime,
                "email": email,
                "email_verified": email_verified,
                "groups": groups or [],
            },
            key,
        )

    def logout_token(self, subject: str) -> str:
        """An OIDC back-channel logout token ending ``subject``'s sessions."""
        now = int(time.time())
        return self._sign(
            {
                "iss": self.url,
                "sub": subject,
                "aud": self.client_id,
                "iat": now,
                "exp": now + 120,
                "jti": uuid.uuid4().hex,
                "events": {BACKCHANNEL_LOGOUT_EVENT: {}},
            }
        )
