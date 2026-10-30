#!/bin/bash
# install/selftest.sh — post-install smoke test against the REAL NAS.
#
# Run this yourself AFTER installing MyCloud, once the share is mounted. It
# verifies the handful of behaviors the daemon depends on but that only the real
# router SMB firmware can confirm: the share is mounted, the install sentinel is
# present, a Chinese-plus-spaces filename round-trips byte-for-byte, a file can
# be renamed into a freshly created nested directory tree (the trash-move
# mechanism relies on rename-into-new-tree), and the NAS heartbeat is fresh.
#
# Usage:   bash install/selftest.sh [MOUNTPOINT]
# Default MOUNTPOINT: ~/Library/Application Support/MyCloud/mnt
#
# Prints PASS/FAIL per check and exits non-zero if any check failed -- the
# clean-up included. Every command that touches the share is time-bounded: this
# is run exactly when something is wrong, and a wedged SMB session must produce
# a FAIL, not a hung terminal.
# bash 3.2-safe (macOS system bash); validated with `bash -n`.

set -u

MP="${1:-$HOME/Library/Application Support/MyCloud/mnt}"
CONFIG="$HOME/Library/Application Support/MyCloud/config.ini"
FAILED=0
T=30   # seconds per share operation

# The backup base on the share is [backup] dest_rel (default MyCloud) -- not
# necessarily "MyCloud".
DEST_REL="MyCloud"
if [ -f "$CONFIG" ]; then
    V=$(sed -n 's/^[[:space:]]*dest_rel[[:space:]]*=[[:space:]]*//p' "$CONFIG" | head -n 1 \
        | sed 's/[[:space:]]*;.*$//; s/[[:space:]]*$//; s#^/*##; s#/*$##')
    [ -n "$V" ] && DEST_REL="$V"
fi

# with_timeout SECS CMD...: run CMD, SIGKILL it after SECS; returns CMD's status
# (or 137). No coreutils `timeout` on macOS.
with_timeout() {
    local secs="$1" cmd_pid watcher rc
    shift
    "$@" &
    cmd_pid=$!
    ( sleep "$secs" && kill -9 "$cmd_pid" 2>/dev/null ) &
    watcher=$!
    wait "$cmd_pid"
    rc=$?
    kill "$watcher" 2>/dev/null
    wait "$watcher" 2>/dev/null
    return $rc
}

pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1"; FAILED=1; }
skip() { printf 'SKIP: %s\n' "$1"; }

LOCAL_TMP=""
SELFTEST=""

cleanup() {
    if [ -n "$SELFTEST" ]; then
        if with_timeout "$T" rm -rf "$SELFTEST" 2>/dev/null \
                && ! with_timeout "$T" test -e "$SELFTEST"; then
            pass "cleaned up $SELFTEST"
        else
            fail "could not clean up $SELFTEST"
        fi
        SELFTEST=""
    fi
    if [ -n "$LOCAL_TMP" ] && [ -d "$LOCAL_TMP" ]; then
        rm -rf "$LOCAL_TMP" 2>/dev/null || true
        LOCAL_TMP=""
    fi
}
# Early exits still clean up; the normal path cleans up BEFORE the verdict so a
# failed clean-up counts (it used to print "RESULT: PASS" and exit 0 first).
trap cleanup EXIT

echo "MyCloud selftest against: $MP"
echo "----------------------------------------------------------------"

# 1. The share is mounted at the expected mount point.
if /sbin/mount | grep -qF " on $MP "; then
    pass "share is mounted at $MP"
else
    fail "share is NOT mounted at $MP"
    echo "----------------------------------------------------------------"
    echo "RESULT: FAIL (cannot continue without a mount)"
    exit 1
fi

# 2. The install sentinel is present on the share root.
if with_timeout "$T" test -f "$MP/.mycloud_sentinel"; then
    pass "sentinel file present (.mycloud_sentinel)"
else
    fail "sentinel file missing (.mycloud_sentinel)"
fi

# 3. Create the scratch area on the share.
SELFTEST="$MP/$DEST_REL/.selftest"
if with_timeout "$T" mkdir -p "$SELFTEST" 2>/dev/null; then
    pass "created scratch dir $DEST_REL/.selftest"
else
    fail "could not create $DEST_REL/.selftest"
    echo "----------------------------------------------------------------"
    echo "RESULT: FAIL"
    exit 1
fi

# 4. Round-trip a Chinese + spaces filename, byte-for-byte.
LOCAL_TMP="$(mktemp -d "${TMPDIR:-/tmp}/mycloud-selftest.XXXXXX")"
FNAME="自检 测试.txt"
LOCAL_FILE="$LOCAL_TMP/$FNAME"
REMOTE_FILE="$SELFTEST/$FNAME"
printf 'MyCloud selftest payload 内容 %s\n' "$(date -u +%Y%m%dT%H%M%SZ)" > "$LOCAL_FILE"

if with_timeout "$T" cp "$LOCAL_FILE" "$REMOTE_FILE" 2>/dev/null \
        && with_timeout "$T" cmp -s "$LOCAL_FILE" "$REMOTE_FILE"; then
    pass "CJK+spaces filename round-tripped byte-for-byte"
else
    fail "CJK+spaces filename did NOT round-trip"
fi

# 5. Rename into a freshly created nested directory tree (trash-move mechanism).
NESTED="$SELFTEST/tree/a/b/c"
MOVED_FILE="$NESTED/$FNAME"
if with_timeout "$T" mkdir -p "$NESTED" 2>/dev/null \
    && with_timeout "$T" mv "$REMOTE_FILE" "$MOVED_FILE" 2>/dev/null \
    && with_timeout "$T" test -f "$MOVED_FILE" \
    && ! with_timeout "$T" test -e "$REMOTE_FILE" \
    && with_timeout "$T" cmp -s "$LOCAL_FILE" "$MOVED_FILE"; then
    pass "rename into freshly created nested tree works"
else
    fail "rename into nested tree FAILED (trash-move would not work here)"
fi

# 6. Heartbeat freshness, if the daemon has written one yet.
HEARTBEAT="$MP/$DEST_REL/last_sync.json"
if with_timeout "$T" test -f "$HEARTBEAT"; then
    MTIME="$(with_timeout "$T" stat -f %m "$HEARTBEAT" 2>/dev/null)"
    NOW="$(date +%s)"
    if [ -n "$MTIME" ]; then
        AGE=$(( NOW - MTIME ))
        if [ "$AGE" -lt 172800 ]; then
            pass "heartbeat is fresh (${AGE}s old)"
        else
            fail "heartbeat is stale (${AGE}s old, > 48h)"
        fi
    else
        fail "could not read heartbeat mtime"
    fi
else
    skip "no heartbeat file yet ($DEST_REL/last_sync.json) — run a sync first"
fi

cleanup
echo "----------------------------------------------------------------"
if [ "$FAILED" -eq 0 ]; then
    echo "RESULT: PASS"
else
    echo "RESULT: FAIL"
fi
exit "$FAILED"
