"""Access-token issuance and verification (JWT): minting, key selection
during a rotation, the signing-role check and the verified-token cache."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import jwt
import orjson

from sandloom.core.auth.marks import (
    IAT_US_CLAIM,
    _datetime_us,
    _issue_instant_us,
    _seconds_to_us,
)
from sandloom.core.config import accepted_risks
from sandloom.core.config.common import strict_profile_active
from sandloom.core.config.settings import settings
from sandloom.core.signing_keys import (
    KeyConfigError,
    load_private_key,
    load_public_key,
    same_public_key,
)


def create_access_token(data: dict[str, Any], expires_delta: int | None = None) -> str:
    """Mint a JWT.

    Adds the standard registered claims every token should carry: ``iat``
    (issued-at), ``nbf`` (not-before) and a unique ``jti`` (token id, the hook a
    future revocation/blocklist story keys on), plus ``iat_us`` — the issue
    instant in microseconds that revocation compares against the subject's
    mark. ``iat_us`` is never earlier than a revocation mark this context has
    seen for the subject (the issuance check :func:`ensure_user_not_blocked`
    or a revocation scheduled earlier in the same request), so a token minted
    after a revocation is never caught by its clock-skew margin.
    ``iss``/``aud`` are added only when configured (``JWT_ISSUER`` /
    ``JWT_AUDIENCE``). Caller-supplied claims win (``setdefault``), so a
    refresh-token mint or an explicit ``token_type`` is preserved. When
    ``JWT_KID`` is configured it is stamped as the ``kid`` header so verifiers
    can pick the key directly during a rotation.
    """
    to_encode = data.copy()
    now = datetime.now(UTC)
    minutes = (
        settings.access_token_expire_minutes if expires_delta is None else expires_delta
    )
    expire = now + timedelta(minutes=minutes)
    to_encode["exp"] = expire
    if IAT_US_CLAIM not in to_encode:
        supplied_iat = to_encode.get("iat")
        if supplied_iat is None:
            to_encode[IAT_US_CLAIM] = _issue_instant_us(to_encode.get("sub"), now)
        elif isinstance(supplied_iat, datetime):
            to_encode[IAT_US_CLAIM] = _datetime_us(supplied_iat)
        else:  # an explicit numeric ``iat`` (seconds)
            to_encode[IAT_US_CLAIM] = _seconds_to_us(float(supplied_iat))
    to_encode.setdefault("iat", now)
    to_encode.setdefault("nbf", now)
    to_encode.setdefault("jti", uuid.uuid4().hex)
    if settings.jwt_issuer:
        to_encode.setdefault("iss", settings.jwt_issuer)
    if settings.jwt_audience:
        to_encode.setdefault("aud", settings.jwt_audience)
    headers = {"kid": settings.jwt_kid} if settings.jwt_kid else None
    encoded_jwt = jwt.encode(
        to_encode,
        cast(Any, _signing_key()),
        algorithm=settings.jwt_algorithm,
        headers=headers,
    )
    return str(encoded_jwt)


class TokenIssuanceUnavailable(RuntimeError):
    """This process cannot mint access tokens (asymmetric, verify-only).

    With an asymmetric ``JWT_ALGORITHM`` only the issuer holds
    ``JWT_PRIVATE_KEY``; every other service verifies with the public keys and
    has nothing to sign with — by design (a compromised verifier cannot mint).
    """


def _signing_key() -> Any:
    """The key :func:`create_access_token` signs with.

    HS*: ``JWT_SECRET``. Asymmetric: the parsed ``JWT_PRIVATE_KEY`` (its type
    checked against the algorithm at boot, :func:`jwt_key_material_problem`).
    """
    if not settings.jwt_asymmetric:
        return settings.jwt_secret
    if not settings.jwt_private_key:
        raise TokenIssuanceUnavailable(
            f"JWT_ALGORITHM={settings.jwt_algorithm} and no JWT_PRIVATE_KEY: this "
            "process is verify-only and cannot mint access tokens"
        )
    return load_private_key(settings.jwt_private_key, settings.jwt_algorithm)


def token_signing_problem(*, issuer_loaded: bool, cfg: Any = None) -> str | None:
    """Why this process's JWT key material does not fit its role, or ``None``.

    Checked at startup (a CRITICAL task: the process refuses to serve) once the
    modules are loaded, so the kernel knows whether this process hosts the
    token issuer (a module declared ``issues_tokens=True``):

    * asymmetric JWT_ALGORITHM, issuer loaded, no JWT_PRIVATE_KEY -> it
      could not mint;
    * asymmetric, NO issuer loaded, but JWT_PRIVATE_KEY (or JWT_SECRET) held
      -> a verify-only service would be able to mint (the point of
      asymmetric keys is that it cannot);
    * production, HS* (shared secret) and a SPLIT process — one that verifies
      tokens it does not issue, or binds remote services — unless
      the risk ``shared_hs_secret_split`` is accepted: every service holding the secret
      could mint a token for any user.
    """
    s = cfg if cfg is not None else settings
    if s.jwt_asymmetric:
        if issuer_loaded and not s.jwt_private_key:
            return (
                f"JWT_ALGORITHM={s.jwt_algorithm}: this process loads the token "
                "issuer but has no JWT_PRIVATE_KEY (+ JWT_KID) to sign with"
            )
        if not issuer_loaded and s.jwt_private_key:
            return (
                f"JWT_ALGORITHM={s.jwt_algorithm}: this process loads no token "
                "issuer, so it must not hold JWT_PRIVATE_KEY (verify-only services "
                "get JWT_PUBLIC_KEYS only)"
            )
        if not issuer_loaded and s.jwt_secret:
            return (
                f"JWT_ALGORITHM={s.jwt_algorithm}: a verify-only process must not "
                "hold JWT_SECRET"
            )
        return None
    split = (not issuer_loaded) or bool(s.service_bindings)
    if s.is_prod and split and strict_profile_active(s):
        return (
            f"JWT_ALGORITHM={s.jwt_algorithm} (shared secret) in a split process "
            "under PRODUCTION_PROFILE=multi_tenant_strict: asymmetric JWT is "
            "required (JWT_PRIVATE_KEY on the issuer only, JWT_PUBLIC_KEYS "
            "everywhere); the accepted risk shared_hs_secret_split does not apply"
        )
    if s.is_prod and split and not accepted_risks.accepts(s, "shared_hs_secret_split"):
        return (
            f"JWT_ALGORITHM={s.jwt_algorithm} (shared secret) in a split "
            "production deployment: every service holding JWT_SECRET can mint "
            "tokens for any user. Switch to an asymmetric algorithm (EdDSA: "
            "`sandloom keys generate`, JWT_PRIVATE_KEY on the issuer only, "
            "JWT_PUBLIC_KEYS everywhere) or accept the risk explicitly: "
            'SANDLOOM_ACCEPTED_RISKS=["shared_hs_secret_split"]'
        )
    return None


def jwt_key_material_problem(cfg: Any = None) -> str | None:
    """Why the configured asymmetric JWT keys are unusable, or ``None``.

    The settings layer checks the key SETTINGS (algorithm family, kids, the
    issuer's ``JWT_KID``); this parses the material: every
    ``JWT_PUBLIC_KEYS`` entry and ``JWT_PRIVATE_KEY`` must be a key of
    ``JWT_ALGORITHM``'s type, and the private key must be the public half
    named by ``JWT_KID``. Run at startup
    (:func:`sandloom.core.app_factory.enforce_configuration`).
    """
    s = cfg if cfg is not None else settings
    if not s.jwt_asymmetric:
        return None
    public: dict[str, Any] = {}
    for kid, pem in s.jwt_public_keys.items():
        try:
            public[kid] = load_public_key(pem, s.jwt_algorithm)
        except KeyConfigError as exc:
            return f"JWT_PUBLIC_KEYS[{kid!r}]: {exc}"
    if not s.jwt_private_key:
        return None
    try:
        private = load_private_key(s.jwt_private_key, s.jwt_algorithm)
    except KeyConfigError as exc:
        return f"JWT_PRIVATE_KEY: {exc}"
    if s.jwt_kid in public and not same_public_key(private, public[s.jwt_kid]):
        return (
            f"JWT_PUBLIC_KEYS[{s.jwt_kid!r}] is not the public half of JWT_PRIVATE_KEY"
        )
    return None


def _asymmetric_verification_key(token: str) -> Any | None:
    """The public key named by ``token``'s ``kid`` header, or ``None``.

    Asymmetric mode requires the ``kid``: there is exactly one candidate key
    per token (no trial verification), and an unknown kid is a rejection.
    """
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        return None
    if not isinstance(kid, str):
        return None
    pem = settings.jwt_public_keys.get(kid)
    if pem is None:
        return None
    return load_public_key(pem, settings.jwt_algorithm)


def _verification_keys(token: str) -> list[Any]:
    """Secrets to try for ``token``: by ``kid`` when it names the current key.

    A token stamped with the current ``JWT_KID`` verifies against
    ``JWT_SECRET`` only. Anything else (no ``kid``, or a retired one) is tried
    against the current secret, then each of ``JWT_PREVIOUS_SECRETS`` — so a
    rotation never strands tokens minted under the previous key.
    """
    current = str(settings.jwt_secret)
    previous = [s for s in settings.jwt_previous_secrets if s]
    if not previous:
        # No rotation in progress: every token verifies against the current
        # secret whatever its ``kid`` — skip parsing the header.
        return [current]
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        return [current]
    if settings.jwt_kid and kid == settings.jwt_kid:
        return [current]
    return [current, *previous]


#: Verified-token cache bound: the newest tokens this process verified.
VERIFIED_TOKEN_CACHE_SIZE = 2048
#: token -> (verification settings, claims as JSON, iat, nbf | None, exp):
#: ONLY tokens whose signature and claims verified. A hit re-checks the time
#: claims exactly as PyJWT does; everything else about a token is immutable.
#: Claims are kept serialized so every hit returns fresh objects.
_verified_tokens: dict[str, tuple[tuple[Any, ...], bytes, int, Any, int]] = {}


def _verification_settings() -> tuple[Any, ...]:
    """Every setting a verification verdict depends on (compared on each hit)."""
    s = settings
    return (
        s.jwt_algorithm,
        s.jwt_secret,
        tuple(s.jwt_previous_secrets),
        s.jwt_kid,
        s.jwt_public_keys,
        s.jwt_audience,
        s.jwt_issuer,
        s.jwt_leeway_seconds,
    )


def _remember_verified(token: str, config: tuple[Any, ...], claims: Any) -> None:
    try:
        iat = int(claims["iat"])
        exp = int(claims["exp"])
        nbf = int(claims["nbf"]) if claims.get("nbf") is not None else None
        raw = orjson.dumps(claims)
    except (KeyError, TypeError, ValueError, OverflowError, orjson.JSONEncodeError):
        return
    if len(_verified_tokens) >= VERIFIED_TOKEN_CACHE_SIZE:
        _verified_tokens.pop(next(iter(_verified_tokens)), None)
    _verified_tokens[token] = (config, raw, iat, nbf, exp)


def decode_token(token: str) -> dict[str, Any] | None:
    """Decode and verify a JWT, returning its claims or ``None`` on any failure.

    A client sends the same access token on every request until it expires,
    and PyJWT spends ~35 µs per verification (per-character base64 checks,
    HMAC key screening). A token this process already verified under the
    SAME verification settings is answered from a bounded cache: its
    signature, ``aud`` / ``iss`` and required claims cannot change, and the
    time claims are re-checked on every hit exactly as PyJWT checks them
    (``iat`` / ``nbf`` not in the future, ``exp`` not past, with the leeway).
    Revocation and the emergency block are separate (marks), never cached
    here.
    """
    config = _verification_settings()
    cached = _verified_tokens.get(token)
    if cached is not None and cached[0] == config:
        _config, raw, iat, nbf, exp = cached
        now = time.time()
        leeway = settings.jwt_leeway_seconds
        if iat > now + leeway or (nbf is not None and nbf > now + leeway):
            return None
        if exp <= now - leeway:
            _verified_tokens.pop(token, None)
            return None
        return cast(dict[str, Any], orjson.loads(raw))
    claims = _decode_token_uncached(token)
    if claims is not None:
        _remember_verified(token, config, claims)
    return claims


def _decode_token_uncached(token: str) -> dict[str, Any] | None:
    """Decode and verify a JWT, returning its claims or ``None`` on any failure.

    Verifies the signature plus the time-based claims (``exp``/``nbf``/``iat``)
    with a small configurable leeway for clock skew. ``exp`` and ``iat`` are
    *required*: a token minted (by any process sharing the secret) without an
    expiry must not verify forever, and one without an issued-at cannot be
    checked against a revocation mark, so their absence is a hard failure
    rather than the default "skip the check when the claim is missing". During
    a key rotation the signing key is selected by the ``kid`` header (see
    :func:`_verification_keys`). ``iss``/``aud`` are enforced only when
    configured (so single-app deployments that mint no issuer/audience are
    unaffected); when unset, audience verification is explicitly disabled so a
    token that happens to carry an ``aud`` does not trip PyJWT's default check.
    """
    options: dict[str, Any] = {
        "require": ["exp", "iat"],
        "verify_exp": True,
    }
    kwargs: dict[str, Any] = {}
    if settings.jwt_audience:
        kwargs["audience"] = settings.jwt_audience
    else:
        options["verify_aud"] = False
    if settings.jwt_issuer:
        kwargs["issuer"] = settings.jwt_issuer
    keys: list[Any]
    if settings.jwt_asymmetric:
        # Only the configured asymmetric algorithm is accepted (never HS* /
        # ``none``), with the one public key the token's kid names.
        public_key = _asymmetric_verification_key(token)
        if public_key is None:
            return None
        keys = [public_key]
    else:
        keys = _verification_keys(token)
    for idx, key in enumerate(keys):
        try:
            payload = jwt.decode(
                token,
                key,
                algorithms=[settings.jwt_algorithm],
                leeway=settings.jwt_leeway_seconds,
                # PyJWT types ``options`` as a closed TypedDict; our keys are
                # valid members but the dict is built dynamically, so cast.
                options=cast(Any, options),
                **kwargs,
            )
            # jwt.decode returns a dict; explicit copy keeps mypy --strict happy.
            return dict(payload) if payload else None
        except jwt.InvalidSignatureError:
            # Wrong key: try the next (previous) secret, if any.
            if idx == len(keys) - 1:
                return None
        except (jwt.PyJWTError, ValueError, TypeError):
            # PyJWTError covers InvalidTokenError AND InvalidKeyError /
            # InvalidAlgorithmError (e.g. a key unusable for the algorithm) —
            # any undecodable token is a 401 at the guard, never a 500.
            return None
    return None
