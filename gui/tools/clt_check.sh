#!/usr/bin/env bash
# gui/tools/clt_check.sh — GUI checks with ONLY the Command Line Tools.
#
# gui/selftest.sh needs full Xcode: in current SDKs SwiftUI's @State is backed
# by a compiler plugin (libSwiftUIMacros) that ships only with Xcode. When Xcode
# is missing or unusable (e.g. its license has not been accepted), this script
# still runs the meaningful gates by compiling a COPY of gui/src in which
# `@State` is replaced with StateShim -- a property wrapper that wraps a real
# SwiftUI.State, so views keep working at runtime:
#
#   1. type-check gui/src for arm64- and x86_64-apple-macosx12.0
#   2. SF Symbol availability gate (gui/tests/check_symbols.py)
#   3. render smoke   (gui/tests/RenderSmokeTest.swift, host target)
#   4. logic tests    (gui/tests/LogicTests.swift, host target)
#
# It never builds the app: shipping a shimmed binary is not the goal -- use
# build_app.sh (Xcode) for that. The Python suite is gui/selftest.sh's job.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/mycloud-clt.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# Force the CLT toolchain even if xcode-select or DEVELOPER_DIR points at Xcode.
export DEVELOPER_DIR=/Library/Developer/CommandLineTools

fail() { echo "CLT-CHECK: FAIL ($1)"; exit 1; }

shim() {
    /usr/bin/sed -E 's/@State([[:space:](])/@StateShim\1/g' "$1" > "$2/$(basename "$1")"
}

mkdir -p "$TMP/src"
for f in "$ROOT"/gui/src/*.swift; do shim "$f" "$TMP/src"; done
cat > "$TMP/StateShim.swift" <<'EOF'
import SwiftUI
@propertyWrapper struct StateShim<Value>: DynamicProperty {
    private var state: State<Value>
    init(wrappedValue: Value) { state = State(wrappedValue: wrappedValue) }
    var wrappedValue: Value {
        get { state.wrappedValue }
        nonmutating set { state.wrappedValue = newValue }
    }
    var projectedValue: Binding<Value> { state.projectedValue }
}
EOF

echo "== 1/4: type-check (macOS 12.0, both architectures) =="
for T in arm64-apple-macosx12.0 x86_64-apple-macosx12.0; do
    if ! xcrun swiftc -typecheck -parse-as-library -target "$T" \
            "$TMP"/src/*.swift "$TMP/StateShim.swift" 2> "$TMP/tc.log"; then
        grep "error:" "$TMP/tc.log" | sed "s|$TMP/src/||" | head -30
        fail "type-check $T"
    fi
    echo "   $T: OK"
done

echo "== 2/4: SF Symbol availability =="
/usr/bin/python3 "$ROOT/gui/tests/check_symbols.py" || fail "symbols"

run_test() {
    local name="$1"
    mkdir -p "$TMP/$name"
    shim "$ROOT/gui/tests/$name.swift" "$TMP/$name"
    local main_free=()
    for f in "$TMP"/src/*.swift; do
        [ "$(basename "$f")" = "MyCloudApp.swift" ] || main_free+=("$f")
    done
    if ! xcrun swiftc -parse-as-library -g "${main_free[@]}" "$TMP/StateShim.swift" \
            "$TMP/$name/$name.swift" -o "$TMP/$name/runner" 2> "$TMP/$name/build.log"; then
        grep "error" "$TMP/$name/build.log" | head -30
        echo "(build failed)"
        return 1
    fi
    "$TMP/$name/runner"
}

echo "== 3/4: render smoke (host) =="
if ! run_test RenderSmokeTest > "$TMP/smoke.out" 2>&1; then
    tail -30 "$TMP/smoke.out"
    fail "render smoke (the last RUN: line names the crashing case)"
fi
tail -1 "$TMP/smoke.out"

echo "== 4/4: logic tests (host) =="
if ! run_test LogicTests > "$TMP/logic.out" 2>&1; then
    grep -E "FAIL|LOGIC-TESTS|build failed|error" "$TMP/logic.out" | tail -30
    fail "logic tests"
fi
tail -1 "$TMP/logic.out"

echo "CLT-CHECK: ALL GREEN (the app itself still needs build_app.sh with Xcode)"
