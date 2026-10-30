#!/usr/bin/env bash
# Build dist/MyCloud.app from the SwiftUI sources in gui/src.
#
# Uses only the Command Line Tools swiftc (no Xcode project, no SPM, no
# packages). Re-runnable: rebuilds the binary, re-renders Info.plist from
# gui/Info.plist.template, re-assembles the bundle, and ad-hoc signs it.
#
#   xcrun swiftc -parse-as-library -O -target {arm64,x86_64}-apple-macosx12.0 \
#       gui/src/*.swift; lipo -create -> <tmp>/MyCloud   (universal, macOS 12+)
#
set -euo pipefail

VERSION="1.3.2"

# --- args -------------------------------------------------------------------
# --force-icon re-renders the app icon even when a cached AppIcon.icns exists.
FORCE_ICON=0
for arg in "$@"; do
	case "$arg" in
		--force-icon) FORCE_ICON=1 ;;
		*) echo "unknown argument: $arg (accepted: --force-icon)" >&2; exit 2 ;;
	esac
done

# Resolve repo root from this script's location so the build works regardless
# of the caller's working directory.
# Pin the toolchain to full Xcode explicitly. SwiftUI's @State is a macro that
# needs libSwiftUIMacros.dylib, which only ships with Xcode -- but the SYSTEM
# xcode-select must stay on the CommandLineTools: the daemon runs via the
# /usr/bin/python3 shim, and pointing xcode-select at Xcode silently swaps the
# resolved interpreter to Xcode's Python3.framework, whose missing Full Disk
# Access grant killed every backup source after the next reboot (2026-08-01
# incident). DEVELOPER_DIR scopes the Xcode toolchain to THIS build only.
if [ -d "/Applications/Xcode.app/Contents/Developer" ]; then
    export DEVELOPER_DIR="/Applications/Xcode.app/Contents/Developer"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SRC_DIR="$ROOT/gui/src"
TEMPLATE="$ROOT/gui/Info.plist.template"
DIST="$ROOT/dist"
# Assemble and SIGN outside iCloud Drive, then copy the finished bundle into
# dist/. This repo lives in iCloud Drive, and the file provider keeps re-adding
# com.apple.FinderInfo / com.apple.fileprovider.* xattrs to anything inside it --
# codesign refuses those outright ("resource fork, Finder information, or similar
# detritus not allowed"), and stripping them in place loses the race against
# iCloud (verified 2026-07-28: xattr -cr succeeded, codesign still failed).
# com.apple.provenance additionally cannot be removed at all (system-protected).
STAGE="${TMPDIR:-/tmp}/mycloud-build-$$"
APP="$STAGE/MyCloud.app"
CONTENTS="$APP/Contents"
FINAL_APP="$DIST/MyCloud.app"
MACOS_DIR="$CONTENTS/MacOS"
RES_DIR="$CONTENTS/Resources"

# Zero-dependency icon pipeline (swiftc + iconutil): the rendered .icns is
# cached here so ordinary rebuilds don't re-render (override with --force-icon).
BUILD_CACHE="$ROOT/gui/.build"
ICON_SRC="$ROOT/gui/tools/render_icon.swift"
ICNS_CACHE="$BUILD_CACHE/AppIcon.icns"

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mycloud-build.XXXXXX")"
# ONE trap for both: a second `trap ... EXIT` replaces the first, which left
# the whole signed staging bundle in $TMPDIR after every build.
cleanup() { rm -rf "$STAGE" "$TMP_DIR"; }
trap cleanup EXIT

echo "==> Compiling Swift sources (universal: arm64 + x86_64, macOS 12 target)"
# Deployment target 12.0 (gui-macos12): the gateway Mac runs Monterey, and it
# may be Intel -- build both slices and lipo them into one binary.
# shellcheck disable=SC2086
xcrun swiftc \
	-parse-as-library \
	-O \
	-target arm64-apple-macosx12.0 \
	"$SRC_DIR"/*.swift \
	-o "$TMP_DIR/MyCloud-arm64"
xcrun swiftc \
	-parse-as-library \
	-O \
	-target x86_64-apple-macosx12.0 \
	"$SRC_DIR"/*.swift \
	-o "$TMP_DIR/MyCloud-x86_64"
lipo -create "$TMP_DIR/MyCloud-arm64" "$TMP_DIR/MyCloud-x86_64" \
	-output "$TMP_DIR/MyCloud"

# --- App icon (render → iconset → iconutil → cached .icns) -------------------
mkdir -p "$BUILD_CACHE"
if [ "$FORCE_ICON" = "1" ] || [ ! -f "$ICNS_CACHE" ]; then
	echo "==> Rendering app icon"
	xcrun swiftc -O -target arm64-apple-macosx12.0 \
		"$ICON_SRC" -o "$TMP_DIR/render_icon"
	ICONSET="$TMP_DIR/AppIcon.iconset"
	"$TMP_DIR/render_icon" "$ICONSET"
	if ! /usr/bin/iconutil -c icns -o "$ICNS_CACHE" "$ICONSET"; then
		echo "ERROR: iconutil failed to build AppIcon.icns" >&2
		exit 1
	fi
else
	echo "==> Reusing cached app icon ($ICNS_CACHE)"
fi

echo "==> Assembling bundle at $APP"
rm -rf "$APP"
mkdir -p "$MACOS_DIR" "$RES_DIR"
cp "$TMP_DIR/MyCloud" "$MACOS_DIR/MyCloud"
chmod +x "$MACOS_DIR/MyCloud"

# Bundle the rendered icon (Info.plist references it via CFBundleIconFile).
cp "$ICNS_CACHE" "$RES_DIR/AppIcon.icns"

# Bundle the onboarding doc so the 未安装引导页 "打开 README" button resolves via
# Bundle.main.resourceURL on a fresh machine (openProjectDoc). PLAN.md (the
# development log) is deliberately not shipped in the app.
for doc in README.md LICENSE; do
	if [ -f "$ROOT/$doc" ]; then
		cp "$ROOT/$doc" "$RES_DIR/$doc"
	fi
done

# Bundle the BACKEND itself so the GUI installs/refreshes it without a repo
# checkout: the 引导页 installer runs `mycloudctl install --from <bundle>/backend`
# (2026-09-12: every operation must be completable from the GUI).
echo "==> Bundling backend into Resources/backend"
BACKEND_DIR="$RES_DIR/backend"
mkdir -p "$BACKEND_DIR"
cp "$ROOT/src/mycloudd.py" "$ROOT/src/mycloudctl.py" "$BACKEND_DIR/"
cp -R "$ROOT/src/mycloud" "$BACKEND_DIR/mycloud"
find "$BACKEND_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$BACKEND_DIR" -name '*.pyc' -delete
for tpl in com.mycloud.agent.plist.template com.mycloud.watchdog.plist.template \
           config.ini.template watchdog.sh; do
	cp "$ROOT/install/$tpl" "$BACKEND_DIR/$tpl"
done

# Render Info.plist from the template (only @@VERSION@@ is substituted).
sed "s/@@VERSION@@/$VERSION/g" "$TEMPLATE" > "$CONTENTS/Info.plist"

# PkgInfo is optional but conventional for APPL bundles.
printf 'APPL????' > "$CONTENTS/PkgInfo"

# Belt and braces: the staging dir is outside iCloud, but the files were COPIED
# from an iCloud-backed source tree and can carry xattrs along.
echo "==> Stripping extended attributes"
xattr -cr "$APP" 2>/dev/null || true

echo "==> Ad-hoc code signing"
codesign -s - --force --timestamp=none "$APP"

echo "==> Verifying signature"
codesign --verify --deep --strict "$APP"

# Publish into dist/. ditto --noextattr --norsrc copies without carrying xattrs
# across; iCloud may re-tag the files afterwards, which is harmless -- xattrs
# only block signing, not running an already-signed bundle.
echo "==> Publishing to $FINAL_APP"
mkdir -p "$DIST"
rm -rf "$FINAL_APP"
ditto --noextattr --norsrc "$APP" "$FINAL_APP"

# Verification of the PUBLISHED copy is best-effort and expected to fail here:
# iCloud re-tags the files within seconds, and codesign --verify rejects any
# xattr. Verified 2026-07-28 that this does NOT stop the app from launching --
# xattrs block SIGNING, not running an already-signed bundle (the signature
# itself was verified in the staging dir above, which is the meaningful check).
echo "==> Verifying published signature (best-effort)"
if codesign --verify --deep --strict "$FINAL_APP" 2>/dev/null; then
    echo "    verified"
else
    echo "    not verifiable in place (iCloud re-tagged it); the bundle is"
    echo "    correctly signed and launches fine -- see the comment above."
fi

echo "OK: $FINAL_APP"
