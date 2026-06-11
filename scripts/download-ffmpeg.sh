#!/usr/bin/env bash
# Optional: download ffmpeg into build/external/ for bundling in PyInstaller builds.
# CI and local builds continue if this fails — users can install ffmpeg on PATH.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/build/external"
mkdir -p "$OUT"

OS="$(uname -s)"
ARCH="$(uname -m)"

download_win() {
  command -v curl >/dev/null || return 0
  ZIP="$OUT/ffmpeg-win.zip"
  curl -fsSL -o "$ZIP" "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" || return 0
  unzip -qo "$ZIP" -d "$OUT/ffmpeg-extract" || return 0
  find "$OUT/ffmpeg-extract" -name ffmpeg.exe -exec cp {} "$OUT/ffmpeg.exe" \;
  find "$OUT/ffmpeg-extract" -name ffprobe.exe -exec cp {} "$OUT/ffprobe.exe" \;
  rm -rf "$OUT/ffmpeg-extract" "$ZIP"
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
    x86_64) ARCHIVE="ffmpeg-release-amd64-static.tar.xz" ;;
    aarch64|arm64) ARCHIVE="ffmpeg-release-arm64-static.tar.xz" ;;
    *) return 0 ;;
  esac
  TAR="$OUT/ffmpeg-linux.tar.xz"
  curl -fsSL -o "$TAR" "https://johnvansickle.com/ffmpeg/releases/${ARCHIVE}" || return 0
  tar -xJf "$TAR" -C "$OUT" --strip-components=1 || return 0
  rm -f "$TAR"
}

case "$OS" in
  MINGW*|MSYS*|CYGWIN*|Windows*) download_win ;;
  Darwin) download_mac ;;
  Linux) download_linux ;;
esac

echo "ffmpeg bundle step finished (optional)"
