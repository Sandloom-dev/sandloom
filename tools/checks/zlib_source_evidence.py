"""Authenticate Debian zlib source/binary evidence in a disposable reference image.

Run after runtime_inventory.py. This deliberately leaves CVE-2026-85091 under
investigation while Debian's tracker still marks stable vulnerable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

PROBE = r"""
import hashlib,io,json,os,pathlib,platform,re,subprocess,sys,tarfile
for p in pathlib.Path('/etc/apt/sources.list.d').glob('*.sources'):
    p.write_text(re.sub(r'^Types: deb$', 'Types: deb deb-src', p.read_text(), flags=re.M))
subprocess.run(['apt-get','update','-qq'],check=True,stdout=sys.stderr)
root=pathlib.Path('/tmp/zlib-evidence');root.mkdir(mode=0o755);os.chdir(root)
version=subprocess.check_output(['dpkg-query','-W','-f=${Version}','zlib1g'],text=True)
subprocess.run(['apt-get','source','--download-only','zlib'],check=True,stdout=sys.stderr)
subprocess.run(['apt-get','download','zlib1g='+version],check=True,stdout=sys.stderr)
files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir() if p.is_file()}
orig=next(root.glob('*orig.tar.*')); packaging=next(root.glob('*debian.tar.*'))
with tarfile.open(orig) as archive:
    sources={m.name:archive.extractfile(m).read().decode() for m in archive.getmembers()
             if m.isfile() and m.name.endswith(('.c','.h'))}
writer=next(v for k,v in sources.items() if k.endswith('/gzwrite.c'))
with tarfile.open(packaging) as archive:
    series=archive.extractfile('debian/patches/series').read().decode()
    patch_files=[m.name for m in archive.getmembers() if m.isfile() and '/patches/' in m.name and not m.name.endswith('/series')]
patched=bool(patch_files or any(s.strip() and not s.lstrip().startswith('#') for s in series.splitlines()))
package=next(root.glob('*.deb'))
contents=subprocess.check_output(['dpkg-deb','--fsys-tarfile',str(package)])
with tarfile.open(fileobj=io.BytesIO(contents)) as archive:
    binaries={m.name:hashlib.sha256(archive.extractfile(m).read()).hexdigest()
              for m in archive.getmembers() if m.isfile() and '/libz.so.' in m.name}
record={'schema':1,'machine':platform.machine(),'binary_version':version,
        'authenticated_by':'APT Release/InRelease and package/source index checksums using the reference image Debian archive keyring; no separate dsc-signature verification claimed',
        'files_sha256':files,'source_orig_sha256':files[orig.name],
        'debian_packaging_sha256':files[packaging.name], 'binary_library_sha256':binaries,
        'debian_patches_present':patched,
        'nonblocking_mechanism_present':any('gz_vacate' in v or 'state->again' in v for v in sources.values()),
        'fatal_write_error':bool(re.search(r'if \(writ < 0\) \{\s*gz_error\(state, Z_ERRNO',writer)),
        'returns_before_memmove':bool(re.search(r'if \(gz_comp\(state, Z_NO_FLUSH\) == -1\)\s*return state->err;\s*memmove',writer)),
        'assessment':'under_investigation','cve':'CVE-2026-85091'}
print(json.dumps(record,indent=2,sort_keys=True))
"""


def verify(proof: dict, inventory: dict, expected: dict) -> dict:
    packages = {p["name"].partition(":")[0]: p for p in inventory["packages"]}
    if inventory["machine"] != proof["machine"]:
        raise ValueError("Reference and runtime architectures differ")
    if packages["zlib1g"]["version"] != proof["binary_version"]:
        raise ValueError("Reference and runtime zlib versions differ")
    hashes = {v for k, v in inventory["library_sha256"].items() if "/libz.so." in k}
    if not hashes or hashes != set(proof["binary_library_sha256"].values()):
        raise ValueError("Installed library differs from authenticated package")
    if (
        proof["source_orig_sha256"] != expected["source_orig_sha256"]
        or proof["debian_packaging_sha256"] != expected["debian_packaging_sha256"]
    ):
        raise ValueError("Source changed: review CVE applicability again")
    if (
        proof["debian_patches_present"]
        or proof["nonblocking_mechanism_present"]
        or not proof["fatal_write_error"]
        or not proof["returns_before_memmove"]
    ):
        raise ValueError("The reviewed old writer implementation no longer matches")
    proof["installed_library_matches_authenticated_package"] = True
    return proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-image", default="python:3.12.16-slim-trixie")
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument(
        "--review",
        type=Path,
        default=Path("tools/checks/debian-runtime-review-2026-10-31.json"),
    )
    args = parser.parse_args()
    if args.reference_image.startswith("-"):
        parser.error("Invalid reference image")
    runtime = json.loads(args.inventory.read_text())
    platform = {"aarch64": "linux/arm64", "x86_64": "linux/amd64"}[runtime["machine"]]
    done = subprocess.run(
        [
            "docker",
            "run",
            "--platform",
            platform,
            "--rm",
            "--user",
            "0",
            "-i",
            "--entrypoint",
            "python",
            args.reference_image,
            "-",
        ],
        input=PROBE,
        text=True,
        capture_output=True,
        check=True,
    )
    proof = verify(
        json.loads(done.stdout),
        json.loads(args.inventory.read_text()),
        json.loads(args.review.read_text())["zlib_evidence"],
    )
    print(json.dumps(proof, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
