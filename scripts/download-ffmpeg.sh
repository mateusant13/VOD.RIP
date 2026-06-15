#!/usr/bin/env bash
# Optional: download ffmpeg into build/external/ for bundling in PyInstaller builds.
# CI and local builds continue if this fails — users can install ffmpeg on PATH.
#
# F2 (ANTIVIRUS_AUDIT): Prefer BtbN/FFmpeg-Builds on GitHub Releases for
# Windows + Linux. Each release ships a single `checksums.sha256` file
# (the per-asset `<asset>.sha256` sidecars BtbN used to ship have been
# removed) so we verify the download against the publisher's checksum
# before extracting. BtbN is the most widely whitelisted ffmpeg builder
# in the AV community, which keeps `PUA:Win32/BundledTool` detections
# low.
#
# macOS stays on evermeet.cx (the historical source for this repo) because
# BtbN does not publish macOS binaries.
#
# Portability notes:
#   * Avoids `local` and `set -e`. The CI runner's Bash is a real GNU
#     Bash, but Windows dev boxes can ship a stub `bash` from the
#     Windows Store (WindowsApps\bash.exe) that mis-handles some
#     constructs. Plain global variables and explicit `if ! ... ; then`
#     guards keep this script behaving the same on every dev box and
#     every CI runner we ship to.
#   * Reads the GitHub release JSON from stdin so we don't blow past
#     the kernel's ARG_MAX (a 10-release page is ~1 MB, well over the
#     limit when passed as argv on some runners — Windows git-bash in
#     particular).
#   * BtbN's release `name` is human-readable ("Auto-Build 2026-06-14
#     13:33") and does NOT contain "win64-gpl"; that string lives on
#     the *asset* name. The original script matched against the release
#     name, which silently never matched and produced an installer
#     without ffmpeg.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/build/external"
mkdir -p "$OUT"

OS="$(uname -s)"
ARCH="$(uname -m)"

# Verify that a downloaded archive's SHA-256 matches a publisher-provided
# checksum. Returns non-zero on mismatch.
verify_sha256() {
    archive="$1"
    expected_hex="$2"
    actual="$(sha256sum "$archive" | awk '{print $1}')"
    if [ "$actual" != "$expected_hex" ]; then
        echo "::error::SHA-256 mismatch for $archive" >&2
        echo "  expected: $expected_hex" >&2
        echo "  actual:   $actual" >&2
        return 1
    fi
    echo "  sha256 ok: $archive"
}

# Pick a working Python interpreter. The CI runners we target ship
# `python3` (Linux/macOS) and the Windows Python launcher `py`, but a
# fresh Windows dev box often has `python3` shadowed by a Windows Store
# placeholder that returns a non-zero exit code and prints a help message
# instead of running. Try `python3` -> `py` -> `python` and pin whichever
# one actually launches. This matters because PyInstaller itself is
# already on the same Python, so whichever we pick is guaranteed to have
# the stdlib zipfile/tarfile modules the extractor needs.
PYTHON_BIN=""
pick_python() {
    PY_CANDIDATE=""
    for PY_CANDIDATE in python3 py python; do
        if command -v "$PY_CANDIDATE" >/dev/null 2>&1; then
            if "$PY_CANDIDATE" -c 'import sys; sys.exit(0)' >/dev/null 2>&1; then
                PYTHON_BIN="$PY_CANDIDATE"
                return 0
            fi
        fi
    done
    return 1
}
if ! pick_python; then
    echo "[ffmpeg] no working python interpreter found; skipping"
    exit 0
fi
echo "[ffmpeg] using $PYTHON_BIN"

# Python-backed archive extractor. The shell-only `unzip` / `tar` invocations
# rely on utilities that are not always available in CI runners (vanilla
# Windows git-bash has shipped without `unzip` since Git for Windows 2.45+,
# and some sandboxed runners strip them entirely). Python's stdlib is always
# available alongside `pyinstaller` and is the same interpreter CI uses to
# build the project, so it is the most portable extractor we can rely on.
py_extract() {
    archive="$1"
    dest="$2"
    if "$PYTHON_BIN" - "$archive" "$dest" <<'PYEOF'
import os, sys, tarfile, zipfile
archive, dest = sys.argv[1], sys.argv[2]
os.makedirs(dest, exist_ok=True)
if zipfile.is_zipfile(archive):
    with zipfile.ZipFile(archive) as z:
        z.extractall(dest)
elif tarfile.is_tarfile(archive):
    with tarfile.open(archive) as t:
        t.extractall(dest)
else:
    sys.exit("unsupported archive: " + archive)
PYEOF
    then
        return 0
    fi
    # Python failed — fall back to shell extractors if available.
    case "$archive" in
        *.zip)
            if ! command -v unzip >/dev/null 2>&1; then
                echo "  unzip missing; cannot fall back"
                return 1
            fi
            unzip -qo "$archive" -d "$dest"
            ;;
        *.tar.xz|*.tar.gz|*.tar.bz2)
            if ! command -v tar >/dev/null 2>&1; then
                echo "  tar missing; cannot fall back"
                return 1
            fi
            tar -xf "$archive" -C "$dest"
            ;;
        *) return 1 ;;
    esac
}

# Resolve the latest BtbN release tag (and asset name) from GitHub.
#
# Usage:  curl -fsSL <releases.json> | latest_btbn_asset <glob>
#
# The matched asset's name is returned on stdout along with the
# release tag_name (one per line, tag first) so the caller can build
# a download URL of the form
# https://github.com/BtbN/FFmpeg-Builds/releases/download/<tag>/<asset>.
latest_btbn_asset() {
    glob="$1"
    "$PYTHON_BIN" -c '
import fnmatch, json, sys
data = json.load(sys.stdin)
glob = sys.argv[1]
for release in data:
    tag = release.get("tag_name", "")
    if "rc" in tag.lower():
        continue
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if fnmatch.fnmatch(name, glob):
            print(tag)
            print(name)
            sys.exit(0)
sys.exit(1)
' "$glob"
}

# Resolve the latest BtbN release tag (and checksums URL fragment) from
# GitHub. Returns the tag on stdout, used to build both the asset URL
# and the checksums.sha256 URL.
latest_btbn_release() {
    api_response="$1"
    if ! printf '%s' "$api_response" | "$PYTHON_BIN" -c '
import json, sys
data = json.load(sys.stdin)
for release in data:
    tag = release.get("tag_name", "")
    if "rc" in tag.lower():
        continue
    if release.get("assets"):
        print(tag)
        sys.exit(0)
sys.exit(1)
' > "$OUT/_release_tag.txt"; then
        return 1
    fi
    return 0
}

download_win() {
    if ! command -v curl >/dev/null 2>&1; then
        echo "[ffmpeg] curl missing; skipping"
        return 0
    fi
    echo "[ffmpeg] resolving latest BtbN Windows release..."

    api_response=""
    if ! api_response="$(curl -fsSL -H 'Accept: application/vnd.github+json' \
        'https://api.github.com/repos/BtbN/FFmpeg-Builds/releases?per_page=10')"; then
        echo "[ffmpeg] GitHub API request failed; skipping"
        return 0
    fi

    # Pick the "mainline" GPL Windows build, NOT the -shared / -<version>
    # variant. BtbN's checksums.sha256 file lists every asset on its own
    # line in `<sha>  <filename>` form, so we look up the row that
    # matches the asset name we just downloaded.
    tag_and_name=""
    if ! tag_and_name="$(printf '%s' "$api_response" | latest_btbn_asset 'ffmpeg-N-*win64-gpl.zip')"; then
        if ! tag_and_name="$(printf '%s' "$api_response" | latest_btbn_asset 'ffmpeg-master-latest-win64-gpl.zip')"; then
            echo "[ffmpeg] could not resolve BtbN release; skipping (users can install ffmpeg on PATH)"
            return 0
        fi
    fi
    tag="$(printf '%s\n' "$tag_and_name" | sed -n '1p')"
    asset_name="$(printf '%s\n' "$tag_and_name" | sed -n '2p')"

    zip_url="https://github.com/BtbN/FFmpeg-Builds/releases/download/${tag}/${asset_name}"
    sha_url="https://github.com/BtbN/FFmpeg-Builds/releases/download/${tag}/checksums.sha256"
    zip="$OUT/ffmpeg-win.zip"
    sha_file="$OUT/ffmpeg-win-checksums.sha256"

    if ! curl -fsSL -o "$zip" "$zip_url"; then
        echo "[ffmpeg] download failed (network or 404); skipping"
        return 0
    fi
    if ! curl -fsSL -o "$sha_file" "$sha_url"; then
        echo "[ffmpeg] checksums.sha256 missing; refusing to extract (F2)"
        rm -f "$zip"
        return 0
    fi

    # The checksums file is one line per asset: "<sha>  <filename>".
    # Look up the row that ends with the asset name we just downloaded.
    expected="$(awk -v asset="$asset_name" '$2 == asset { print $1; exit }' "$sha_file")"
    if [ -z "$expected" ]; then
        echo "[ffmpeg] no checksum entry for $asset_name; skipping"
        rm -f "$zip" "$sha_file"
        return 0
    fi
    if ! verify_sha256 "$zip" "$expected"; then
        rm -f "$zip" "$sha_file"
        return 1
    fi

    extract_dir="$OUT/ffmpeg-extract"
    rm -rf "$extract_dir"
    if ! py_extract "$zip" "$extract_dir"; then
        echo "[ffmpeg] extraction failed; skipping"
        rm -f "$zip" "$sha_file"
        return 0
    fi
    find "$extract_dir" -name ffmpeg.exe -exec cp {} "$OUT/ffmpeg.exe" \;
    find "$extract_dir" -name ffprobe.exe -exec cp {} "$OUT/ffprobe.exe" \;
    rm -rf "$extract_dir" "$zip" "$sha_file"
    if [ -f "$OUT/ffmpeg.exe" ] && [ -f "$OUT/ffprobe.exe" ]; then
        echo "[ffmpeg] bundled: $OUT/ffmpeg.exe, $OUT/ffprobe.exe (BtbN $tag)"
    else
        echo "[ffmpeg] extraction did not yield ffmpeg.exe/ffprobe.exe; skipping"
        return 0
    fi
}

download_mac() {
    if ! command -v curl >/dev/null 2>&1; then
        echo "[ffmpeg] curl missing; skipping"
        return 0
    fi
    ZIP="$OUT/ffmpeg-mac.zip"
    if ! curl -fsSL -o "$ZIP" "https://evermeet.cx/ffmpeg/getrelease/zip"; then
        echo "[ffmpeg] download failed; skipping"
        return 0
    fi
    if ! py_extract "$ZIP" "$OUT"; then
        echo "[ffmpeg] extraction failed; skipping"
        rm -f "$ZIP"
        return 0
    fi
    mv -f "$OUT/ffmpeg" "$OUT/ffmpeg.bin" 2>/dev/null || true
    chmod +x "$OUT/ffmpeg.bin" 2>/dev/null || true
    rm -f "$ZIP"
    if [ -f "$OUT/ffmpeg.bin" ]; then
        echo "[ffmpeg] bundled: $OUT/ffmpeg.bin (evermeet.cx)"
    fi
}

download_linux() {
    if ! command -v curl >/dev/null 2>&1; then
        echo "[ffmpeg] curl missing; skipping"
        return 0
    fi
    BTBN_ASSET_GLOB=""
    case "$ARCH" in
        x86_64) BTBN_ASSET_GLOB='ffmpeg-N-*linux64-gpl.tar.xz' ;;
        aarch64|arm64) BTBN_ASSET_GLOB='ffmpeg-N-*linuxarm64-gpl.tar.xz' ;;
        *) echo "[ffmpeg] unsupported arch $ARCH; skipping"; return 0 ;;
    esac

    echo "[ffmpeg] resolving latest BtbN Linux ($ARCH) release..."
    api_response=""
    if ! api_response="$(curl -fsSL -H 'Accept: application/vnd.github+json' \
        'https://api.github.com/repos/BtbN/FFmpeg-Builds/releases?per_page=10')"; then
        echo "[ffmpeg] BtbN API request failed; skipping"
        return 0
    fi

    tag_and_name=""
    if ! tag_and_name="$(printf '%s' "$api_response" | latest_btbn_asset "$BTBN_ASSET_GLOB")"; then
        echo "[ffmpeg] could not resolve BtbN Linux release; skipping"
        return 0
    fi
    tag="$(printf '%s\n' "$tag_and_name" | sed -n '1p')"
    asset_name="$(printf '%s\n' "$tag_and_name" | sed -n '2p')"

    tar_url="https://github.com/BtbN/FFmpeg-Builds/releases/download/${tag}/${asset_name}"
    sha_url="https://github.com/BtbN/FFmpeg-Builds/releases/download/${tag}/checksums.sha256"
    tar="$OUT/ffmpeg-linux.tar.xz"
    sha_file="$OUT/ffmpeg-linux-checksums.sha256"

    if ! curl -fsSL -o "$tar" "$tar_url"; then
        echo "[ffmpeg] download failed; skipping"
        return 0
    fi
    if ! curl -fsSL -o "$sha_file" "$sha_url"; then
        echo "[ffmpeg] checksums.sha256 missing; refusing to extract (F2)"
        rm -f "$tar"
        return 0
    fi
    expected="$(awk -v asset="$asset_name" '$2 == asset { print $1; exit }' "$sha_file")"
    if [ -z "$expected" ]; then
        echo "[ffmpeg] no checksum entry for $asset_name; skipping"
        rm -f "$tar" "$sha_file"
        return 0
    fi
    if ! verify_sha256 "$tar" "$expected"; then
        rm -f "$tar" "$sha_file"
        return 1
    fi
    extract_dir="$OUT/ffmpeg-extract"
    rm -rf "$extract_dir"
    if ! py_extract "$tar" "$extract_dir"; then
        echo "[ffmpeg] extraction failed; skipping"
        rm -f "$tar" "$sha_file"
        return 0
    fi
    find "$extract_dir" -name ffmpeg -type f -exec cp {} "$OUT/ffmpeg" \;
    find "$extract_dir" -name ffprobe -type f -exec cp {} "$OUT/ffprobe" \;
    chmod +x "$OUT/ffmpeg" "$OUT/ffprobe" 2>/dev/null || true
    rm -rf "$extract_dir" "$tar" "$sha_file"
    if [ -f "$OUT/ffmpeg" ] && [ -f "$OUT/ffprobe" ]; then
        echo "[ffmpeg] bundled: $OUT/ffmpeg, $OUT/ffprobe (BtbN $tag)"
    else
        echo "[ffmpeg] extraction did not yield ffmpeg/ffprobe; skipping"
        return 0
    fi
}

case "$OS" in
  MINGW*|MSYS*|CYGWIN*|Windows*) download_win ;;
  Darwin) download_mac ;;
  Linux) download_linux ;;
  *) echo "[ffmpeg] unknown OS '$OS'; skipping" ;;
esac

echo "ffmpeg bundle step finished (optional)"
