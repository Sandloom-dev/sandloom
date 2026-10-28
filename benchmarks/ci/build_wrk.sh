#!/usr/bin/env bash
# Build wrk or wrk2 from a PINNED commit into <out-dir>/<tool>.
#
#   benchmarks/ci/build_wrk.sh wrk  <wrk-sha>  <out-dir>
#   benchmarks/ci/build_wrk.sh wrk2 <wrk2-sha> <out-dir> <wrk-sha>
#
# Sources are fetched as the GitHub tarball OF THAT COMMIT (content-addressed
# by the SHA), so a cached binary keyed by the SHAs is always the same build.
# Needs: build-essential, libssl-dev, unzip, zlib1g-dev, curl.
#
# wrk2 bundles LuaJIT 2.0, whose x86-64 allocator needs MAP_32BIT memory and
# fails ("luajit: cannot create state: not enough memory") on some kernels /
# under emulation. We swap in the LuaJIT 2.1 that the pinned wrk commit
# bundles (deps/LuaJIT-2.1.zip) — same Lua 5.1 C API, so wrk2's code is
# unchanged; that is why the wrk SHA is part of the wrk2 build.
set -euo pipefail

tool="$1"
sha="$2"
out="$3"
wrk_sha="${4:-}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
fetch() { curl -fsSL "https://codeload.github.com/$1/tar.gz/$2" | tar -xz -C "$work"; }

make_args=()
case "$tool" in
  wrk)
    fetch wg/wrk "$sha"
    src="$work/wrk-${sha}"
    # Use the system OpenSSL instead of building the bundled 1.1.1 (minutes).
    make_args+=(WITH_OPENSSL=/usr)
    ;;
  wrk2)
    [ -n "$wrk_sha" ] || { echo "wrk2 needs the wrk SHA (LuaJIT 2.1)" >&2; exit 2; }
    fetch giltene/wrk2 "$sha"
    fetch wg/wrk "$wrk_sha"
    src="$work/wrk2-${sha}"
    rm -rf "$src/deps/luajit"
    unzip -q "$work/wrk-${wrk_sha}/deps/LuaJIT-2.1.zip" -d "$work/luajit21"
    mv "$work"/luajit21/LuaJIT-* "$src/deps/luajit"
    # wrk2 predates GCC 10's -fno-common default (tentative definitions shared
    # across its translation units); keep the old linkage semantics.
    # LuaJIT 2.1 dropped the 5.0-era `luaL_reg` alias wrk2 still uses.
    sed -i.orig 's/^CFLAGS  := /CFLAGS  := -fcommon -DluaL_reg=luaL_Reg /' "$src/Makefile"
    ;;
  *)
    echo "unknown tool: $tool" >&2
    exit 2
    ;;
esac

make -C "$src" -j"$(nproc)" "${make_args[@]}" >"$work/build.log" 2>&1 || {
  tail -60 "$work/build.log" >&2
  exit 1
}
mkdir -p "$out"
install -m 0755 "$src/wrk" "$out/$tool"
echo "built $tool @ $sha -> $out/$tool"
