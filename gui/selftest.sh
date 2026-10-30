#!/usr/bin/env bash
# gui/selftest.sh — the gui-macos12 self-test gate. Five gates, all must pass:
#
#   1. dual-target compile     gui/src builds with ZERO errors for
#                              arm64- and x86_64-apple-macosx12.0
#   2. SF Symbol availability  every symbol literal exists on macOS 12.0
#   3. render smoke (host)     every view really renders with fixture data
#                              (catches compiles-clean/crashes-at-runtime bugs,
#                              e.g. the groupedFormCompat self-recursion)
#   4. logic tests (host)      Bridge subprocess contract, liveness/stall
#                              detection, busy accounting, config retry, models
#                              -- against a sandboxed MYCLOUD_APP + fake ctl
#   5. python regression       the daemon/ctl suite stays green
#
# The test binaries are built WITHOUT MyCloudApp.swift (its @main would collide
# with the test runner's) for the HOST target, so 13+ branches run for real;
# macOS-12-only branches are covered by gate 1 plus on-hardware checks listed
# in gui/DEVLOG.md.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -d "/Applications/Xcode.app/Contents/Developer" ]; then
    export DEVELOPER_DIR="/Applications/Xcode.app/Contents/Developer"
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/mycloud-selftest.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

SRC=("$ROOT"/gui/src/*.swift)
MAIN_FREE=()
for f in "${SRC[@]}"; do
    [ "$(basename "$f")" = "MyCloudApp.swift" ] || MAIN_FREE+=("$f")
done

fail() { echo "SELFTEST: FAIL ($1)"; exit 1; }

echo "== gate 1/5: dual-target compile (macOS 12.0) =="
for T in arm64-apple-macosx12.0 x86_64-apple-macosx12.0; do
    if ! xcrun swiftc -parse-as-library -O -target "$T" \
            "${SRC[@]}" -o "$TMP/gate1-${T%%-*}" 2> "$TMP/gate1-${T%%-*}.log"; then
        sed -n '1,40p' "$TMP/gate1-${T%%-*}.log"
        fail "compile $T"
    fi
    echo "   $T: OK"
done

echo "== gate 2/5: SF Symbol availability (macOS <= 12.0) =="
/usr/bin/python3 "$ROOT/gui/tests/check_symbols.py" || fail "symbol availability"

echo "== gate 3/5: render smoke (host target) =="
if ! xcrun swiftc -parse-as-library -g \
        "${MAIN_FREE[@]}" "$ROOT/gui/tests/RenderSmokeTest.swift" \
        -o "$TMP/smoke" 2> "$TMP/smoke-build.log"; then
    sed -n '1,40p' "$TMP/smoke-build.log"
    fail "smoke build"
fi
if ! "$TMP/smoke"; then
    fail "render smoke (last RUN: line above names the crashing case)"
fi

echo "== gate 4/5: logic tests (host target) =="
if ! xcrun swiftc -parse-as-library -g \
        "${MAIN_FREE[@]}" "$ROOT/gui/tests/LogicTests.swift" \
        -o "$TMP/logic" 2> "$TMP/logic-build.log"; then
    sed -n '1,40p' "$TMP/logic-build.log"
    fail "logic tests build"
fi
if ! "$TMP/logic"; then
    fail "logic tests"
fi

echo "== gate 5/5: python regression =="
# Single run, captured: some tests print argparse noise to stdout, so show only
# the unittest summary lines; the gate is the exit code.
if ! (cd "$ROOT" && /usr/bin/python3 -m unittest discover -s tests \
        > "$TMP/pytests.log" 2>&1); then
    tail -30 "$TMP/pytests.log"
    fail "python suite"
fi
grep -E "^(Ran|OK|FAILED)" "$TMP/pytests.log" | sed 's/^/   /'

echo "SELFTEST: ALL GREEN"
