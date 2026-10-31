"""Bind the Debian CVE review to a fresh runtime inventory and unfiltered Snyk scan.

Outputs evidence only. No Snyk ignore, VEX acceptance or patched-library claim
is generated. Unknown findings/components require a new review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path


def predicates(inventory: dict, proof: dict | None) -> dict[str, bool]:
    tools = inventory["tools"]
    absent = lambda *names: all(name in tools and tools[name] is None for name in names)
    return {
        "absent-acl-tools": absent("getfacl", "setfacl", "chacl"),
        "absent-attr-tools": absent("getfattr", "setfattr"),
        "absent-apt-key": absent("apt-key"),
        "absent-bzip2recover": absent("bzip2recover"),
        "absent-nscd": absent("nscd"),
        "absent-systemd-daemons": absent(
            "systemd",
            "systemd-homed",
            "systemd-oomd",
            "systemd-journald",
            "journalctl",
            "systemd-tmpfiles",
        )
        and not any(inventory["systemd_paths"].values()),
        "absent-perl-archive-tar": not inventory["perl_modules"]["Archive/Tar.pm"],
        "absent-perl-pod-text": not inventory["perl_modules"]["Pod/Text.pm"],
        "absent-sqlite-zipfile": "zipfile" not in inventory["sqlite_modules"]
        and not inventory["sqlite_zipfile_extensions"],
        "absent-sqlite-apply-v3": not inventory["symbols"]["sqlite3"][
            "sqlite3changeset_apply_v3"
        ],
        "not-power8": inventory["machine"] in ("aarch64", "x86_64"),
        "util-linux-no-chfn-chsh": all(
            source != "util-linux"
            for source in inventory["tool_source_packages"].values()
        ),
        "runtime-no-privilege-gain": inventory.get("boundary_checks") == "pass"
        and inventory["no_new_privileges"] == 1
        and inventory["cap_effective"] == inventory["cap_bounding"] == 0
        and not inventory["privileged_files"]
        and any(m["path"] == "/" and "ro" in m["options"] for m in inventory["mounts"]),
        "zlib-source-review": bool(
            proof
            and proof.get("assessment") == "under_investigation"
            and proof.get("installed_library_matches_authenticated_package")
            and proof["machine"] == inventory["machine"]
            and set(proof["binary_library_sha256"].values())
            == {v for k, v in inventory["library_sha256"].items() if "/libz.so." in k}
        ),
    }


def assess(review: dict, inventory: dict, scan: dict, proof: dict | None) -> dict:
    known = {row["id"] for row in review["findings"]}
    present = {row["id"] for row in scan.get("vulnerabilities", [])}
    if present - known:
        raise ValueError(
            "Unreviewed scanner findings: " + ", ".join(sorted(present - known))
        )
    checked = predicates(inventory, proof)
    if set(checked) != set(review["verification_predicates"]):
        raise ValueError("Review predicates and checker differ")
    sources = {row["source"] for row in inventory["packages"]}
    rows = []
    for finding in review["findings"]:
        assertions = {key: checked[key] for key in finding["verification_predicates"]}
        status = finding["assessment_status"]
        if finding["id"] not in present:
            status = (
                "removed_component"
                if finding["source_package"] not in sources
                else "not_reported_requires_review"
            )
        elif assertions and not all(assertions.values()):
            status = "needs_reassessment"
        rows.append(
            {
                "id": finding["id"],
                "cve": finding["cve"],
                "severity": finding["severity"],
                "scan_present": finding["id"] in present,
                "status": status,
                "verified_predicates": assertions,
                "vendor_status": finding["debian_stable"]["status"],
                "manual_review_required": finding["manual_review_required"],
                "automatic_scanner_ignore_authorized": False,
            }
        )
    return {
        "schema": 1,
        "purpose": "Evidence only; not an ignore policy or vulnerability-free certification",
        "machine": inventory["machine"],
        "baseline_advisories": len(known),
        "current_advisories": len(present),
        "ignored": 0,
        "summary": dict(Counter(row["status"] for row in rows)),
        "verification_predicates": checked,
        "findings": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--review",
        type=Path,
        default=Path("tools/checks/debian-runtime-review-2026-10-31.json"),
    )
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--scan", type=Path, required=True)
    parser.add_argument("--zlib-evidence", type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    files = {
        "review": args.review,
        "inventory": args.inventory,
        "scan": args.scan,
        "zlib_evidence": args.zlib_evidence,
    }
    values = {key: json.loads(path.read_text()) for key, path in files.items()}
    result = assess(
        values["review"], values["inventory"], values["scan"], values["zlib_evidence"]
    )
    result["input_sha256"] = {
        key: hashlib.sha256(path.read_bytes()).hexdigest()
        for key, path in files.items()
    }
    result["image"] = args.image
    result["image_id"] = subprocess.check_output(
        ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"], text=True
    ).strip()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
