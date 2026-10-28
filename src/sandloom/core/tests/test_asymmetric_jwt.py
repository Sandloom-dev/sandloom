"""Asymmetric access tokens (EdDSA / RS256 / ES256): issue / verify, kid
rotation, algorithm-confusion attacks, verify-only processes cannot mint, the
startup role check and ``sandloom keys generate``."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import stat
import time
import uuid
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from sandloom.core.app_factory import ConfigurationError, enforce_configuration
from sandloom.core.auth import tokens
from sandloom.core.cli import keys as keys_cli
from sandloom.core.config.settings import Settings, settings
from sandloom.core.signing_keys import (
    generate_private_key,
    load_private_key,
    private_pem,
    public_pem,
)

_STRONG = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"
_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
    "CACHE_URL": "redis://h:6379/0",
    "BROKER_URL": "redis://h:6379/1",
}

#: A configuration is refused at load (``Settings``) or at boot
#: (:func:`sandloom.core.app_factory.enforce_configuration`).
_REFUSED = (ValidationError, ConfigurationError)


def _settings(**env: Any) -> Settings:
    # JWT_SECRET / JWT_PRIVATE_KEY "" = unset, whatever the test process env
    # holds (the suite runs with the .env.test HS secret exported).
    base = {**_BASE_ENV, "JWT_SECRET": "", "JWT_PRIVATE_KEY": ""}
    cfg = Settings(_env_file=None, **{**base, **env})  # type: ignore[call-arg]
    enforce_configuration(cfg)  # the checks a process runs at boot
    return cfg


def _pair(algorithm: str) -> tuple[str, str]:
    key = generate_private_key(algorithm)
    return private_pem(key), public_pem(key)


_PAIRS: dict[str, tuple[str, str]] = {
    alg: _pair(alg) for alg in ("EdDSA", "RS256", "ES256")
}
_ED_OLD = _pair("EdDSA")


def _use_asymmetric(
    monkeypatch: pytest.MonkeyPatch,
    algorithm: str = "EdDSA",
    *,
    kid: str = "k-new",
    private: str | None = None,
    public_keys: dict[str, str] | None = None,
    issuer: bool = True,
) -> None:
    priv, pub = _PAIRS[algorithm]
    monkeypatch.setattr(settings, "jwt_algorithm", algorithm)
    monkeypatch.setattr(settings, "jwt_kid", kid)
    monkeypatch.setattr(
        settings, "jwt_private_key", (private or priv) if issuer else None
    )
    monkeypatch.setattr(
        settings, "jwt_public_keys", public_keys if public_keys else {kid: pub}
    )
    monkeypatch.setattr(settings, "jwt_previous_secrets", [])


def _claims(**extra: Any) -> dict[str, Any]:
    now = int(time.time())
    return {
        "sub": str(uuid.uuid4()),
        "token_type": "access",
        "iat": now,
        "exp": now + 600,
        **extra,
    }


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


# -- issue / verify ---------------------------------------------------------


@pytest.mark.parametrize("algorithm", ["EdDSA", "RS256", "ES256"])
def test_issue_and_verify_roundtrip(
    monkeypatch: pytest.MonkeyPatch, algorithm: str
) -> None:
    _use_asymmetric(monkeypatch, algorithm)
    token = tokens.create_access_token({"sub": "u1", "token_type": "access"})
    header = jwt.get_unverified_header(token)
    assert header["alg"] == algorithm
    assert header["kid"] == "k-new"
    payload = tokens.decode_token(token)
    assert payload is not None and payload["sub"] == "u1"


def test_settings_accept_a_consistent_issuer_and_a_verify_only_process() -> None:
    priv, pub = _PAIRS["EdDSA"]
    issuer = _settings(
        JWT_ALGORITHM="EdDSA",
        JWT_PRIVATE_KEY=priv,
        JWT_KID="k1",
        JWT_PUBLIC_KEYS=json.dumps({"k1": pub}),
        JWT_SECRET=_STRONG,
    )
    assert issuer.jwt_asymmetric
    verifier = _settings(JWT_ALGORITHM="EdDSA", JWT_PUBLIC_KEYS=json.dumps({"k1": pub}))
    assert verifier.jwt_secret is None and verifier.jwt_private_key is None


def test_escaped_newlines_in_env_pem_are_accepted() -> None:
    priv, pub = _PAIRS["ES256"]
    cfg = _settings(
        JWT_ALGORITHM="ES256",
        JWT_PRIVATE_KEY=priv.replace("\n", "\\n"),
        JWT_KID="k1",
        JWT_PUBLIC_KEYS=json.dumps({"k1": pub.replace("\n", "\\n")}),
    )
    assert cfg.jwt_private_key


# -- kid rotation ----------------------------------------------------------


def test_rotation_both_kids_verify_and_retired_kid_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_priv, old_pub = _ED_OLD
    _, new_pub = _PAIRS["EdDSA"]
    # Before the switch: the issuer signs with k-old.
    _use_asymmetric(
        monkeypatch, kid="k-old", private=old_priv, public_keys={"k-old": old_pub}
    )
    old_token = tokens.create_access_token({"sub": "u", "token_type": "access"})
    # Rotation window: both public keys published, the issuer signs with k-new.
    _use_asymmetric(
        monkeypatch, kid="k-new", public_keys={"k-old": old_pub, "k-new": new_pub}
    )
    new_token = tokens.create_access_token({"sub": "u", "token_type": "access"})
    assert jwt.get_unverified_header(new_token)["kid"] == "k-new"
    assert tokens.decode_token(old_token) is not None
    assert tokens.decode_token(new_token) is not None
    # After the window k-old is dropped: its tokens stop verifying.
    _use_asymmetric(monkeypatch, kid="k-new", public_keys={"k-new": new_pub})
    assert tokens.decode_token(old_token) is None
    assert tokens.decode_token(new_token) is not None


def test_token_without_or_with_unknown_kid_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_asymmetric(monkeypatch)
    priv, _ = _PAIRS["EdDSA"]
    key = tokens._signing_key()
    no_kid = jwt.encode(_claims(), key, algorithm="EdDSA")
    unknown = jwt.encode(_claims(), key, algorithm="EdDSA", headers={"kid": "other"})
    assert tokens.decode_token(no_kid) is None
    assert tokens.decode_token(unknown) is None
    assert priv  # the private key itself never travels


def test_kid_bound_to_a_different_key_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_priv, _ = _ED_OLD
    _use_asymmetric(monkeypatch)
    forged = jwt.encode(
        _claims(),
        load_private_key(old_priv, "EdDSA"),
        algorithm="EdDSA",
        headers={"kid": "k-new"},
    )
    assert tokens.decode_token(forged) is None


# -- algorithm confusion ---------------------------------------------------


def _hs256_with_secret(secret: bytes, kid: str = "k-new") -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode())
    body = _b64(json.dumps(_claims()).encode())
    sig = hmac.new(secret, f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(sig)}"


def test_hs256_token_signed_with_the_public_key_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The classic RS/HS confusion: HMAC keyed with the (public) PEM."""
    _use_asymmetric(monkeypatch, "RS256")
    _, pub = _PAIRS["RS256"]
    assert tokens.decode_token(_hs256_with_secret(pub.encode())) is None
    assert tokens.decode_token(_hs256_with_secret(pub.strip().encode())) is None


def test_hs256_token_signed_with_jwt_secret_is_rejected_in_asymmetric_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_asymmetric(monkeypatch)
    secret = settings.jwt_secret or _STRONG
    token = jwt.encode(_claims(), secret, algorithm="HS256", headers={"kid": "k-new"})
    assert tokens.decode_token(token) is None


def test_alg_none_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_asymmetric(monkeypatch)
    header = _b64(json.dumps({"alg": "none", "typ": "JWT", "kid": "k-new"}).encode())
    body = _b64(json.dumps(_claims()).encode())
    assert tokens.decode_token(f"{header}.{body}.") is None


def test_token_of_another_asymmetric_algorithm_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ES256-signed token is not accepted by an EdDSA deployment."""
    _use_asymmetric(monkeypatch, "EdDSA")
    es_priv, _ = _PAIRS["ES256"]
    token = jwt.encode(
        _claims(),
        load_private_key(es_priv, "ES256"),
        algorithm="ES256",
        headers={"kid": "k-new"},
    )
    assert tokens.decode_token(token) is None


@pytest.mark.parametrize(
    ("algorithm", "key_alg"),
    [("EdDSA", "RS256"), ("RS256", "EdDSA"), ("ES256", "EdDSA"), ("EdDSA", "ES256")],
)
def test_key_type_is_bound_to_the_algorithm(algorithm: str, key_alg: str) -> None:
    priv, pub = _PAIRS[key_alg]
    with pytest.raises(_REFUSED, match="requires"):
        _settings(JWT_ALGORITHM=algorithm, JWT_PUBLIC_KEYS=json.dumps({"k": pub}))
    _, good_pub = _PAIRS[algorithm]
    with pytest.raises(_REFUSED, match="requires"):
        _settings(
            JWT_ALGORITHM=algorithm,
            JWT_PRIVATE_KEY=priv,
            JWT_KID="k",
            JWT_PUBLIC_KEYS=json.dumps({"k": good_pub}),
        )


def test_short_rsa_keys_are_refused() -> None:
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)  # noqa: S505 - the weak key under test
    with pytest.raises(_REFUSED, match="2048"):
        _settings(
            JWT_ALGORITHM="RS256", JWT_PUBLIC_KEYS=json.dumps({"k": public_pem(weak)})
        )


def test_hmac_material_is_refused_with_asymmetric_and_vice_versa() -> None:
    priv, pub = _PAIRS["EdDSA"]
    with pytest.raises(_REFUSED, match="JWT_PREVIOUS_SECRETS"):
        _settings(
            JWT_ALGORITHM="EdDSA",
            JWT_PUBLIC_KEYS=json.dumps({"k": pub}),
            JWT_PREVIOUS_SECRETS=[_STRONG],
        )
    with pytest.raises(_REFUSED, match="asymmetric"):
        _settings(JWT_ALGORITHM="HS256", JWT_SECRET=_STRONG, JWT_PRIVATE_KEY=priv)
    with pytest.raises(_REFUSED, match="JWT_SECRET is required"):
        _settings(JWT_ALGORITHM="HS256")
    with pytest.raises(_REFUSED, match="JWT_PUBLIC_KEYS"):
        _settings(JWT_ALGORITHM="EdDSA")
    with pytest.raises(_REFUSED):
        _settings(JWT_ALGORITHM="none", JWT_SECRET=_STRONG)


def test_private_key_must_match_its_published_kid() -> None:
    priv, _ = _PAIRS["EdDSA"]
    _, other_pub = _ED_OLD
    with pytest.raises(_REFUSED, match="not the public half"):
        _settings(
            JWT_ALGORITHM="EdDSA",
            JWT_PRIVATE_KEY=priv,
            JWT_KID="k1",
            JWT_PUBLIC_KEYS=json.dumps({"k1": other_pub}),
        )
    with pytest.raises(_REFUSED, match="not in JWT_PUBLIC_KEYS"):
        _settings(
            JWT_ALGORITHM="EdDSA",
            JWT_PRIVATE_KEY=priv,
            JWT_KID="k2",
            JWT_PUBLIC_KEYS=json.dumps({"k1": other_pub}),
        )
    with pytest.raises(_REFUSED, match="JWT_KID is required"):
        _settings(
            JWT_ALGORITHM="EdDSA",
            JWT_PRIVATE_KEY=priv,
            JWT_PUBLIC_KEYS=json.dumps({"k1": other_pub}),
        )


# -- verify-only processes cannot mint --------------------------------------


def test_verify_only_process_cannot_mint(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_asymmetric(monkeypatch, issuer=True)
    issued = tokens.create_access_token({"sub": "u", "token_type": "access"})
    _use_asymmetric(monkeypatch, issuer=False)
    with pytest.raises(tokens.TokenIssuanceUnavailable):
        tokens.create_access_token({"sub": "u", "token_type": "access"})
    # ...but it verifies the issuer's tokens.
    assert tokens.decode_token(issued) is not None


def test_verify_only_process_may_not_hold_a_symmetric_secret() -> None:
    _, pub = _PAIRS["EdDSA"]
    with pytest.raises(_REFUSED, match="verify-only"):
        _settings(
            JWT_ALGORITHM="EdDSA",
            JWT_PUBLIC_KEYS=json.dumps({"k": pub}),
            JWT_SECRET=_STRONG,
        )


def _role_cfg(**env: Any) -> Settings:
    return _settings(**env)


def test_startup_role_check() -> None:
    priv, pub = _PAIRS["EdDSA"]
    keys = json.dumps({"k1": pub})
    issuer_cfg = _role_cfg(
        JWT_ALGORITHM="EdDSA", JWT_PRIVATE_KEY=priv, JWT_KID="k1", JWT_PUBLIC_KEYS=keys
    )
    verifier_cfg = _role_cfg(JWT_ALGORITHM="EdDSA", JWT_PUBLIC_KEYS=keys)
    problem = tokens.token_signing_problem
    assert problem(issuer_loaded=True, cfg=issuer_cfg) is None
    assert problem(issuer_loaded=False, cfg=verifier_cfg) is None
    assert "JWT_PRIVATE_KEY" in str(problem(issuer_loaded=True, cfg=verifier_cfg))
    assert "must not hold" in str(problem(issuer_loaded=False, cfg=issuer_cfg))


def test_prod_split_deployment_refuses_a_shared_hs_secret() -> None:
    env = {
        "ENVIRONMENT": "prod",
        "JWT_SECRET": _STRONG,
        "SANDLOOM_ACCEPTED_RISKS": "fake_psp_in_prod",
    }
    problem = tokens.token_signing_problem
    monolith = _role_cfg(**env)
    assert problem(issuer_loaded=True, cfg=monolith) is None
    # A service without the issuer verifies tokens it does not mint.
    assert "split" in str(problem(issuer_loaded=False, cfg=monolith))
    from sandloom.core.contract_testing import rpc_env

    bound = _role_cfg(
        **env,
        SERVICE_BINDINGS={"x.y.v1": {"base_url": "http://x", "service_id": "x"}},
        **rpc_env("caller", peers=["x"], allowed_calls={"caller": ["x.y.v1"]}),
    )
    assert "split" in str(problem(issuer_loaded=True, cfg=bound))
    opted_out = _role_cfg(
        **{**env, "SANDLOOM_ACCEPTED_RISKS": "fake_psp_in_prod,shared_hs_secret_split"}
    )
    assert problem(issuer_loaded=False, cfg=opted_out) is None
    # Outside prod the shared secret stays the default.
    assert problem(issuer_loaded=False, cfg=_role_cfg(JWT_SECRET=_STRONG)) is None


def test_app_factory_registers_a_critical_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from sandloom.core import app_factory

    _use_asymmetric(monkeypatch, issuer=True)
    added: list[tuple[Any, bool]] = []
    facade = SimpleNamespace(
        add_startup_task=lambda fn, critical=False, phase="modules": added.append(
            (fn, critical)
        )
    )
    summary = SimpleNamespace(loaded=[SimpleNamespace(issues_tokens=False)])
    app_factory._check_token_signing_role(
        facade,  # type: ignore[arg-type]
        summary,  # type: ignore[arg-type]
        app_factory.logging.getLogger("t"),
    )
    assert len(added) == 1 and added[0][1] is True
    added.clear()
    summary.loaded = [SimpleNamespace(issues_tokens=True)]
    app_factory._check_token_signing_role(
        facade,  # type: ignore[arg-type]
        summary,  # type: ignore[arg-type]
        app_factory.logging.getLogger("t"),
    )
    assert added == []


def test_identity_declares_itself_the_token_issuer() -> None:
    import inspect

    import sandloom_identity as identity_pkg

    assert "issues_tokens=True" in inspect.getsource(identity_pkg)


# -- sandloom keys generate ------------------------------------------------------


def test_keys_generate_prints_a_usable_pair(capsys: pytest.CaptureFixture[str]) -> None:
    assert keys_cli.main(["generate", "--alg", "EdDSA", "--kid", "k9"]) == 0
    out = capsys.readouterr().out
    private = out[
        # sandloom: secret-fixture — PEM marker lookup; key generated during the test.
        out.index("-----BEGIN PRIVATE KEY-----") : out.index(
            "-----END PRIVATE KEY-----"
        )
        + len("-----END PRIVATE KEY-----")
    ]
    public_keys = json.loads(out.strip().splitlines()[-1])
    cfg = _settings(
        JWT_ALGORITHM="EdDSA",
        JWT_PRIVATE_KEY=private,
        JWT_KID="k9",
        JWT_PUBLIC_KEYS=json.dumps(public_keys),
    )
    assert cfg.jwt_kid == "k9"


def test_keys_generate_writes_secrets_dir_files(tmp_path: Path) -> None:
    assert (
        keys_cli.main(["generate", "--alg", "ES256", "--out-dir", str(tmp_path)]) == 0
    )
    private = tmp_path / "jwt_private_key"
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    public_keys = json.loads((tmp_path / "jwt_public_keys").read_text())
    (kid,) = public_keys
    cfg = _settings(
        JWT_ALGORITHM="ES256",
        JWT_PRIVATE_KEY=private.read_text(),
        JWT_KID=kid,
        JWT_PUBLIC_KEYS=json.dumps(public_keys),
    )
    assert cfg.jwt_asymmetric


def test_create_app_refuses_key_material_that_fails_the_boot_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The settings layer cannot parse keys; ``create_app`` refuses to boot."""
    from sandloom.core.app_factory import create_app

    _priv, rsa_pub = _PAIRS["RS256"]
    monkeypatch.setattr(settings, "jwt_algorithm", "EdDSA")
    monkeypatch.setattr(settings, "jwt_public_keys", {"k": rsa_pub})
    monkeypatch.setattr(settings, "jwt_private_key", None)
    with pytest.raises(ConfigurationError, match=r"JWT_PUBLIC_KEYS\['k'\]"):
        create_app()
