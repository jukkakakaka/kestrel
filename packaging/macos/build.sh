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

# --- build environment -----------------------------------------------------
# 构建用独立环境，绝不碰开发用的 .venv。
# 早先这里是 `uv sync --extra desktop`，它会把 .venv 里的 dev 依赖（pytest、
# ruff）一并移除——打完包就再也跑不了测试，而且没有任何提示。
# UV_PROJECT_ENVIRONMENT 让下面所有 uv 命令都指向这个目录，.venv 保持原样。
BUILD_ENV="$ROOT/.build-venv"
export UV_PROJECT_ENVIRONMENT="$BUILD_ENV"
BUILD_PY="$BUILD_ENV/bin/python"
echo "==> build environment: $BUILD_ENV"

echo "==> Kestrel ${VERSION}"

# --- icon ------------------------------------------------------------------
# 所有图标都从 scripts/brand.py 一处定义生成，网页、README、Dock 看到的是同一个标。
if [[ ! -f packaging/icon.png || packaging/icon.png -ot scripts/brand.py ]]; then
  echo "==> generating brand assets"
  uv run python scripts/make_brand_assets.py
else
  echo "==> brand assets up to date"
fi

# --- dependencies ----------------------------------------------------------
# PyInstaller itself is a build tool, not a runtime dependency.
echo "==> syncing build dependencies"
uv sync --extra desktop --quiet
uv pip install --python "$BUILD_PY" pyinstaller --quiet

# --- build -----------------------------------------------------------------
if [[ "$CLEAN" == "1" ]]; then
  echo "==> cleaning build/ and dist/"
  rm -rf build dist
fi

echo "==> running PyInstaller (this takes a minute)"
uv run pyinstaller packaging/pyinstaller/kestrel.spec --noconfirm --log-level WARN

[[ -d "$APP" ]] || { echo "build produced no $APP" >&2; exit 1; }

# --- dark-mode icon --------------------------------------------------------
# macOS 13+ 才能让 Dock 图标跟随系统深浅色：把 AppIcon.appiconset 编译成
# Assets.car 放进 Resources，Info.plist 里的 CFBundleIconName 指向它。
# actool 需要完整的 Xcode；没装时回退到 .icns（只有浅色），而不是构建失败。
if [[ -d packaging/Assets.xcassets ]]; then
  echo "==> compiling icon asset catalog"
  ACTOOL="$(xcrun --find actool 2>/dev/null || true)"
  if [[ -n "$ACTOOL" ]]; then
    ASSET_TMP="$(mktemp -d)"
    if "$ACTOOL" packaging/Assets.xcassets \
        --compile "$ASSET_TMP" \
        --platform macosx \
        --minimum-deployment-target 13.0 \
        --app-icon AppIcon \
        --output-partial-info-plist "$ASSET_TMP/partial.plist" >/dev/null 2>&1; then
      cp "$ASSET_TMP/Assets.car" "$APP/Contents/Resources/Assets.car"
      echo "    Assets.car installed"
    else
      echo "    actool failed; falling back to the static .icns" >&2
    fi
    rm -rf "$ASSET_TMP"
  else
    echo "    Xcode command line tools without actool; using the static .icns"
  fi
fi

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
  uv pip install --python "$BUILD_PY" dmgbuild --quiet
  if [[ ! -f packaging/dmg-background.png || packaging/dmg-background.png -ot scripts/make_dmg_background.py ]]; then
    echo "==> generating dmg background"
    uv run python scripts/make_dmg_background.py
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
