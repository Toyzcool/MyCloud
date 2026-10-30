#!/bin/bash
# Orchestrate the end-to-end integration tests against a REAL local disk image
# standing in for the router NAS (PLAN sections 8/T13, 10).
#
# Creates a 512 MiB APFS image, attaches it at a private mount point, exports
# MC_FAKE_NAS (+ MC_FAKE_DMG so the detach/reattach scenario can run), runs the
# integration test module, and ALWAYS detaches + cleans up on exit.
#
# bash 3.2-safe (the macOS system bash); validated with `bash -n`.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

HDIUTIL="/usr/bin/hdiutil"
PYTHON="/usr/bin/python3"

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mycloud-int.XXXXXX")"
DMG_PATH="$WORK_DIR/fakenas.dmg"
MOUNT_POINT="$WORK_DIR/fakenas"

cleanup() {
    # Detach if still mounted; force if the polite detach is refused. Never fail
    # the script from cleanup.
    if [ -n "${MOUNT_POINT:-}" ]; then
        "$HDIUTIL" detach "$MOUNT_POINT" -quiet 2>/dev/null \
            || "$HDIUTIL" detach "$MOUNT_POINT" -force -quiet 2>/dev/null \
            || true
    fi
    if [ -n "${WORK_DIR:-}" ] && [ -d "$WORK_DIR" ]; then
        rm -rf "$WORK_DIR" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

echo "[integration] creating 512 MiB APFS image at $DMG_PATH"
if ! "$HDIUTIL" create -size 512m -fs APFS -volname MCTEST "$DMG_PATH" -quiet; then
    echo "[integration] FAILED to create disk image" >&2
    exit 1
fi

mkdir -p "$MOUNT_POINT"
echo "[integration] attaching image at $MOUNT_POINT"
if ! "$HDIUTIL" attach "$DMG_PATH" -mountpoint "$MOUNT_POINT" -nobrowse -quiet; then
    echo "[integration] FAILED to attach disk image" >&2
    exit 1
fi

export MC_FAKE_NAS="$MOUNT_POINT"
export MC_FAKE_DMG="$DMG_PATH"

echo "[integration] running test_integration"
cd "$ROOT_DIR" || exit 1
"$PYTHON" -m unittest tests.test_integration -v
STATUS=$?

echo "[integration] test exit status: $STATUS"
exit "$STATUS"
