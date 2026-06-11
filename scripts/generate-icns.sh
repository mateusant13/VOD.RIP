#!/usr/bin/env bash
# macOS only — build assets/icon.icns from assets/icon.png (for PyInstaller .app bundle).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PNG="$ROOT/assets/icon.png"
ICONSET="$ROOT/assets/icon.iconset"
ICNS="$ROOT/assets/icon.icns"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "generate-icns: skipped (macOS only)"
  exit 0
fi

if [[ ! -f "$PNG" ]]; then
  python "$ROOT/scripts/generate-icon.py"
fi

rm -rf "$ICONSET"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$PNG" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" "$PNG" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$ICNS"
rm -rf "$ICONSET"
echo "Wrote $ICNS"
