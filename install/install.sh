#!/bin/bash
# MyCloud installer -- the interactive shell front end of `mycloudctl install`.
#
# Everything that matters (stopping a running daemon before replacing files,
# releasing its mount, verifying the connection with the CANDIDATE credentials
# before storing them, sentinel handling with explicit adoption, retiring a
# stale manifest, rendering + loading the LaunchAgents) lives in ONE place:
# `mycloudctl install`, the same code the GUI installer runs. This script used
# to re-implement those steps in shell and had drifted (credentials stored
# before verification, verification riding an existing SMB session, a sentinel
# read error treated as "absent", silent adoption of another machine's backup,
# files replaced under a running daemon -- 2026-09-25 review).
#
# Refuses to run as root. bash 3.2 safe. Never uses sudo. The password is read
# without echo and handed to mycloudctl on stdin only -- never on any argv.

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
SRC="$REPO_ROOT/src"
APP="$HOME/Library/Application Support/MyCloud"
UID_NUM=$(id -u)
STAGE=""

abort() {
    echo "ERROR: $*" >&2
    exit 1
}

cleanup() {
    if [ -n "$STAGE" ]; then
        rm -rf "$STAGE"
        STAGE=""
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

stage_backend() {
    STAGE=$(mktemp -d "${TMPDIR:-/tmp}/mycloud_backend.XXXXXX") || abort "mktemp failed"
    cp "$SRC/mycloudd.py" "$SRC/mycloudctl.py" "$STAGE/" \
        && cp -R "$SRC/mycloud" "$STAGE/mycloud" \
        && cp "$SCRIPT_DIR/com.mycloud.agent.plist.template" \
              "$SCRIPT_DIR/com.mycloud.watchdog.plist.template" \
              "$SCRIPT_DIR/config.ini.template" "$SCRIPT_DIR/watchdog.sh" "$STAGE/" \
        || abort "Could not stage the backend files."
    find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null
}

json_get() {
    # json_get KEY  <<< json   (prints "" when absent or unparseable)
    /usr/bin/python3 -c 'import json,sys
try:
    d = json.loads(sys.stdin.read() or "{}")
except ValueError:
    d = {}
v = d.get(sys.argv[1]) if isinstance(d, dict) else None
print("" if v is None else v)' "$1"
}

# --- 0. refuse root -------------------------------------------------------------
if [ "$UID_NUM" -eq 0 ]; then
    abort "Do not run this installer as root (or with sudo). MyCloud installs into your user LaunchAgents and Keychain. Run it as your normal user."
fi

# --- 1. toolchain -----------------------------------------------------------------
echo "==> Checking prerequisites"
if ! /usr/bin/python3 -V >/dev/null 2>&1; then
    abort "/usr/bin/python3 is not working. Install the Command Line Tools with:
    xcode-select --install"
fi
if ! xcode-select -p >/dev/null 2>&1; then
    abort "Xcode Command Line Tools are not installed. Install them with:
    xcode-select --install
Then re-run this installer."
fi
echo "    python3: $(/usr/bin/python3 -V 2>&1)"
case "$(xcode-select -p 2>/dev/null)" in
    /Library/Developer/CommandLineTools) ;;
    *) echo "    WARNING: xcode-select points at $(xcode-select -p); /usr/bin/python3 then"
       echo "    resolves inside Xcode, and a Full Disk Access grant made for the"
       echo "    Command Line Tools' python3 will not apply. Recommended:"
       echo "        sudo xcode-select -s /Library/Developer/CommandLineTools" ;;
esac

# --- 1b. `install.sh --refresh`: update an existing install in place ---------------
# Copies this checkout's backend over the installed one, re-renders the
# LaunchAgents and restarts the daemon -- connection settings, backup sources
# and the manifest are left exactly as they are. (The GUI's 「更新后端」 does the
# same from the app bundle.)
if [ "${1:-}" = "--refresh" ]; then
    stage_backend
    echo "==> Refreshing the installed backend from $SRC"
    OUT=$(/usr/bin/python3 "$STAGE/mycloudctl.py" install --from "$STAGE" --refresh); RC=$?
    NOTICE=$(printf '%s' "$OUT" | json_get notice)
    [ -n "$NOTICE" ] && printf '%s\n' "$NOTICE" | sed 's/^/    /'
    if [ "$RC" -ne 0 ]; then
        ERR=$(printf '%s' "$OUT" | json_get error)
        abort "${ERR:-refresh failed (no details were returned).}"
    fi
    echo "Done."
    exit 0
fi

# --- 2. connection settings --------------------------------------------------------
prompt_conn() {
    printf "    SMB host [192.168.5.1]: "
    read -r SMB_HOST
    SMB_HOST=${SMB_HOST:-192.168.5.1}
    printf "    Share name [SHARE]: "
    read -r SMB_SHARE
    SMB_SHARE=${SMB_SHARE:-SHARE}
    printf "    Connection mode, user or anonymous [user]: "
    read -r SMB_MODE
    SMB_MODE=${SMB_MODE:-user}
    case "$SMB_MODE" in
        user|anonymous) ;;
        *) echo "    (unknown mode '$SMB_MODE', using user)"; SMB_MODE=user ;;
    esac
    SMB_USER=guest
    if [ "$SMB_MODE" = "user" ]; then
        printf "    Username [admin]: "
        read -r SMB_USER
        SMB_USER=${SMB_USER:-admin}
    fi
}

echo
echo "==> NAS connection settings (press Enter to accept the default)"
prompt_conn
# Confirm before collecting the password or changing anything: a real install
# once had the user name typed at the share prompt (//admin@host/admin).
while : ; do
    echo
    echo "    Please confirm the connection settings:"
    echo "        Mount URL: //$SMB_USER@$SMB_HOST/$SMB_SHARE"
    echo "        Mode:      $SMB_MODE"
    echo
    printf "    Press Enter to continue, or type anything to re-enter these values: "
    read -r SMB_CONFIRM
    [ -z "$SMB_CONFIRM" ] && break
    echo
    echo "==> Re-enter NAS connection settings"
    prompt_conn
done

SMB_PASS=""
if [ "$SMB_MODE" = "user" ]; then
    printf "    Password: "
    # IFS= keeps leading/trailing spaces: plain `read -r` silently trimmed them.
    IFS= read -r -s SMB_PASS
    echo
    [ -n "$SMB_PASS" ] || abort "Password must not be empty."
fi

# --- 3. stage the backend and hand over to mycloudctl install ----------------------
stage_backend

run_install() {
    # $1 (optional): --adopt. The password travels on stdin only.
    if [ "$SMB_MODE" = "user" ]; then
        printf '%s\n' "$SMB_PASS" | /usr/bin/python3 "$STAGE/mycloudctl.py" install \
            --from "$STAGE" --host "$SMB_HOST" --share "$SMB_SHARE" --mode user \
            --username "$SMB_USER" --password-stdin ${1:+"$1"}
    else
        /usr/bin/python3 "$STAGE/mycloudctl.py" install \
            --from "$STAGE" --host "$SMB_HOST" --share "$SMB_SHARE" --mode anonymous \
            ${1:+"$1"}
    fi
}

echo
echo "==> Verifying the connection and installing (up to ~2 minutes)"
OUT=$(run_install); RC=$?
if [ "$RC" -ne 0 ] && [ "$(printf '%s' "$OUT" | json_get needs_confirm)" = "adopt" ]; then
    echo
    printf '%s\n' "$OUT" | json_get error | sed 's/^/    /'
    printf "    Type adopt to take this backup over (this Mac becomes its only writer): "
    read -r ADOPT
    [ "$ADOPT" = "adopt" ] || abort "Not adopted; nothing was changed."
    OUT=$(run_install --adopt); RC=$?
fi
SMB_PASS=""
NOTICE=$(printf '%s' "$OUT" | json_get notice)
[ -n "$NOTICE" ] && printf '%s\n' "$NOTICE" | sed 's/^/    /'
if [ "$RC" -ne 0 ]; then
    ERR=$(printf '%s' "$OUT" | json_get error)
    abort "${ERR:-mycloudctl install failed (no details were returned).}"
fi

# --- 4. optional CLI link -----------------------------------------------------------
SHIM="$APP/bin/mycloudctl"
LINK=/usr/local/bin/mycloudctl
if [ -w /usr/local/bin ]; then
    if [ ! -e "$LINK" ] && [ ! -L "$LINK" ]; then
        ln -s "$SHIM" "$LINK" && echo "    Linked $LINK"
    elif [ -L "$LINK" ] && case "$(readlink "$LINK")" in "$APP"/*) true ;; *) false ;; esac; then
        ln -sf "$SHIM" "$LINK"
    else
        echo "    $LINK exists and is not MyCloud's; left untouched."
    fi
else
    echo "    To use the 'mycloudctl' command, add to your shell profile:"
    echo "        export PATH=\"$APP/bin:\$PATH\""
fi

# --- 5. the one step macOS will not let a program do ---------------------------------
echo
echo "============================================================"
echo " MyCloud is installed. One manual step remains:"
echo "============================================================"
echo
echo " FULL DISK ACCESS for /usr/bin/python3 (required -- without it the daemon"
echo " cannot read your folders OR the files on the mounted NAS):"
echo "   macOS 13+:  System Settings > Privacy & Security > Full Disk Access"
echo "   macOS 12:   System Preferences > Security & Privacy > Privacy > Full Disk Access"
echo "   Click +, press Cmd+Shift+G, enter /usr/bin/python3, enable it."
echo "   (Or run:  open -R /usr/bin/python3  and drag it into the list.)"
echo " Then restart the daemon so the grant applies:"
echo "   /usr/bin/python3 \"$APP/mycloudctl.py\" pause"
echo "   /usr/bin/python3 \"$APP/mycloudctl.py\" resume"
echo
echo " The MyCloud app (dist/MyCloud.app) shows status, guides this step, and"
echo " edits sources and connection settings."
echo "Done."
