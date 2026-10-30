#!/bin/bash
#
# MyCloud uninstaller.
#
# Removes the local agent, watchdog, mount, CLI symlink and (optionally) the
# stored credentials. It NEVER touches the backup living on the NAS.
#
# bash 3.2 safe (the macOS system bash). Interactive. Refuses to run as root.

set -u

# --- guard: never run as root -------------------------------------------------
# Everything installed lives in the invoking user's home / LaunchAgents; running
# as root would target the wrong domain and could touch files we do not own.
if [ "$(id -u)" -eq 0 ]; then
    echo "Error: do not run uninstall.sh as root (or with sudo)." >&2
    echo "Run it as the same user that installed MyCloud." >&2
    exit 1
fi

UID_NUM="$(id -u)"

# --- fixed locations (mirror src/mycloud/paths.py) ----------------------------
APP="$HOME/Library/Application Support/MyCloud"
MNT="$APP/mnt"
LOGDIR="$HOME/Library/Logs/MyCloud"
CONFIG="$APP/config.ini"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
AGENT_PLIST="$LAUNCH_AGENTS/com.mycloud.agent.plist"
WATCHDOG_PLIST="$LAUNCH_AGENTS/com.mycloud.watchdog.plist"
CTL_SYMLINK="/usr/local/bin/mycloudctl"
NSMB_CONF="$HOME/Library/Preferences/nsmb.conf"

AGENT_LABEL="com.mycloud.agent"
WATCHDOG_LABEL="com.mycloud.watchdog"

# --- read HOST / SMB username from config (best effort; fall back to spec) ----
read_ini_value() {
    # $1 = key. Prints the trimmed value of the first "key = value" line.
    [ -f "$CONFIG" ] || return 0
    grep -E "^[[:space:]]*$1[[:space:]]*=" "$CONFIG" 2>/dev/null \
        | head -1 \
        | sed -E "s/^[^=]*=[[:space:]]*//; s/[[:space:]]*\$//"
}

HOST="$(read_ini_value host)"
[ -n "$HOST" ] || HOST="192.168.5.1"
SMB_USER="$(read_ini_value username)"
[ -n "$SMB_USER" ] || SMB_USER="admin"
SHARE="$(read_ini_value share)"
[ -n "$SHARE" ] || SHARE="SHARE"

# --- prompt helper ------------------------------------------------------------
# ask "question" default   ->   default is "y" or "n"; returns 0 for yes.
ask() {
    local question="$1"
    local default="$2"
    local hint reply
    if [ "$default" = "y" ]; then
        hint="[Y/n]"
    else
        hint="[y/N]"
    fi
    printf "%s %s " "$question" "$hint"
    read reply || reply=""
    if [ -z "$reply" ]; then
        reply="$default"
    fi
    case "$reply" in
        y|Y|yes|YES|Yes) return 0 ;;
        *) return 1 ;;
    esac
}

echo "MyCloud uninstaller"
echo "-------------------"
echo

# --- 1. stop the LaunchAgents -------------------------------------------------
echo "Stopping launchd agents..."
/bin/launchctl bootout "gui/$UID_NUM/$AGENT_LABEL" 2>/dev/null || true
/bin/launchctl bootout "gui/$UID_NUM/$WATCHDOG_LABEL" 2>/dev/null || true

# --- 1b. wait until nothing can still mount onto $MNT --------------------------
# bootout returns before the daemon exits, and a mount_smbfs it already started
# runs in its own session: it can land the share on $MNT after the gate below,
# while you answer the prompt -- and the delete would then empty the share.
daemon_gone() {
    /bin/launchctl print "gui/$UID_NUM/$AGENT_LABEL" >/dev/null 2>&1 && return 1
    if [ -f "$APP/runtime/daemon.lock" ]; then
        /usr/bin/python3 -c 'import fcntl,os,sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
finally:
    os.close(fd)' "$APP/runtime/daemon.lock" >/dev/null 2>&1 || return 1
    fi
    /bin/ps -axo command= | /usr/bin/grep -F -- "$MNT" | /usr/bin/grep -q '^/sbin/mount_smbfs' && return 1
    return 0
}
waited=0
until daemon_gone; do
    waited=$((waited + 1))
    if [ "$waited" -ge 90 ]; then
        echo "ERROR: the MyCloud daemon (or a mount it started) is still running;" >&2
        echo "nothing was deleted. Try again in a minute." >&2
        exit 1
    fi
    sleep 1
done

# --- 2. unmount the private mount point ---------------------------------------
# Only the private mount at $MNT is our concern; the user's own /Volumes/SHARE
# (if any) is untouched.
is_mnt_mounted() {
    # True (0) iff anything is still mounted at $MNT. The mount table prints
    # the CANONICAL path, so also ask the filesystem (st_dev differs from the
    # parent's) -- a text match alone missed non-canonical spellings.
    /sbin/mount | grep -qF " on $MNT " && return 0
    /usr/bin/python3 -c 'import os,sys;sys.exit(0 if os.path.ismount(sys.argv[1]) else 1)' "$MNT" 2>/dev/null
}

if is_mnt_mounted; then
    echo "Unmounting $MNT ..."
    if ! /sbin/umount "$MNT" 2>/dev/null; then
        echo "  umount busy; forcing via diskutil..."
        /usr/sbin/diskutil unmount force "$MNT" 2>/dev/null || true
    fi
fi

# --- 2b. HARD GATE: never rm -rf into a live NAS mount ------------------------
# If both umount and diskutil-force failed (a wedged SMB session -- exactly the
# scenario this project defends against), $MNT is still the live NAS share.
# Recursively deleting $APP would then delete MyCloud/data and MyCloud/.trash ON
# THE NAS. Refuse to continue rather than destroy the backup (PLAN T12/R9).
if is_mnt_mounted; then
    echo "ERROR: $MNT is still mounted; the NAS share could not be unmounted." >&2
    echo "Refusing to remove local files while the share is mounted, because" >&2
    echo "doing so could delete your backup ON THE NAS." >&2
    echo >&2
    echo "Reboot (or reboot the router) to clear the wedged SMB session, then" >&2
    echo "re-run this uninstaller. The launchd agents were stopped, but NO files" >&2
    echo "(local or on the NAS) have been deleted." >&2
    exit 1
fi

# --- 3. remove the plists -----------------------------------------------------
rm -f "$AGENT_PLIST" "$WATCHDOG_PLIST"

# --- 4. remove the CLI symlink (only if it points into APP) -------------------
# Never delete a same-named file that belongs to something else.
if [ -L "$CTL_SYMLINK" ]; then
    target="$(readlink "$CTL_SYMLINK")"
    case "$target" in
        "$APP"/*)
            echo "Removing CLI symlink $CTL_SYMLINK"
            rm -f "$CTL_SYMLINK" 2>/dev/null \
                || echo "  could not remove $CTL_SYMLINK (check permissions)" >&2
            ;;
        *)
            echo "Leaving $CTL_SYMLINK (does not point into MyCloud)."
            ;;
    esac
fi

# --- 5. remove app dir, optionally preserving config + logs -------------------
if ask "Keep a backup of your config.ini and logs?" "y"; then
    BAK="$APP.bak-$(date +%Y%m%d)"
    mkdir -p "$BAK"
    if [ -f "$CONFIG" ]; then
        cp -p "$CONFIG" "$BAK/config.ini" 2>/dev/null || true
    fi
    if [ -d "$LOGDIR" ]; then
        rm -rf "$BAK/logs"
        mv "$LOGDIR" "$BAK/logs" 2>/dev/null || true
    fi
    echo "Saved config and logs to: $BAK"
else
    rm -rf "$LOGDIR"
fi
# Atomic gate: rmdir succeeds only on an EMPTY, UNMOUNTED directory -- and once
# $MNT is gone, a late mount has nowhere to land.
if [ -d "$MNT" ] && ! rmdir "$MNT" 2>/dev/null; then
    if is_mnt_mounted; then
        echo "ERROR: $MNT is mounted again; refusing to delete anything more." >&2
        exit 1
    fi
fi
# -x: never cross a mountpoint, whatever happens after the checks above.
rm -rfx "$APP"
if [ -e "$APP" ]; then
    echo "ERROR: $APP was not fully removed (a mount inside it?). The NAS was not touched." >&2
    exit 1
fi
echo "Removed $APP"

# --- 6. optional: delete the Keychain credential ------------------------------
# Anonymous mode stores no credential of its own (username is the guest
# account); a Keychain item, if any, belongs to an earlier registered-user setup.
if [ "$SMB_USER" = "guest" ]; then
    echo "Anonymous (guest) mode: no Keychain credential of its own to delete."
elif ask "Also delete the saved NAS password from your login Keychain?" "n"; then
    # Label + protocol pin MyCloud's own item; host+account alone could remove
    # the user's Finder "remember password" item instead.
    if /usr/bin/security delete-internet-password -s "$HOST" -a "$SMB_USER" -r "smb " -l "MyCloud NAS" 2>/dev/null; then
        echo "Deleted Keychain item for $SMB_USER@$HOST"
    else
        echo "No matching Keychain item found (nothing deleted)."
    fi
fi

# --- 7. optional: strip the [HOST] section from nsmb.conf ---------------------
# Preserve every other section byte-for-byte; only the MyCloud host block goes.
if [ -f "$NSMB_CONF" ]; then
    if ask "Remove the [$HOST] section from $NSMB_CONF?" "n"; then
        /usr/bin/python3 - "$NSMB_CONF" "$HOST" <<'PY'
import os
import re
import sys
import tempfile

path, host = sys.argv[1], sys.argv[2]
with open(path, "rb") as fh:
    data = fh.read()
text = data.decode("utf-8", "surrogateescape")

# A section runs from its header line up to (but not including) the next
# header line or EOF. Match case-insensitively: nsmb.conf may upper-case host.
pattern = re.compile(
    r"(?im)^[ \t]*\[" + re.escape(host) + r"\][^\n]*\n(?:(?![ \t]*\[).*\n?)*"
)
new_text = pattern.sub("", text)

if new_text == text:
    print("  no [%s] section found in nsmb.conf" % host)
    sys.exit(0)

new_data = new_text.encode("utf-8", "surrogateescape")
directory = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(dir=directory)
try:
    with os.fdopen(fd, "wb") as out:
        out.write(new_data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
print("  removed [%s] section from nsmb.conf" % host)
PY
    fi
fi

# --- 8. final, unmissable statement about the NAS -----------------------------
echo
BOLD=""
RESET=""
if [ -t 1 ]; then
    BOLD="$(printf '\033[1m')"
    RESET="$(printf '\033[0m')"
fi
printf "%s========================================================================%s\n" "$BOLD" "$RESET"
printf "%s  NOTHING ON THE NAS WAS TOUCHED.%s\n" "$BOLD" "$RESET"
printf "%s========================================================================%s\n" "$BOLD" "$RESET"
echo
echo "Your backup is still on the NAS, exactly as it was:"
echo "    backup data : //$HOST/$SHARE/MyCloud/data"
echo "    version/trash: //$HOST/$SHARE/MyCloud/.trash"
echo
echo "This uninstaller only removed the local MyCloud agent from this Mac."
echo "To recover files, browse the share in Finder and copy them back."
echo
