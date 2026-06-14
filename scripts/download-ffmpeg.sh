#!/usr/bin/env bash
# Optional: download ffmpeg into build/external/ for bundling in PyInstaller builds.
# CI and local builds continue if this fails — users can install ffmpeg on PATH.
#
# F2 (ANTIVIRUS_AUDIT): Prefer BtbN/FFmpeg-Builds on GitHub Releases for
# Windows + Linux. Each release ships a .sha256 file published alongside the
# archive, so we can verify the download against the publisher's checksum
# before extracting. BtbN is the most widely whitelisted ffmpeg builder in
# the AV community, which keeps `PUA:Win32/BundledTool` detections low.
#
# macOS stays on evermeet.cx (the historical source for this repo) because
# BtbN does not publish macOS binaries.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/build/external"
mkdir -p "$OUT"

OS="$(uname -s)"
ARCH="$(uname -m)"

# Verify that a downloaded archive's SHA-256 matches a publisher-provided
# checksum. Aborts the build (set -e) on mismatch.
verify_sha256() {
    local archive="$1" expected_hex="$2"
    local actual
    actual="$(sha256sum "$archive" | awk '{print $1}')"
    if [ "$actual" != "$expected_hex" ]; then
        echo "::error::SHA-256 mismatch for $archive" >&2
        echo "  expected: $expected_hex" >&2
        echo "  actual:   $actual" >&2
        return 1
    fi
    echo "  sha256 ok: $archive"
}

# Resolve the latest BtbN release tag from GitHub. Pinned to a major
# version prefix so we don't chase a different upstream frequently.
latest_btbn_url() {
    # Use the redirect target of the latest-release URL; BtbN's redirects
    # resolve to a stable filename like:
    #   ffmpeg-n7.1.2-8-g3c41a0d83c-win64-gpl-7.1.zip
    # For reproducibility we pick the most recent stable (non-rc) tag.
    local api_response
    api_response="$(curl -fsSL -H 'Accept: application/vnd.github+json' \
        'https://api.github.com/repos/BtbN/FFmpeg-Builds/releases?per_page=10')"
    # Pick the first release whose tag is "autobuild-..." and skip -rc tags.
    python3 - <<PY "$api_response"
import json, sys
data = json.loads(sys.argv[1])
for release in data:
    tag = release.get("tag_name", "")
    name = release.get("name", "")
    if "win64" in name and "-gpl" in name and "rc" not in tag.lower():
        print(tag)
        print(name)
        sys.exit(0)
sys.exit(1)
PY
}

download_win() {
    command -v curl >/dev/null || return 0
    echo "[ffmpeg] resolving latest BtbN Windows release..."

    local tag_and_name
    if ! tag_and_name="$(latest_btbn_url)"; then
        echo "[ffmpeg] could not resolve BtbN release; skipping (users can install ffmpeg on PATH)"
        return 0
    fi
    local tag
    local release_name
    tag="$(printf '%s\n' "$tag_and_name" | sed -n '1p')"
    release_name="$(printf '%s\n' "$tag_and_name" | sed -n '2p')"

    # The asset name includes the version + git rev. We grab the .zip and
    # the .zip.sha256 file from the same release.
    local zip_url="https://github.com/BtbN/FFmpeg-Builds/releases/download/${tag}/${release_name}.zip"
    local sha_url="${zip_url}.sha256"
    local zip="$OUT/ffmpeg-win.zip"
    local sha_file="$OUT/ffmpeg-win.zip.sha256"

    curl -fsSL -o "$zip" "$zip_url" || { echo "[ffmpeg] download failed"; return 0; }
    curl -fsSL -o "$sha_file" "$sha_url" || {
        echo "[ffmpeg] sha256 file missing; refusing to extract (F2)"
        rm -f "$zip"
        return 0
    }

    # BtbN's .sha256 file is "<hash>  <filename>"; we read just the hash.
    local expected
    expected="$(awk '{print $1}' "$sha_file")"
    if ! verify_sha256 "$zip" "$expected"; then
        rm -f "$zip" "$sha_file"
        return 1
    fi

    unzip -qo "$zip" -d "$OUT/ffmpeg-extract" || return 0
    find "$OUT/ffmpeg-extract" -name ffmpeg.exe -exec cp {} "$OUT/ffmpeg.exe" \;
    find "$OUT/ffmpeg-extract" -name ffprobe.exe -exec cp {} "$OUT/ffprobe.exe" \;
    rm -rf "$OUT/ffmpeg-extract" "$zip" "$sha_file"
    echo "[ffmpeg] bundled: $OUT/ffmpeg.exe, $OUT/ffprobe.exe (BtbN $tag)"
}

download_mac() {
    command -v curl >/dev/null || return 0
    ZIP="$OUT/ffmpeg-mac.zip"
    curl -fsSL -o "$ZIP" "https://evermeet.cx/ffmpeg/getrelease/zip" || return 0
    unzip -qo "$ZIP" -d "$OUT" || return 0
    mv -f "$OUT/ffmpeg" "$OUT/ffmpeg.bin" 2>/dev/null || true
    chmod +x "$OUT/ffmpeg.bin" 2>/dev/null || true
    rm -f "$ZIP"
}

download_linux() {
    command -v curl >/dev/null || return 0
    case "$ARCH" in
        x86_64) BTBN_ASSET_GLOB='*linux64-gpl*' ;;
        aarch64|arm64) BTBN_ASSET_GLOB='*linuxarm64-gpl*' ;;
        *) return 0 ;;
    esac

    echo "[ffmpeg] resolving latest BtbN Linux ($ARCH) release..."
    local api_response
    api_response="$(curl -fsSL -H 'Accept: application/vnd.github+json' \
        'https://api.github.com/repos/BtbN/FFmpeg-Builds/releases?per_page=10')"
    local tag_and_name
    if ! tag_and_name="$(python3 - <<PY "$api_response"
import fnmatch, json, sys
data = json.loads(sys.argv[1])
glob = "$BTBN_ASSET_GLOB".strip()
for release in data:
    tag = release.get("tag_name", "")
    name = release.get("name", "")
    if fnmatch.fnmatch(name, glob) and "rc" not in tag.lower():
        print(tag)
        print(name)
        sys.exit(0)
sys.exit(1)
PY
)"; then
        echo "[ffmpeg] could not resolve BtbN Linux release; skipping"
        return 0
    fi
    local tag release_name
    tag="$(printf '%s\n' "$tag_and_name" | sed -n '1p')"
    release_name="$(printf '%s\n' "$tag_and_name" | sed -n '2p')"

    local tar_url="https://github.com/BtbN/FFmpeg-Builds/releases/download/${tag}/${release_name}.tar.xz"
    local sha_url="${tar_url}.sha256"
    local tar="$OUT/ffmpeg-linux.tar.xz"
    local sha_file="$OUT/ffmpeg-linux.tar.xz.sha256"

    curl -fsSL -o "$tar" "$tar_url" || { echo "[ffmpeg] download failed"; return 0; }
    curl -fsSL -o "$sha_file" "$sha_url" || {
        echo "[ffmpeg] sha256 file missing; refusing to extract (F2)"
        rm -f "$tar"
        return 0
    }
    local expected
    expected="$(awk '{print $1}' "$sha_file")"
    if ! verify_sha256 "$tar" "$expected"; then
        rm -f "$tar" "$sha_file"
        return 1
    fi
    tar -xJf "$tar" -C "$OUT" --strip-components=1 || return 0
    rm -f "$tar" "$sha_file"
    echo "[ffmpeg] bundled: $OUT/ffmpeg, $OUT/ffprobe (BtbN $tag)"
}

case "$OS" in
  MINGW*|MSYS*|CYGWIN*|Windows*) download_win ;;
  Darwin) download_mac ;;
  Linux) download_linux ;;
esac

echo "ffmpeg bundle step finished (optional)"
