#!/usr/bin/env python3
"""MyCloud operator CLI (PLAN sections 6.7, 6.9, 8/T10).

Subcommands: status / sync-now / approve-deletions / reload / pause / resume /
logs / doctor / materialize.

Like mycloudd.py this file inserts its own directory on sys.path so that
"import mycloud" resolves when it is copied alongside the mycloud/ package into
<APP>. All filesystem roots and every external interaction (subprocess runner,
TCP probe, os.kill, capability probe, os.execv, input) are injectable so the
tests never touch the real network, the real daemon, or launchctl.

Python 3.9 stdlib only.
"""

import argparse
import configparser
import fcntl
import io
import json
import os
import re
import shutil
import signal
import socket
import stat as stat_mod
import sys
import tempfile
import threading
import time as _time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mycloud import config as config_mod
from mycloud import icloud
from mycloud import paths
from mycloud import util
from mycloud.engine import SyncEngine
from mycloud.state import StateStore

# xcode-select is not a filesystem root; it is a system binary that ships with
# macOS, in the same category as the binaries hardcoded in paths.py. paths.py
# happens not to list it and this module may not edit paths.py, so it is named
# here (routed through run_cmd so tests stub it).
XCODE_SELECT = "/usr/bin/xcode-select"
TAIL = "/usr/bin/tail"

AGENT_LABEL = "com.mycloud.agent"

# System Settings deep links used by the open-settings one-click fixes (A4).
_FDA_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")

# One-click-fix registry (A4, pinned). Each id maps to a short Chinese action
# label the GUI renders on a doctor row; cmd_fix dispatches the id to a handler.
# Doctor rows carry fix_id (+ fix_label) only when ok=false and a remedy exists.
_FIX_LABELS = {
    "restart_daemon": "重启守护进程",
    "cleanup_stale_mounts": "清理遗留挂载",
    "mount_share": "挂载共享",
    "open_fda_settings": "打开完全磁盘访问设置",
    "rerun_capability_probe": "重新检测 rsync 能力",
    "prune_trash_now": "立即清理回收站",
    "sync_now": "立即同步",
    "run_deep_verify": "立即校验备份",
}

# `materialize` batching (PLAN 16.2 T-C2, 2026-07-30). Files are downloaded in
# chunks so progress is printed while a long run proceeds; the guard refuses to
# start unless free space is at least this multiple of the bytes to download.
_MATERIALIZE_BATCH = 50
_MATERIALIZE_FREE_FACTOR = 2


def _default_probe_tcp(host, port=445, timeout=3.0):
    # type: (str, int, float) -> bool
    """Real TCP reachability probe. Never ping (the router blocks ICMP)."""
    try:
        conn = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return False
    try:
        conn.close()
    except OSError:
        pass
    return True


_AGENT_LABEL = "com.mycloud.agent"


def _now_utc():
    # type: () -> datetime
    return datetime.now(timezone.utc)


def _parse_iso(text):
    # type: (object) -> object
    if not text or not isinstance(text, str):
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_seconds(iso, now_dt):
    # type: (object, datetime) -> object
    dt = _parse_iso(iso)
    if dt is None:
        return None
    return (now_dt - dt).total_seconds()


def _fmt_age_cn(seconds):
    # type: (object) -> str
    """Chinese relative age, for the Chinese-language progress lines.

    _fmt_age stays English because the status table it feeds is English; mixing
    them produced "最近传输 12s ago".
    """
    if seconds is None:
        return "从未"
    seconds = int(seconds)
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "%d 秒前" % seconds
    if seconds < 3600:
        return "%d 分钟前" % (seconds // 60)
    if seconds < 86400:
        return "%d 小时前" % (seconds // 3600)
    return "%d 天前" % (seconds // 86400)


def _fmt_age(seconds):
    # type: (object) -> str
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "%ds ago" % seconds
    if seconds < 3600:
        return "%dm ago" % (seconds // 60)
    if seconds < 86400:
        return "%dh ago" % (seconds // 3600)
    return "%dd ago" % (seconds // 86400)


def _fingerprint_changed_keys(stored_fp, probe):
    # type: (Optional[str], Dict[str, Any]) -> List[str]
    """Keys whose probed behavior differs from the stored fingerprint, compared
    KEY BY KEY (the daemon's semantics): a probe key added by an upgrade is not
    a change. An unparseable stored value that differs counts as changed."""
    if not stored_fp:
        return []
    try:
        stored_map = json.loads(stored_fp)
    except (TypeError, ValueError):
        stored_map = None
    if not isinstance(stored_map, dict):
        return [] if stored_fp == _fingerprint(probe) else ["<stored fingerprint unreadable>"]
    return sorted(k for k in stored_map if k in probe and probe[k] != stored_map[k])


def _fingerprint(probe):
    # type: (dict) -> str
    """Deterministic string identity of a capability-probe result dict."""
    return json.dumps(probe, sort_keys=True)


def _default_upload_selftest(rsync_bin):
    # type: (str) -> Any
    """SyncEngine.batch_selftest bound to this rsync, with a silent logger."""
    def _run(tmp_dir):
        # type: (str) -> Optional[str]
        import logging
        log = logging.getLogger("mycloud.doctor.upload_selftest")
        log.propagate = False
        if not log.handlers:
            log.addHandler(logging.NullHandler())
        return SyncEngine(rsync_bin, os.devnull, log).batch_selftest(tmp_dir)
    return _run


def _held_of(entry):
    # type: (dict) -> tuple
    """Return (count, paths) of held deletions for a folder state entry.

    The daemon persists held deletions as a LIST of relpaths under
    "held_deletions" (state.py). Older/foreign fixtures may instead store an int
    count under "held_deletions" plus the path list under "held_paths". Accept
    both so the CLI reads exactly what the daemon writes.
    """
    raw = entry.get("held_deletions")
    if isinstance(raw, list):
        # The list is capped (1000 per folder); held_total is the real count --
        # and an approval releases ALL of them.
        try:
            total = int(entry.get("held_total") or 0)
        except (TypeError, ValueError):
            total = 0
        return max(total, len(raw)), list(raw)
    try:
        count = int(raw or 0)
    except (TypeError, ValueError):
        count = 0
    paths_list = entry.get("held_paths") or []
    if not isinstance(paths_list, list):
        paths_list = []
    if count == 0 and paths_list:
        count = len(paths_list)
    return count, paths_list


# ---------------------------------------------------------------------------
# config.ini rewrite mechanism (the GUI write path).
#
# The GUI never edits config.ini by hand; it calls the config-* subcommands,
# which read the file with configparser, mutate one thing, re-validate the whole
# candidate through config.load_config (so nesting/duplicate/range rules apply),
# and rewrite it atomically in a stable key order. Comments are lost on the
# first GUI edit (a one-line stderr notice warns about this). The [server]
# password is never present in config.ini and is never written here.
# ---------------------------------------------------------------------------

_SERVER_ORDER = ("host", "share", "username", "auth_mode", "sentinel_uuid")
_BACKUP_ORDER = (
    "dest_rel", "folders", "exclude", "retention_days", "trash_max_gb",
    "hot_interval", "full_interval", "max_delete_files", "max_delete_pct",
    "offline_notify_hours", "watch_mode", "icloud_policy",
    "icloud_evict_after_backup",
)
_MULTILINE_KEYS = frozenset(("folders", "exclude"))
# Keys config-set may write (PLAN 12.2 whitelist + M4 iCloud keys + 15 P0).
_SETTABLE_KEYS = frozenset((
    "retention_days", "trash_max_gb", "hot_interval", "full_interval",
    "max_delete_files", "max_delete_pct", "offline_notify_hours", "watch_mode",
    "icloud_policy", "icloud_evict_after_backup",
    "upload_concurrency", "rsync_inplace", "rsync_no_times",
))


def _resolve_path(raw):
    # type: (str) -> Path
    """expanduser + resolve, with an abspath fallback if resolve() fails."""
    expanded = Path(str(raw)).expanduser()
    try:
        return expanded.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(str(expanded)))


def _split_multiline(raw):
    # type: (object) -> list
    """Split an INI multiline value into entries, skipping blanks/comments."""
    if raw is None:
        return []
    out = []
    for line in str(raw).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        out.append(stripped)
    return out


def _new_parser():
    # type: () -> configparser.ConfigParser
    # interpolation=None so '%' in paths/patterns is never treated specially;
    # inline_comment_prefixes matches config.load_config's reader.
    return configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",))


def _read_parser(path):
    # type: (str) -> configparser.ConfigParser
    """Read config.ini into a parser (empty parser if the file is absent).

    If the file EXISTS but configparser cannot parse it (e.g. a hand edit left a
    duplicated key or a line with no '='), raising here is mandatory: swallowing
    the error into an empty parser would let a config-* mutation validate an
    otherwise-empty candidate and atomically rewrite config.ini down to just the
    mutated key -- silently destroying [server] host/share/sentinel_uuid and
    every other configured source. Never commit when the on-disk file existed but
    could not be parsed.
    """
    parser = _new_parser()
    if not os.path.exists(path):
        return parser
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as exc:
        raise config_mod.ConfigError(
            "config.ini could not be parsed (fix it by hand before editing "
            "config from the GUI): %s" % (exc,))
    return parser


def _file_has_comments(path):
    # type: (str) -> bool
    """True if the raw file contains a '#' or ';' comment line."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith(";"):
                    return True
    except OSError:
        return False
    return False


def _render_config(parser):
    # type: (configparser.ConfigParser) -> str
    """Render the parser to text in a stable section/key order.

    Known keys come first in the canonical order; any unrecognized keys are
    preserved afterwards. A [server] 'password' key is never emitted.
    """
    lines = []  # type: list

    def _emit_section(section, order, multiline_keys, skip):
        if not parser.has_section(section):
            return
        lines.append("[%s]" % section)
        rendered = set()
        for key in order:
            if not parser.has_option(section, key):
                continue
            rendered.add(key)
            raw = parser.get(section, key, raw=True)
            if key in multiline_keys:
                lines.append("%s =" % key)
                for item in _split_multiline(raw):
                    lines.append("    %s" % item)
            else:
                lines.append("%s = %s" % (key, (raw or "").strip()))
        for key, raw in parser.items(section, raw=True):
            if key in rendered or key in skip:
                continue
            lines.append("%s = %s" % (key, (raw or "").strip()))
        lines.append("")

    _emit_section("server", _SERVER_ORDER, frozenset(), frozenset(("password",)))
    _emit_section("backup", _BACKUP_ORDER, _MULTILINE_KEYS, frozenset())
    text = "\n".join(lines).rstrip("\n")
    return text + "\n" if text else ""


class _ServerSetAbort(Exception):
    """server-set: abort with a user-facing message (daemon is resumed by finally)."""


class _NeedsAdopt(Exception):
    """server-set: the share carries a foreign sentinel; explicit adoption required."""

    def __init__(self, found):
        # type: (str) -> None
        Exception.__init__(self, found)
        self.found = found


class Ctl(object):
    """Operator command implementations, with all side-effects injectable."""

    def __init__(
        self,
        runtime_dir=None,
        logdir=None,
        config_path=None,
        mnt=None,
        agent_plist=None,
        uid=None,
        rsync_bin=None,
        run_cmd=None,
        probe_tcp=None,
        kill=None,
        cap_probe=None,
        execv=None,
        now_fn=None,
        input_fn=None,
        out=None,
        err=None,
        mount_factory=None,
        trash_factory=None,
        sleep_fn=None,
        nsmb_conf=None,
        verify_mount_factory=None,
        dataless_check=None,
        materialize_fn=None,
        free_bytes_fn=None,
        upload_selftest=None,
    ):
        self.runtime_dir = str(runtime_dir) if runtime_dir is not None else str(paths.RUNTIME)
        self.logdir = str(logdir) if logdir is not None else str(paths.LOGDIR)
        self.config_path = str(config_path) if config_path is not None else str(paths.CONFIG)
        self.mnt = str(mnt) if mnt is not None else str(paths.MNT)
        self.agent_plist = str(agent_plist) if agent_plist is not None else str(paths.AGENT_PLIST)
        self.uid = uid if uid is not None else os.getuid()
        self.rsync_bin = rsync_bin if rsync_bin is not None else paths.RSYNC

        # Injected seams (tests stub these; production uses the real thing).
        self.run_cmd = run_cmd if run_cmd is not None else util.run
        self.probe_tcp = probe_tcp if probe_tcp is not None else _default_probe_tcp
        self.kill = kill if kill is not None else os.kill
        self.cap_probe = cap_probe if cap_probe is not None else SyncEngine.capability_probe
        self.upload_selftest = (upload_selftest if upload_selftest is not None
                                else _default_upload_selftest(self.rsync_bin))
        self.execv = execv if execv is not None else os.execv
        self.now_fn = now_fn if now_fn is not None else _now_utc
        self.input_fn = input_fn if input_fn is not None else input
        self.out = out if out is not None else sys.stdout
        self.err = err if err is not None else sys.stderr
        self.mount_factory = mount_factory if mount_factory is not None else self._default_mount_factory
        self.trash_factory = trash_factory if trash_factory is not None else self._default_trash_factory
        # `materialize` seams (T-C2): tests cannot create a real dataless file,
        # and a real materialization would download from iCloud, so the
        # predicate, the downloader and the free-space probe are all injectable.
        self.dataless_check = (
            dataless_check if dataless_check is not None else icloud.is_dataless)
        self.materialize_fn = (
            materialize_fn if materialize_fn is not None
            else icloud.materialize_batch)
        self.free_bytes_fn = (
            free_bytes_fn if free_bytes_fn is not None else icloud.free_bytes)

        self.lock_path = os.path.join(self.runtime_dir, "daemon.lock")
        self.state_path = os.path.join(self.runtime_dir, "state.json")
        self.snapshot_path = os.path.join(self.runtime_dir, "snapshot.json")
        self.approval_path = os.path.join(self.runtime_dir, "approval.token")
        self.verify_request_path = os.path.join(self.runtime_dir, "verify.request")
        self.prune_request_path = os.path.join(self.runtime_dir, "prune.request")
        self.log_path = os.path.join(self.logdir, "mycloud.log")
        self.store = StateStore(self.runtime_dir)

        # Deep-verify polling (PLAN M3): ctl requests a verify, then polls
        # state.json for the daemon's result. All timing is injectable so tests
        # never block for the real 600s.
        self.sleep_fn = sleep_fn if sleep_fn is not None else _time.sleep
        self.nsmb_conf = str(nsmb_conf) if nsmb_conf is not None else str(paths.NSMB_CONF)
        self.verify_mount_factory = (verify_mount_factory if verify_mount_factory is not None
                                     else self._default_verify_mount_factory)
        self.verify_timeout = 600.0
        self.verify_poll_interval = 2.0

    # -- output helper -----------------------------------------------------

    def _p(self, msg=""):
        # type: (str) -> None
        self.out.write(msg + "\n")

    def _eprint(self, msg=""):
        # type: (str) -> None
        """Human-facing niceties go to stderr so stdout stays pure JSON."""
        self.err.write(msg + "\n")

    def _emit_json(self, obj):
        # type: (object) -> None
        """Write a single JSON value to stdout (the machine contract)."""
        self._p(json.dumps(obj, ensure_ascii=False, sort_keys=True))

    def _emit_error(self, msg):
        # type: (str) -> int
        """Emit {"error": msg} on stdout and return exit code 1."""
        self._emit_json({"error": msg})
        return 1

    def _default_mount_factory(self, cfg):
        # type: (object) -> object
        """Construct a real MountManager for cmd_mount (stubbed in tests)."""
        import logging
        from mycloud.mount import MountManager
        return MountManager(
            cfg.host, cfg.share, cfg.mount_username, Path(self.mnt),
            cfg.sentinel_uuid, logging.getLogger("mycloud.mount"),
            guest=cfg.anonymous)

    def _default_verify_mount_factory(self, host, share, username, mountpoint,
                                      sentinel_uuid, guest, password=None):
        # type: (str, str, str, str, str, bool, Optional[str]) -> object
        """A real MountManager on a TEMPORARY mountpoint (server-set verification;
        stubbed in tests). The daemon's own mountpoint is never touched. With a
        candidate `password` the manager mounts with THAT credential only."""
        import logging
        from mycloud.mount import MountManager
        return MountManager(host, share, username, Path(mountpoint), sentinel_uuid,
                            logging.getLogger("mycloud.mount"), guest=guest,
                            password=password)

    def _default_trash_factory(self, trash_root, retention_days, trash_max_gb):
        # type: (str, int, int) -> object
        """Construct a real TrashManager for prune_trash_now (stubbed in tests).

        prune() is the only fix that destroys NAS data, and its rmtree lives in
        trash.py, so this seam keeps that invariant intact while letting tests
        point pruning at a temp trash fixture.
        """
        import logging
        from mycloud.trash import TrashManager
        return TrashManager(
            trash_root, retention_days, trash_max_gb,
            logging.getLogger("mycloud.trash"))

    # -- config (best-effort) ----------------------------------------------

    def _load_config(self):
        # type: () -> object
        try:
            return config_mod.load_config(self.config_path)
        except config_mod.ConfigError:
            return None

    # -- daemon signalling -------------------------------------------------

    def _read_pid(self):
        # type: () -> object
        """Return the daemon PID from the lock file, or None if unavailable."""
        try:
            with open(self.lock_path, "r", encoding="utf-8") as handle:
                text = handle.read().strip()
        except (OSError, ValueError):
            return None
        if not text:
            return None
        try:
            return int(text.split()[0])
        except (ValueError, IndexError):
            return None

    def _daemon_alive(self):
        # type: () -> bool
        """True iff a live daemon holds the flock on daemon.lock.

        The daemon takes an exclusive flock on daemon.lock for its whole lifetime
        and never unlinks the file, so a recorded PID can outlive the daemon and
        be reassigned by the OS to an unrelated process. os.kill(pid, 0) would
        then succeed on that impostor, and the follow-up SIGUSR1/SIGUSR2 (whose
        default disposition is to terminate) would kill it. The flock is the
        authoritative liveness signal: if WE can acquire it, no daemon is holding
        it, so the PID in the file is stale. We release immediately when it is
        acquirable so we never disturb a daemon about to start.
        """
        try:
            fd = os.open(self.lock_path, os.O_RDWR)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, IOError):
                return True  # a live daemon holds the lock
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            return False
        finally:
            os.close(fd)

    def _signal_daemon(self, sig):
        # type: (int) -> object
        """Send sig to the daemon. Return (ok, message)."""
        pid = self._read_pid()
        if pid is None:
            return False, "MyCloud daemon does not appear to be running (no PID in %s)." % (
                self.lock_path,)
        if not self._daemon_alive():
            return False, "MyCloud daemon (pid %s) is not running (lock is stale)." % pid
        try:
            self.kill(pid, sig)
        except ProcessLookupError:
            return False, "MyCloud daemon (pid %s) is not running." % pid
        except PermissionError:
            return False, "Not permitted to signal the MyCloud daemon (pid %s)." % pid
        except OSError as exc:
            return False, "Could not signal the MyCloud daemon (pid %s): %s" % (pid, exc)
        return True, "Signalled MyCloud daemon (pid %s)." % pid

    def _maybe_signal_reload(self):
        # type: () -> bool
        """Send SIGUSR2 iff a live daemon PID is recorded. Return True if sent.

        Liveness is verified via the flock on daemon.lock (see _daemon_alive),
        NOT os.kill(pid, 0): the daemon never unlinks its lock file, so a stale
        PID can be reassigned to an unrelated process that os.kill(pid, 0) would
        happily confirm and SIGUSR2 would then kill. A missing lock file or a
        stale (unlocked) PID is not an error here: the config was still
        rewritten; the daemon picks it up on restart.
        """
        pid = self._read_pid()
        if pid is None:
            return False
        if not self._daemon_alive():
            return False
        try:
            self.kill(pid, signal.SIGUSR2)
        except OSError:
            return False
        return True

    # -- derived status ----------------------------------------------------

    def _build_derived(self, state, configured=None):
        # type: (dict, object) -> dict
        """Derive status rollups from a state document.

        configured, when given, is the set of source names currently in
        config.ini. TOTALS are then restricted to those sources: state.folders
        retains an entry for every folder ever synced, and a removed source keeps
        the counters it had at removal, so summing everything inflates the
        headline numbers with data about folders the daemon no longer touches
        (2026-07-28: a removed source contributed 315306 phantom dataless files
        and 2 phantom failures). per_folder and the orphaned list still cover
        EVERY entry -- surfacing orphans is exactly their job.
        """
        now_dt = self.now_fn()
        folders = state.get("folders")
        if not isinstance(folders, dict):
            folders = {}
        active = set(configured) if configured else None

        per_folder = {}
        total_held = 0
        total_failed = 0
        orphaned = []
        permanently_failing = {}
        for name, entry in sorted(folders.items()):
            if not isinstance(entry, dict):
                entry = {}
            failed = entry.get("failed_files") or []
            if not isinstance(failed, list):
                failed = []
            _held_count, _held_paths = _held_of(entry)
            held = _held_count
            counts_toward_totals = (active is None or name in active)
            if counts_toward_totals:
                total_held += held
                total_failed += len(failed)
                if failed:
                    permanently_failing[name] = list(failed)
            if entry.get("orphaned"):
                orphaned.append(name)
            per_folder[name] = {
                "failed_files_count": len(failed),
                "held_deletions": held,
                "last_ok_age_seconds": _age_seconds(entry.get("last_ok"), now_dt),
                "configured": counts_toward_totals,
            }

        snap_age = None
        try:
            snap_age = now_dt.timestamp() - os.path.getmtime(self.snapshot_path)
        except OSError:
            snap_age = None

        return {
            "now": now_dt.isoformat(),
            "last_sync_ok_age_seconds": _age_seconds(state.get("last_sync_ok_at"), now_dt),
            "state_age_seconds": _age_seconds(state.get("updated_at"), now_dt),
            "snapshot_age_seconds": snap_age,
            "deletions_held": total_held > 0,
            "total_held_deletions": total_held,
            "total_failed_files": total_failed,
            "orphaned_folders": orphaned,
            "permanently_failing_files": permanently_failing,
            "watcher_degraded": bool(state.get("watcher_degraded")),
            "deletion_disabled": bool(state.get("deletion_disabled")),
            "folders": per_folder,
        }

    def cmd_status(self, as_json=False):
        # type: (bool) -> int
        state = self.store.get()
        # Restrict rollups to configured sources (see _build_derived). A config
        # that will not load yields None, which keeps the legacy sum-everything
        # behavior rather than under-reporting.
        cfg = self._load_config()
        configured = set(f.name for f in cfg.folders) if cfg is not None else None
        derived = self._build_derived(state, configured=configured)

        if as_json:
            payload = dict(state)
            payload["derived"] = derived
            self._p(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
            return 0

        self._p("MyCloud status")
        self._p("  overall state : %s" % state.get("state"))
        self._p("  last sync ok  : %s (%s)" % (
            state.get("last_sync_ok_at") or "never",
            _fmt_age(derived["last_sync_ok_age_seconds"])))
        self._p("  state updated : %s" % _fmt_age(derived["state_age_seconds"]))
        if derived["snapshot_age_seconds"] is not None:
            self._p("  snapshot age  : %s" % _fmt_age(derived["snapshot_age_seconds"]))
        if derived["deletion_disabled"]:
            self._p("  deletion propagation is DISABLED (capability fingerprint changed).")
        if derived["watcher_degraded"]:
            self._p("  watcher: DEGRADED (kqueue budget exceeded; running in scan mode).")

        # Offline iCloud prefetch (PLAN M4): shown whenever a prefetch is active
        # or one has run this offline period. Byte progress is read from the live
        # progress snapshot when it is still a "downloading" phase.
        prefetch = state.get("prefetch")
        if isinstance(prefetch, dict) and (
                prefetch.get("active") or prefetch.get("total") or prefetch.get("blocked")):
            done = prefetch.get("done", 0) or 0
            total = prefetch.get("total", 0) or 0
            verb = "downloading" if prefetch.get("active") else "prefetched"
            extra = ""
            prog = state.get("progress")
            if isinstance(prog, dict) and prog.get("phase") == "downloading":
                bd, bt = prog.get("bytes_done"), prog.get("bytes_total")
                if isinstance(bd, (int, float)) and isinstance(bt, (int, float)) and bt:
                    gib = float(2 ** 30)
                    extra = " (%.1f/%.1f GiB)" % (bd / gib, bt / gib)
            if prefetch.get("total") or prefetch.get("active"):
                self._p("  prefetch      : %s %s/%s cloud files%s" % (verb, done, total, extra))
            skipped = state.get("prefetch_skipped")
            if isinstance(skipped, (int, float)) and int(skipped) > 0:
                self._p("                  %d file(s) skipped (download timed out)" % int(skipped))
            if prefetch.get("blocked"):
                self._p("  iCloud download: BLOCKED by macOS (launchd agents cannot trigger "
                        "iCloud downloads; open/download the files yourself and they will "
                        "back up automatically).")

        folders = state.get("folders")
        if not isinstance(folders, dict) or not folders:
            self._p("  (no folders recorded yet)")
        else:
            self._p("")
            header = "  %-20s %-14s %-12s %8s %8s %6s %8s %s" % (
                "folder", "state", "last ok", "copied", "trashed", "held", "failed",
                "orphaned")
            self._p(header)
            self._p("  " + "-" * (len(header) - 2))
            for name in sorted(folders):
                entry = folders[name] if isinstance(folders[name], dict) else {}
                fd = derived["folders"].get(name, {})
                self._p("  %-20s %-14s %-12s %8s %8s %6s %8s %s" % (
                    name[:20],
                    str(entry.get("state", "?"))[:14],
                    _fmt_age(fd.get("last_ok_age_seconds")),
                    entry.get("files_copied", 0),
                    entry.get("files_trashed", 0),
                    fd.get("held_deletions", 0),
                    fd.get("failed_files_count", 0),
                    "yes" if entry.get("orphaned") else "-"))

        if derived["deletions_held"]:
            self._p("")
            self._p("  *** DELETIONS HELD: %d deletion(s) awaiting approval. Run "
                    "`mycloudctl approve-deletions`. ***" % derived["total_held_deletions"])

        deferred = 0
        for name, entry in folders.items():
            if not derived["folders"].get(name, {}).get("configured", True):
                continue           # a removed source's frozen state
            dd = entry.get("deferred_deletions") if isinstance(entry, dict) else None
            if isinstance(dd, dict) and isinstance(dd.get("count"), int):
                deferred += dd["count"]
        if deferred:
            self._p("")
            self._p("  %d deletion(s) deferred: the file may have moved while it "
                    "is only in iCloud (macOS <= 13 placeholder); the NAS copy "
                    "stays until it is downloaded and backed up." % deferred)

        if derived["permanently_failing_files"]:
            self._p("")
            self._p("  Permanently failing files (excluded from backup):")
            for name in sorted(derived["permanently_failing_files"]):
                for rel in derived["permanently_failing_files"][name]:
                    self._p("    %s: %s" % (name, rel))

        return 0

    # -- sync-now ----------------------------------------------------------

    def cmd_sync_now(self):
        # type: () -> int
        """Request an immediate pass, then report what will ACTUALLY happen.

        The bare "Signalled MyCloud daemon" this used to print was actively
        misleading (2026-07-27 user report: "clicking sync-now does nothing").
        SIGUSR1 only sets a flag that the main loop reads BETWEEN passes, so
        during a multi-hour first backup the request sits queued and nothing
        observable changes. Read the live state and say so, including the current
        progress, so the user can tell "already working" from "stuck".
        """
        ok, message = self._signal_daemon(signal.SIGUSR1)
        if not ok:
            self._p(message)
            return 1

        state = self.store.get()
        st = state.get("state")
        prog = state.get("progress")
        prog = prog if isinstance(prog, dict) else {}

        if st == "SYNCING" and prog.get("active"):
            self._p("守护进程当前已在同步中，无需再次触发。")
            detail = self._progress_line(prog, state)
            if detail:
                self._p("  " + detail)
            self._p("  已记下你的请求：本轮结束后会立即再跑一轮完整同步。")
        elif st == "OFFLINE":
            self._p("已记下同步请求，但 NAS 当前不可达（离线）。")
            self._p("  一旦重新挂载成功，会立即执行一轮完整同步。")
        else:
            self._p("已触发立即同步。")
        return 0

    def _progress_line(self, prog, state):
        # type: (dict, dict) -> str
        """One-line human summary of a live progress snapshot (sync-now / status).

        Byte percentage is the headline because it reflects actual transferred
        volume; file counts are a poor proxy across 315k files of wildly
        differing size. "最近活动" comes last and matters most for the
        "is it stuck?" question: files_done advances in jumps (rsync
        block-buffers itemize output), so a frozen counter with a fresh activity
        stamp means healthy-but-slow, not stalled.
        """
        phase_label = {
            "planning": "正在计算变更（扫描本地文件清单）",
            "downloading": "正在从 iCloud 下载云端文件",
            "transferring": "正在上传到 NAS",
            "verifying": "正在深度校验",
        }.get(prog.get("phase") or "", "同步中")
        parts = [phase_label]
        folder = prog.get("folder")
        if folder:
            parts.append("同步项 %s" % folder)

        bd = prog.get("bytes_done")
        bt = prog.get("bytes_total")
        if isinstance(bd, (int, float)) and isinstance(bt, (int, float)) and bt > 0:
            gib = float(1 << 30)
            parts.append("已传 %.1f/%.1f GiB（%.0f%%）"
                         % (bd / gib, bt / gib, 100.0 * bd / bt))
        fd = prog.get("files_done")
        tf = prog.get("total_files")
        if isinstance(fd, int):
            if isinstance(tf, int) and tf > 0:
                parts.append("%d/%d 个文件" % (fd, tf))
            else:
                parts.append("%d 个文件" % fd)
        bdone = prog.get("batches_done")
        btot = prog.get("batches_total")
        if isinstance(bdone, int) and isinstance(btot, int) and btot > 0:
            parts.append("第 %d/%d 批" % (min(bdone + 1, btot), btot))

        last = prog.get("last_transfer_at")
        if last:
            age = _age_seconds(last, self.now_fn())
            if age is not None:
                parts.append("最近传输 %s" % _fmt_age_cn(age))
        return " · ".join(parts)

    # -- reload ------------------------------------------------------------

    def cmd_reload(self):
        # type: () -> int
        ok, message = self._signal_daemon(signal.SIGUSR2)
        self._p(message if not ok else "Requested config reload. " + message)
        return 0 if ok else 1

    # -- verify (deep verify: request + poll) ------------------------------

    def _request_deep_verify(self):
        # type: () -> object
        """Write verify.request and signal SIGUSR1. Return (ok, message)."""
        try:
            util.atomic_write_text(
                self.verify_request_path, self.now_fn().isoformat() + "\n")
        except OSError as exc:
            return False, "无法写入校验请求：%s" % exc
        return self._signal_daemon(signal.SIGUSR1)

    def cmd_verify(self, as_json=False):
        # type: (bool) -> int
        """Request a deep verify and poll state.json for the daemon's result.

        Writes RUNTIME/verify.request + SIGUSR1 (PLAN M3); the daemon runs the
        verify next tick and writes state["verify"]. We poll up to verify_timeout
        for a summary whose finished_at is newer than this invocation, then print
        it. A daemon that is not running gets the request on its next start.
        """
        start_dt = self.now_fn()
        ok, message = self._request_deep_verify()
        if not ok:
            payload = {
                "requested": True, "running": False, "finished": False,
                "detail": message + " 校验请求已写入，将在守护进程下次启动时执行。",
            }
            if as_json:
                self._emit_json(payload)
            else:
                self._p(payload["detail"])
            return 1

        deadline = _time.monotonic() + self.verify_timeout
        while _time.monotonic() < deadline:
            verify = self.store.get().get("verify")
            if isinstance(verify, dict) and not verify.get("running"):
                fin = _parse_iso(verify.get("finished_at"))
                if fin is not None and fin >= start_dt:
                    if as_json:
                        self._emit_json(verify)
                    else:
                        self._print_verify(verify)
                    return 0
            self.sleep_fn(self.verify_poll_interval)

        timed = {"requested": True, "running": True, "finished": False,
                 "detail": "校验仍在进行（已等待 %d 秒）；稍后用 `mycloudctl status "
                           "--json` 查看 verify 结果。" % int(self.verify_timeout)}
        if as_json:
            self._emit_json(timed)
        else:
            self._p(timed["detail"])
        return 1

    def _print_verify(self, verify):
        # type: (dict) -> None
        self._p("Deep verify finished at %s" % verify.get("finished_at"))
        if verify.get("aborted"):
            # T-C1: an aborted verify stopped mid-walk (mount lost or daemon
            # stopping), so the counts below cover only the finished folders.
            self._p("  注意：本次校验中途中止（挂载丢失或守护进程停止），"
                    "以下计数只覆盖已完成的文件夹；稍后会自动重试。")
        # "queued", not "re-uploaded": these are manifest rows marked pending;
        # the upload happens in the follow-up pass. The old label read as done
        # and hid two months of failed uploads on the gateway (2026-08..10).
        self._p("  missing, queued for re-upload : %s" % verify.get("missing_uploaded", 0))
        self._p("  drift, queued for re-upload   : %s" % verify.get("drift_reuploaded", 0))
        self._p("  extra found (trashed): %s" % verify.get("extra_found", 0))
        errs = verify.get("errors") or []
        if errs:
            self._p("  errors:")
            for e in errs[:20]:
                self._p("    %s" % e)

    # -- config contract (12.2) --------------------------------------------

    def _config_get_payload(self, cfg):
        # type: (object) -> dict
        """Full-config JSON payload (config-get). excludes are USER patterns."""
        return {
            "host": cfg.host,
            "share": cfg.share,
            "username": cfg.username,
            "auth_mode": getattr(cfg, "auth_mode", "user"),
            "dest_rel": cfg.dest_rel,
            "sources": [
                {"path": str(f.src), "is_file": bool(f.is_file), "name": f.name}
                for f in cfg.folders
            ],
            "excludes": list(cfg.excludes),
            "retention_days": cfg.retention_days,
            "trash_max_gb": cfg.trash_max_gb,
            "hot_interval": cfg.hot_interval,
            "full_interval": cfg.full_interval,
            "max_delete_files": cfg.max_delete_files,
            "max_delete_pct": cfg.max_delete_pct,
            "offline_notify_hours": cfg.offline_notify_hours,
            "watch_mode": cfg.watch_mode,
            "icloud_policy": cfg.icloud_policy,
            "icloud_evict_after_backup": cfg.icloud_evict_after_backup,
            "upload_concurrency": cfg.upload_concurrency,
            "rsync_inplace": cfg.rsync_inplace,
            "rsync_no_times": cfg.rsync_no_times,
            "warnings": list(cfg.warnings),
        }

    def _validate_candidate(self, parser):
        # type: (configparser.ConfigParser) -> tuple
        """Render + re-validate a mutated parser via config.load_config.

        Returns (cfg, None) on success or (None, error_message) if the candidate
        fails config validation (nesting, duplicates, ranges, bad dest_rel...).
        """
        text = _render_config(parser)
        fd, tmp_path = tempfile.mkstemp(prefix="mycloud-cfg-", suffix=".ini")
        try:
            os.close(fd)
            with open(tmp_path, "w", encoding="utf-8") as handle:
                handle.write(text)
            try:
                cfg = config_mod.load_config(tmp_path)
            except config_mod.ConfigError as exc:
                return None, str(exc)
            return cfg, None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _commit_config(self, parser):
        # type: (configparser.ConfigParser) -> None
        """Atomically rewrite config.ini, warning once if comments are dropped."""
        if _file_has_comments(self.config_path):
            self._eprint(
                "notice: comments in config.ini are not preserved by GUI edits.")
        util.atomic_write_text(self.config_path, _render_config(parser))

    def _read_parser_or_error(self):
        # type: () -> tuple
        """Read config.ini for mutation. Return (parser, None) or (None, msg).

        A parse failure of an existing file is fatal to the mutation: we refuse
        to rewrite an unparseable config down to just the mutated key.
        """
        try:
            return _read_parser(self.config_path), None
        except config_mod.ConfigError as exc:
            return None, str(exc)

    def _existing_sources(self, parser):
        # type: (configparser.ConfigParser) -> list
        """Return [(raw_entry, resolved_Path)] of currently-configured sources."""
        if not parser.has_section("backup"):
            return []
        raw_list = _split_multiline(parser.get("backup", "folders", fallback=None))
        return [(entry, _resolve_path(entry)) for entry in raw_list]

    def cmd_config_get(self):
        # type: () -> int
        cfg = self._load_config()
        if cfg is None:
            try:
                config_mod.load_config(self.config_path)
            except config_mod.ConfigError as exc:
                return self._emit_error(str(exc))
            return self._emit_error("config.ini could not be loaded.")
        self._emit_json(self._config_get_payload(cfg))
        return 0

    def cmd_config_add_source(self, path):
        # type: (str) -> int
        resolved = _resolve_path(path)
        parser, perr = self._read_parser_or_error()
        if perr is not None:
            return self._emit_error(perr)
        existing = self._existing_sources(parser)
        for _raw, res in existing:
            if res == resolved:
                return self._emit_error(
                    "已经是备份源：%s" % resolved)
        new_list = [raw for raw, _res in existing] + [str(resolved)]
        if not parser.has_section("backup"):
            parser.add_section("backup")
        parser.set("backup", "folders", "\n".join(new_list))
        cfg, err = self._validate_candidate(parser)
        if err is not None:
            return self._emit_error(err)
        # Round-trip guard: an INI value containing ' ;' is truncated at the
        # inline-comment prefix, so the path we just set could have silently
        # become a different (nonexistent) path that still validates. If the
        # resolved path is not among the re-parsed sources, refuse rather than
        # commit a mangled source.
        if str(resolved) not in [str(f.src) for f in cfg.folders]:
            return self._emit_error(
                "无法保存该路径（可能包含无法在配置文件中表示的字符，例如空格后紧跟分号）：%s"
                % resolved)
        self._commit_config(parser)
        payload = self._config_get_payload(cfg)
        if not resolved.exists():
            payload["notice"] = (
                "路径当前不存在，出现后将自动开始同步：%s" % resolved)
        if self._maybe_signal_reload():
            payload["reload_signalled"] = True
        self._emit_json(payload)
        return 0

    def cmd_config_remove_source(self, path):
        # type: (str) -> int
        resolved = _resolve_path(path)
        parser, perr = self._read_parser_or_error()
        if perr is not None:
            return self._emit_error(perr)
        existing = self._existing_sources(parser)
        kept = [raw for raw, res in existing if res != resolved]
        if len(kept) == len(existing):
            return self._emit_error("不是已配置的备份源：%s" % resolved)
        if not parser.has_section("backup"):
            parser.add_section("backup")
        parser.set("backup", "folders", "\n".join(kept))
        cfg, err = self._validate_candidate(parser)
        if err is not None:
            return self._emit_error(err)
        self._commit_config(parser)
        payload = self._config_get_payload(cfg)
        payload["notice"] = "NAS 镜像将保留（orphaned），不会被删除"
        if self._maybe_signal_reload():
            payload["reload_signalled"] = True
        self._emit_json(payload)
        return 0

    def cmd_config_set_dest(self, rel):
        # type: (str) -> int
        try:
            normalized = config_mod._validate_dest_rel(rel)
        except config_mod.ConfigError as exc:
            return self._emit_error(str(exc))
        parser, perr = self._read_parser_or_error()
        if perr is not None:
            return self._emit_error(perr)
        if not parser.has_section("backup"):
            parser.add_section("backup")
        parser.set("backup", "dest_rel", normalized)
        cfg, err = self._validate_candidate(parser)
        if err is not None:
            return self._emit_error(err)
        self._commit_config(parser)
        payload = self._config_get_payload(cfg)
        payload["notice"] = (
            "旧目标目录下的数据将原样保留（orphaned），不会被移动或删除")
        if self._maybe_signal_reload():
            payload["reload_signalled"] = True
        self._emit_json(payload)
        return 0

    def cmd_config_set(self, key, value):
        # type: (str, str) -> int
        if key not in _SETTABLE_KEYS:
            return self._emit_error(
                "未知或不可设置的配置项：%s" % key)
        parser, perr = self._read_parser_or_error()
        if perr is not None:
            return self._emit_error(perr)
        if not parser.has_section("backup"):
            parser.add_section("backup")
        parser.set("backup", key, str(value))
        cfg, err = self._validate_candidate(parser)
        if err is not None:
            return self._emit_error(err)
        self._commit_config(parser)
        payload = self._config_get_payload(cfg)
        if self._maybe_signal_reload():
            payload["reload_signalled"] = True
        self._emit_json(payload)
        return 0

    # -- server-set (NAS connection: host / share / auth mode) --------------

    _AUTH_MODES = ("user", "anonymous")

    def _quiet(self, fn):
        # type: (Any) -> tuple
        """Run a printing command with its stdout captured -> (rc, text)."""
        saved = self.out
        buf = io.StringIO()
        self.out = buf
        try:
            rc = fn()
        finally:
            self.out = saved
        return rc, buf.getvalue().strip()

    @staticmethod
    def _security_quote(value):
        # type: (str) -> str
        """Quote one token for `security -i` (backslash-escape \\ and \")."""
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _store_keychain_password(self, host, username, password):
        # type: (str, str, str) -> bool
        """add-internet-password via `security -i`, command text on STDIN.

        The password is never an argv element of any process (the install.sh
        contract, PLAN 8): it travels only inside security's stdin text.
        """
        line = ("add-internet-password -U -a %s -s %s -r \"smb \" "
                "-l \"MyCloud NAS\" -w %s\n"
                % (self._security_quote(username), self._security_quote(host),
                   self._security_quote(password)))
        res = self.run_cmd([paths.SECURITY, "-i"], 30, input_text=line)
        return (not getattr(res, "timed_out", False)
                and getattr(res, "rc", 1) == 0)

    def _rewrite_nsmb_conf(self, host, section_lines):
        # type: (str, list) -> None
        """Replace nsmb.conf's [host] section with section_lines (drop it when
        empty), preserving every other section verbatim. 0600 whenever a
        password is written (mirrors install.sh write_nsmb_conf)."""
        path = self.nsmb_conf
        existing = None  # type: Optional[str]
        mode = 0o600
        try:
            with open(path, "r", encoding="utf-8", errors="surrogateescape") as h:
                existing = h.read()
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            existing = None
        if existing is None and not section_lines:
            return
        header = "[%s]" % host.lower()
        kept = []  # type: list
        skipping = False
        for raw in (existing or "").splitlines():
            stripped = raw.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                low = stripped.lower()
                # [host] and the [host:USER] variants read_nsmb_password honours
                skipping = (low == header or low.startswith(header[:-1] + ":"))
                if skipping:
                    continue
            if not skipping:
                kept.append(raw)
        text = "\n".join(kept).rstrip("\n")
        if section_lines:
            text = (text + "\n" if text else "") + "\n".join(section_lines)
            mode = 0o600
        text = text + "\n" if text else ""
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".nsmb.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as out:
                out.write(text)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _share_listing_hint(self, host, username):
        # type: (str, str) -> str
        """Best-effort `smbutil view -N` so a wrong share name is obvious."""
        res = self.run_cmd(
            [paths.SMBUTIL, "view", "-N", "//%s@%s" % (username, host)], 15)
        out = (getattr(res, "out", "") or "").strip()
        if getattr(res, "timed_out", False) or not out:
            return ""
        lines = [ln for ln in out.splitlines() if ln.strip()][:12]
        return "\n该服务器提供的共享：\n" + "\n".join("  " + ln for ln in lines)

    def _wait_agent_stopped(self, timeout_s=45.0, poll_s=0.5):
        # type: (float, float) -> bool
        """After bootout, wait until launchd has unloaded the agent AND the
        daemon released its lock. bootout is asynchronous (PLAN 15.2 注意③): an
        immediate bootstrap collides with it (rc=5), and moving the manifest
        while the daemon still has it open would race the writer."""
        attempts = max(1, int(round(timeout_s / poll_s)))
        for _ in range(attempts):
            if not self._agent_loaded() and not self._daemon_alive():
                return True
            self.sleep_fn(poll_s)
        return not self._daemon_alive()

    def _retire_manifest(self):
        # type: () -> Optional[str]
        """Move manifest.db (+WAL/SHM) aside as manifest.db.bak-<ts>; None if absent."""
        base = os.path.join(self.runtime_dir, "manifest.db")
        if not os.path.exists(base):
            return None
        target = base + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        os.replace(base, target)
        for suffix in ("-wal", "-shm"):
            try:
                os.replace(base + suffix, target + suffix)
            except OSError:
                pass
        return target

    def _mount_table_lists(self, mountpoint):
        # type: (str) -> bool
        """True if `mount -t smbfs` lists a mount at exactly this path. Pass a
        realpath: the table prints /private/var/..., never /var/... ."""
        m = self.run_cmd([paths.MOUNT, "-t", "smbfs"], 10)
        return (" on %s " % mountpoint) in (getattr(m, "out", "") or "")

    def _other_mounts_for_host(self, host):
        # type: (str) -> list
        """Mountpoints, other than our own, where this machine already has the
        same SMB server mounted (a Finder mount at /Volumes/<share>, typically).

        The SMB client reuses such an authenticated session, so a verification
        mount can succeed regardless of the candidate password (review #4). The
        caller names these paths in its notice: "eject that volume, then restart
        the daemon" is the only way to prove a NEW password actually works, and
        an unproven password stays hidden until the next reboot."""
        m = self.run_cmd([paths.MOUNT, "-t", "smbfs"], 10)
        needle = "@%s/" % host.lower()
        ours = os.path.realpath(self.mnt)
        found = []  # type: list
        for line in (getattr(m, "out", "") or "").splitlines():
            if needle not in line.lower():
                continue
            on_idx, par_idx = line.find(" on "), line.rfind(" (")
            if on_idx == -1 or par_idx <= on_idx:
                continue
            mp = line[on_idx + 4:par_idx]
            if os.path.realpath(mp) != ours:
                found.append(mp)
        return found

    def _release_daemon_mount(self):
        # type: () -> bool
        """Unmount whatever sits on the daemon's private mountpoint (daemon
        already stopped). After a server/share change the OLD share would stay
        there: mount_smbfs fails rc=64 'File exists' and the new is_mounted()
        never matches -- permanent OFFLINE (review #1)."""
        real = os.path.realpath(self.mnt)
        if not self._mount_table_lists(real):
            return True
        u = self.run_cmd([paths.UMOUNT, real], 30)
        if getattr(u, "timed_out", False) or getattr(u, "rc", 1) != 0:
            self.run_cmd([paths.DISKUTIL, "unmount", "force", real], 30)
        return not self._mount_table_lists(real)

    def _release_temp_mount(self, mountpoint):
        # type: (str) -> bool
        """umount the verification mount (diskutil force on refusal); rmdir the
        temp dir once the table no longer lists it. `mountpoint` is a realpath."""
        if self._mount_table_lists(mountpoint):
            u = self.run_cmd([paths.UMOUNT, mountpoint], 30)
            if getattr(u, "timed_out", False) or getattr(u, "rc", 1) != 0:
                self.run_cmd([paths.DISKUTIL, "unmount", "force", mountpoint], 30)
        still = self._mount_table_lists(mountpoint)
        if not still:
            try:
                os.rmdir(mountpoint)
            except OSError:
                pass
        return not still

    def _wait_daemon_online(self, timeout_s=60.0, poll_s=1.0, since=None):
        # type: (float, float, Optional[datetime]) -> Optional[str]
        """After a restart, wait for state.json to report an online state and
        return it (None on timeout). This is the only proof that the launchd
        daemon -- with ITS TCC / credential context, not the GUI's -- can use
        the new connection ("doctor 全绿 ≠ 守护进程正常", PLAN 17.4c).

        With `since`, only a daemon that started at or after that moment
        counts (state.daemon_started_at, written first thing by every daemon
        start): the state a PREVIOUS run left behind read as "online" on the
        very first poll, even when the restarted daemon could not connect."""
        attempts = max(1, int(round(timeout_s / poll_s)))
        for _ in range(attempts):
            doc = self.store.get() or {}
            state = str(doc.get("state", "")).upper()
            if state in ("MOUNTED_IDLE", "SYNCING") and self._started_since(doc, since):
                return state
            self.sleep_fn(poll_s)
        return None

    @staticmethod
    def _started_since(doc, since):
        # type: (Dict[str, Any], Optional[datetime]) -> bool
        if since is None:
            return True
        raw = doc.get("daemon_started_at")
        if not raw:
            return False
        try:
            started = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return False
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return started >= since - timedelta(seconds=2)

    def _mount_in_flight(self):
        # type: () -> bool
        """True if a mount_smbfs targeting our mountpoint is running (the
        process table only -- nothing is logged; argv may hold a URL)."""
        res = self.run_cmd(["/bin/ps", "-axo", "command="], 10)
        if getattr(res, "timed_out", False) or getattr(res, "rc", 1) != 0:
            return True   # cannot tell -> assume the worst
        needles = (self.mnt, os.path.realpath(self.mnt))
        for line in (getattr(res, "out", "") or "").splitlines():
            line = line.strip()
            if line.startswith(paths.MOUNT_SMBFS) and any(n in line for n in needles):
                return True
        return False

    def _wait_no_mount_in_flight(self, timeout_s=75.0, poll_s=1.0):
        # type: (float, float) -> bool
        attempts = max(1, int(round(timeout_s / poll_s)))
        for _ in range(attempts):
            if not self._mount_in_flight():
                return True
            self.sleep_fn(poll_s)
        return False

    def _mark_restarting(self):
        # type: () -> None
        """Only while no daemon runs: replace the last run's state with one a
        live daemon never writes, so nothing mistakes it for the new run's."""
        if self._daemon_alive():
            return
        try:
            self.store.update(state="RESTARTING", guard=None)
        except OSError:
            pass

    def cmd_server_set(self, host, share, mode, username=None, password=None,
                       verify=True, adopt=False):
        # type: (str, str, str, Optional[str], Optional[str], bool, bool) -> int
        """Configure the NAS connection (设置 → 云盘连接).

        mode "user": credentials for `username`; the password arrives via STDIN
        only, is stored in the login Keychain AND in nsmb.conf (0600) -- the
        launchd daemon cannot rely on the Keychain (PLAN 15.2) -- and is never an
        argv element. mode "anonymous": SMB guest session, no credentials; the
        host's nsmb.conf password section is removed.

        Order of operations (each step guards the next; supervised review
        2026-09-12):
          1. validate inputs and the candidate config -- no side effects yet;
          2. if the daemon runs and (target changed or a new password): stop it,
             wait for unload + lock release, free the private mountpoint -- a
             verification riding on the daemon's live SMB session would pass
             with ANY password, and the old share left mounted would wedge the
             new daemon (rc=64 File exists);
          3. verify with a real mount on a TEMP mountpoint using the CANDIDATE
             credentials; check the guard can match the mount-table line; read
             the sentinel (a read *error* is an error, not "absent"); adopt a
             foreign sentinel only with explicit `adopt`; otherwise initialise;
          4. only now persist credentials; retire manifest.db when the target
             (or the share's identity) changed -- its rows describe the OLD
             share; commit [server];
          5. finally: a stopped daemon is ALWAYS resumed (also on errors); a
             running one is kickstarted (SIGUSR2 reload does not rebuild
             MountManager); then wait for it to report online.
        """
        host = (host or "").strip()
        share = (share or "").strip().strip("/")
        mode = (mode or "").strip().lower()
        if not host or "/" in host or any(ch.isspace() for ch in host):
            return self._emit_error("服务器地址无效：%r" % host)
        if not share or "/" in share or any(ch.isspace() for ch in share):
            return self._emit_error(
                "共享名无效（不能为空，不能含空格或斜杠）：%r" % share)
        if mode not in self._AUTH_MODES:
            return self._emit_error("连接模式必须是 user 或 anonymous：%r" % mode)
        if password is not None:
            if password == "":
                return self._emit_error("密码不能为空。")
            if "\n" in password or "\r" in password:
                return self._emit_error("密码不能包含换行。")
            if password != password.strip():
                return self._emit_error(
                    "密码首尾不能有空格（nsmb.conf 回退无法保留首尾空格）。")
        if mode == "anonymous":
            username = "guest"
            password = None
        else:
            username = (username or "").strip()
            if not username:
                return self._emit_error("注册用户模式需要填写用户名。")
            if (any(ch in username for ch in "@/:\\\"")
                    or any(ch.isspace() for ch in username)):
                return self._emit_error(
                    "用户名不能包含空格或 @ / : \\ \" 字符：%r" % username)

        parser, perr = self._read_parser_or_error()
        if perr is not None:
            return self._emit_error(perr)

        def _old(key):
            # type: (str) -> str
            if not parser.has_section("server"):
                return ""
            return (parser.get("server", key, fallback="") or "").strip()

        def _norm(value):
            # type: (str) -> str
            return (value or "").strip().strip("/").lower()

        old_host, old_share = _old("host"), _old("share")
        old_sentinel = _old("sentinel_uuid")
        target_changed = bool(old_host or old_share) and (
            _norm(old_host) != _norm(host) or _norm(old_share) != _norm(share))

        # 1. fail fast on anything the config layer would reject -- BEFORE the
        #    daemon stop / manifest move below.
        candidate = _new_parser()
        candidate.read_dict({s: dict(parser.items(s, raw=True))
                             for s in parser.sections()})
        if not candidate.has_section("server"):
            candidate.add_section("server")
        for key, value in (("host", host), ("share", share),
                           ("username", username), ("auth_mode", mode)):
            candidate.set("server", key, value)
        _unused, err = self._validate_candidate(candidate)
        if err is not None:
            return self._emit_error(err)

        notes = []  # type: list
        sentinel_uuid = "" if target_changed else old_sentinel
        manifest_stale = target_changed
        agent_loaded = self._agent_loaded()
        old_mode = (_old("auth_mode") or "user").lower()
        identity_changed = bool(old_host or old_share) and (
            _norm(_old("username")) != _norm(username) or old_mode != mode)
        if (mode == "user" and password is None
                and (target_changed or identity_changed)):
            from mycloud.mount import read_nsmb_password
            if read_nsmb_password(host, self.nsmb_conf, username=username) is None:
                # Verification would pass in THIS (GUI/terminal) context via the
                # Keychain, but the launchd daemon usually cannot read the
                # Keychain and nsmb.conf has no password for this server/user
                # (switching to anonymous removed it): it would be stuck OFFLINE
                # at its next remount.
                return self._emit_error(
                    "切换到注册用户模式（或更换服务器/用户名）需要输入密码：后台守护进程"
                    "通常无法读取钥匙串，需要把密码写入 nsmb.conf。")
        # Stop the daemon for ANY verification, not only for a new password or
        # target: its live mount of the same share makes the verification mount
        # collide ("File exists" -> a false "无法挂载") or ride its session, and a
        # changed identity must not be mounted on top of the old one.
        releases_mount = verify or target_changed or identity_changed
        needs_stop = agent_loaded and (releases_mount or password is not None)
        stopped = False
        prev_term = None
        cfg = None
        try:
            # A GUI timeout arrives as SIGTERM; turn it into SystemExit so the
            # finally blocks (temp unmount, daemon resume) still run.
            try:
                prev_term = signal.signal(signal.SIGTERM, lambda *_a: sys.exit(143))
            except (ValueError, OSError):
                prev_term = None

            # 2. stop + free the mountpoint when the verification must not ride
            #    on the daemon's session / the old share must go
            if needs_stop:
                self._quiet(self.cmd_pause)
                stopped = True   # resume in finally even if the wait fails
                if not self._wait_agent_stopped():
                    raise _ServerSetAbort(
                        "守护进程未能在限时内停止，连接设置未更改，请稍后重试。")
                if not self._release_daemon_mount():
                    raise _ServerSetAbort(
                        "旧共享仍挂载于 %s，无法切换；请先手工卸载（umount）后重试。"
                        % self.mnt)
                self._mark_restarting()
            elif releases_mount and self._mount_table_lists(os.path.realpath(self.mnt)):
                # No daemon running (install pauses it first), but its mount
                # outlived it: release before verifying.
                if not self._release_daemon_mount():
                    raise _ServerSetAbort(
                        "守护进程的挂载仍占用 %s，请稍后重试。" % self.mnt)
            if verify:
                # A mycloud_verify.* mount left by an interrupted run of this
                # command makes the next verification collide ("File exists").
                self._fix_cleanup_stale_mounts()
            others = self._other_mounts_for_host(host) if verify else []
            if others and password is not None:
                notes.append(
                    "⚠️ 本机已在 %s 挂载了同一服务器；SMB 复用了那条已认证的会话，"
                    "因此本次验证【不能】证明新密码正确。若刚改过密码：在访达推出该卷，"
                    "再用「暂停」→「恢复」重启守护进程；能重新上线才说明新密码可用"
                    "（否则日志会出现 rc=77 认证错误）。" % "、".join(others))
            elif others:
                notes.append("提示：本机已在 %s 挂载了同一服务器；本次未更改密码，"
                             "不影响结果。" % "、".join(others))

            # 3. verification mount on a temp dir with the CANDIDATE credentials
            if verify:
                tmp = os.path.realpath(tempfile.mkdtemp(prefix="mycloud_verify."))
                mm = self.verify_mount_factory(host, share, username, tmp,
                                               sentinel_uuid, mode == "anonymous",
                                               password=password)
                try:
                    if not mm.mount():
                        raise _ServerSetAbort(
                            "无法挂载 //%s@%s/%s：请检查服务器地址、共享名、连接模式与凭据。%s"
                            % (username, host, share,
                               self._share_listing_hint(host, username)))
                    if not mm.is_mounted():
                        raise _ServerSetAbort(
                            "已挂载，但挂载表条目不符合守卫的匹配规则（用户名/主机/共享名"
                            "拼写不一致），守护进程将无法确认该挂载。")
                    sentinel_path = os.path.join(tmp, paths.SENTINEL_REL)
                    try:
                        with open(sentinel_path, "r", encoding="utf-8") as h:
                            found = h.read().strip()
                    except FileNotFoundError:
                        found = ""
                    except OSError as exc:
                        raise _ServerSetAbort(
                            "已连接，但无法读取共享上的哨兵文件：%s。若为 Operation not "
                            "permitted，请在「隐私与安全性」允许 MyCloud 访问网络宗卷/"
                            "完全磁盘访问。" % exc)
                    if found:
                        if found != sentinel_uuid:
                            if not adopt:
                                raise _NeedsAdopt(found)
                            notes.append("已按确认接管该共享上现有的 MyCloud 备份（哨兵 %s…）；"
                                         "请确保没有其他机器仍在向它写入。" % found[:8])
                            if not target_changed:
                                manifest_stale = True
                        sentinel_uuid = found
                    else:
                        if not target_changed and old_sentinel:
                            manifest_stale = True
                            notes.append("共享上的哨兵已消失（共享可能被重建）：将重新初始化"
                                         "并转存旧清单。")
                        sentinel_uuid = str(uuid.uuid4()).upper()
                        dest_rel = "MyCloud"
                        if parser.has_section("backup"):
                            dest_rel = (parser.get("backup", "dest_rel",
                                                   fallback="MyCloud") or "MyCloud")
                        try:
                            dest_rel = config_mod._validate_dest_rel(dest_rel)
                            # "x": never truncate a sentinel that appeared meanwhile
                            with open(sentinel_path, "x", encoding="utf-8") as h:
                                h.write(sentinel_uuid + "\n")
                            os.makedirs(os.path.join(tmp, paths.data_rel(dest_rel)),
                                        exist_ok=True)
                            os.makedirs(os.path.join(tmp, paths.trash_rel(dest_rel)),
                                        exist_ok=True)
                        except (OSError, config_mod.ConfigError) as exc:
                            raise _ServerSetAbort(
                                "已连接，但无法在共享上初始化 MyCloud 目录"
                                "（可能没有写权限）：%s" % exc)
                        notes.append("已在该共享上初始化 MyCloud 备份目录（新哨兵）。")
                finally:
                    if not self._release_temp_mount(tmp):
                        notes.append("警告：校验挂载 %s 未能卸载，请在 doctor 中执行"
                                     "「清理遗留挂载」。" % tmp)
            elif target_changed:
                notes.append("未验证连接：哨兵未核对，守护进程首次挂载时可能因哨兵不匹配而拒绝。")

            # 4a. credentials -- only after the verification passed
            if mode == "user" and password is not None:
                if self._store_keychain_password(host, username, password):
                    notes.append("密码已保存到登录钥匙串。")
                else:
                    notes.append("写入钥匙串失败；将仅依赖 nsmb.conf 回退。")
                self._rewrite_nsmb_conf(host, ["[%s]" % host,
                                              "username=%s" % username,
                                              "password=%s" % password])
                notes.append("已写入 nsmb.conf 常驻回退（权限 600）。")
            elif mode == "anonymous":
                self._rewrite_nsmb_conf(host, [])
                notes.append("匿名模式：已移除 nsmb.conf 中该服务器的密码"
                             "（钥匙串中的旧条目保留）。")

            # 4b. the manifest describes the OLD target -> retire it
            if manifest_stale:
                if agent_loaded and not stopped:
                    self._quiet(self.cmd_pause)
                    stopped = True   # resume in finally even if the wait fails
                    if not self._wait_agent_stopped():
                        raise _ServerSetAbort(
                            "守护进程未能在限时内停止，连接设置未更改，请稍后重试。")
                retired = self._retire_manifest()
                if retired:
                    notes.append("备份目标已变更：旧备份清单已转存为 %s，将重新全量备份。"
                                 % os.path.basename(retired))

            # 4c. commit [server]
            if not parser.has_section("server"):
                parser.add_section("server")
            for key, value in (("host", host), ("share", share),
                               ("username", username), ("auth_mode", mode)):
                parser.set("server", key, value)
            if sentinel_uuid:
                parser.set("server", "sentinel_uuid", sentinel_uuid)
            cfg, err = self._validate_candidate(parser)
            if err is not None:
                raise _ServerSetAbort(err)
            self._commit_config(parser)
        except _NeedsAdopt as exc:
            self._emit_json({
                "error": ("该共享上已有另一份 MyCloud 备份（哨兵 %s…），与本机记录不同。"
                          "接管它意味着本机成为其写者——请确认没有其他机器仍在使用它。"
                          % exc.found[:8]),
                "needs_confirm": "adopt",
                "found_sentinel": exc.found,
            })
            return 1
        except _ServerSetAbort as exc:
            return self._emit_error(str(exc))
        finally:
            # 5. never leave the daemon stopped -- success or failure
            restart_t0 = self.now_fn()
            if stopped:
                rc_resume, _msg = self._quiet(self.cmd_resume)
                notes.append("守护进程已重启以应用新的连接设置。" if rc_resume == 0
                             else "守护进程重启失败，请手动执行 resume。")
            if prev_term is not None:
                try:
                    signal.signal(signal.SIGTERM, prev_term)
                except (ValueError, OSError):
                    pass

        if agent_loaded and not stopped:
            restart_t0 = self.now_fn()
            res = self._fix_restart_daemon()
            notes.append("守护进程已重启以应用新的连接设置。" if res.get("ok")
                         else "配置已保存，但重启守护进程失败：%s" % res.get("detail", ""))
        if agent_loaded:
            online = self._wait_daemon_online(since=restart_t0)
            if online:
                notes.append("守护进程已重新上线（%s）。" % online)
            else:
                notes.append("守护进程重启后 60 秒内未上线——本次验证只证明本应用能连通，"
                             "后台守护进程还需要「完全磁盘访问」授权；请查看 doctor 与日志。")
        else:
            notes.append("配置已保存；守护进程当前未运行，恢复后生效。")

        payload = self._config_get_payload(cfg)
        payload["notice"] = "\n".join(notes)
        payload["verified"] = bool(verify)
        self._emit_json(payload)
        return 0

    def cmd_version(self):
        # type: () -> int
        """Print the backend version + available commands as JSON.

        The GUI calls this on the INSTALLED backend to detect drift from the
        version bundled in the app: an older backend simply lacks newer
        subcommands, and argparse then exits 2 with everything on stderr --
        which looked like "后端未返回可解析的结果" (2026-09-12 field report).
        """
        from mycloud import __version__ as backend_version
        self._emit_json({"backend_version": backend_version,
                         "commands": _command_names()})
        return 0

    # -- install / uninstall (GUI-driven; port of install.sh / uninstall.sh) ---

    _BACKEND_FILES = ("mycloudd.py", "mycloudctl.py")
    _BACKEND_TEMPLATES = ("com.mycloud.agent.plist.template",
                          "com.mycloud.watchdog.plist.template",
                          "config.ini.template", "watchdog.sh")

    def _config_is_provisioned(self):
        # type: () -> bool
        """True iff config.ini exists AND carries a sentinel_uuid -- i.e. an
        install that actually completed. A config left behind by an attempt
        that failed at verification must not be mistaken for a finished one."""
        if not os.path.exists(self.config_path):
            return False
        probe = _new_parser()
        try:
            probe.read(self.config_path, encoding="utf-8")
        except configparser.Error:
            return False
        return bool((probe.get("server", "sentinel_uuid", fallback="") or "").strip())

    @property
    def app_dir(self):
        # type: () -> str
        """<APP>: the directory config.ini lives in (paths.APP in production;
        the config_path seam relocates it for tests)."""
        return os.path.dirname(os.path.abspath(self.config_path))

    @property
    def launch_agents_dir(self):
        # type: () -> str
        return os.path.dirname(os.path.abspath(self.agent_plist))

    @property
    def watchdog_plist(self):
        # type: () -> str
        return os.path.join(self.launch_agents_dir, "com.mycloud.watchdog.plist")

    @staticmethod
    def _render_template(text, app_dir, logdir):
        # type: (str, str, str) -> str
        return text.replace("__APP__", app_dir).replace("__LOGDIR__", logdir)

    def _read_backend_file(self, backend_dir, name):
        # type: (str, str) -> str
        path = os.path.join(backend_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as h:
                return h.read()
        except OSError as exc:
            raise _ServerSetAbort("后端文件缺失或不可读：%s（%s）" % (path, exc))

    def _copy_backend(self, backend_dir):
        # type: (str) -> None
        """Copy mycloudd.py / mycloudctl.py / mycloud/ from the bundle into <APP>
        (replacing the old package wholesale, exactly like install.sh)."""
        for name in self._BACKEND_FILES:
            src = os.path.join(backend_dir, name)
            if not os.path.isfile(src):
                raise _ServerSetAbort("后端文件缺失：%s" % src)
            shutil.copy2(src, os.path.join(self.app_dir, name))
        pkg_src = os.path.join(backend_dir, "mycloud")
        if not os.path.isdir(pkg_src):
            raise _ServerSetAbort("后端包缺失：%s" % pkg_src)
        pkg_dst = os.path.join(self.app_dir, "mycloud")
        # Stage the new package next to the old one and swap it in: deleting
        # first meant a failed copy (disk full) left no package at all, and the
        # resumed daemon crash-looped on ImportError.
        staged = pkg_dst + ".new"
        retired = pkg_dst + ".old"
        for leftover in (staged, retired):
            if os.path.lexists(leftover):
                util.remove_local_tree(leftover)
        shutil.copytree(pkg_src, staged,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if os.path.isdir(pkg_dst):
            os.rename(pkg_dst, retired)
        os.rename(staged, pkg_dst)
        if os.path.lexists(retired):
            util.remove_local_tree(retired)

    def _write_executable(self, path, text):
        # type: (str, str) -> None
        util.atomic_write_text(path, text)
        os.chmod(path, 0o755)

    def _label_loaded(self, label):
        # type: (str) -> bool
        res = self.run_cmd([paths.LAUNCHCTL, "print", "gui/%d/%s" % (self.uid, label)], 15)
        return (not getattr(res, "timed_out", False)) and getattr(res, "rc", 1) == 0

    def _bootstrap_agent(self, plist, label, attempts=6, wait_s=2.0):
        # type: (str, str, int, float) -> bool
        """bootstrap one LaunchAgent and CONFIRM it is loaded.

        rc=5 is ambiguous: "already loaded", but also "the previous instance is
        still being torn down" -- right after a bootout (asynchronous) or a
        quick 暂停->恢复. Taking it as success left nothing loaded once the
        teardown finished. Ask launchctl; if not loaded, wait and retry."""
        for attempt in range(attempts):
            res = self.run_cmd([paths.LAUNCHCTL, "bootstrap", "gui/%d" % self.uid, plist], 30)
            ok = (not getattr(res, "timed_out", False)) and getattr(res, "rc", 1) == 0
            if ok or self._label_loaded(label):
                return True
            if attempt + 1 < attempts:
                self.sleep_fn(wait_s)
        return False

    def _bootstrap_plist(self, plist, label):
        # type: (str, str) -> bool
        """bootout (ignoring 'not loaded') then bootstrap one LaunchAgent."""
        self.run_cmd([paths.LAUNCHCTL, "bootout", "gui/%d/%s" % (self.uid, label)], 30)
        return self._bootstrap_agent(plist, label)

    @staticmethod
    def _render_config_template(text, host, share, username, mode, folders):
        # type: (str, str, str, str, str, Optional[list]) -> str
        """Fill the shipped config.ini.template: [server] identity (the sentinel
        line is written later by server-set) and, when given, the folders."""
        # Replacement strings go in through a function: re.sub interprets
        # backslashes in a replacement STRING, so a user name like CORP\\user
        # raised re.error and a source path like /Docs/\\2024 was silently
        # rewritten.
        def _put(pattern, replacement, flags_text):
            return re.sub(pattern, lambda _m: replacement, flags_text, count=1)
        text = _put(r"(?m)^host = .*$", "host = %s" % host, text)
        text = _put(r"(?m)^share = .*$", "share = %s" % share, text)
        text = _put(r"(?m)^username = .*$",
                    "username = %s\nauth_mode = %s" % (username, mode), text)
        if folders:
            block = "folders =\n" + "".join("    %s\n" % f for f in folders)
            text = _put(r"(?ms)^folders =\n(?:    .*\n)*", block, text)
        return text

    def cmd_install(self, backend_dir, host=None, share=None, mode=None,
                    username=None, password=None, folders=None, verify=True,
                    adopt=False, refresh=False):
        # type: (str, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[list], bool, bool, bool) -> int
        """GUI-driven installer (port of install.sh). Idempotent: re-running
        refreshes the backend files, keeps an existing config.ini's [backup]
        section, re-renders the LaunchAgents and restarts the daemon.

        Steps: preflight -> stop a running agent (never swap .py files under a
        live daemon) -> lay out <APP> and copy the backend from `backend_dir`
        (the app bundle's Resources/backend) -> render config.ini from the
        template on first install -> credentials + verification mount +
        sentinel through cmd_server_set (same guards, same adoption rule) ->
        watchdog.sh + LaunchAgent plists + CLI shim -> bootstrap both agents ->
        wait for the daemon to report online. The one step macOS forbids
        automating -- Full Disk Access for /usr/bin/python3 -- is reported as
        `fda_hint` so the GUI can guide the user through it.
        """
        backend_dir = os.path.abspath(backend_dir or "")
        if not os.path.isdir(backend_dir):
            return self._emit_error("找不到后端目录：%s" % backend_dir)
        if not os.path.exists(self.rsync_bin):
            return self._emit_error(
                "系统缺少 rsync（%s）：请先安装命令行工具（Command Line Tools）。" % self.rsync_bin)
        mode_n = (mode or "").strip().lower()
        if refresh:
            # Backend-only update: keep every connection setting as it is.
            if not self._config_is_provisioned():
                return self._emit_error(
                    "尚未完成过安装，无法只更新后端；请先完成安装向导。")
        elif mode_n not in self._AUTH_MODES:
            return self._emit_error("连接模式必须是 user 或 anonymous：%r" % mode)
        elif not (host or "").strip() or not (share or "").strip():
            return self._emit_error("安装需要服务器地址与共享名。")
        eff_user = "guest" if mode_n == "anonymous" else (username or "").strip()
        notes = []  # type: list
        stopped = False
        completed = False
        try:
            if self._agent_loaded():
                self._quiet(self.cmd_pause)
                stopped = True   # resume in the finally even if the wait fails
                if not self._wait_agent_stopped():
                    raise _ServerSetAbort("正在运行的守护进程未能在限时内停止，请稍后重试。")
                notes.append("已停止旧的守护进程以更新后端文件。")
            for d in (self.app_dir, self.runtime_dir, self.logdir,
                      os.path.join(self.app_dir, "bin"), self.launch_agents_dir):
                os.makedirs(d, exist_ok=True)
            os.makedirs(self.mnt, exist_ok=True)
            if not os.path.ismount(self.mnt):
                # Bare mountpoint stays 0500: while the share is not mounted a
                # stray write fails with EACCES instead of filling the boot disk.
                os.chmod(self.mnt, 0o500)
            self._copy_backend(backend_dir)
            # A config.ini without a sentinel_uuid belongs to an install that
            # never completed (the previous attempt failed at verification):
            # re-render it, or the sources the user just corrected in the
            # wizard would be silently ignored.
            if refresh:
                notes.append("仅更新后端文件，连接设置与备份源保持不变。")
            elif self._config_is_provisioned():
                notes.append("保留现有 config.ini（备份源与阈值未改动）。")
            else:
                text = self._render_config_template(
                    self._read_backend_file(backend_dir, "config.ini.template"),
                    host.strip(), share.strip().strip("/"), eff_user, mode_n,
                    [str(_resolve_path(f)) for f in (folders or [])])
                util.atomic_write_text(self.config_path, text)
                notes.append("已生成 config.ini。")

            # credentials + verification mount + sentinel: the server-set contract
            rc, out = (0, "") if refresh else self._quiet(lambda: self.cmd_server_set(
                host, share, mode_n, username=username, password=password,
                verify=verify, adopt=adopt))
            try:
                payload = json.loads(out) if out else {}
            except ValueError:
                payload = {}
            if rc != 0:
                if payload.get("needs_confirm"):
                    self._emit_json(payload)   # pass the adoption question through
                    return 1
                raise _ServerSetAbort(payload.get("error") or "连接配置失败。")
            if payload.get("notice"):
                notes.append(payload["notice"])

            # watchdog + LaunchAgents + CLI shim
            try:
                self._write_executable(
                    os.path.join(self.app_dir, "watchdog.sh"),
                    self._render_template(self._read_backend_file(backend_dir, "watchdog.sh"),
                                          self.app_dir, self.logdir))
                util.atomic_write_text(
                    self.agent_plist,
                    self._render_template(
                        self._read_backend_file(backend_dir, "com.mycloud.agent.plist.template"),
                        self.app_dir, self.logdir))
                util.atomic_write_text(
                    self.watchdog_plist,
                    self._render_template(
                        self._read_backend_file(backend_dir, "com.mycloud.watchdog.plist.template"),
                        self.app_dir, self.logdir))
                self._write_executable(
                    os.path.join(self.app_dir, "bin", "mycloudctl"),
                    "#!/bin/sh\nexec /usr/bin/python3 \"%s\" \"$@\"\n"
                    % os.path.join(self.app_dir, "mycloudctl.py"))
            except OSError as exc:
                raise _ServerSetAbort("写入启动配置失败：%s" % exc)
            if not self._bootstrap_plist(self.watchdog_plist, "com.mycloud.watchdog"):
                notes.append("警告：看门狗代理加载失败（不影响备份）。")
            self._mark_restarting()
            restart_t0 = self.now_fn()
            rc_resume, _msg = self._quiet(self.cmd_resume)
            if rc_resume != 0:
                raise _ServerSetAbort("守护进程启动失败（launchctl）。请查看日志。")
            stopped = False   # the daemon is up again; nothing to undo
            completed = True
            notes.append("守护进程已启动。")
        except _ServerSetAbort as exc:
            return self._emit_error(str(exc))
        except (OSError, re.error, configparser.Error) as exc:
            # The GUI parses stdout as JSON: an unexpected error must still
            # answer with one (it used to die with a traceback on stderr only).
            return self._emit_error("安装失败：%s" % exc)
        finally:
            # An aborted re-install must never leave the machine with a daemon
            # we stopped: back up it goes, on the OLD configuration.
            if stopped and not completed:
                self._quiet(self.cmd_resume)

        online = self._wait_daemon_online(timeout_s=45.0, since=restart_t0)
        fda_hint = False
        if online:
            notes.append("守护进程已上线（%s）。" % online)
        else:
            guard = (self.store.get() or {}).get("guard") or {}
            detail = str(guard.get("detail", "")) if isinstance(guard, dict) else ""
            fda_hint = ("Operation not permitted" in detail) or not detail
            notes.append("守护进程尚未上线——通常是「完全磁盘访问」还没授予 /usr/bin/python3；"
                         "在概览页按提示授权后重启守护进程即可。")
        self._emit_json({"installed": True, "app_dir": self.app_dir,
                         "online": online, "fda_hint": fda_hint,
                         "notice": "\n".join(notes)})
        return 0

    def cmd_uninstall(self, keep_config=True, delete_keychain=False,
                      strip_nsmb=False):
        # type: (bool, bool, bool) -> int
        """GUI-driven uninstaller (port of uninstall.sh). Removes the local
        agents, plists, CLI shim and <APP>; backs up config.ini + logs by
        default. NEVER touches the NAS: the hard gate refuses to rm -rf while
        the share is still mounted under <APP>/mnt."""
        notes = []  # type: list
        cfg = self._load_config()
        host = cfg.host if (cfg is not None and cfg.host) else ""
        user = cfg.username if (cfg is not None and cfg.username) else ""
        for label in ("com.mycloud.agent", "com.mycloud.watchdog"):
            self.run_cmd([paths.LAUNCHCTL, "bootout", "gui/%d/%s" % (self.uid, label)], 30)
        # bootout returns before the daemon is gone, and a mount_smbfs it had
        # already started runs in its own session -- it can finish mounting the
        # share onto <APP>/mnt AFTER any check made now, right before the local
        # tree is deleted. Wait for both to be over; if they will not end,
        # delete nothing.
        if not self._wait_agent_stopped() or not self._wait_no_mount_in_flight():
            self._quiet(self.cmd_resume)
            return self._emit_error(
                "守护进程或它发起的挂载仍在运行；为避免波及 NAS，未删除任何文件"
                "（备份已恢复运行），请稍后重试。")
        if not self._release_daemon_mount():
            # Nothing was deleted. Put the daemon back so the machine keeps
            # backing up while the user deals with the wedged mount.
            self._quiet(self.cmd_resume)
            return self._emit_error(
                "无法卸载 %s 上的共享；为避免误删 NAS 上的备份，已停止卸载"
                "（本地与 NAS 文件均未删除，备份已恢复运行）。请重启电脑后重试。" % self.mnt)
        for plist in (self.agent_plist, self.watchdog_plist):
            try:
                os.remove(plist)
            except OSError:
                pass
        symlink = "/usr/local/bin/mycloudctl"
        try:
            if os.path.islink(symlink) and os.readlink(symlink).startswith(self.app_dir + "/"):
                os.remove(symlink)
        except OSError:
            pass
        if keep_config:
            bak = self.app_dir + ".bak-" + datetime.now().strftime("%Y%m%d")
            try:
                os.makedirs(bak, exist_ok=True)
                if os.path.isfile(self.config_path):
                    shutil.copy2(self.config_path, os.path.join(bak, "config.ini"))
                if os.path.isdir(self.logdir):
                    dst = os.path.join(bak, "logs")
                    util.remove_local_tree(dst)
                    shutil.move(self.logdir, dst)
            except OSError as exc:
                notes.append("备份配置/日志失败：%s" % exc)
            else:
                notes.append("配置与日志已备份到 %s" % bak)
        else:
            try:
                util.remove_local_tree(self.logdir)
            except OSError as exc:
                notes.append("删除日志失败：%s" % exc)
        real = os.path.realpath(self.mnt)
        if os.path.ismount(real) or self._mount_table_lists(real):
            return self._emit_error(
                "挂载点仍处于挂载状态，拒绝删除本地目录（可能波及 NAS 数据）。")
        try:
            # Structural hard gate: remove_local_tree raises EBUSY at any
            # mountpoint inside <APP> instead of recursing into a live share.
            util.remove_local_tree(self.app_dir)
        except OSError as exc:
            return self._emit_error(
                "删除本地目录被拒绝（目录内仍有挂载点，可能波及 NAS 数据）：%s" % exc)
        if delete_keychain and host and user and user != "guest":
            # Match MyCloud's own item exactly (label + protocol, as stored by
            # _store_keychain_password): host+account alone could delete the
            # user's Finder "remember password" item and keep ours.
            self.run_cmd([paths.SECURITY, "delete-internet-password",
                          "-s", host, "-a", user, "-r", "smb ",
                          "-l", "MyCloud NAS"], 30)
            notes.append("已删除钥匙串中的 NAS 密码。")
        if strip_nsmb and host:
            self._rewrite_nsmb_conf(host, [])
            notes.append("已移除 nsmb.conf 中该服务器的凭据。")
        notes.append("NAS 上的备份数据未作任何改动。")
        self._emit_json({"uninstalled": True, "notice": "\n".join(notes)})
        return 0

    # -- mount (GUI destination browsing) ----------------------------------

    def _bare_dir_problem(self, mm):
        # type: (Any) -> Optional[str]
        """The daemon's rule: never mount over stray local data in the bare
        mountpoint (it would be hidden under the share, then lost). A missing
        precheck method (test doubles) counts as clean."""
        check = getattr(mm, "ensure_bare_dir_safe", None)
        if check is None:
            return None
        try:
            check()
        except Exception as exc:
            name = type(exc).__name__
            if name == "CriticalLocalWriteDetected":
                return ("挂载点 %s 里有本地文件（离线时误写入），为保护这些文件拒绝挂载；"
                        "请先把它们移走。" % self.mnt)
            if name == "MountError":
                return None   # something is mounted there: mount() handles it
            return "无法检查挂载点 %s：%s" % (self.mnt, exc)
        return None

    def cmd_mount(self):
        # type: () -> int
        cfg = self._load_config()
        if cfg is None or not cfg.host or not cfg.share:
            return self._emit_error(
                "config.ini 中没有 [server] host/share，无法挂载。")
        try:
            mm = self.mount_factory(cfg)
            if mm.is_mounted():
                self._emit_json({"mounted": True, "mountpoint": self.mnt})
                return 0
            problem = self._bare_dir_problem(mm)
            if problem:
                return self._emit_error(problem)
            if not mm.probe_reachable():
                return self._emit_error(
                    "NAS %s 不可达（设备离线；直连 445 与 SMB 代理探测均失败）。"
                    % cfg.host)
            if not mm.mount():
                return self._emit_error(
                    "挂载 //%s/%s 失败（mount_smbfs）。" % (cfg.host, cfg.share))
            # guard-lite: mount table entry present and sentinel matches.
            if not mm.is_mounted():
                return self._emit_error(
                    "已挂载，但挂载表中未出现该共享。")
            if not mm.sentinel_ok():
                return self._emit_error(
                    "已挂载，但哨兵文件不匹配；这可能不是 MyCloud 的共享。")
            self._emit_json({"mounted": True, "mountpoint": self.mnt})
            return 0
        except Exception as exc:
            return self._emit_error("挂载失败：%s" % exc)

    # -- approve-deletions -------------------------------------------------

    def cmd_approve_deletions(self, assume_yes=False):
        # type: (bool) -> int
        state = self.store.get()
        folders = state.get("folders")
        if not isinstance(folders, dict):
            folders = {}

        held = []  # type: list
        for name in sorted(folders):
            entry = folders[name] if isinstance(folders[name], dict) else {}
            count, paths_list = _held_of(entry)
            if count > 0 or paths_list:
                held.append((name, count, paths_list))

        total = sum(max(c, len(p)) for _n, c, p in held)

        # --yes is the GUI path: it must emit the {"...":...}/{"error":...} JSON
        # envelope like every other 12.2 command (the GUI confirms in its own
        # dialog first, so no interactive prompt here).
        if assume_yes:
            if total == 0:
                return self._emit_error("当前没有待批准的删除。")
            util.atomic_write_text(
                self.approval_path, self.now_fn().isoformat() + "\n")
            signalled, _msg = self._signal_daemon(signal.SIGUSR1)
            self._emit_json({
                "approved": total,
                "folders": dict(
                    (name, list(paths_list)) for name, _c, paths_list in held),
                "signalled": bool(signalled),
            })
            return 0

        # Interactive CLI path: human-readable listing + confirmation prompt.
        if total == 0:
            self._p("Nothing is held: there are no deletions awaiting approval.")
            return 1

        self._p("The following deletions are held and will be moved to the NAS trash "
                "on approval:")
        for name, count, paths_list in held:
            self._p("")
            self._p("  [%s] %d deletion(s):" % (name, max(count, len(paths_list))))
            if paths_list:
                for rel in paths_list:
                    self._p("    %s" % rel)
            else:
                self._p("    (paths not recorded in state; %d file(s))" % count)

        self._p("")
        answer = self.input_fn("Type 'yes' to approve these deletions: ")
        if (answer or "").strip().lower() != "yes":
            self._p("Aborted. No deletions were approved.")
            return 1

        util.atomic_write_text(self.approval_path, self.now_fn().isoformat() + "\n")
        self._p("Approval token written: %s" % self.approval_path)
        ok, message = self._signal_daemon(signal.SIGUSR1)
        if ok:
            self._p(message)
        else:
            self._p("Note: %s The token will be honored when it next runs." % message)
        return 0

    # -- pause / resume ----------------------------------------------------

    def _domain_target(self):
        # type: () -> str
        return "gui/%d/%s" % (self.uid, AGENT_LABEL)

    def _service_target(self):
        # type: () -> str
        return "gui/%d" % self.uid

    def _agent_loaded(self):
        # type: () -> bool
        """True iff launchd currently has the agent loaded.

        `launchctl print <domain-target>` exits 0 only for a loaded service.
        Loaded is independent of the daemon PROCESS being alive: a crashed
        daemon leaves the agent loaded (KeepAlive restarts it), while pause
        boots the whole agent out.
        """
        result = self.run_cmd(
            [paths.LAUNCHCTL, "print", self._domain_target()], 30)
        return (not getattr(result, "timed_out", False)
                and getattr(result, "rc", 1) == 0)

    def _wait_daemon_alive(self, timeout_s=10.0, poll_s=0.5):
        # type: (float, float) -> bool
        """Poll for the daemon's flock after asking launchd to start it.

        bootstrap of a RunAtLoad agent (and kickstart) return before the
        daemon has necessarily reached its lock, and launchctl itself can
        overrun its budget while the daemon comes up fine behind it -- on
        the macOS 12 gateway's first start with a freshly migrated 161MB
        manifest (2026-08-01) resume declared failure for a healthy daemon.
        """
        attempts = max(1, int(round(timeout_s / poll_s)))
        for i in range(attempts):
            if self._daemon_alive():
                return True
            if i + 1 < attempts:
                self.sleep_fn(poll_s)
        return self._daemon_alive()

    def cmd_pause(self):
        # type: () -> int
        """Boot the agent out. IDEMPOTENT: pausing an already-paused MyCloud
        succeeds with an explanation instead of a bare launchctl error --
        clicking 暂停 twice used to print "Could not pause (rc=3)" for the
        second click (2026-07-31 user report, same class as the resume bug)."""
        if not self._agent_loaded():
            self._p("MyCloud 已处于暂停状态（LaunchAgent 未加载），无需再次暂停。")
            return 0
        result = self.run_cmd(
            [paths.LAUNCHCTL, "bootout", self._domain_target()], 30)
        if getattr(result, "timed_out", False) or getattr(result, "rc", 0) != 0:
            rc = "timeout" if getattr(result, "timed_out", False) else result.rc
            self._p("暂停失败（launchctl bootout rc=%s）。请查看日志。" % rc)
            return 1
        self._p("已暂停：守护进程已停止，登录后也不会自启。"
                "恢复备份请点「恢复」或运行 mycloudctl resume。")
        return 0

    def cmd_resume(self):
        # type: () -> int
        """Start (or confirm) the agent. IDEMPOTENT: clicking 恢复 while the
        daemon is already running used to fail with "launchctl bootstrap rc=5"
        (EALREADY) and do nothing useful -- the 2026-07-31 user report. Now:
        loaded + alive -> report that, no action; loaded + dead -> kickstart;
        not loaded -> bootstrap, verify liveness, kickstart only as fallback.

        The agent is RunAtLoad, so bootstrap alone starts the daemon. The old
        unconditional kickstart -k after bootstrap KILLED that fresh daemon
        mid-startup and restarted it; on the macOS 12 gateway's slow first
        start (freshly migrated 161MB manifest) the kill+restart overran the
        30s launchctl budget and resume reported failure for a perfectly
        healthy daemon (2026-08-01 field report). Any kickstart error is now
        followed by a liveness probe before declaring failure, and the
        fallback kickstart carries no -k (nothing should die for it)."""
        if self._agent_loaded():
            if self._daemon_alive():
                self._p("守护进程已在运行，无需恢复。")
                return 0
            kick = self.run_cmd(
                [paths.LAUNCHCTL, "kickstart", "-k", self._domain_target()], 30)
            if getattr(kick, "timed_out", False) or getattr(kick, "rc", 0) != 0:
                if self._wait_daemon_alive():
                    self._p("守护进程已重新启动（kickstart 未及时返回，"
                            "已通过存活探测确认）。")
                    return 0
                rc = "timeout" if getattr(kick, "timed_out", False) else kick.rc
                self._p("守护进程未响应且重启失败（launchctl kickstart rc=%s）。"
                        "请查看日志。" % rc)
                return 1
            self._p("守护进程未在运行，已重新启动。")
            return 0
        if not self._bootstrap_agent(self.agent_plist, _AGENT_LABEL):
            self._p("恢复失败（launchctl bootstrap 未能加载守护进程）。请查看日志。")
            return 1
        if self._wait_daemon_alive():
            self._p("已恢复：守护进程已启动。")
            return 0
        kick = self.run_cmd(
            [paths.LAUNCHCTL, "kickstart", self._domain_target()], 30)
        if getattr(kick, "timed_out", False) or getattr(kick, "rc", 0) != 0:
            if self._wait_daemon_alive():
                self._p("已恢复：守护进程已启动（kickstart 未及时返回，"
                        "已通过存活探测确认）。")
                return 0
            rc = "timeout" if getattr(kick, "timed_out", False) else kick.rc
            self._p("LaunchAgent 已加载，但启动守护进程失败（kickstart rc=%s）。" % rc)
            return 1
        self._p("已恢复：LaunchAgent 已加载，守护进程启动中（可用 status/doctor 复核）。")
        return 0

    # -- logs --------------------------------------------------------------

    def cmd_logs(self, follow=False):
        # type: (bool) -> int
        self._p(self.log_path)
        if follow:
            argv = [TAIL, "-f", self.log_path]
        else:
            argv = [TAIL, "-n", "200", self.log_path]
        self.execv(TAIL, argv)
        # execv only returns if it was stubbed (tests) or failed.
        return 0

    # -- materialize (download cloud-only iCloud files) ---------------------

    def _scan_dataless(self, root):
        # type: (str) -> list
        """Return [(relpath, abspath, size)] for every dataless file under root.

        lstat-only scandir walk with an explicit stack (same shape as
        manifest._walk_scope): a file is NEVER opened here, because an open is
        exactly what downloads it -- the scan must only count. OSError is
        swallowed per directory/entry so one unreadable folder cannot abort the
        scan (it just contributes nothing).

        macOS <= 13 has no dataless files: an evicted file X is a hidden
        ".X.icloud" placeholder (a small local plist, safe to read for the real
        size) and X itself is absent. Those are listed under X's path, which
        icloud.materialize_batch downloads with brctl.
        """
        from mycloud import manifest as manifest_mod
        found = []  # type: list
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as scan:
                    for entry in scan:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                                continue
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        target = manifest_mod.stub_target(entry.name)
                        if target is not None and manifest_mod._is_stub_stat(st):
                            tpath = os.path.join(current, target)
                            if not os.path.lexists(tpath):
                                size = manifest_mod.stub_logical_size(entry.path)
                                found.append((os.path.relpath(tpath, root), tpath,
                                              int(size or 0)))
                            continue
                        try:
                            if not self.dataless_check(entry.path, st):
                                continue
                        except OSError:
                            continue
                        found.append((os.path.relpath(entry.path, root),
                                      entry.path, int(st.st_size)))
            except OSError:
                continue
        found.sort(key=lambda item: item[0])   # deterministic --limit truncation
        return found

    def cmd_materialize(self, limit=None, dry_run=False):
        # type: (object, bool) -> int
        """Download cloud-only (dataless) iCloud files so they can be backed up.

        WHY (2026-07-30): a launchd-managed daemon cannot trigger iCloud
        downloads -- fileproviderd refuses, the daemon has persisted
        state.prefetch.blocked -- so under icloud_policy=skip those files are
        never backed up at all (967 of them on this machine). A USER-CONTEXT
        process can download them (verified repeatedly), which is what this
        command is: it materializes the files here, in your terminal, and the
        daemon then picks them up on its normal cadence.

        The scan is lstat-only (never opens a file); the download itself reuses
        icloud.materialize_batch, whose per-file read trigger is a time-bounded
        SUBPROCESS read, so a wedged iCloud read cannot hang this CLI.
        """
        cfg = self._load_config()
        if cfg is None:
            self._p("无法读取配置文件（%s），请先修复配置。" % self.config_path)
            return 1

        gib = float(2 ** 30)
        entries = []   # type: list
        roots = []     # type: list
        for folder in getattr(cfg, "folders", []):
            if getattr(folder, "is_file", False):
                continue           # single-file sources: nothing to walk
            root = str(folder.src)
            hits = self._scan_dataless(root)
            if hits:
                roots.append(root)
            entries.extend(hits)

        total_files = len(entries)
        total_bytes = sum(size for _rel, _abs, size in entries)
        if total_files == 0:
            self._p("没有需要下载的云端文件。")
            return 0
        self._p("发现 %d 个仅云端文件，共 %.1f GB。" % (total_files, total_bytes / gib))
        if dry_run:
            self._p("（--dry-run：未下载任何文件。）")
            return 0

        selected = entries
        if limit is not None and int(limit) < total_files:
            selected = entries[:max(0, int(limit))]
            self._p("按 --limit 只处理前 %d 个。" % len(selected))
        if not selected:
            self._p("--limit 为 0，未下载任何文件。")
            return 0
        need_bytes = sum(size for _rel, _abs, size in selected)

        # Disk guard: downloading fills the boot disk, and a full boot disk is a
        # far worse outcome than an un-backed-up cloud file. Any probe failure is
        # treated as "not enough room" (fail safe).
        guard_root = roots[0]
        try:
            free = int(self.free_bytes_fn(guard_root))
        except OSError as exc:
            self._p("无法检查磁盘可用空间（%s）：%s；已放弃下载。" % (guard_root, exc))
            return 1
        if free < _MATERIALIZE_FREE_FACTOR * need_bytes:
            self._p("磁盘可用空间不足：需要 %.1f GB（待下载 %.1f GB 的 %d 倍），"
                    "当前仅剩 %.1f GB。请先清理磁盘后重试。"
                    % (_MATERIALIZE_FREE_FACTOR * need_bytes / gib,
                       need_bytes / gib, _MATERIALIZE_FREE_FACTOR, free / gib))
            return 1

        # Never set: the CLI has no signal handling, and Ctrl-C interrupts the
        # run naturally. materialize_batch still needs the parameter to bound
        # its own waits.
        stop_event = threading.Event()
        size_of = dict((abspath, size) for _rel, abspath, size in selected)
        ok_paths = []      # type: list
        failed_paths = []  # type: list
        done_bytes = 0
        total = len(selected)
        for start in range(0, total, _MATERIALIZE_BATCH):
            chunk = [abspath for _rel, abspath, _size
                     in selected[start:start + _MATERIALIZE_BATCH]]
            # is_dataless_fn is called with the path alone (the st is unknown
            # after a download), which icloud.is_dataless accepts.
            materialized, failed = self.materialize_fn(
                chunk, is_dataless_fn=self.dataless_check,
                stop_event=stop_event, run_cmd=self.run_cmd)
            ok_paths.extend(materialized)
            failed_paths.extend(failed)
            done_bytes += sum(size_of.get(p, 0) for p in materialized)
            self._p("已下载 %d/%d（%.1f GB）"
                    % (len(ok_paths), total, done_bytes / gib))

        self._p("完成：成功 %d 个，失败 %d 个。"
                % (len(ok_paths), len(failed_paths)))
        if failed_paths:
            self._p("失败文件（最多列出 10 个）：")
            for path in failed_paths[:10]:
                self._p("  %s" % path)
        self._p("守护进程将在 full_interval 内自动备份这些文件；"
                "可运行 mycloudctl sync-now 立即触发。")
        return 1 if failed_paths else 0

    # -- doctor ------------------------------------------------------------

    def _probe_smb_reachable(self, host, user, timeout=8.0):
        # type: (str, str, float) -> bool
        """Brokered reachability probe: smbutil view -N //<user>@<host>.

        Goes through the SMB subsystem (a system service), so it reaches the NAS
        even when a direct python3 socket is refused by the macOS 26 "Local
        Network" privacy gate -- mirroring MountManager.probe_smb_view so doctor
        agrees with the daemon. Uses the run_cmd seam so tests stay hermetic; -N
        keeps any password off argv. Merely getting a share listing proves
        reachability, which is all this row asserts.
        """
        if not host:
            return False
        target = "//" + (user or "guest") + "@" + host
        try:
            result = self.run_cmd([paths.SMBUTIL, "view", "-N", target], timeout)
        except Exception:
            return False
        return (not getattr(result, "timed_out", False)) and getattr(
            result, "rc", 1) == 0

    def _power_sleep_status(self):
        # type: () -> Tuple[bool, str, str]
        """(ok, detail, hint) for the idle-sleep setting of the CURRENT power
        source. Fails only on AC power with system sleep enabled; on battery
        sleeping is expected; unreadable settings are not reported as a fault."""
        try:
            batt = self.run_cmd([paths.PMSET, "-g", "batt"], 10)
            cfg = self.run_cmd([paths.PMSET, "-g"], 10)
        except OSError:
            return True, "无法读取电源设置", ""
        if (getattr(cfg, "timed_out", False) or getattr(cfg, "rc", 1) != 0
                or getattr(batt, "timed_out", False) or getattr(batt, "rc", 1) != 0):
            return True, "无法读取电源设置", ""
        on_ac = "AC Power" in (getattr(batt, "out", "") or "")
        sleep_min = None
        disabled = False
        for line in (getattr(cfg, "out", "") or "").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "sleep" and fields[1].isdigit():
                sleep_min = int(fields[1])
            if len(fields) >= 2 and fields[0] == "SleepDisabled" and fields[1] == "1":
                disabled = True
        if disabled or sleep_min == 0:
            return True, "系统睡眠已关闭", ""
        if not on_ac:
            return True, ("正在使用电池（%s 分钟后睡眠）；作为常开网关请接通电源"
                          % sleep_min if sleep_min is not None else "正在使用电池"), ""
        return (False,
                "接通电源时 %s 分钟无操作就会睡眠" % sleep_min if sleep_min is not None
                else "接通电源时仍会自动睡眠",
                "每次睡眠都会断开 NAS 连接、打断正在进行的备份。macOS 13+：系统设置 → 电池 → "
                "选项 → 开启「使用电源适配器时防止自动进入睡眠」（台式机在「节能」）；macOS 12："
                "系统偏好设置 → 电池 → 电源适配器 → 勾选「显示器关闭时，防止电脑自动进入睡眠」。"
                "或终端执行 sudo pmset -c sleep 0。合盖运行还需要外接显示器。")

    def _df_available_bytes(self, path, timeout=10):
        # type: (str, float) -> Optional[int]
        """Free bytes of the filesystem holding `path`, via `df -kP` in a timed
        child; None if it times out or cannot be parsed."""
        try:
            res = self.run_cmd([paths.DF, "-kP", path], timeout)
        except OSError:
            return None
        if getattr(res, "timed_out", False) or getattr(res, "rc", 1) != 0:
            return None
        lines = [ln for ln in (getattr(res, "out", "") or "").splitlines() if ln.strip()]
        if len(lines) < 2:
            return None
        fields = lines[-1].split()
        try:
            return int(fields[3]) * 1024
        except (IndexError, ValueError):
            return None

    def _du_bytes(self, path, timeout=20):
        # type: (str, float) -> Optional[int]
        """Size of the tree at `path` via `du -sk` in a timed child: 0 when it
        does not exist, None when it could not be measured in time."""
        try:
            res = self.run_cmd([paths.DU, "-sk", path], timeout)
        except OSError:
            return None
        if getattr(res, "timed_out", False):
            return None
        out = getattr(res, "out", "") or ""
        if getattr(res, "rc", 1) != 0:
            return 0 if "No such file" in (getattr(res, "err", "") or "") else None
        try:
            return int(out.split()[0]) * 1024
        except (IndexError, ValueError):
            return None

    def cmd_doctor(self, as_json=False):
        # type: (bool) -> int
        results = self._doctor_checks()
        if as_json:
            self._emit_json(results)
            return 0 if all(r["ok"] for r in results) else 1

        failures = 0
        for r in results:
            if r["ok"]:
                self._p("[ok]   %s" % r["name"])
            else:
                failures += 1
                if r["hint"]:
                    self._p("[FAIL] %s -- %s" % (r["name"], r["hint"]))
                else:
                    self._p("[FAIL] %s" % r["name"])
            if r["detail"]:
                self._p("       %s" % r["detail"])
        if failures:
            self._p("")
            self._p("doctor: %d check(s) FAILED." % failures)
            return 1
        self._p("")
        self._p("doctor: all checks passed.")
        return 0

    def _doctor_checks(self):
        # type: () -> list
        """Run every diagnostic check and return a list of result dicts.

        Each dict is {"id","name","ok","detail","hint","fix_id","fix_label"}
        (PLAN 12.2 + A4). fix_id/fix_label are non-null only for a FAILED row that
        has a registered one-click remedy. Both the human-readable and --json
        doctor renderings consume this single list.
        """
        results = []  # type: list

        def check(cid, ok, label, detail="", hint="", fix_id=None):
            row = {
                "id": cid,
                "name": label,
                "ok": bool(ok),
                "detail": detail,
                "hint": hint,
                "fix_id": None,
                "fix_label": None,
            }
            # A remediation is surfaced only when the row FAILED and an id was
            # supplied; the label is looked up from the pinned registry.
            if not row["ok"] and fix_id:
                row["fix_id"] = fix_id
                row["fix_label"] = _FIX_LABELS.get(fix_id)
            results.append(row)

        state = self.store.get()
        cfg = self._load_config()

        # Shared SMB mount-table read. Our-share presence (check 4), leftover
        # verification mounts (4b), the free-space guard (check 10), and the
        # Local-Network heuristic (6b) all derive from this single bounded call,
        # so the table is parsed once and consistently.
        mres = self.run_cmd([paths.MOUNT, "-t", "smbfs"], 10)
        mount_lines = []  # type: list
        if not getattr(mres, "timed_out", False) and getattr(mres, "rc", 1) == 0:
            mount_lines = (getattr(mres, "out", "") or "").splitlines()
        mounted_entry = False
        for line in mount_lines:
            if (" on " + self.mnt + " ") in line or line.endswith(" on " + self.mnt):
                mounted_entry = True
                break
            if self.mnt in line and "smbfs" in line:
                mounted_entry = True
                break
        stale_verify = []  # type: list
        for line in mount_lines:
            on_idx = line.find(" on ")
            par_idx = line.rfind(" (")
            if on_idx == -1 or par_idx == -1 or par_idx <= on_idx:
                continue
            mp = line[on_idx + 4:par_idx]
            if os.path.basename(mp).startswith("mycloud_verify."):
                stale_verify.append(mp)

        # 1) python3 3.9.x + Command Line Tools present.
        ver = sys.version_info
        ver_ok = (ver[0] == 3 and ver[1] == 9)
        clt = self.run_cmd([XCODE_SELECT, "-p"], 10)
        clt_ok = (not getattr(clt, "timed_out", False)) and getattr(clt, "rc", 1) == 0
        check("python_clt", ver_ok and clt_ok,
              "python3 为 3.9.x（%d.%d.%d）且已安装命令行工具（Command Line Tools）" % (
                  ver[0], ver[1], ver[2]),
              hint="目标 /usr/bin/python3 必须是 3.9.x；运行 `xcode-select --install` "
              "以恢复命令行工具（Command Line Tools）")

        # 2) config loads.
        if cfg is None:
            check("config_loads", False, "config.ini 可正常加载",
                  hint="请修复 config.ini（参见 `mycloudctl status`）")
        else:
            check("config_loads", True, "config.ini 可正常加载",
                  detail="; ".join(cfg.warnings))

        # 3) NAS reachable: direct TCP-445 socket first, then a brokered SMB
        #    fallback (smbutil) that survives the macOS 26 "Local Network" block
        #    on a launchd python3's direct sockets -- the daemon uses the same
        #    two-step probe, so a socket-only FAIL here would misrepresent a
        #    NAS that the daemon can (and does) reach via the SMB subsystem.
        host = cfg.host if (cfg is not None and cfg.host) else ""
        user = cfg.username if (cfg is not None and cfg.username) else ""
        if not host:
            check("tcp445", False, "TCP 445 可达性",
                  hint="config.ini 的 [server] 中未配置 host")
        else:
            socket_ok = False
            try:
                socket_ok = self.probe_tcp(host)
            except Exception:
                socket_ok = False
            if socket_ok:
                check("tcp445", True, "TCP 445 可达：%s" % host)
            elif self._probe_smb_reachable(host, user):
                check("tcp445", True,
                      "NAS 可达：%s（直连 445 socket 被拦，经 SMB 子系统代理可达）"
                      % host,
                      detail="直连 socket 探测被 macOS 拦截（后台 python3 拿不到"
                      "「本地网络」授权，且系统不会在该设置面板中为它提供开关——"
                      "这不是需要、也不是能够手动操作的项）。守护进程已自动改用 "
                      "SMB 子系统代理探测，同步不受影响，无需任何操作。")
            else:
                # Both probes failed. Deliberately NOT mentioning the Local
                # Network pane here: macOS never lists a bare launchd python3
                # there (no entry, no + button), so pointing users at it sends
                # them hunting for a switch that does not exist (2026-07-31
                # user report). A blocked-but-healthy machine takes the branch
                # above; landing HERE means the NAS itself is unreachable.
                check("tcp445", False, "NAS 不可达：%s" % host,
                      hint="直连 445 与 SMB 子系统代理探测均失败——NAS 已离线、"
                      "断电或不在当前网段。请检查设备电源与网络连接。")

        # 4) mount table entry present (uses the shared mount-table read above).
        check("mount_present", mounted_entry, "SMB 共享已挂载于 %s" % self.mnt,
              hint="守护进程会按需挂载；若持续未挂载，请检查网络连通性以及 "
              "`mycloudctl doctor` 的第 3、6 项检查",
              fix_id="mount_share")

        # 4b) no leftover install verification mounts (mycloud_verify.*). A stale
        # one of the same share makes mount_smbfs fail with "File exists" (rc=64).
        if stale_verify:
            check("stale_verify_mounts", False,
                  "存在遗留的安装校验挂载（mycloud_verify.*）",
                  detail="; ".join(stale_verify),
                  hint="上次安装可能异常退出，遗留了校验挂载点；这会让守护进程自己的 "
                  "mount_smbfs 因“File exists”(rc=64) 失败。用 `umount <路径>`"
                  "（失败则 `diskutil unmount force <路径>`）清理，或重新运行 "
                  "install.sh（它会在启动时自动清理这些挂载）。",
                  fix_id="cleanup_stale_mounts")
        else:
            check("stale_verify_mounts", True,
                  "没有遗留的安装校验挂载（mycloud_verify.*）")

        # 5) sentinel readable + matches configured UUID.
        want_uuid = cfg.sentinel_uuid if cfg is not None else ""
        sentinel_path = os.path.join(self.mnt, paths.SENTINEL_REL)
        sres = self.run_cmd([paths.CAT, sentinel_path], 10)
        sentinel_ok = (
            not getattr(sres, "timed_out", False)
            and getattr(sres, "rc", 1) == 0
            and bool(want_uuid)
            and (getattr(sres, "out", "") or "").strip() == want_uuid
        )
        check("sentinel", sentinel_ok, "挂载哨兵文件存在且与安装 UUID 匹配",
              hint="哨兵不匹配意味着挂载点上可能是另一个（非本程序）的共享；此处"
              "刻意不提供一键修复：自动重挂或写入哨兵有指向错误共享、覆盖他人数据的"
              "风险。请人工确认挂载目标无误后重新运行 install.sh 以（重新）写入哨兵文件。")

        # 6) smbutil statshares session alive.
        share = cfg.share if (cfg is not None and cfg.share) else ""
        stat_res = self.run_cmd([paths.SMBUTIL, "statshares", "-m", self.mnt], 10)
        session_ok = (
            not getattr(stat_res, "timed_out", False)
            and getattr(stat_res, "rc", 1) == 0
            and (not share or share in (getattr(stat_res, "out", "") or ""))
        )
        check("smb_session", session_ok, "SMB 会话存活（smbutil statshares）",
              hint="没有存活的 SMB 会话；守护进程会重新挂载，但卡死的会话"
              "可能需要重启路由器",
              fix_id="mount_share")

        # 6b) Local-Network denial heuristic. state.json OFFLINE while the share
        # is actually mounted at our mountpoint is the macOS 26「本地网络」signature:
        # the kernel mount_smbfs path works but python3's in-process 445 probe is
        # blocked, so the daemon reads as offline over a healthy mount. The
        # condition REQUIRES mounted_entry precisely so it does NOT misfire during
        # an offline iCloud prefetch (PLAN M4): that state is OFFLINE with the
        # share NOT mounted -- a genuine away-from-home condition, not a denial.
        if state.get("state") == "OFFLINE" and mounted_entry:
            # No fix_id on purpose: the old one opened the Local Network pane,
            # but macOS never lists a bare launchd python3 there (no entry, no
            # + button), so the "fix" sent users hunting for a switch that does
            # not exist (2026-07-31 user report). The socket block itself is
            # already bypassed in code (probe_reachable's smbutil fallback).
            check("local_network", False,
                  "守护进程离线但共享已挂载",
                  detail="state.json 状态为 OFFLINE，但挂载表显示共享已挂载于 %s" % self.mnt,
                  hint="常见原因：SMB 凭据失效（日志中 rc=77）、守护进程正在退避"
                  "重连、或路由器 SMB 服务短暂无响应。请用 `mycloudctl logs` 查看"
                  "具体原因。注：macOS 对后台 python3 的「本地网络」拦截已由 SMB "
                  "代理探测自动绕过，且系统不会在设置面板中提供 python3 的开关"
                  "——无需（也无法）手动授权。")
        else:
            check("local_network", True,
                  "未检测到「本地网络」权限拦截迹象")

        # 7) bare-dir mode is 0500 when unmounted.
        try:
            is_mounted = os.path.ismount(self.mnt)
        except OSError:
            is_mounted = False
        if is_mounted:
            check("bare_dir_mode", True,
                  "裸挂载点目录权限（已跳过：当前已挂载）")
        else:
            try:
                mode = stat_mod.S_IMODE(os.stat(self.mnt).st_mode)
                check("bare_dir_mode", mode == 0o500,
                      "裸挂载点目录未挂载时应为 0500（当前 %o）" % mode,
                      hint="用 `chmod 0500 %s` 重置；非 0500 的裸目录会让误写入"
                      "填满启动磁盘" % self.mnt)
            except OSError as exc:
                check("bare_dir_mode", False, "裸挂载点目录可读",
                      hint="无法 stat %s：%s" % (self.mnt, exc))

        # 8) rsync capability probe fingerprint. TemporaryDirectory (not
        # mkdtemp + shutil.rmtree) so no rmtree literal lives outside trash.py
        # -- the data-safety invariant (tests/test_invariants.py) that rmtree is
        # confined to trash.py now covers this file too.
        probe = None
        probe_error = ""
        try:
            with tempfile.TemporaryDirectory(prefix="mycloud-doctor-") as tmp_dir:
                try:
                    probe = self.cap_probe(tmp_dir, self.rsync_bin)
                except Exception as exc:
                    probe_error = "capability probe raised: %s" % exc
        except OSError as exc:
            probe_error = "capability probe sandbox unavailable: %s" % exc
        if probe is None:
            check("rsync_capability", False, "rsync 能力探测",
                  detail=probe_error,
                  hint="探测无法运行；在其成功之前删除传播不安全",
                  fix_id="rerun_capability_probe")
        else:
            current_fp = _fingerprint(probe)
            stored_fp = state.get("capability_fingerprint")
            # Only the behaviors the two-phase protocol actually DEPENDS ON are
            # safety-critical. delete_suppressed_with_backup is informational:
            # True on Apple openrsync (macOS 15+), False on the real samba
            # rsync 2.6.9 that older macOS ships (verified on a Monterey
            # gateway, 2026-08-01) -- both are safe because write-mode argv
            # never carries --delete (a static invariant), so suppression never
            # matters. inplace_updates_ok is informational too (the daemon
            # auto-falls back to the -b path). Requiring all() of them here
            # falsely failed every pre-openrsync machine.
            critical_ok = (bool(probe.get("backup_moves_overwritten"))
                           and bool(probe.get("deleting_line_format_ok")))
            # Match the daemon's stored-fingerprint semantics: compare KEY BY
            # KEY, so a probe added by an upgrade (a new key) is not a change.
            changed_keys = _fingerprint_changed_keys(stored_fp, probe)
            if stored_fp and changed_keys:
                check("rsync_capability", False,
                      "rsync 能力指纹与记录值匹配",
                      hint="自上次记录以来 rsync 行为发生变化（变化项：%s；原为 %r，"
                      "现为 %r）；这正是删除传播被禁用的原因——在信任删除之前请先"
                      "排查" % (", ".join(changed_keys), stored_fp, current_fp),
                      fix_id="rerun_capability_probe")
            elif not critical_ok:
                check("rsync_capability", False, "rsync 能力探测指纹",
                      hint="本机 rsync 缺少两阶段协议依赖的关键行为"
                      "（backup-dir 版本化 / *deleting 行解析）：%s；"
                      "删除传播必须保持禁用" % current_fp,
                      fix_id="rerun_capability_probe")
            else:
                notes = []
                if state.get("deletion_disabled"):
                    notes.append("注意：删除传播当前在 state 中处于禁用状态。")
                if not probe.get("delete_suppressed_with_backup"):
                    notes.append(
                        "本机 rsync 不抑制 -b 与删除旗标的组合（真 rsync 2.6.9 "
                        "行为，非 openrsync）——写模式命令从不携带任何删除旗标"
                        "（静态不变量保证），安全性不受影响。")
                check("rsync_capability", True,
                      "rsync 能力探测指纹符合预期",
                      detail=" ".join(notes))

        # 8b) The REAL upload argv, run on a scratch tree. The probe above uses
        # its own argv, so it stayed green on the gateway while every upload
        # died with "rsync: --contimeout=15: unknown option" (2026-08..10).
        up_problem = None
        try:
            with tempfile.TemporaryDirectory(prefix="mycloud-doctor-up-") as tmp_dir:
                up_problem = self.upload_selftest(tmp_dir)
        except Exception as exc:
            up_problem = "self-test could not run: %s" % exc
        if isinstance(up_problem, str) and up_problem:
            check("rsync_upload", False, "rsync 上传命令自检",
                  detail=up_problem,
                  hint="本机 rsync 不接受 MyCloud 实际使用的上传命令，所有上传都会失败"
                  "（2026-08 网关事故：rsync 2.6.9 不认识 --contimeout）。请在"
                  "「设置 → 守护进程」更新后端。")
        else:
            check("rsync_upload", True, "rsync 上传命令自检（真实参数本机试传）")

        # 9) TCC scandir test per configured folder.
        if cfg is None or not cfg.folders:
            check("fda_scandir", True,
                  "完全磁盘访问（Full Disk Access）scandir 测试（未配置任何文件夹）")
        else:
            for folder in cfg.folders:
                src = str(folder.src)
                cid = "fda_scandir:%s" % folder.name
                if folder.is_file:
                    # A single-file source: probe its parent directory instead.
                    probe_target = os.path.dirname(src) or src
                else:
                    probe_target = src
                try:
                    import contextlib
                    with contextlib.closing(os.scandir(probe_target)) as it:
                        next(it, None)
                    check(cid, True, "可读取：%s（%s）" % (folder.name, src))
                except PermissionError:
                    check(cid, False,
                          "%s（%s）的完全磁盘访问（Full Disk Access）" % (folder.name, src),
                          hint="为 /usr/bin/python3 授予完全磁盘访问权限：系统设置 > "
                          "隐私与安全性 > 完全磁盘访问（Full Disk Access）",
                          fix_id="open_fda_settings")
                except FileNotFoundError:
                    check(cid, True,
                          "%s 不存在（%s）——出现前将被跳过" % (
                              folder.name, src))
                except OSError as exc:
                    check(cid, False, "scandir %s（%s）" % (folder.name, src),
                          hint=str(exc))

        # 10) NAS free space + trash size vs cap. Guarded by the mount check:
        # statvfs on the UNMOUNTED bare dir reports the LOCAL boot volume (a real
        # install once showed the 115.8 GiB local disk as "NAS 剩余空间"), so skip
        # cleanly instead of reporting a meaningless local number.
        if not mounted_entry:
            check("nas_space", False, "NAS 剩余空间 / 回收站大小",
                  hint="共享未挂载，跳过 NAS 空间检查")
        else:
            # Both numbers come from TIMED child processes: an in-process
            # statvfs blocks on a wedged session, and walking the trash in
            # Python took 10+ minutes for 71k files -- the GUI gives doctor 60s.
            free_bytes = self._df_available_bytes(self.mnt)
            dest_rel = getattr(cfg, "dest_rel", None) or "MyCloud"
            trash_root = os.path.join(self.mnt, paths.trash_rel(dest_rel))
            trash_bytes = self._du_bytes(trash_root)
            cap_gb = cfg.trash_max_gb if cfg is not None else 50
            gib = float(2 ** 30)
            if free_bytes is None:
                check("nas_space", False, "NAS 剩余空间 / 回收站大小",
                      hint="无法读取 NAS 剩余空间（df 超时或失败）；共享可能已无响应")
            else:
                space_ok = free_bytes >= (1 * 2 ** 30)
                cap_ok = trash_bytes is None or trash_bytes <= (cap_gb * 2 ** 30)
                trash_txt = ("%.1f GiB" % (trash_bytes / gib) if trash_bytes is not None
                             else "未知（统计超时）")
                check("nas_space", space_ok and cap_ok,
                      "NAS 剩余空间 %.1f GiB，回收站 %s（上限 %d GiB）" % (
                          free_bytes / gib, trash_txt, cap_gb),
                      hint="剩余空间低于 1 GiB 会停止同步，或回收站已超出上限；"
                      "每日会自动清理，但你可能需要调高上限",
                      fix_id="prune_trash_now")

        # 10b) Power: an always-on gateway must not idle-sleep. Every sleep
        # kills the SMB session, fails the pass in flight and forces a full
        # re-plan on wake -- the likeliest outside cause of "时常失败".
        ok, detail, hint = self._power_sleep_status()
        check("power_sleep", ok, "接通电源时不会自动睡眠", detail=detail, hint=hint)

        # 11) LaunchAgent loaded.
        lres = self.run_cmd([paths.LAUNCHCTL, "print", self._domain_target()], 15)
        agent_ok = (not getattr(lres, "timed_out", False)) and getattr(lres, "rc", 1) == 0
        check("launchagent", agent_ok, "LaunchAgent %s 已加载" % AGENT_LABEL,
              hint="用 `mycloudctl resume` 加载，或重新运行 install.sh")

        # 12) state.json freshness.
        state_age = _age_seconds(state.get("updated_at"), self.now_fn())
        if state_age is None:
            check("state_fresh", False, "state.json 新鲜度",
                  hint="state.json 从未被写入；守护进程可能未在运行",
                  fix_id="restart_daemon")
        else:
            check("state_fresh", state_age <= 48 * 3600,
                  "state.json 是新鲜的（%s）" % _fmt_age(state_age),
                  hint="state.json 已超过 48 小时；守护进程可能已停止"
                  "（这也是独立看门狗会捕捉的情况）",
                  fix_id="restart_daemon")

        # 13) iCloud dataless files not backed up (PLAN M5). Only a problem under
        # the skip policy: files exist only in the cloud and are left out of the
        # backup. fix_id is deliberately null -- the remedy is a policy CHOICE
        # (set icloud_policy=download), not a one-click action.
        folders_state = state.get("folders")
        if not isinstance(folders_state, dict):
            folders_state = {}
        # Only CONFIGURED sources count. state.folders keeps an entry for every
        # folder ever synced, including sources the user has since removed, and
        # those entries freeze the counters they had at removal time. Summing all
        # of them reported 316275 dataless files when the real figure was 969 --
        # a removed source (an accidental path added on 2026-07-25) was still
        # contributing its stale 315306 (verified 2026-07-28). An empty
        # configured set means the config failed to load; fall back to summing
        # everything rather than silently reporting zero.
        configured = set()
        if cfg is not None:
            configured = set(f.name for f in cfg.folders)
        skipped_total = 0
        for name, entry in folders_state.items():
            if not isinstance(entry, dict):
                continue
            if configured and name not in configured:
                continue
            isk = entry.get("icloud_skipped")
            if isinstance(isk, dict):
                try:
                    skipped_total += int(isk.get("count", 0) or 0)
                except (TypeError, ValueError):
                    continue
        policy = cfg.icloud_policy if cfg is not None else "skip"
        if skipped_total > 0 and policy == "skip":
            check("icloud_dataless", False,
                  "有 %d 个 iCloud 云端文件未纳入备份" % skipped_total,
                  detail="这些文件仅存在于 iCloud 云端（dataless），当前 "
                  "icloud_policy=skip 未备份它们。",
                  hint="如需备份这些文件，请在「设置」中把 iCloud 策略改为 "
                  "download（会在备份时逐批下载、备份后再驱逐），"
                  "或用 `mycloudctl config-set icloud_policy download`。")
        else:
            check("icloud_dataless", True,
                  "iCloud 云端（dataless）文件处理正常（策略：%s）" % policy)

        # 13b) iCloud materialization availability. When state.prefetch.blocked is
        # true the launchd-agent process tree cannot trigger iCloud downloads
        # (fileproviderd refuses background download requests) -- a macOS
        # restriction, not a fault. fix_id is deliberately null: there is no
        # one-click remedy; the user must open/download the files themselves.
        prefetch_state = state.get("prefetch")
        materialize_blocked = (
            isinstance(prefetch_state, dict) and bool(prefetch_state.get("blocked")))
        if materialize_blocked:
            check("icloud_materialize", False,
                  "iCloud 后台下载被系统限制",
                  detail="state.prefetch.blocked=true：一整批下载全部超时、0 个成功。",
                  hint="这是 macOS 的限制，不是故障：launchd 后台代理进程无法触发 "
                  "iCloud 下载（fileproviderd 拒绝后台进程的下载请求，而相同的读取"
                  "在用户终端里可正常下载）。云端（dataless）文件会在你手动打开或"
                  "下载后由 MyCloud 自动备份；也可把它们复制出 iCloud 目录以确保备份。")
        else:
            check("icloud_materialize", True,
                  "iCloud 物化（下载触发）可用")

        # 14) deep verify age (PLAN M5): stale past 8 days -> run_deep_verify fix.
        verify = state.get("verify")
        last_verify_iso = state.get("last_deep_verify_at")
        if not last_verify_iso and isinstance(verify, dict):
            last_verify_iso = verify.get("finished_at")
        verify_age = _age_seconds(last_verify_iso, self.now_fn())
        if verify_age is None:
            check("deep_verify_age", False, "从未进行过深度校验",
                  hint="深度校验会逐文件核对 NAS 备份与本地清单，抓取 NAS 侧的"
                  "位腐 / 外部篡改；建议立即运行一次。",
                  fix_id="run_deep_verify")
        else:
            check("deep_verify_age", verify_age <= 8 * 86400,
                  "上次深度校验：%s" % _fmt_age(verify_age),
                  hint="距上次深度校验已超过 8 天；运行一次以核对 NAS 备份完整性。",
                  fix_id="run_deep_verify")

        return results

    # -- one-click fixes (A4) ----------------------------------------------

    def cmd_fix(self, fix_id):
        # type: (str) -> int
        """Run a remediation by id, emitting {"ok","detail","followup"} JSON.

        The registry is pinned (A4); an unknown id is an error. followup is
        "reopen_doctor" when the fix changed system/daemon state (so the GUI
        should re-run doctor), else null. No fix deletes NAS data except
        prune_trash_now, which routes through the existing TrashManager so the
        rmtree-confined-to-trash.py invariant stays intact. No password handling.
        """
        handlers = {
            "restart_daemon": self._fix_restart_daemon,
            "cleanup_stale_mounts": self._fix_cleanup_stale_mounts,
            "mount_share": self._fix_mount_share,
            "open_fda_settings": self._fix_open_fda_settings,
            "rerun_capability_probe": self._fix_rerun_capability_probe,
            "prune_trash_now": self._fix_prune_trash_now,
            "sync_now": self._fix_sync_now,
            "run_deep_verify": self._fix_run_deep_verify,
        }
        handler = handlers.get(fix_id)
        if handler is None:
            self._emit_json({
                "ok": False,
                "detail": "未知的修复项：%s" % fix_id,
                "followup": None,
            })
            return 1
        result = handler()
        self._emit_json(result)
        return 0 if result.get("ok") else 1

    def _fix_restart_daemon(self):
        # type: () -> dict
        """launchctl kickstart -k the agent (also the fix for a stale/dead daemon)."""
        res = self.run_cmd(
            [paths.LAUNCHCTL, "kickstart", "-k", self._domain_target()], 30)
        failed = getattr(res, "timed_out", False) or getattr(res, "rc", 1) != 0
        if failed and self._wait_daemon_alive():
            # Same false alarm as cmd_resume (2026-08-01): a slow kill+restart
            # overruns launchctl's budget while the daemon comes up fine.
            return {"ok": True,
                    "detail": "已重启守护进程（kickstart 未及时返回，已通过存活探测确认）。",
                    "followup": "reopen_doctor"}
        if getattr(res, "timed_out", False):
            return {"ok": False,
                    "detail": "重启守护进程超时（launchctl kickstart 未在限时内完成）。",
                    "followup": None}
        if getattr(res, "rc", 1) != 0:
            return {"ok": False,
                    "detail": "重启守护进程失败（launchctl kickstart rc=%s）；请确认 "
                              "LaunchAgent 已加载，或重新运行 install.sh。"
                              % getattr(res, "rc", "?"),
                    "followup": None}
        return {"ok": True,
                "detail": "已请求重启守护进程（launchctl kickstart -k）。",
                "followup": "reopen_doctor"}

    def _fix_cleanup_stale_mounts(self):
        # type: () -> dict
        """Unmount leftover mycloud_verify.* mounts (umount, then diskutil force)."""
        mres = self.run_cmd([paths.MOUNT, "-t", "smbfs"], 10)
        if getattr(mres, "timed_out", False) or getattr(mres, "rc", 1) != 0:
            return {"ok": False,
                    "detail": "无法读取挂载表（mount -t smbfs），暂时无法清理遗留挂载。",
                    "followup": None}
        stale = []  # type: list
        for line in (getattr(mres, "out", "") or "").splitlines():
            on_idx = line.find(" on ")
            par_idx = line.rfind(" (")
            if on_idx == -1 or par_idx == -1 or par_idx <= on_idx:
                continue
            mp = line[on_idx + 4:par_idx]
            if os.path.basename(mp).startswith("mycloud_verify."):
                stale.append(mp)
        if not stale:
            return {"ok": True,
                    "detail": "没有需要清理的遗留安装校验挂载（mycloud_verify.*）。",
                    "followup": "reopen_doctor"}
        cleaned = []  # type: list
        failed = []  # type: list
        for mp in stale:
            u = self.run_cmd([paths.UMOUNT, mp], 30)
            if not getattr(u, "timed_out", False) and getattr(u, "rc", 1) == 0:
                cleaned.append(mp)
                continue
            # umount refused (busy) or timed out -> escalate to a forced unmount.
            d = self.run_cmd([paths.DISKUTIL, "unmount", "force", mp], 30)
            if not getattr(d, "timed_out", False) and getattr(d, "rc", 1) == 0:
                cleaned.append(mp)
            else:
                failed.append(mp)
        if failed:
            return {"ok": False,
                    "detail": "已清理 %d 个遗留挂载，但仍有 %d 个无法卸载：%s。"
                              % (len(cleaned), len(failed), "、".join(failed)),
                    "followup": "reopen_doctor"}
        return {"ok": True,
                "detail": "已清理 %d 个遗留安装校验挂载。" % len(cleaned),
                "followup": "reopen_doctor"}

    def _fix_mount_share(self):
        # type: () -> dict
        """Mount the share (reusing the mount seam); already-mounted counts as ok."""
        cfg = self._load_config()
        if cfg is None or not cfg.host or not cfg.share:
            return {"ok": False,
                    "detail": "config.ini 中没有 [server] host/share，无法挂载。",
                    "followup": None}
        try:
            mm = self.mount_factory(cfg)
            if mm.is_mounted():
                return {"ok": True,
                        "detail": "共享已经挂载于 %s。" % self.mnt,
                        "followup": "reopen_doctor"}
            problem = self._bare_dir_problem(mm)
            if problem:
                return {"ok": False, "detail": problem, "followup": None}
            if not mm.probe_reachable():
                return {"ok": False,
                        "detail": "NAS %s 不可达（设备离线；直连 445 与 SMB 代理"
                                  "探测均失败）。" % cfg.host,
                        "followup": None}
            if not mm.mount():
                return {"ok": False,
                        "detail": "挂载 //%s/%s 失败（mount_smbfs）。"
                                  % (cfg.host, cfg.share),
                        "followup": None}
            if not mm.is_mounted():
                return {"ok": False,
                        "detail": "已执行挂载，但挂载表中仍未出现该共享。",
                        "followup": None}
            return {"ok": True,
                    "detail": "已挂载共享到 %s。" % self.mnt,
                    "followup": "reopen_doctor"}
        except Exception as exc:
            return {"ok": False, "detail": "挂载失败：%s" % exc, "followup": None}

    def _fix_open_fda_settings(self):
        # type: () -> dict
        """Open the Full Disk Access privacy pane."""
        res = self.run_cmd([paths.OPEN, _FDA_SETTINGS_URL], 10)
        if getattr(res, "timed_out", False) or getattr(res, "rc", 1) != 0:
            return {"ok": False,
                    "detail": "无法打开「完全磁盘访问」设置页；请手动前往 系统设置 > "
                              "隐私与安全性 > 完全磁盘访问。",
                    "followup": None}
        return {"ok": True,
                "detail": "已打开「完全磁盘访问」设置页；请为 /usr/bin/python3 授予完全"
                          "磁盘访问权限后重新运行诊断。",
                "followup": None}

    def _fix_rerun_capability_probe(self):
        # type: () -> dict
        """Re-probe openrsync. Matching fingerprint -> clear deletion_disabled;
        a changed fingerprint -> do NOT clear (semantics may have shifted)."""
        stored = self.store.get().get("capability_fingerprint")
        try:
            with tempfile.TemporaryDirectory(prefix="mycloud-fix-cap-") as tmp:
                probe = self.cap_probe(tmp, self.rsync_bin)
        except Exception as exc:
            return {"ok": False,
                    "detail": "无法运行 rsync 能力探测（%s）；删除传播保持禁用。" % exc,
                    "followup": None}
        current = _fingerprint(probe)
        # Key by key, like the daemon and doctor: a whole-string comparison
        # never matched again once an upgrade added a probe key, so deletion
        # propagation could never be re-enabled.
        changed = _fingerprint_changed_keys(stored, probe)
        if changed:
            return {"ok": False,
                    "detail": "rsync 的能力指纹与记录值不一致（%s），说明其删除 / 备份"
                              "语义可能已改变；为防止误删，删除传播保持禁用，未清除该标记。"
                              % ", ".join(changed),
                    "followup": None}
        # Matches the stored baseline (or first-ever probe): re-enable deletion.
        self.store.update(deletion_disabled=False, capability_fingerprint=current)
        # The daemon reads the flag at startup only (and a running daemon's own
        # state writes could race this one): restart it so the change is real.
        if self._agent_loaded():
            res = self._fix_restart_daemon()
            if res.get("ok"):
                return {"ok": True,
                        "detail": "rsync 能力指纹与记录值一致，已清除删除禁用标记并重启"
                                  "守护进程；删除传播恢复启用。",
                        "followup": "reopen_doctor"}
            return {"ok": True,
                    "detail": "已清除删除禁用标记，但重启守护进程失败；重启后生效。",
                    "followup": "reopen_doctor"}
        return {"ok": True,
                "detail": "rsync 能力指纹与记录值一致，已清除删除禁用标记；守护进程下次"
                          "启动时生效。",
                "followup": "reopen_doctor"}

    def _fix_prune_trash_now(self):
        # type: () -> dict
        """Prune the trash now, but only when mounted AND the strict four-layer
        guard passes (pruning is the sole path that truly destroys NAS data)."""
        cfg = self._load_config()
        if cfg is None:
            return {"ok": False,
                    "detail": "config.ini 无法加载，无法清理回收站。",
                    "followup": None}
        try:
            mm = self.mount_factory(cfg)
        except Exception as exc:
            return {"ok": False,
                    "detail": "无法初始化挂载管理器：%s。" % exc,
                    "followup": None}
        try:
            mounted = mm.is_mounted()
        except Exception:
            mounted = False
        if not mounted:
            return {"ok": False,
                    "detail": "共享未挂载；仅在已挂载且四层守卫全部通过时才会清理回收站。",
                    "followup": None}
        try:
            guard_ok = mm.guard_ok()
        except Exception:
            guard_ok = False
        if not guard_ok:
            return {"ok": False,
                    "detail": "四层挂载守卫未通过；为安全起见拒绝清理回收站"
                              "（这是唯一会真正销毁 NAS 数据的操作）。",
                    "followup": None}
        if self._daemon_alive():
            # The daemon is the only writer to the NAS: hand the prune to it
            # (it runs between passes) instead of deleting from this process
            # -- which could collide with the batch the daemon is writing and
            # would outrun the GUI's 60s budget on a large trash.
            try:
                util.atomic_write_text(self.prune_request_path,
                                       self.now_fn().isoformat() + "\n")
            except OSError as exc:
                return {"ok": False, "detail": "无法写入清理请求：%s。" % exc,
                        "followup": None}
            ok, _msg = self._signal_daemon(signal.SIGUSR1)
            if not ok:
                return {"ok": False, "detail": "无法通知守护进程清理回收站。",
                        "followup": None}
            return {"ok": True,
                    "detail": "已请求守护进程清理回收站；它会在当前同步结束后执行。",
                    "followup": "reopen_doctor"}
        trash_root = os.path.join(self.mnt, paths.trash_rel(cfg.dest_rel))
        try:
            tm = self.trash_factory(
                trash_root, cfg.retention_days, cfg.trash_max_gb)
            result = tm.prune(guard_cb=mm.guard_ok, now_utc=self.now_fn())
        except Exception as exc:
            return {"ok": False,
                    "detail": "清理回收站时出错：%s。" % exc,
                    "followup": None}
        freed_mib = getattr(result, "freed_bytes", 0) / float(2 ** 20)
        removed = list(getattr(result, "removed_stamps", []))
        detail = "已清理回收站：移除 %d 个批次，释放约 %.1f MiB。" % (
            len(removed), freed_mib)
        errs = list(getattr(result, "errors", []))
        if errs:
            detail += "（部分未完成：%s）" % "；".join(errs[:3])
        return {"ok": True, "detail": detail, "followup": "reopen_doctor"}

    def _fix_sync_now(self):
        # type: () -> dict
        """Ask the daemon to sync now (SIGUSR1), like the sync-now subcommand."""
        ok, _message = self._signal_daemon(signal.SIGUSR1)
        if ok:
            return {"ok": True,
                    "detail": "已向守护进程发送立即同步请求（SIGUSR1）。",
                    "followup": None}
        return {"ok": False,
                "detail": "无法通知守护进程立即同步（守护进程可能未在运行）；"
                          "可先尝试「重启守护进程」。",
                "followup": None}

    def _fix_run_deep_verify(self):
        # type: () -> dict
        """Trigger a deep verify (verify.request + SIGUSR1) and return at once.

        Per PLAN M3 the fix does NOT wait for the verify to finish (it can take
        many minutes over SMB); it returns immediately with followup null and a
        detail saying it started. Progress is visible in status ("verify").
        """
        ok, message = self._request_deep_verify()
        if ok:
            return {"ok": True,
                    "detail": "已请求深度校验，正在后台运行；完成后可在 `mycloudctl "
                              "status --json` 的 verify 字段查看结果。",
                    "followup": None}
        return {"ok": False,
                "detail": message + " 无法立即启动深度校验（守护进程可能未在运行）。",
                "followup": None}


def _command_names():
    # type: () -> list
    """Every subcommand this build understands, taken from the parser itself so
    the list can never drift from what is actually dispatchable."""
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    return []


def build_parser():
    # type: () -> argparse.ArgumentParser
    parser = argparse.ArgumentParser(prog="mycloudctl", description="MyCloud control CLI")
    parser.add_argument("--config", default=None, help="Path to config.ini")
    sub = parser.add_subparsers(dest="command")

    p_status = sub.add_parser("status", help="Show backup status")
    p_status.add_argument("--json", action="store_true", dest="as_json",
                          help="Emit raw state plus derived fields as JSON")

    sub.add_parser("sync-now", help="Ask the daemon to sync now (SIGUSR1)")

    p_approve = sub.add_parser("approve-deletions", help="Approve held deletions")
    p_approve.add_argument("--yes", action="store_true", dest="assume_yes",
                           help="Skip the interactive prompt (GUI confirms first)")

    sub.add_parser("reload", help="Ask the daemon to reload config (SIGUSR2)")

    p_verify = sub.add_parser(
        "verify", help="Run a deep verify (manifest vs NAS) and print the result")
    p_verify.add_argument("--json", action="store_true", dest="as_json",
                          help="Emit the verify result as JSON")

    sub.add_parser("pause", help="Stop the LaunchAgent (launchctl bootout)")
    sub.add_parser("resume", help="Start the LaunchAgent (launchctl bootstrap + kickstart)")

    p_logs = sub.add_parser("logs", help="Print the log path and tail it")
    p_logs.add_argument("-f", "--follow", action="store_true", help="Follow the log")

    p_doctor = sub.add_parser("doctor", help="Run diagnostic checks")
    p_doctor.add_argument("--json", action="store_true", dest="as_json",
                          help="Emit the check results as a JSON array")

    # -- GUI config contract (PLAN 12.2) --
    p_cget = sub.add_parser("config-get", help="Print the full config as JSON")
    p_cget.add_argument("--json", action="store_true", dest="as_json",
                        help="(always JSON; accepted for symmetry)")

    p_cadd = sub.add_parser("config-add-source", help="Add a backup source")
    p_cadd.add_argument("path", help="File or directory to back up")

    p_crm = sub.add_parser("config-remove-source", help="Remove a backup source")
    p_crm.add_argument("path", help="Source to remove (matched by resolved path)")

    p_cdest = sub.add_parser("config-set-dest", help="Set the destination base dir")
    p_cdest.add_argument("rel", help="Share-relative destination directory")

    p_cset = sub.add_parser("config-set", help="Set a whitelisted numeric/watch_mode key")
    p_cset.add_argument("key", help="One of the settable config keys")
    p_cset.add_argument("value", help="New value")

    sub.add_parser("mount", help="Mount the SMB share (for GUI destination browsing)")

    p_mat = sub.add_parser(
        "materialize", help="下载仅存在于 iCloud 云端的文件，使其能被备份")
    p_mat.add_argument("--limit", type=int, default=None,
                       help="最多下载前 N 个文件（默认不限）")
    p_mat.add_argument("--dry-run", action="store_true", dest="dry_run",
                       help="只统计云端文件，不下载")

    p_srv = sub.add_parser(
        "server-set",
        help="Configure the NAS connection (host / share / auth mode). The "
             "password is read from STDIN with --password-stdin -- never an argument.")
    p_srv.add_argument("--host", required=True)
    p_srv.add_argument("--share", required=True)
    p_srv.add_argument("--mode", required=True, choices=("user", "anonymous"))
    p_srv.add_argument("--username", default=None)
    p_srv.add_argument("--password-stdin", action="store_true", dest="password_stdin",
                       help="Read the password (one line) from stdin")
    p_srv.add_argument("--no-verify", action="store_true", dest="no_verify",
                       help="Skip the verification mount (testing only)")
    p_srv.add_argument("--adopt", action="store_true",
                       help="Take over a share that already carries a different MyCloud sentinel")
    p_inst = sub.add_parser(
        "install",
        help="GUI installer: copy the backend from a bundle dir, configure the NAS "
             "connection, render + load the LaunchAgents. Password via stdin only.")
    p_inst.add_argument("--from", dest="backend_dir", required=True,
                        help="Directory holding mycloudd.py, mycloudctl.py, mycloud/ and the templates")
    p_inst.add_argument("--host", default=None)
    p_inst.add_argument("--share", default=None)
    p_inst.add_argument("--mode", default=None, choices=("user", "anonymous"))
    p_inst.add_argument("--refresh", action="store_true",
                        help="Only refresh the backend files and LaunchAgents; "
                             "keep the existing connection settings")
    p_inst.add_argument("--username", default=None)
    p_inst.add_argument("--password-stdin", action="store_true", dest="password_stdin")
    p_inst.add_argument("--folder", action="append", dest="folders", default=None,
                        help="Backup source (repeatable); first install only")
    p_inst.add_argument("--no-verify", action="store_true", dest="no_verify")
    p_inst.add_argument("--adopt", action="store_true")
    p_un = sub.add_parser(
        "uninstall",
        help="Remove the local agents, plists and app dir (config/logs backed up). Never touches the NAS.")
    p_un.add_argument("--yes", action="store_true", help="Required: confirm non-interactively")
    p_un.add_argument("--no-backup", action="store_true", dest="no_backup")
    p_un.add_argument("--remove-keychain", action="store_true", dest="delete_keychain",
                      help="Also remove the NAS password from the login Keychain")
    p_un.add_argument("--strip-nsmb", action="store_true", dest="strip_nsmb")
    sub.add_parser("version",
                   help="Print the backend version and supported commands as JSON")
    p_fix = sub.add_parser("fix", help="Run a one-click remediation by id (A4)")
    p_fix.add_argument("fix_id", help="One of the registered fix ids")
    return parser


def main(argv=None):
    # type: (object) -> int
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    ctl = Ctl(config_path=args.config)

    if args.command == "status":
        return ctl.cmd_status(as_json=args.as_json)
    if args.command == "sync-now":
        return ctl.cmd_sync_now()
    if args.command == "approve-deletions":
        return ctl.cmd_approve_deletions(assume_yes=args.assume_yes)
    if args.command == "reload":
        return ctl.cmd_reload()
    if args.command == "verify":
        return ctl.cmd_verify(as_json=args.as_json)
    if args.command == "pause":
        return ctl.cmd_pause()
    if args.command == "resume":
        return ctl.cmd_resume()
    if args.command == "logs":
        return ctl.cmd_logs(follow=args.follow)
    if args.command == "doctor":
        return ctl.cmd_doctor(as_json=args.as_json)
    if args.command == "config-get":
        return ctl.cmd_config_get()
    if args.command == "config-add-source":
        return ctl.cmd_config_add_source(args.path)
    if args.command == "config-remove-source":
        return ctl.cmd_config_remove_source(args.path)
    if args.command == "config-set-dest":
        return ctl.cmd_config_set_dest(args.rel)
    if args.command == "config-set":
        return ctl.cmd_config_set(args.key, args.value)
    if args.command == "mount":
        return ctl.cmd_mount()
    if args.command == "materialize":
        return ctl.cmd_materialize(limit=args.limit, dry_run=args.dry_run)
    if args.command == "fix":
        return ctl.cmd_fix(args.fix_id)
    if args.command == "version":
        return ctl.cmd_version()
    if args.command in ("install", "uninstall"):
        # A GUI timeout arrives as SIGTERM: turn it into SystemExit so the
        # finally blocks (resume the daemon we stopped, release mounts) run.
        try:
            signal.signal(signal.SIGTERM, lambda *_a: sys.exit(143))
        except (ValueError, OSError):
            pass
    if args.command == "install":
        password = None
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\r\n")
        return ctl.cmd_install(args.backend_dir, args.host, args.share, args.mode,
                               username=args.username, password=password,
                               folders=args.folders, verify=not args.no_verify,
                               adopt=args.adopt, refresh=args.refresh)
    if args.command == "uninstall":
        if not args.yes:
            ctl._emit_error("uninstall 需要 --yes 确认。")
            return 2
        return ctl.cmd_uninstall(keep_config=not args.no_backup,
                                 delete_keychain=args.delete_keychain,
                                 strip_nsmb=args.strip_nsmb)
    if args.command == "server-set":
        password = None
        if args.password_stdin:
            # One line from stdin; only the line terminator is stripped so a
            # password with leading/trailing spaces survives intact.
            password = sys.stdin.readline().rstrip("\r\n")
        return ctl.cmd_server_set(args.host, args.share, args.mode,
                                  username=args.username, password=password,
                                  verify=not args.no_verify, adopt=args.adopt)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
