# Debian runtime finding review — 2026-10-31

This reviews every one of the **68 original Debian source-package advisories**
in the Sandloom backend's `linux/arm64` image. The baseline is the image
`sandloom-backend:snyk-audit-20261031`, captured during the PR #14
audit. The source-package names are not proof that the affected executable,
optional extension or architecture implementation is present.

The complete per-CVE review is
[`tools/checks/debian-runtime-review-2026-10-31.json`](../../tools/checks/debian-runtime-review-2026-10-31.json).
Each record gives the affected function/component, observed applicability,
remediation, stable status, the upstream fix version in Debian unstable when
one exists, and its primary sources. Named verification predicates allow the
parent runtime checker to enforce component absence, architecture and actual
container controls. Conditional call-path reviews are explicitly marked manual;
the predicates never authorize scanner ignores. This is review evidence, not an ignore
policy or an acceptance of unresolved risk. The results below describe the
baseline; the hardening PR must attach its own final image inventory and scan.

## Vendor status and safe remediation boundaries

Every one of these 68 CVE records is still **open for Debian 13/trixie** in the
[Debian tracker](https://security-tracker.debian.org/tracker/data/json) snapshot
used by this review. No record supplies a fixed stable source-package version.
The JSON snapshot SHA-256 is recorded for reproducibility; every CVE's separate
primary record was also read and hashed. Some fixes exist in unstable, but
installing those binaries into a stable image is not the proposed remedy.

Fourteen newer glibc records are marked for a future stable point update.
That is useful release information, not a claim that the installed libc is
patched. For example, the stable
[resolver configuration issue](https://security-tracker.debian.org/tracker/CVE-2026-8674)
and [loader issue](https://security-tracker.debian.org/tracker/CVE-2026-86805)
still remain open.

Removing unused runtime tools can remove their actual attack surface. Keep
normal Debian package metadata and SBOM provenance so a lower finding count
reflects removal rather than hiding the inventory. `diffutils`, `ncurses-bin`,
`util-linux`, `perl-base`, `coreutils` and `dash` are **Essential** in the baseline;
force-removing them is an intentional minimal-runtime contract change that
needs operator/CLI/container compatibility tests. `apt`, `mount`, `login` and
`passwd` are not Essential. Never remove libc, libz, libsqlite or another loaded
library solely to quiet a source-package warning.

## The three high findings

### ACL: real local privileged-path precondition

[CVE-2026-54369](https://security-tracker.debian.org/tracker/CVE-2026-54369)
concerns pathname-based `libacl` operations following a replaced symlink when a
privileged caller operates over attacker-controlled paths. `libacl1` is present;
no Sandloom ACL operation was identified. Running as UID 10001 reduces the
privilege context, but does not itself remove this vulnerability or prevent
setuid executables from gaining privilege.

The stable update is deferred because the fix adds an ACL ABI and related tree
walking changes. Debian's fixed source version is `2.4.0-1` in newer distributions.
Do not copy that unstable binary into trixie. Remove the library only if no
remaining installed component needs it. Otherwise strip unused privilege bits,
set `no-new-privileges`, drop capabilities and make the root filesystem read-only.
Those are measurable mitigations, not a statement that libacl is patched.

The medium [CVE-2026-54370](https://security-tracker.debian.org/tracker/CVE-2026-54370)
concerns the recursive `getfacl`/`setfacl`/`chacl` utilities; all three are absent
from the baseline. Its utility-specific applicability differs from the high
libacl pathname finding.

### attr: affected tools absent

[CVE-2026-54371](https://security-tracker.debian.org/tracker/CVE-2026-54371)
concerns privileged recursive `getfattr` and `setfattr` operations. Neither tool
is installed. The source-package match comes from `libattr1`; it does not make
those utilities appear. The narrowly scoped assessment is
`not_affected_component_absent`, conditional on checking that the tools remain
absent in the final image. A future image that adds them must reopen the review.

### zlib: authenticated source evidence, vendor classification unresolved

[CVE-2026-85091](https://security-tracker.debian.org/tracker/CVE-2026-85091)
concerns stale-buffer handling after a stalled non-blocking gzip write.
The [introducing change](https://github.com/madler/zlib/commit/81cc0bebedd935daeb81b0b6e475d8786b51af3d)
adds the non-blocking mechanism; the
[upstream fix](https://github.com/madler/zlib/commit/df84af25dc1942490e1d1c899a07619152a46148)
changes that later retry path.

The installed Debian source is `1:1.3.dfsg+really1.3.1-1`. Through Debian's
configured, signed archive indexes the parent audit fetched its source and
binary artifacts and verified:

- The Debian patch series is empty; the source contains neither `gz_vacate`
  nor `state->again`.
- In this older writer, a failed write sets `Z_ERRNO`, and the caller returns
  before the following `memmove`. The described retry mechanism is absent.
- The installed arm64 `libz.so.1.3.1` matches the authenticated binary package.
  Its SHA-256 is
  `6fe00acadf8119ff245855e5dcc7f26c6c0ffaf4bfa2baa88feab58f23727d65`.
- Source archive, Debian packaging, `.dsc` and binary package hashes are
  recorded in the JSON. Verification is through signed APT repository metadata;
  separate `.dsc` signer verification is not claimed.

This is strong evidence for an affected-version classification discrepancy,
not a vendor-approved adjudication. Debian still labels trixie vulnerable.
The review therefore keeps **`under_investigation`** rather than silently
turning the CVE into a fixed or accepted-risk entry. Reopen on any package/source
change and obtain a Debian/Snyk classification correction or supported fix.

The separate low [CVE-2026-27171](https://security-tracker.debian.org/tracker/CVE-2026-27171)
affects CRC-combine calculations. Python 3.12 does not expose `crc32_combine`,
but the native library exists and native callers are not universally excluded.
Its status remains conditional/open.

## Remaining component findings

The JSON contains a distinct assessment for all 68 records. These groupings
explain the practical remediation without replacing that individual review.

| Source package | Findings | Component assessment and action |
|---|---:|---|
| glibc | 22 | Retain libc. Separate debug DNS printers, `wordexp`, native regex/glob, locale/configuration and loader preconditions. `nscd` is absent. The Power8 `strncasecmp` issue is not an arm64 implementation. Loader privilege findings need actual no-new-privileges/privilege-bit checks. Four older mitigation claims are disputed upstream. |
| systemd | 7 | Only shared libraries are installed. `systemd-homed`, `systemd-oomd`, journald, journalctl and the system manager are absent; the reported daemon/journal operations do not run in this image. |
| util-linux | 6 | `mount`, `nsenter` and `login` exist in the baseline but are not application features; remove unused tools or their privileged execution paths. The historical chfn/chsh CVE is a separate source/binary mismatch documented by Debian. |
| coreutils | 4 | `sort`, `uniq`, `unexpand`, recursive chown/chgrp have specific argument/path prerequisites. No framework request invokes these operations with user-selected command arguments. Tool removal needs Essential-package compatibility review. |
| perl | 4 | Archive::Tar and Pod::Text modules are absent. The interpreter and File::Temp exist; their regex/temp-file findings remain conditional until removal or a stable fix. |
| sqlite3 | 4 | Python can load SQLite and ENABLE_SESSION is built. The ZIP extension is absent; `changeset_apply_v3` is absent, but concat/changegroup APIs exist. Retain a separate open assessment for changeset merge and a provisional classification review for v3. |
| tar | 4 | Installed GNU tar is distinct from Python tarfile. Do not treat Python's `data` filter as a fix for GNU tar CLI extraction. Remove unused GNU tar or retain the restrictions/open findings. |
| acl | 2 | High library path condition remains open/mitigated; medium CLI tree-walking component is absent. |
| dash | 2 | Shell printf/glob findings need adversarial patterns/arguments. Use exec-form startup/probes; shell removal is an explicit compatibility decision. |
| gcc-14 | 2 | Source matching includes runtime libraries. The policy-based C++ heap and aligned-new paths need native-extension/caller review; compiler absence alone is not proof. |
| ncurses | 2 | tic/infocmp are present; remove unused compilers only while retaining Python readline/libtinfo dependencies. |
| shadow | 2 | OS login logging/subuid policies differ from Sandloom authentication. No newuidmap is installed, but subordinate UID mappings exist in the baseline. Remove unused account tools/mappings after creating the app user. |
| zlib | 2 | High classification is provisional despite exact source/binary evidence; CRC-combine issue remains conditional. |
| apt | 1 | apt-key absent. Remove unused package management from runtime while preserving signed verification during build. |
| attr | 1 | Affected getfattr/setfattr tools absent. |
| bzip2 | 1 | Affected bzip2recover utility absent; normal Python bz2 library use is separate. |
| diffutils | 1 | diff3 is unused backend tooling; removal needs Essential-package contract review. |
| pam | 1 | pam_userdb.so exists but Sandloom authentication does not use it. Remove unused OS login/PAM components if dependencies permit; otherwise preserve a conditional finding. |

Primary examples establishing these distinctions:

- [Debian's SQLite ZIP-extension note](https://security-tracker.debian.org/tracker/CVE-2025-70873)
  says Debian binary packages do not build the extension.
- [SQLite changeset_apply_v3 issue](https://security-tracker.debian.org/tracker/CVE-2026-50812)
  names the newer API, while the actual 3.46.1 library has no such symbol.
- [Power8-only implementation](https://security-tracker.debian.org/tracker/CVE-2026-97399)
  must not be extrapolated to arm64.
- [Debian's util-linux chfn/chsh note](https://security-tracker.debian.org/tracker/CVE-2022-0563)
  says these binaries are disabled in util-linux and provided by shadow instead.
- [systemd-homed](https://security-tracker.debian.org/tracker/CVE-2026-16742),
  [systemd-oomd](https://security-tracker.debian.org/tracker/CVE-2026-15059) and
  [journald](https://security-tracker.debian.org/tracker/CVE-2026-40228)
  require the named absent services.
- [glibc's long DNS search configuration](https://security-tracker.debian.org/tracker/CVE-2026-8674)
  affects a live resolver and remains an operator-configuration concern.

## Repeatable applicability checks

Run against the exact image under review; these checks are read-only and do
not require a service environment or network access:

```bash
docker run --rm --network none -i --entrypoint python IMAGE - <<'PY'
import ctypes, ctypes.util, json, pathlib, shutil, sqlite3, subprocess, zlib
names = ['getfattr', 'setfattr', 'getfacl', 'setfacl', 'chacl', 'apt-key',
         'bzip2recover', 'nscd', 'systemd', 'systemd-homed', 'systemd-oomd',
         'systemd-journald', 'journalctl']
assert all(shutil.which(name) is None for name in names)
for module in ['Archive::Tar', 'Pod::Text']:
    assert subprocess.run(['perl', '-M' + module, '-e', '1'],
                          capture_output=True).returncode != 0
connection = sqlite3.connect(':memory:')
assert not connection.execute(
    "SELECT name FROM pragma_module_list WHERE name='zipfile'").fetchall()
assert not any(pathlib.Path('/usr/lib').rglob('*zipfile*.so'))
library = ctypes.CDLL(ctypes.util.find_library('sqlite3'))
assert not hasattr(library, 'sqlite3changeset_apply_v3')
print(json.dumps({'sqlite': sqlite3.sqlite_version,
                  'zlib': zlib.ZLIB_RUNTIME_VERSION,
                  'python_crc32_combine': hasattr(zlib, 'crc32_combine')}))
PY
```

If the hardened image removes the Perl interpreter itself, replace the module
loads with a check that Perl and its modules are absent. Never turn a failed
applicability check into an ignore. Repeat native-library/API checks for every
published image platform and when base/package versions change; this arm64
review does not certify other architectures or user applications with extra
native modules.
