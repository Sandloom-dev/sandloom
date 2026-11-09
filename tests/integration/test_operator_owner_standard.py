"""The operator owner resolver against the standard batteries' map.

The standard composition root declares identity, tenancy and billing with
their public names (``Authority("identity", module="sandloom_identity")``),
so a process that does not install a battery still resolves the operator
contract it is served under: checked here in a separate process where
``sandloom_identity`` is neither installed nor importable (the last import
segment would be ``sandloom_identity``, not ``identity``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from typing import Any

import pytest
from sandloom_standard import STANDARD_CLAIM_AUTHORITIES

from sandloom.core.app_factory.claim_authorities import (
    ClaimAuthorityError,
    build_claim_authorities,
)
from sandloom.core.claims import Authority, ClaimAuthorities
from sandloom.core.cli.operator_owner import operator_owner


def _core(authorities: ClaimAuthorities) -> Any:
    return SimpleNamespace(claim_authorities=authorities, module_descriptors=[])


def _standard() -> ClaimAuthorities:
    return build_claim_authorities(STANDARD_CLAIM_AUTHORITIES)


# --- resolver --------------------------------------------------------------


def test_standard_composition_resolves_identity_and_tenancy() -> None:
    core = _core(_standard())
    sessions = operator_owner(core, "sessions", command="revoke-sessions")
    assert (sessions.key, sessions.module, sessions.path) == (
        "issuer",
        "identity",
        "sandloom_identity",
    )
    assert sessions.contract == "identity.security.v1"
    admin = operator_owner(core, "platform_admin", command="grant-admin")
    assert (admin.key, admin.contract) == ("roles", "identity.security.v1")
    members = operator_owner(core, "tenant_members", command="revoke-sessions")
    assert (members.key, members.contract) == ("tenant_ids", "tenancy.security.v1")


def test_a_declared_name_must_be_the_installed_entry_point_name() -> None:
    with pytest.raises(ClaimAuthorityError, match="installed as 'identity'"):
        build_claim_authorities(
            {"issuer": Authority("idp", module="sandloom_identity")}
        )


# --- no provider wheel: a separate process ----------------------------------

_NO_WHEEL = textwrap.dedent(
    """
    import importlib.abc, json, sys

    class _Absent(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "sandloom_identity" or name.startswith("sandloom_identity."):
                raise ImportError(name)
            return None

    sys.meta_path.insert(0, _Absent())
    from sandloom.core.descriptors import module_names
    module_names._cache = tuple(
        m for m in module_names.installed_modules() if m.path != "sandloom_identity"
    )
    from types import SimpleNamespace
    from sandloom.core.app_factory.claim_authorities import (
        build_claim_authorities, filter_to_composition,
    )
    from sandloom.core.cli.operator_owner import operator_owner
    from sandloom_standard import STANDARD_CLAIM_AUTHORITIES

    def core(authorities):
        return SimpleNamespace(claim_authorities=authorities, module_descriptors=[])

    whole = build_claim_authorities(STANDARD_CLAIM_AUTHORITIES)
    split = filter_to_composition(
        whole, loaded={"tenancy": "sandloom_tenancy"},
        bindings={"identity.claims.v1": {"service_id": "svc-identity"}},
    )
    owner = operator_owner(core(split), "sessions", command="revoke-sessions")
    try:
        operator_owner(
            core(build_claim_authorities({"issuer": "sandloom_identity"})),
            "sessions", command="revoke-sessions",
        )
        bare = None
    except SystemExit as exc:
        bare = str(exc)
    print(json.dumps({
        "module": owner.module, "path": owner.path, "contract": owner.contract,
        "issuer": split.issuer, "roles_owner": split.verified_owners["roles"],
        "kept_roles": "roles" in split.authorities,
        "imported": "sandloom_identity" in sys.modules, "bare": bare,
    }))
    """
)


def test_no_provider_wheel_resolves_the_declared_name_in_a_subprocess() -> None:
    env = {**os.environ, "SANDLOOM_COMPOSITION_ROOT": "sandloom.main"}
    out = subprocess.run(
        [sys.executable, "-c", _NO_WHEEL],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        timeout=120,
    )
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["imported"] is False
    assert (got["module"], got["path"], got["contract"]) == (
        "identity",
        "sandloom_identity",
        "identity.security.v1",
    )
    assert got["issuer"] is None
    assert got["roles_owner"] == "identity"
    assert got["kept_roles"] is True  # bound through identity.claims.v1
    assert "SANDLOOM-B039" in got["bare"]
    assert "Authority('<name>', module='sandloom_identity')" in got["bare"]
