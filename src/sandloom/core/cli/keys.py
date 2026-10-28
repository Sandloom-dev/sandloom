"""``sandloom keys`` — generate signing keys (no settings, works anywhere).

    sandloom keys generate [--alg EdDSA|RS256|ES256] [--kid KID] [--out-dir DIR]
    sandloom keys generate --purpose rpc --service ID [--kid KID] [--out-dir DIR]
    sandloom keys generate --purpose rpc-dh --service ID [--kid KID] [--out-dir DIR]
    sandloom keys allowlist --bindings ID='<SERVICE_BINDINGS JSON>' [...]
    sandloom keys dev-env [--deploy-dir DIR] [--out FILE]

Generates an access-token signing key pair for an asymmetric
``JWT_ALGORITHM``. Prints the issuer-only ``JWT_PRIVATE_KEY`` and the
``JWT_PUBLIC_KEYS`` entry every service verifies with; ``--out-dir`` writes
them as ``SECRETS_DIR`` files instead (``jwt_private_key`` mode 0600 for the
issuer, ``jwt_public_keys`` JSON for everyone) and prints only the public part.

Rotation (no forced logout): generate a new kid, add its public key to
``JWT_PUBLIC_KEYS`` everywhere (both kids verify), switch the issuer's
``JWT_PRIVATE_KEY`` + ``JWT_KID`` to it, and drop the old kid after
``ACCESS_TOKEN_EXPIRE_MINUTES`` (+ leeway). See docs/reference/security-model.md.

``--purpose rpc`` generates a service's internal-RPC keys (ADR-0004): the
X25519 key-agreement pair that authenticates every request and response
(``RPC_DH_PRIVATE_KEY`` + ``RPC_DH_KEY_ID`` and its ``RPC_SERVICE_DH_KEYS``
entry) plus a SEPARATE Ed25519 pair that signs the service's COMMAND calls
(``RPC_PRIVATE_KEY`` + ``RPC_KEY_ID`` and its ``RPC_SERVICE_KEYS`` entry).
Merge the public entries into the maps every service shares. ``allowlist``
prints ``RPC_ALLOWED_CALLS`` derived from each calling service's
``SERVICE_BINDINGS`` (every wired contract allowed, nothing else).

``dev-env`` (DEVELOPMENT / CI ONLY) prints, or writes with ``--out`` (mode
0600, never over an existing file), a compose ``--env-file`` with fresh keys
for every variable the microservices profile (``deploy/docker-compose.
microservices.yml``, written by ``sandloom add deploy``) requires: the JWT pair,
each service's RPC keys and the shared public maps, the ops token, a 2FA key
and a demo PSP. The keys sit in a plain file: production generates them with
the commands above into a secret store.

``--purpose rpc-dh`` generates ONLY the X25519 pair — a provider that calls
nothing needs no more, and the key to rotate in (append its
``RPC_SERVICE_DH_KEYS`` entry LAST, after the service holds it in
``RPC_DH_EXTRA_PRIVATE_KEYS``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import sandloom.core.testing.deploy_profile as _deploy_profile
from sandloom.core.rpc_identity import allowlist_from_bindings
from sandloom.core.signing_keys import (
    generate_private_key,
    generate_x25519_key,
    private_pem,
    public_pem,
    x25519_private_pem,
    x25519_public_pem,
)


def _default_kid(prefix: str) -> str:
    return f"{prefix}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"


def _write_key_files(out: Path, files: dict[str, str]) -> None:
    """Create a whole key set without replacing any existing file or symlink."""
    for name in files:
        if Path(name).name != name:
            raise ValueError("service id must not contain path separators")
    out.mkdir(parents=True, exist_ok=True)
    for name in files:
        if os.path.lexists(out / name):
            raise FileExistsError(
                f"{out / name} exists; use a fresh rotation directory"
            )
    created: list[tuple[Path, int, int]] = []
    try:
        for name, content in files.items():
            path = out / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                identity = os.fstat(handle.fileno())
                created.append((path, identity.st_dev, identity.st_ino))
                handle.write(content)
    except BaseException:
        for path, device, inode in reversed(created):
            try:
                identity = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            # Never remove a replacement published by a concurrent writer.
            # stat+unlink is not an atomic adversarial-filesystem primitive:
            # use a private fresh rotation directory, not an untrusted shared
            # writable directory, for key generation.
            if identity.st_dev == device and identity.st_ino == inode:
                path.unlink(missing_ok=True)
        raise


def _cmd_generate_rpc(args: argparse.Namespace) -> int:
    if not args.service:
        print("error: --purpose rpc needs --service <id>", file=sys.stderr)
        return 2
    kid = args.kid or _default_kid(args.service)
    key = generate_private_key("EdDSA")
    private = private_pem(key)
    entry = json.dumps({args.service: {kid: public_pem(key)}})
    # A SEPARATE X25519 key for response binding (never the signing key).
    dh = generate_x25519_key()
    dh_private = x25519_private_pem(dh)
    dh_entry = json.dumps({args.service: {kid: x25519_public_pem(dh)}})
    if args.out_dir is not None:
        out: Path = args.out_dir
        _write_key_files(
            out,
            {
                "rpc_private_key": private,
                "rpc_dh_private_key": dh_private,
                f"rpc_service_keys.{args.service}.json": entry,
                f"rpc_service_dh_keys.{args.service}.json": dh_entry,
            },
        )
        print(f"wrote {out / 'rpc_private_key'} ({args.service} only, 0600)")
        print(f"wrote {out / 'rpc_dh_private_key'} ({args.service} only, 0600)")
        print(f"wrote {out / f'rpc_service_keys.{args.service}.json'} (public)")
        print(f"wrote {out / f'rpc_service_dh_keys.{args.service}.json'} (public)")
        print(f"RPC_SERVICE_ID={args.service}")
        print(f"RPC_KEY_ID={kid}")
        print(f"RPC_DH_KEY_ID={kid}")
        return 0
    print(
        f"# RPC_SERVICE_ID={args.service}   RPC_KEY_ID={kid}   "
        f"RPC_DH_KEY_ID={kid}   ({args.service} only)"
    )
    print(f"# RPC_PRIVATE_KEY — {args.service} ONLY (Ed25519, signs its calls):")
    print(private, end="")
    print(
        f"# RPC_DH_PRIVATE_KEY — {args.service} ONLY (X25519, keys response "
        "binding; a pure provider needs only this one):"
    )
    print(dh_private, end="")
    print("# RPC_SERVICE_KEYS entry — merge into the map every service shares:")
    print(entry)
    print("# RPC_SERVICE_DH_KEYS entry — merge into the map every service shares:")
    print(dh_entry)
    return 0


def _cmd_generate_rpc_dh(args: argparse.Namespace) -> int:
    if not args.service:
        print("error: --purpose rpc-dh needs --service <id>", file=sys.stderr)
        return 2
    kid = args.kid or _default_kid(f"{args.service}-dh")
    dh = generate_x25519_key()
    dh_private = x25519_private_pem(dh)
    dh_entry = json.dumps({args.service: {kid: x25519_public_pem(dh)}})
    if args.out_dir is not None:
        out: Path = args.out_dir
        _write_key_files(
            out,
            {
                "rpc_dh_private_key": dh_private,
                f"rpc_service_dh_keys.{args.service}.json": dh_entry,
            },
        )
        print(f"wrote {out / 'rpc_dh_private_key'} ({args.service} only, 0600)")
        print(f"wrote {out / f'rpc_service_dh_keys.{args.service}.json'} (public)")
        print(f"RPC_SERVICE_ID={args.service}")
        print(f"RPC_DH_KEY_ID={kid}")
        return 0
    print(
        f"# RPC_SERVICE_ID={args.service}   RPC_DH_KEY_ID={kid}   ({args.service} only)"
    )
    print(
        f"# RPC_DH_PRIVATE_KEY — {args.service} ONLY (X25519; in service_mac it "
        "authenticates every call of this service — keep it like a signing key):"
    )
    print(dh_private, end="")
    print(
        "# RPC_SERVICE_DH_KEYS entry — merge into the map every service shares "
        "(a rotation: append it as the service's LAST kid):"
    )
    print(dh_entry)
    return 0


def _cmd_allowlist(args: argparse.Namespace) -> int:
    by_service: dict[str, dict[str, object]] = {}
    for item in args.bindings or []:
        service, sep, raw = item.partition("=")
        if not sep or not service:
            print(f"error: expected ID=<JSON>, got {item!r}", file=sys.stderr)
            return 2
        try:
            bindings = json.loads(raw)
        except ValueError:
            print(f"error: {service}: SERVICE_BINDINGS is not JSON", file=sys.stderr)
            return 2
        if not isinstance(bindings, dict):
            print(
                f"error: {service}: SERVICE_BINDINGS is not an object", file=sys.stderr
            )
            return 2
        by_service[service] = bindings
    print(json.dumps(allowlist_from_bindings(by_service)))
    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    if args.purpose == "rpc":
        return _cmd_generate_rpc(args)
    if args.purpose == "rpc-dh":
        return _cmd_generate_rpc_dh(args)
    algorithm = args.alg
    kid = args.kid or _default_kid("jwt")
    key = generate_private_key(algorithm)
    private = private_pem(key)
    public = public_pem(key)
    public_keys = json.dumps({kid: public})
    if args.out_dir is not None:
        out: Path = args.out_dir
        _write_key_files(
            out, {"jwt_private_key": private, "jwt_public_keys": public_keys}
        )
        print(f"wrote {out / 'jwt_private_key'} (issuer only, 0600)")
        print(f"wrote {out / 'jwt_public_keys'} (every service)")
        print(f"JWT_ALGORITHM={algorithm}")
        print(f"JWT_KID={kid}")
        return 0
    print(f"# JWT_ALGORITHM={algorithm}   (every service)")
    print(f"# JWT_KID={kid}   (issuer)")
    print("# JWT_PRIVATE_KEY  — issuer ONLY (identity); keep it in a secret store:")
    print(private, end="")
    print("# JWT_PUBLIC_KEYS — every service (merge with the kids still in use):")
    print(public_keys)
    return 0


def _cmd_dev_env(args: argparse.Namespace) -> int:
    deploy_profile = _deploy_profile
    deploy_dir: Path | None = args.deploy_dir
    if deploy_dir is None:
        here = Path.cwd()
        deploy_dir = next(
            (
                d / "deploy"
                for d in (here, *here.parents)
                if (d / "deploy" / deploy_profile.PROFILE).is_file()
            ),
            None,
        )
    profile = deploy_dir / deploy_profile.PROFILE if deploy_dir else None
    if profile is None or not profile.is_file():
        print(
            "error: no deploy/compose/microservices.yml: "
            "`sandloom add deploy <module> --service <name>` creates it",
            file=sys.stderr,
        )
        return 2
    text = profile.read_text(encoding="utf-8")
    body = deploy_profile.env_file_text(deploy_profile.dev_shell(text), text)
    if args.out is None:
        print(body, end="")
        return 0
    out: Path = args.out
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(body)
    print(f"wrote {out} (DEVELOPMENT ONLY keys, 0600)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sandloom keys", description="generate signing keys"
    )
    sub = parser.add_subparsers(dest="action", required=True)
    gen = sub.add_parser("generate", help="generate an access-token signing key pair")
    gen.add_argument(
        "--alg",
        choices=("EdDSA", "RS256", "ES256"),
        default="EdDSA",
        help="JWT algorithm (default EdDSA / Ed25519)",
    )
    gen.add_argument(
        "--purpose",
        choices=("jwt", "rpc", "rpc-dh"),
        default="jwt",
        help=(
            "jwt: access-token pair (default); rpc: a service's internal-RPC "
            "keys (X25519 + Ed25519); rpc-dh: only the X25519 key"
        ),
    )
    gen.add_argument("--service", default=None, help="service id (--purpose rpc)")
    gen.add_argument("--kid", default=None, help="key id (default <prefix>-<time>)")
    gen.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="write SECRETS_DIR files instead of printing the private key",
    )
    allow = sub.add_parser(
        "allowlist", help="derive RPC_ALLOWED_CALLS from SERVICE_BINDINGS"
    )
    allow.add_argument(
        "--bindings",
        action="append",
        metavar="ID=JSON",
        help="a calling service and its SERVICE_BINDINGS (repeatable)",
    )
    dev = sub.add_parser(
        "dev-env",
        help="DEVELOPMENT ONLY: an env file with fresh keys for the "
        "microservices profile",
    )
    dev.add_argument(
        "--deploy-dir", type=Path, default=None, help="the deploy/ directory"
    )
    dev.add_argument(
        "--out", type=Path, default=None, help="write this file (0600, new only)"
    )
    args = parser.parse_args(argv)
    if args.action == "dev-env":
        try:
            return _cmd_dev_env(args)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.action == "generate":
        try:
            return _cmd_generate(args)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.action == "allowlist":
        return _cmd_allowlist(args)
    parser.print_help(sys.stderr)  # pragma: no cover
    return 2  # pragma: no cover


__all__ = ["main"]
