#!/usr/bin/env bash
#
# Build Kestrel.app for macOS.
#
#   ./packaging/macos/build.sh            # app bundle only
#   ./packaging/macos/build.sh --dmg      # app bundle + Kestrel-0.1.0.dmg
#   ./packaging/macos/build.sh --clean    # wipe build/ and dist/ first
#
# The bundle is ad-hoc signed, which is enough to run locally and to get past
# the "damaged app" prompt after removing the quarantine flag:
#
#   xattr -dr com.apple.quarantine /Applications/Kestrel.app
#
# Shipping to other machines without that step needs a Developer ID certificate
# and notarisation; set CODESIGN_ID and the script will use it instead.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

VERSION="$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)"
APP="dist/Kestrel.app"
DMG="dist/Kestrel-${VERSION}.dmg"
WANT_DMG=0
CLEAN=0

for arg in "$@"; do
  case "$arg" in
    --dmg) WANT_DMG=1 ;;
    --clean) CLEAN=1 ;;
    -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script builds the macOS bundle. On Windows use packaging/windows/build.ps1." >&2
  exit 1
fi

command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }

echo "==> Kestrel ${VERSION}"

# --- icon ------------------------------------------------------------------
# .icns is a container of fixed-size PNGs; sips resizes, iconutil packs.
if [[ ! -f packaging/icon.png || packaging/icon.png -ot scripts/make_icon.py ]]; then
  echo "==> generating app icon"
  uv run python scripts/make_icon.py
fi
if [[ ! -f packaging/icon.icns || packaging/icon.png -nt packaging/icon.icns ]]; then
  echo "==> generating icon.icns"
  TMP="$(mktemp -d)"
  ICONSET="$TMP/Kestrel.iconset"
  mkdir -p "$ICONSET"
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" packaging/icon.png --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z "$double" "$double" packaging/icon.png --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o packaging/icon.icns
  rm -rf "$TMP"
else
  echo "==> icon.icns up to date"
fi

# --- dependencies ----------------------------------------------------------
# PyInstaller itself is a build tool, not a runtime dependency.
echo "==> syncing dependencies"
uv sync --extra desktop --quiet
uv pip install pyinstaller --quiet

# --- build -----------------------------------------------------------------
if [[ "$CLEAN" == "1" ]]; then
  echo "==> cleaning build/ and dist/"
  rm -rf build dist
fi

echo "==> running PyInstaller (this takes a minute)"
uv run pyinstaller packaging/pyinstaller/kestrel.spec --noconfirm --log-level WARN

[[ -d "$APP" ]] || { echo "build produced no $APP" >&2; exit 1; }

# --- sign ------------------------------------------------------------------
# --deep is deprecated for real distribution but correct for an ad-hoc seal of a
# self-contained bundle like this one.
IDENTITY="${CODESIGN_ID:--}"
echo "==> signing with identity '${IDENTITY}'"
codesign --force --deep --sign "$IDENTITY" "$APP"
codesign --verify --deep --strict "$APP" && echo "    signature ok"

# --- verify ----------------------------------------------------------------
# A frozen app can start and still serve a blank page if the templates were not
# bundled, so check a rendered page rather than just the process.
echo "==> smoke test"
PORT=$(( 8300 + RANDOM % 400 ))
KESTREL_HOME="$(mktemp -d)" "$APP/Contents/MacOS/Kestrel" serve --port "$PORT" &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  if curl -fsS --noproxy '*' -m 2 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.5
done

if ! curl -fsS --noproxy '*' -m 5 "http://127.0.0.1:${PORT}/advice" | grep -q "操作建议"; then
  echo "smoke test failed: /advice did not render" >&2
  exit 1
fi
echo "    /advice renders"

kill "$SERVER_PID" 2>/dev/null || true
trap - EXIT

# --- package ---------------------------------------------------------------
if [[ "$WANT_DMG" == "1" ]]; then
  echo "==> building $DMG"
  rm -f "$DMG"

  # dmgbuild 才能把图标位置、背景图和窗口尺寸写进 .DS_Store；
  # hdiutil -srcfolder 只能得到一个空白窗口，看起来像个没做完的安装包。
  uv pip install dmgbuild --quiet
  if [[ ! -f packaging/dmg-background.png || packaging/dmg-background.png -ot scripts/make_dmg_background.py ]]; then
    echo "==> generating dmg background"
    uv run python scripts/make_dmg_background.py
  fi
  # 图标同理：脚本改了就要重新生成，否则发出去的是旧脸
  if [[ ! -f packaging/icon.png || packaging/icon.png -ot scripts/make_icon.py ]]; then
    echo "==> generating app icon"
    uv run python scripts/make_icon.py
  fi

  uv run dmgbuild -s packaging/macos/dmg_settings.py \
    -D "app=$APP" \
    -D "background=packaging/dmg-background.png" \
    -D "icon=packaging/icon.icns" \
    -D "volume_name=Kestrel" \
    "Kestrel" "$DMG"
fi

echo
echo "done: $APP  ($(du -sh "$APP" | cut -f1))"
[[ "$WANT_DMG" == "1" ]] && echo "      $DMG  ($(du -sh "$DMG" | cut -f1))"
echo
echo "Run it:      open $APP"
echo "Install it:  cp -R $APP /Applications/"
