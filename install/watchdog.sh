#!/bin/sh
# MyCloud liveness watchdog (PLAN 6.7 / 6.8).
#
# Deliberately independent of the Python daemon: this is the backstop for the
# most insidious failures -- a macOS upgrade breaks the CLT /usr/bin/python3 and
# the daemon never starts at all, or the daemon runs but never manages to sync
# (offline/recovery loops keep state.json fresh every 30s, so file age alone
# could not see that; 2026-09-25 review). POSIX sh only, base-OS tools only.
#
# __APP__ is rendered at install time.

STATE="__APP__/runtime/state.json"
MAX_AGE=172800  # 48 hours in seconds

notify() {
    # $1 = message. osascript arguments carry no secrets.
    if [ -n "${WATCHDOG_DRY_RUN:-}" ]; then
        printf 'NOTIFY: %s\n' "$1"
        return 0
    fi
    /usr/bin/osascript -e 'on run argv' \
        -e 'display notification (item 1 of argv) with title "MyCloud"' \
        -e 'end run' \
        "$1" >/dev/null 2>&1 || true
}

DEAD='MyCloud 守护进程已超过 48 小时没有运行，备份已停止。请打开 MyCloud 查看原因。'
NOSYNC='MyCloud 仍在运行，但已超过 48 小时没有成功同步到 NAS。请打开 MyCloud 查看原因。'

if [ ! -f "$STATE" ]; then
    notify "$DEAD"
    exit 0
fi

NOW=$(/bin/date +%s)

# 1) Is the daemon alive? It rewrites state.json at least every 30s while it
#    runs (stat -f %m: modification time as a Unix epoch, BSD stat).
MTIME=$(/usr/bin/stat -f %m "$STATE" 2>/dev/null)
if [ -z "$MTIME" ] || [ $((NOW - MTIME)) -gt "$MAX_AGE" ]; then
    notify "$DEAD"
    exit 0
fi

# 2) Alive -- but has it SYNCED? last_sync_ok_at is written only after a pass
#    that fully succeeded (UTC ISO-8601). Never synced / unparseable: the
#    liveness check above is all we can say.
TS=$(/usr/bin/sed -n 's/.*"last_sync_ok_at":[[:space:]]*"\([0-9]\{4\}-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]\)[^"]*".*/\1/p' "$STATE" | /usr/bin/head -n 1)
[ -n "$TS" ] || exit 0
OK=$(/bin/date -j -u -f '%Y-%m-%dT%H:%M:%S' "$TS" +%s 2>/dev/null) || exit 0
if [ -n "$OK" ] && [ $((NOW - OK)) -gt "$MAX_AGE" ]; then
    notify "$NOSYNC"
fi
exit 0
