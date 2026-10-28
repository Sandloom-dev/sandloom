"""``sandloom contracts`` — sync-contract snapshots and compatibility checks.

    sandloom contracts snapshot                 print every loaded module's snapshot
    sandloom contracts snapshot --write         (re)write
    <module>/contracts.snapshot.json
    sandloom contracts snapshot --check         exit 1 if a committed snapshot is stale
    sandloom contracts check --against <path>   is THIS build compatible with a snapshot
                                           (file or directory)?
    sandloom contracts check --against <url>    ... with a running provider
                                           (GET <url>/_sandloom/rpc/<contract>)

``check`` takes a direction (``--as``):

* ``provider`` (default for a snapshot): this build PROVIDES the contracts and
  the snapshot is what already-deployed callers expect — the CI gate against
  a breaking change (``--against`` the snapshot of the released version,
  e.g. ``git show origin/main:src/sandloom_billing/contracts.snapshot.json``);
* ``consumer`` (default for a URL): this build CALLS the contracts and the
  target is the provider it will talk to — the pre-deploy rolling-upgrade
  check (the same comparison ``/ready`` runs at runtime).

Both build the deployment's application (``SANDLOOM_COMPOSITION_ROOT``, default
``sandloom.main``) to read the contracts its modules register; no startup task,
consumer or relay runs. Exit codes: 0 compatible, 1 incompatible / stale,
2 usage or I/O error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

import sandloom.core.composition as _composition
import sandloom.core.contract_compat as _contract_compat
from sandloom.core import rpc_binding
from sandloom.core.config.settings import settings
from sandloom.core.contract_compat import (
    compare_contract,
    load_snapshot_contracts,
    module_snapshots,
)
from sandloom.core.descriptors.module_names import module_name as _module_name
from sandloom.core.rpc.outbound import _get_auth, _outbound_mac
from sandloom.core.rpc.wire import _NONCE_HEADER, _unpack


def _build_core() -> Any:
    app = _composition.load_composition_app()
    return app.state.core


def _local_contracts(core: Any) -> dict[str, dict[str, Any]]:
    contracts: dict[str, dict[str, Any]] = {}
    for doc in module_snapshots(core).values():
        contracts.update(doc["contracts"])
    return contracts


def _cmd_snapshot(write: bool, check: bool, only: list[str]) -> int:
    core = _build_core()
    docs = _contract_compat.module_snapshots(core)
    if only:
        docs = {m: d for m, d in docs.items() if m in only or _module_name(m) in only}
    if not write and not check:
        print(json.dumps(docs, indent=2, sort_keys=True))
        return 0
    stale: list[str] = []
    for module, doc in sorted(docs.items()):
        path = _contract_compat.snapshot_path(module)
        if path is None:
            print(
                f"error: cannot locate package directory of {module}", file=sys.stderr
            )
            return 2
        text = _contract_compat.render_snapshot(doc)
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current == text:
            continue
        if check:
            stale.append(
                f"{module}: {path} is {'missing' if current is None else 'stale'}"
            )
        else:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path}")
    if stale:
        print(
            "contract snapshots out of date (run `sandloom contracts snapshot --write` "
            "and review the diff):\n  " + "\n  ".join(stale),
            file=sys.stderr,
        )
        return 1
    if check:
        print(f"contract snapshots up to date ({len(docs)} module(s))")
    return 0


def _fetch_remote(
    url: str, names: list[str], timeout: float
) -> dict[str, dict[str, Any] | None]:
    # The describe GET is MACed with the pair key of the provider the
    # contract's binding pins (SERVICE_BINDINGS[<contract>]["service_id"]).
    found: dict[str, dict[str, Any] | None] = {}
    with httpx.Client(timeout=timeout) as client:
        for name in names:
            pinned = settings.service_bindings.get(name, {}).get("service_id")
            provider = str(pinned) if pinned else None
            mac_key = _outbound_mac(settings, provider, name)
            headers, signer = _get_auth(name, settings, mac_key, provider)
            nonce = headers[_NONCE_HEADER] = rpc_binding.new_nonce()
            signer.apply(headers, nonce)
            response = client.get(
                f"{url.rstrip('/')}/_sandloom/rpc/{quote(name, safe='.')}",
                headers=headers,
            )
            if response.status_code == 404:
                found[name] = None
                continue
            response.raise_for_status()
            data = _unpack(response.content)
            contract = data.get("contract") if isinstance(data, dict) else None
            found[name] = contract if isinstance(contract, dict) else None
    return found


def _cmd_check(  # noqa: C901
    against: str,
    direction: str | None,
    services: list[str],
    timeout: float,
) -> int:
    is_url = against.startswith(("http://", "https://"))
    role = direction or ("consumer" if is_url else "provider")
    core = _build_core()
    local = _local_contracts(core)
    try:
        if is_url:
            names = services or sorted(local)
            target = _fetch_remote(against, names, timeout)
        else:
            target = dict(load_snapshot_contracts(against))
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: cannot read {against}: {exc}", file=sys.stderr)
        return 2
    names = services or sorted(set(local) | {n for n, d in target.items() if d})
    failed = False
    checked = 0
    for name in names:
        mine = local.get(name)
        theirs = target.get(name)
        if role == "provider":
            # Existing callers expect `theirs`; this build provides `mine`.
            if theirs is None:
                continue  # a new contract: nobody can depend on it yet
            report = compare_contract(name, theirs, mine)
        else:
            # This build calls `mine`'s shape; the target provides `theirs`.
            if mine is None:
                continue
            if is_url and name not in target:
                continue
            report = compare_contract(name, mine, theirs)
        checked += 1
        for warning in report.warnings:
            print(f"warning: {warning}")
        if report.errors:
            failed = True
            for error in report.errors:
                print(f"INCOMPATIBLE: {error}")
    if failed:
        print(f"contracts INCOMPATIBLE (as {role}, against {against})", file=sys.stderr)
        return 1
    print(f"contracts compatible (as {role}, against {against}; {checked} checked)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sandloom contracts",
        description="Sync-contract snapshots and rolling-upgrade compatibility",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    snap = sub.add_parser("snapshot", help="print / write / verify module snapshots")
    mode = snap.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write the snapshot files")
    mode.add_argument(
        "--check", action="store_true", help="exit 1 if a snapshot file is stale"
    )
    snap.add_argument(
        "--module", action="append", default=[], help="limit to module(s)"
    )
    chk = sub.add_parser("check", help="check compatibility against a snapshot/URL")
    chk.add_argument(
        "--against",
        required=True,
        help="snapshot file, directory of snapshots, or a provider base URL",
    )
    chk.add_argument(
        "--as",
        dest="direction",
        choices=("provider", "consumer"),
        default=None,
        help="this build's role (default: provider for a snapshot, consumer for a URL)",
    )
    chk.add_argument(
        "--service", action="append", default=[], help="limit to contract(s)"
    )
    chk.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout")
    args = parser.parse_args(argv)
    if args.action == "snapshot":
        return _cmd_snapshot(args.write, args.check, list(args.module))
    if args.action == "check":
        path_like = not args.against.startswith(("http://", "https://"))
        if path_like and not Path(args.against).exists():
            print(f"error: {args.against} does not exist", file=sys.stderr)
            return 2
        return _cmd_check(
            args.against, args.direction, list(args.service), args.timeout
        )
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
