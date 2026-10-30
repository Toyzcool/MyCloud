"""Hardcoded real filesystem locations and system binary paths.

This is the ONLY module in the codebase permitted to hardcode real locations.
Every other module receives filesystem roots as parameters so tests can
redirect them to temporary directories.
"""

import os
from pathlib import Path

# Runtime layout under the user's Library.
# APP is overridable via the MYCLOUD_APP environment variable so that ctl tests
# and the GUI dev mode can redirect it to a scratch directory. Every derived
# constant below is computed from APP, so overriding it relocates the whole tree.
APP = Path(os.environ.get("MYCLOUD_APP") or Path.home() / "Library" / "Application Support" / "MyCloud")
MNT = APP / "mnt"
RUNTIME = APP / "runtime"
LOGDIR = Path.home() / "Library" / "Logs" / "MyCloud"

# macOS SMB client config. mount() reads the embedded-credential fallback
# password from the section matching the share host (mount.read_nsmb_password);
# on this macOS `mount_smbfs -N` does not deliver this file to the SMB auth
# exchange, so the password has to be recovered and embedded in the mount URL.
NSMB_CONF = Path.home() / "Library" / "Preferences" / "nsmb.conf"

CONFIG = APP / "config.ini"
LOCK = RUNTIME / "daemon.lock"
STATE = RUNTIME / "state.json"
SNAPSHOT = RUNTIME / "snapshot.json"
EXCLUDES = RUNTIME / "excludes.txt"
APPROVAL = RUNTIME / "approval.token"
JOURNAL = RUNTIME / "journal.ndjson"
MANIFEST_DB = RUNTIME / "manifest.db"
VERIFY_REQUEST = RUNTIME / "verify.request"

# LaunchAgents.
_LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
AGENT_PLIST = _LAUNCH_AGENTS / "com.mycloud.agent.plist"
WATCHDOG_PLIST = _LAUNCH_AGENTS / "com.mycloud.watchdog.plist"

# NAS-relative names (joined onto the mount point at runtime).
# These fixed constants assume the default dest_rel "MyCloud"; they remain until
# G2 migrates the daemon/ctl to the dest_rel-derived helpers below. New code
# should prefer data_rel()/trash_rel()/heartbeat_rel() with cfg.dest_rel.
DATA_REL = "MyCloud/data"
TRASH_REL = "MyCloud/.trash"
HEARTBEAT_REL = "MyCloud/last_sync.json"
SENTINEL_REL = ".mycloud_sentinel"


def data_rel(dest_rel):
    # type: (str) -> str
    """Share-relative path to the mirror data root under dest_rel."""
    return dest_rel + "/data"


def trash_rel(dest_rel):
    # type: (str) -> str
    """Share-relative path to the trash root under dest_rel."""
    return dest_rel + "/.trash"


def heartbeat_rel(dest_rel):
    # type: (str) -> str
    """Share-relative path to the heartbeat file under dest_rel."""
    return dest_rel + "/last_sync.json"

# System binaries (all ship with macOS 26.6; never Homebrew/Rosetta paths).
RSYNC = "/usr/bin/rsync"
MOUNT_SMBFS = "/sbin/mount_smbfs"
MOUNT = "/sbin/mount"
UMOUNT = "/sbin/umount"
DISKUTIL = "/usr/sbin/diskutil"
SMBUTIL = "/usr/bin/smbutil"
SECURITY = "/usr/bin/security"
OSASCRIPT = "/usr/bin/osascript"
CAFFEINATE = "/usr/bin/caffeinate"
BRCTL = "/usr/bin/brctl"
CAT = "/bin/cat"
MKDIR = "/bin/mkdir"
DF = "/bin/df"
DU = "/usr/bin/du"
PMSET = "/usr/bin/pmset"
# head reads the first byte of an iCloud dataless file to TRIGGER its download.
# It is run as a timed child (icloud.materialize_batch) so a kernel-stuck read
# cannot wedge a Python worker thread -- util.run's timeout uses an
# interruptible poll on the child's pipes and kills the child on expiry.
HEAD = "/usr/bin/head"
OPEN = "/usr/bin/open"
LAUNCHCTL = "/bin/launchctl"
