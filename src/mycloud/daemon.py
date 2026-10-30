"""Daemon state machine, scheduler, watchdog and recovery (PLAN sections 6.1-6.8).

This module is the conductor: it wires the injected components together into the
startup sequence, the two-phase full pass, the adaptive main loop (wake / watch /
tier-2 scheduling), the guard watchdog thread, and crash/disconnect recovery.

All filesystem roots are derived from the injected components (mount.mountpoint,
state.runtime_dir) plus the relative-name constants in paths.py; nothing here
hardcodes an absolute location. Python 3.9 stdlib only.

Mount-guard reporting and the Local-Network soft-pass
-----------------------------------------------------
Every "can we sync now?" decision goes through ``_guard_check`` rather than
``mount.guard_ok`` directly. On a guard failure it logs the failing layer
(``MountManager.last_failure``) once at INFO, rate-limited to a layer change or
once per 10 minutes so a persistent outage never spams the log.

One failure pattern gets special handling. On macOS 26 the launchd-spawned
python3 can be denied "Local Network" access, which blocks its in-process TCP
445 probe while the kernel mount_smbfs path keeps working -- the share is
mounted and healthy yet the guard's first layer (tcp445) fails, and the symptom
is indistinguishable from a real outage. When tcp445 is the only failing layer
AND the SMB session is provably alive (the sentinel child-``cat`` read and
``smbutil statshares`` both succeed), ``_guard_check`` SOFT-PASSES: it returns
True so backups keep flowing under the denial instead of the daemon tearing a
working mount down and looping OFFLINE forever. The condition is log-only: no
notification, because the user cannot act on it (macOS never lists a bare
launchd python3 in the Local Network pane, and this soft-pass IS the bypass).
The single data-destroying path (trash pruning) is deliberately NOT soft-passed;
it still requires a full four-layer ``guard_ok``.

The guard watchdog is for LIVENESS, not safety
-----------------------------------------------
While a sync runs, ``_watchdog_loop`` re-checks the guard every 60s and can kill
the rsync process group. It is deliberately RELUCTANT: it kills only when the
mount is truly gone from the mount table (``is_mounted`` False -- a cheap,
load-independent check) OR the soft-pass-aware guard fails on three CONSECUTIVE
checks. During those checks the sentinel/statshares child timeouts are raised to
30s, because under a full-throttle initial sync the router's SMB stack gets slow
enough that a 10s sentinel read intermittently times out and reads as a false
"mount dead" -- the exact loop (killpg -> recovery -> rescan from scratch ->
repeat every ~15 min) observed in production on a 21GB/65k-dir first sync.

Eager killing is unnecessary because data safety does NOT depend on it. Two
structural guarantees already make the classic "write into a dead mountpoint and
fill the boot disk" disaster impossible regardless of how long a wedged rsync
lingers: (1) the bare mount point is chmod 0500 whenever the share is not
mounted, so any write under it hits EACCES immediately; and (2) the share is
mounted ``-o soft``, so I/O against a dead SMB session returns EIO rather than
succeeding against local disk. The watchdog therefore exists purely to end a
hung/blocked transfer promptly so recovery can remount -- a liveness concern --
and a slightly delayed kill costs only latency, never correctness. Every kill
decision logs a WARNING naming the failing layer(s) and the consecutive-failure
count; a transient failure that does not (yet) kill logs once at INFO.
"""

import faulthandler
import fcntl
import fnmatch
import json
import os
import platform
import random
import signal
import stat as stat_mod
import subprocess
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import paths
from . import util
from . import scanner
from . import manifest as manifest_mod
from .kq import KqueueWatcher
from .mount import CriticalLocalWriteDetected, MountError

# State-machine values, mirrored into state.json["state"] on every transition.
OFFLINE = "OFFLINE"
MOUNTED_IDLE = "MOUNTED_IDLE"
SYNCING = "SYNCING"
DELETIONS_HELD = "DELETIONS_HELD"
DEGRADED = "DEGRADED"
# Written once the lock is held, before anything else: proves to a caller
# waiting on a restart that THIS process is the one now reporting.
STARTING = "STARTING"

# Per-folder state values (PLAN 6.9).
F_UP_TO_DATE = "upToDate"
F_SYNCING = "syncing"
F_DELETIONS_HELD = "deletionsHeld"
F_NEEDS_PERMISSION = "needsPermission"
F_SOURCE_MISSING = "sourceMissing"
F_DEGRADED = "degraded"

# Backoff schedule (seconds), capped; each delay jittered by rng.uniform(0.8,1.2).
_BACKOFF = [30, 60, 120, 300, 600]

# Local-Network-denial recheck cadence for the offline mount gate. The mount is
# normally gated on a live tcp445 probe (never fire mount_smbfs at an unreachable
# host -- the away-from-home wedge). But under a macOS Local-Network permission
# denial the probe is blocked while the kernel mount_smbfs path still works (this
# machine runs under exactly that denial: python3 never appears in the settings
# pane). So every _DENIAL_RECHECK_EVERY consecutive probe-failed iterations we
# attempt a mount ANYWAY to detect/recover that case -- safe now that util.run
# cannot wedge. The streak resets on any successful probe or mount.
_DENIAL_RECHECK_EVERY = 5

# Caps on persisted lists.
_FAILED_CAP = 500
_HELD_CAP = 1000
# icloud_skipped sample cap surfaced in status (PLAN M5).
_ICLOUD_SAMPLE_CAP = 100

# Batched upload limits (PLAN 14.3): a batch is at most this many files or this
# many bytes, whichever comes first, so a crash loses at most one batch and
# progress advances at a predictable granularity.
_BATCH_MAX_FILES = 500
_BATCH_MAX_BYTES = 2 * 2 ** 30

# Batch retry on rc=20 (PLAN 16.2 T-A2). rc=20 is rsync's OWN --timeout=180 I/O
# timeout, which this router trips transiently under load; escalating that to a
# SyncFailure drags the whole daemon through the recovery path (unmount, remount,
# full re-pass) for what is usually a few seconds of jitter. One in-place retry
# after a short pause absorbs it. Only rc=20 retries: a signal death (rc < 0) is
# our own watchdog killpg, and recovery is already on its way.
_BATCH_RETRY_SLEEP = 5.0
# How many consecutive verification failures a file needs before it is named in
# failed_files (PLAN 16.2 T-A1). A file that misses one partial batch is simply
# re-planned next pass and is not worth alarming the user about; one that keeps
# missing is a real problem the user must see.
_UPLOAD_FAIL_DISCLOSE = 3
# A single NAS lstat slower than this means the share is dying: stop verifying.
_NAS_STAT_SLOW_S = 10.0
# A pass that failed for a reason a remount cannot fix (rsync rejected its own
# command line, or failed while the mount guard is healthy): scheduled passes
# back off 5 min, doubling to 1 h, instead of re-planning ~270k files and
# unmounting/remounting every few minutes (the gateway's 2026-08..10 loop).
_PASS_FAIL_BACKOFF_BASE = 300.0
_PASS_FAIL_BACKOFF_MAX = 3600.0
# Failure kinds recovery() exists for: the transfer was killed, rsync's own I/O
# timeout fired, the destination stopped answering (mkdir -p hung), or the
# watchdog asked. None = legacy raise sites (recover).
_TRANSPORT_KINDS = frozenset(
    ("killed", "io_timeout", "dest_timeout", "recovery", None))
# Right after a wake the network and the SMB client may still be settling: ask
# the guard this many times, this far apart, before unmounting a mount that may
# be perfectly fine a few seconds later.
_WAKE_GUARD_TRIES = 4
_WAKE_GUARD_WAIT = 15.0
_USAGE_MSG = ("无法上传：本机的 rsync 不接受 MyCloud 的上传命令（%s）。这不是 NAS "
              "或网络问题，重新挂载也无济于事；请在「设置 → 守护进程」更新后端，"
              "并查看日志。")

# Offline iCloud prefetch (PLAN section 14 M4, "download while away, upload once
# home"). When the NAS is unreachable but icloud_policy=download and dataless
# files are pending, the daemon fills the idle backoff windows by materializing
# those files from Apple a batch at a time, so they are already local when the
# home network returns and the normal pass uploads them.
_PREFETCH_BATCH_FILES = 200          # files per prefetch batch (materializer).
_PREFETCH_WORKERS = 4                 # concurrent downloads per batch.
# Absolute local-free-space floor: prefetch STOPS (this batch is refused) once
# the boot disk would drop below this. 25 GiB of headroom is kept free at all
# times regardless of batch size, on top of the existing >=3x-batch disk guard.
_PREFETCH_MIN_FREE_BYTES = 25 * 2 ** 30
_PREFETCH_JOURNAL_EVERY = 20         # NDJSON checkpoint cadence (batches).
# Cheap TCP-445 re-probe between batches: the short (load-independent) timeout,
# matching MountManager.probe_tcp445's default. A reachable NAS aborts prefetch
# at once so uploads take priority (never delayed by more than one batch).
_PREFETCH_PROBE_TIMEOUT = 3.0
# Probe-first materialization: until a download has worked in this process, a
# batch starts with this many files. Under the launchd iCloud restriction every
# read times out, and concluding that from a WHOLE batch took hours -- a 500-file
# online batch at 120s per file on 4 workers is ~4h, the 174-minute field hang in
# which the daemon answered neither a dead mount nor anything else. A probe
# decides within one per-file timeout.
_MATERIALIZE_PROBE_FILES = 4
# The "downloads are blocked" latch expires. A re-probe costs one per-file
# timeout, and the restriction can be lifted -- the agent plist now asks launchd
# to let the job materialize dataless files -- so check daily instead of never.
_MATERIALIZE_BLOCKED_TTL_S = 24 * 3600.0

# Deep verify cadence (PLAN 14.6 / M3): the daemon runs it at most weekly, and
# the doctor deep_verify_age row fails past 8 days.
_DEEP_VERIFY_INTERVAL_S = 7 * 24 * 3600.0
# NAS-vs-manifest drift tolerance: SMB/exFAT mtime granularity is coarse, so
# match the transfer-time --modify-window=2 (in nanoseconds) when comparing.
_VERIFY_MTIME_TOLERANCE_NS = 2 * 10 ** 9
# Deep-verify walk guards (PLAN 16.2 T-C1, 2026-07-30). The verify walks the NAS
# mirror on the MAIN thread, and a mount that dies mid-walk makes os.walk either
# wedge or (after the soft-mount timeout) silently return a TRUNCATED tree --
# whose absent rows then read as "missing on NAS" and get marked pending, i.e. a
# false re-upload storm, while absent-from-manifest survivors read as "extra" and
# get archived. So the walk re-checks _stop and mount.is_mounted() every this
# many directories OR this many seconds (whichever comes first) and aborts the
# whole verify before it can act on a partial picture. Module-level so tests can
# tighten them.
_VERIFY_GUARD_EVERY_DIRS = 256
_VERIFY_GUARD_EVERY_S = 5.0

_TICK_SECONDS = 5.0
_APPROVAL_MAX_AGE_H = 24.0   # a deletion-approval token older than this is stale.
_WAKE_THRESHOLD = 5.0        # wall-vs-monotonic drift that signals a wake.
_DEBOUNCE_ARM = 5.0          # first change arms a 5s timer.
_DEBOUNCE_HARD_CAP = 30.0    # hard cap from first change.
_WATCHDOG_INTERVAL = 60.0
# Pause after an unexpected main-loop error before the next tick.
_LOOP_ERROR_BACKOFF = 30.0
# EX_TEMPFAIL: a startup failure worth a launchd retry (KeepAlive restarts non-zero).
_EXIT_TEMPFAIL = 75
# Upper bound on the fallback mirror walk (see _mirror_count). Only the
# circuit breaker may trigger it, and only when the manifest has no rows;
# a lower bound is enough for the percentage rule, while an unbounded walk
# over SMB can take many minutes.
_MIRROR_WALK_CAP = 20000

_GUARD_TERM_GRACE = 10.0
# Guard-failure logging is rate-limited to a layer change or once per 10 minutes.
_GUARD_LOG_INTERVAL = 600.0

# Watchdog kill policy (A1). While syncing, kill the rsync group only when the
# mount is gone (immediate) or the soft-pass-aware guard fails this many times in
# a row; and widen the sentinel/statshares child timeouts to _WATCHDOG_SYNC_
# CHILD_TIMEOUT so a slow-under-load router does not read as a dead mount.
_WATCHDOG_CONSECUTIVE_KILL = 3
_WATCHDOG_SYNC_CHILD_TIMEOUT = 30.0
# Load-tolerance grace (PLAN 15 P0 deployment finding, 2026-07-26): several
# parallel upload streams saturate a weak router's SMB link so hard that the
# guard's sentinel/statshares reads get starved and time out -- a FALSE "mount
# dead" that made the watchdog kill rsync and thrash (offline/remount loop). If
# rsync transferred a file within this window the session is provably alive and
# writable (stronger proof than those reads), so the watchdog must not kill on a
# starved-read failure. A genuinely wedged session stops producing itemize
# output, so progress goes stale past this grace and the normal kill path returns.
_WATCHDOG_PROGRESS_GRACE = 90.0
# Transfer-stall kill (2026-07-28, observed on real hardware). The guard can be
# perfectly healthy -- sentinel readable, statshares alive, mount present -- while
# an rsync child sits in uninterruptible kernel I/O against a wedged SMB server
# (state UN, 0% CPU, zero bytes for 14 minutes and counting). The guard-based
# rules then RESET the failure streak every minute, so nothing ever intervened
# and the sync thread waited forever. If uploads make no progress for this long
# while a live rsync exists, kill the group so the batch retries: an uncommitted
# batch loses nothing (the manifest only advances on success) and --partial keeps
# the bytes already written. Generous on purpose -- one huge file emits a single
# itemize line and would otherwise look stalled while it legitimately transfers.
_TRANSFER_STALL_KILL = 1200.0
# Attribute a phase1 failure to a watchdog kill only if the kill was this recent.
_KILL_REASON_TTL = 60.0
# While MOUNTED_IDLE, refresh state.json's updated_at at least this often so the
# GUI staleness heuristic has a reliable liveness signal during quiet periods.
_IDLE_TOUCH_INTERVAL = 30.0

# Shown (WARNING + notification) when tcp445 is the only failing guard layer
# while the share is still mounted -- the macOS 26 "Local Network" denial
# signature. Contains the exact remediation path.
_LOCAL_NETWORK_HINT = (
    "TCP 445 直连探测被 macOS 拦截（后台 python3 无法获得「本地网络」授权，"
    "且系统不会在该设置面板中提供它的开关——无需、也无法手动设置），"
    "但 SMB 会话验证通过：同步经代理路径继续，不受影响，无需任何操作。"
)


def _excluded_basename(name, patterns):
    # type: (str, List[str]) -> bool
    """True if a basename matches any exclude pattern (fnmatch, like scanner)."""
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


class SyncFailure(Exception):
    """A Phase 1 rsync run failed hard.

    kind is the engine's failure kind ("killed", "io_timeout", "usage",
    "error") or "recovery" for a watchdog request; _pass_failed decides from it
    whether recovery() can help. None (legacy raise sites) means recover.
    """

    def __init__(self, msg="", kind=None):
        # type: (str, Optional[str]) -> None
        super(SyncFailure, self).__init__(msg)
        self.kind = kind


class VerifyAborted(Exception):
    """The deep-verify NAS walk hit a stop request or a dead mount (T-C1).

    Raised from INSIDE _verify_folder's walk -- before any mark_pending or
    extra-file handling -- so an aborted folder produces zero side effects, and
    caught by deep_verify, which stops the whole verify (never continues to the
    next folder: a dead mount would make every remaining folder look empty).
    """


class _AnyEvent(object):
    """is_set()/wait() over several Events -- set as soon as any one is.

    Duck-types threading.Event for icloud.materialize_batch, so a download stage
    stops for a lost mount (the watchdog's recovery request) as promptly as for
    shutdown: it used to watch only _stop and kept downloading for hours while
    the watchdog logged "mount is gone" every minute.
    """

    def __init__(self, *events):
        # type: (*threading.Event) -> None
        self._events = events

    def is_set(self):
        # type: () -> bool
        return any(e.is_set() for e in self._events)

    def wait(self, timeout=None):
        # type: (Optional[float]) -> bool
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            step = 0.25
            if deadline is not None:
                step = min(step, deadline - time.monotonic())
                if step <= 0:
                    return False
            self._events[0].wait(step)
        return True


class _PrefetchSession(object):
    """One offline-prefetch run: the pending dataless files + live counters.

    entries is a flat list of (folder_name, src, relpath, abspath, size); the
    session is consumed in _PREFETCH_BATCH_FILES-sized slices across successive
    backoff windows, tracking materialized/skipped counts so progress spans the
    whole offline period rather than resetting each backoff.
    """

    __slots__ = ("entries", "total_files", "total_bytes", "index",
                 "done_files", "done_bytes", "skipped", "batches", "started_at")

    def __init__(self, entries, total_bytes, started_at):
        # type: (List[Any], int, str) -> None
        self.entries = entries
        self.total_files = len(entries)
        self.total_bytes = int(total_bytes)
        self.index = 0
        self.done_files = 0
        self.done_bytes = 0
        self.skipped = 0
        self.batches = 0
        self.started_at = started_at

    @property
    def exhausted(self):
        # type: () -> bool
        return self.index >= len(self.entries)


class Daemon(object):
    def __init__(self, cfg, state, mount, engine, trash_mgr, notifier, logger,
                 once=False, rng=random, manifest=None, dataless_check=None,
                 is_dataless_fn=None, read_fn=None, run_cmd=None):
        # type: (Any, Any, Any, Any, Any, Any, Any, bool, Any, Any, Any, Any, Any, Any) -> None
        self.cfg = cfg
        self.state = state
        self.mount = mount
        self.engine = engine
        self.trash_mgr = trash_mgr
        self.notifier = notifier
        self.logger = logger
        self.once = once
        self.rng = rng

        # v2 manifest + iCloud pipeline (PLAN section 14). All injectable so tests
        # -- which cannot create real dataless files -- can fake the predicates.
        from . import icloud as _icloud
        if manifest is None:
            from .manifest import Manifest
            manifest = Manifest(
                os.path.join(os.fspath(state.runtime_dir), "manifest.db"))
        self.manifest = manifest
        self.dataless_check = dataless_check or _icloud.dataless_check
        self.is_dataless_fn = is_dataless_fn or _icloud.is_dataless
        self.read_fn = read_fn
        self.run_cmd = run_cmd or util.run
        # brctl evict availability is probed once (lazily) and cached; on this
        # macOS build it is absent, so eviction degrades to a logged no-op.
        self._evict_available = None  # type: Optional[bool]
        self._evict_warned = False
        # Per-pass current-folder plan totals feed the progress total_files/bytes.
        self._pass_total_files = None  # type: Optional[int]
        self._pass_total_bytes = None  # type: Optional[int]
        self._bytes_done = 0
        # Set while a deep verify is running so the tick does not also sync.
        self._verifying = threading.Event()

        # Roots derived from cfg.dest_rel (PLAN 12.1): data_root, trash_root and
        # heartbeat_path all live under <mountpoint>/<dest_rel>/. Recomputed on
        # every reload so a dest_rel change re-points every path (and the trash
        # manager) without a restart.
        self.mountpoint = mount.mountpoint
        self.runtime_dir = os.fspath(state.runtime_dir)
        self.data_root = ""
        self.trash_root = ""
        self.heartbeat_path = ""
        self._recompute_roots()
        self.lock_path = os.path.join(self.runtime_dir, "daemon.lock")
        self.approval_path = os.path.join(self.runtime_dir, "approval.token")
        self.prune_request_path = os.path.join(
            os.fspath(state.runtime_dir), "prune.request")
        self.verify_request_path = os.path.join(
            self.runtime_dir, "verify.request")

        # Capability fail-closed flag (mirrors state deletion_disabled).
        self.deletion_disabled = False
        # --inplace fast path runs only after the capability probe demonstrates
        # it working (PLAN 15 P0; fail-closed to the -b path until then).
        self._inplace_capable = False
        # One-shot: whether the privacy settings deep link was opened this run.
        self._settings_opened = []  # type: List[bool]

        # Lock file descriptor (held for the daemon's lifetime).
        self._lock_fd = None  # type: Optional[int]

        # Loop machinery.
        self._stop = threading.Event()          # SIGTERM -> graceful shutdown.
        self._sync_requested = threading.Event()  # SIGUSR1 -> immediate pass.
        self._reload_requested = threading.Event()  # SIGUSR2 -> reload config.
        self._recovery_needed = threading.Event()  # watchdog -> loop recovers.
        self._syncing = threading.Event()        # true only while a pass runs.

        # In-memory Tier-1 snapshot and adaptive interval bookkeeping.
        self._snapshot = {}  # type: Dict[str, int]
        self._last_full_duration = 0.0
        self._watcher = None  # type: Optional[KqueueWatcher]
        self._degraded_halt = False
        # Whether an approval token was consumed at the start of the current pass
        # (applies to every folder in that pass, so a multi-folder hold releases
        # atomically). Set by full_pass, read by _apply_deletions.
        self._approval_active = False
        # Latched flag so a kqueue degradation is written to state.json only once.
        self._reported_degraded = False

        # Guard-failure logging bookkeeping (rate-limited; see _report_guard_failure).
        self._guard_last_layer = None  # type: Optional[str]
        self._guard_last_log_mono = 0.0

        # Consecutive tcp445-probe failures in the current offline loop, driving
        # the Local-Network-denial mount recheck (_try_online_gated). Reset on any
        # successful probe/mount and at each offline-loop entry.
        self._probe_fail_streak = 0

        # Guard watchdog bookkeeping (A1). _watchdog_consecutive counts guard
        # failures in a row (reset by any pass/soft-pass); the kill-reason fields
        # hand the failing layer from the watchdog thread to the sync thread so a
        # phase1 failure caused by our own killpg is logged as "killed by
        # watchdog (reason: <layer>)" rather than a bare rc (A3).
        self._watchdog_consecutive = 0
        self._watchdog_gone_reported = False
        self._loop_error_key = None  # type: Optional[str]
        self._loop_error_mono = 0.0
        self._watchdog_log_layer = None  # type: Optional[str]
        self._watchdog_log_mono = 0.0
        self._kill_reason = None  # type: Optional[str]
        self._kill_reason_mono = 0.0
        # monotonic time of the most recent rsync transfer line; the watchdog
        # uses it to tell a bandwidth-starved guard read (mount fine) from a
        # genuinely wedged session (no transfer progress). See _WATCHDOG_PROGRESS_GRACE.
        self._last_upload_mono = 0.0
        # Wall-clock (ISO) twin of _last_upload_mono, surfaced in progress so the
        # UI can render "last activity Xs ago" -- the signal that tells a healthy
        # slow transfer apart from a stall while files_done sits frozen.
        self._last_transfer_at = None  # type: Optional[str]
        # Coarse batch counters for the current folder's upload stage; they move
        # once per batch (~seconds to a minute) even when nothing else does.
        self._batches_done = 0
        self._batches_total = None  # type: Optional[int]
        # Consecutive post-upload verification failures per (folder, relpath),
        # feeding the failed_files disclosure at _UPLOAD_FAIL_DISCLOSE (PLAN 16.2
        # T-A1). Deliberately in memory only: a restart zeroes it, so a chronically
        # failing file needs _UPLOAD_FAIL_DISCLOSE more passes to be named again.
        # That is acceptable -- this is best-effort DISCLOSURE, not correctness
        # (correctness comes from not committing an unverified file at all, which
        # makes the next plan retry it regardless of any counter).
        self._upload_retry_counts = {}  # type: Dict[Any, int]

        # Live-progress throttling (PLAN 12.1): progress writes are at least
        # _progress_min_interval seconds apart. Tests may set the interval to 0.
        self._progress_min_interval = 2.0
        self._progress_last_write = 0.0
        self._progress_started_at = None  # type: Optional[str]
        # Progress heartbeat (PLAN 12.4: the progress field must refresh at least
        # every ~5s during a sync). A large single-file transfer or a phase-2
        # dry-run emits no itemize lines for minutes, so a dedicated thread
        # re-stamps the last progress snapshot while a pass runs. _progress_lock
        # serializes heartbeat re-stamps against the per-folder/end-of-pass writes
        # so a heartbeat can never resurrect an already-cleared (inactive)
        # progress after the pass ends.
        self._progress_hb_interval = 3.0
        self._progress_lock = threading.Lock()
        self._progress_last = None  # type: Optional[Dict[str, Any]]
        self._hb_thread = None  # type: Optional[threading.Thread]
        # Guard watchdog thread; started before the initial pass (see
        # _start_watchdog) and reused by _main_loop so it never runs twice.
        self._watchdog_thread = None  # type: Optional[threading.Thread]

        # Offline iCloud prefetch bookkeeping (M4). _prefetching gates the
        # progress heartbeat while downloading offline (there is no full_pass /
        # _syncing then, but state.json must still stay <=5s fresh). The session
        # is enumerated lazily per offline period; _prefetch_complete latches
        # "everything downloaded" so the daemon stops looping into prefetch and
        # falls back to plain idle backoff; the notify flag makes the completion
        # notification fire exactly once.
        self._prefetching = threading.Event()
        # Set while the daemon is spinning in an offline reconnect/backoff loop
        # (ensure_online / recovery). The progress heartbeat uses it to keep
        # updated_at fresh through offline segments the main thread cannot touch
        # during (the recheck mount_smbfs, an in-thread os.listdir on a wedged
        # mount) -- the ~34-min staleness-window fix.
        self._offline_active = threading.Event()
        # Set while the main thread does post-pass work (snapshot walk, NAS
        # heartbeat, daily prune -- minutes on a large trash) with no progress
        # to re-stamp: the heartbeat keeps updated_at fresh so the GUI does not
        # call a busy daemon unresponsive.
        self._main_busy = threading.Event()
        self._prefetch_session = None  # type: Optional[_PrefetchSession]
        self._prefetch_complete = False
        self._prefetch_done_notified = False
        # Latched once a materialize batch reveals the launchd-agent iCloud
        # restriction: fileproviderd silently refuses/stalls download requests
        # from the background agent's process tree, so every read times out even
        # though the same read works from a user shell. When set, both the
        # offline prefetch and the online download stage stop attempting
        # materialization. NOT reset by _reset_prefetch -- re-attempting a
        # systemic OS restriction every offline period would waste ~120s per file.
        #
        # RESTORED FROM STATE on startup. The latch used to live only in memory,
        # so every restart re-probed the restriction and burned ~120s per file
        # again: with 968 cloud-only files that is ~32 HOURS of the daemon
        # sitting in "downloading" before it re-learned what it already knew, and
        # a user watching the UI just sees it stuck (reported 2026-07-30). The
        # restriction is a property of the OS, not of this process, and the flag
        # was already being persisted -- it simply was never read back.
        # Scheduled-pass backoff after failures recovery() cannot fix.
        self._pass_fail_streak = 0
        self._pass_backoff_until = 0.0
        # The last_error text _note_pass_failure wrote (cleared only by a pass
        # that proves the failure over, and only while it is still that text).
        self._pass_error_msg = None  # type: Optional[str]
        # rsync rejected the upload argv (startup self-test or a real batch):
        # the message to show until an upload batch actually succeeds.
        self._upload_broken = None  # type: Optional[str]
        # Upload batches that landed in the current pass.
        self._pass_batches_ok = 0
        # This rsync copies a dangling symlink as a link without dropping the
        # rest of its batch (probed at startup; see _probe_dangling_links).
        self._dangling_links_ok = False
        self._materialize_blocked_at = None  # type: Optional[datetime]
        self._materialize_blocked = self._restore_materialize_blocked()
        # A download has succeeded in this process: batches skip the probe.
        self._materialize_proven = False
        # Per-file materialize timeout (offline prefetch AND the online download
        # stage). It genuinely bounds each file's download attempt now that the
        # read is a subprocess (icloud._make_default_read_trigger); tests shorten
        # it to exercise the timeout/skip path without a real 120s wait.
        self._materialize_timeout = _icloud._DEFAULT_PER_FILE_TIMEOUT
        # How often the offline loop wakes to re-check stop / NAS reachability
        # while a prefetch batch downloads in a background thread. This -- NOT the
        # batch duration -- bounds how long a reconnect (or shutdown) can be
        # delayed by prefetch. Tests shorten it.
        self._prefetch_poll_interval = 1.0

    # -- small helpers -----------------------------------------------------

    def _effective_excludes(self):
        # type: () -> List[str]
        """Builtin + user exclude patterns for every local/NAS walk.

        The v2 pipeline never passes --exclude-from on its upload path (the
        relpath lists ARE the plan), so the manifest walk, the scanner and the
        deep-verify NAS walk must all honor the SAME builtin+user pattern list
        the excludes.txt file carries. Using cfg.excludes alone silently backed
        up .DS_Store/node_modules/AppleDouble litter (bug fixed 2026-07-21).
        Falls back to cfg.excludes for test doubles without the property.
        """
        effective = getattr(self.cfg, "effective_excludes", None)
        if effective is not None:
            return list(effective)
        return list(getattr(self.cfg, "excludes", None) or [])

    def _forget_excluded_rows(self):
        # type: () -> None
        """Forget manifest rows the CURRENT exclude list would never walk again.

        WHY (PLAN 16.2 T-B2, 2026-07-30): the exclude list changes -- a user edit
        to excludes, or a new BUILTIN_EXCLUDES pattern shipped with an upgrade.
        Rows tracked under a now-excluded path stop appearing in the local walk,
        and the planner reads that absence as "vanished", i.e. a DELETION. The
        day the developer-litter patterns landed that would have planned the
        trashing of roughly a third of the mirror. Running the forget pass BEFORE
        any planning turns the change into a no-op instead.

        Full sweep every start/reload rather than a version-stamped one-shot: it
        is a single folded query per folder (sub-second at 300k rows), it is
        idempotent, and it costs nothing to be right after ANY exclude change.
        """
        patterns = self._effective_excludes()
        for folder in getattr(self.cfg, "folders", None) or []:
            if getattr(folder, "is_file", False):
                continue
            try:
                dropped = self.manifest.forget_excluded(folder.name, patterns)
            except Exception as exc:
                self._log("warning",
                          "exclude migration failed for %s: %s", folder.name, exc)
                continue
            if dropped:
                self._log("info",
                          "%s: forgot %d manifest row(s) now matched by the "
                          "exclude patterns (no deletion is propagated)",
                          folder.name, dropped)

    def _recompute_roots(self):
        # type: () -> None
        """(Re)derive data/trash/heartbeat roots from cfg.dest_rel.

        Also re-points the trash manager so pruning and Phase-1 backup-dir writes
        always target the same trash root as the configured destination.
        """
        mp = os.fspath(self.mountpoint)
        dest_rel = getattr(self.cfg, "dest_rel", "MyCloud") or "MyCloud"
        self.data_root = os.path.join(mp, paths.data_rel(dest_rel))
        self.trash_root = os.path.join(mp, paths.trash_rel(dest_rel))
        self.heartbeat_path = os.path.join(mp, paths.heartbeat_rel(dest_rel))
        try:
            self.trash_mgr.trash_root = self.trash_root
        except Exception:
            pass

    def _ensure_dest_layout(self):
        # type: () -> None
        """Create <dest>/data and <dest>/.trash; notify on a dest_rel change.

        Runs on every pass (mount/first pass and after). Directory creation is
        idempotent. When the stored dest_rel differs from the configured one the
        change is journalled and the user is notified that the OLD location's
        data is left untouched (PLAN 12.1); the stored value is then advanced so
        the notification fires exactly once per change.
        """
        self._recompute_roots()
        for directory in (self.data_root, self.trash_root):
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as exc:
                self._log("warning", "could not create %s: %s", directory, exc)

        current = getattr(self.cfg, "dest_rel", "MyCloud") or "MyCloud"
        stored = self.state.get().get("dest_rel")
        if stored == current:
            return
        if stored is not None:
            # A genuine change (not the first-ever run): the old mirror stays put.
            self.state.journal(
                {"event": "dest_changed", "old": stored, "new": current})
            self.notifier.notify(
                "dest_changed", "MyCloud backup location changed",
                "The backup destination changed from '%s' to '%s'. The data at "
                "the old location is left untouched (orphaned); remove it "
                "manually if you no longer need it." % (stored, current),
                force=True)
        try:
            self.state.update(dest_rel=current)
        except Exception:
            pass

    # -- live progress -----------------------------------------------------

    def _make_progress(self, name):
        # type: (str) -> object
        """Return an on_progress(files_done, current) callback bound to a folder."""
        def _cb(files_done, current):
            # type: (int, str) -> None
            self._on_progress(name, files_done, current)
        return _cb

    def _progress_entry(self, name, phase, files_done, current):
        # type: (str, str, int, Optional[str]) -> Dict[str, Any]
        """Build a full progress snapshot (PLAN M5 phases + bytes).

        last_transfer_at / batches_done / batches_total exist for ONE reason:
        making "is it still working?" answerable at a glance (2026-07-27 user
        report, and the same trap fooled the author repeatedly). files_done
        advances in JUMPS because rsync block-buffers its itemize output into the
        pipe (tens of lines at a time) and a batch is up to 500 files, so a
        by-the-numbers display can sit frozen for a minute while the transfer is
        perfectly healthy. A wall-clock stamp of the last transfer line plus a
        coarse batch counter both keep moving, so the UI can say "last activity
        12s ago · batch 3/12" instead of looking paused.
        """
        return {
            "active": True,
            "folder": name,
            "phase": phase,
            "files_done": int(files_done),
            "total_files": self._pass_total_files,
            "bytes_done": int(self._bytes_done),
            "bytes_total": self._pass_total_bytes,
            "current": current,
            "started_at": self._progress_started_at,
            "last_transfer_at": self._last_transfer_at,
            "batches_done": self._batches_done,
            "batches_total": self._batches_total,
        }

    def _begin_folder_progress(self, name):
        # type: (str) -> None
        """Roll live progress onto a new folder in the "planning" phase (PLAN M5).

        Written at the START of each folder so a folder that emits no transfer
        lines never leaves the PREVIOUS folder's progress on screen. The phase
        begins as "planning" (the local manifest walk; "scanning" no longer
        exists) and moves to downloading/transferring as the pipeline proceeds;
        the heartbeat re-stamps whichever phase is current.
        """
        self._pass_total_files = None
        self._pass_total_bytes = None
        self._bytes_done = 0
        # Batch counters belong to one folder's upload stage; clear them so the
        # UI never shows the previous folder's "batch 7/7" against a new folder.
        self._batches_done = 0
        self._batches_total = None
        entry = self._progress_entry(name, "planning", 0, None)
        with self._progress_lock:
            self._progress_last = entry
            self._progress_last_write = 0.0  # let the first transfer line write
            try:
                self.state.update(progress=dict(entry))
            except Exception:
                pass

    def _set_progress_phase(self, name, phase, files_done=0, current=None):
        # type: (str, str, int, Optional[str]) -> None
        """Force a phase transition (planning->downloading->transferring/verifying)."""
        entry = self._progress_entry(name, phase, files_done, current)
        with self._progress_lock:
            self._progress_last = entry
            self._progress_last_write = 0.0
            try:
                self.state.update(progress=dict(entry))
            except Exception:
                pass

    def _on_progress(self, name, files_done, current, phase="transferring"):
        # type: (str, int, str, str) -> None
        """Throttled (>= _progress_min_interval s) progress write to state.json.

        Throttling is the daemon's job (the engine calls once per transfer line);
        a write failure is swallowed since progress is observability-only.
        """
        now = time.monotonic()
        # Every transferring-phase call is one rsync itemize line = live upload
        # progress; record it (even when the state-write below is throttled) so
        # the watchdog can distinguish a starved guard read from a wedged session,
        # and so the UI can show how long ago the last file actually moved.
        if phase == "transferring":
            self._last_upload_mono = now
            try:
                self._last_transfer_at = self._now().isoformat()
            except Exception:
                pass
        if (now - self._progress_last_write) < self._progress_min_interval:
            return
        self._progress_last_write = now
        entry = self._progress_entry(name, phase, files_done, current)
        with self._progress_lock:
            self._progress_last = entry
            try:
                self.state.update(progress=dict(entry))
            except Exception:
                pass

    def _on_transfer_alive(self):
        # type: () -> None
        """rsync is still producing output (a --progress redraw while one large
        file transfers). Counts as upload progress for the watchdog's stall and
        load-tolerance rules; itemize lines alone left a single file taking
        longer than _TRANSFER_STALL_KILL looking stalled, killed and restarted
        from zero forever.

        Also the GUI's "最近传输" stamp: a single large file used to leave
        last_transfer_at frozen at its start for the whole transfer, which the
        GUI's stall detection reads as a wedged pass."""
        self._last_upload_mono = time.monotonic()
        try:
            self._last_transfer_at = self._now().isoformat()
        except Exception:
            pass

    def _progress_heartbeat_loop(self):
        # type: () -> None
        """Every ~3s: re-stamp the progress snapshot while a pass, an offline
        prefetch OR a deep verify runs, AND (its liveness role) refresh
        updated_at whenever the
        daemon is in an OFFLINE loop. The latter is what closes the observed
        ~34-min staleness window: the offline iteration has segments the main
        thread cannot touch during -- the recheck mount_smbfs attempt (main
        thread blocked in the subprocess) and, worst, an in-thread os.listdir /
        os.path.ismount in the bare-dir precheck that can wedge for minutes on a
        half-dead SMB vnode (no util.run timeout bounds it). Neither the
        main-loop idle-touch (MOUNTED_IDLE only) nor _offline_wait (only during
        the backoff, not during those calls) covers them. This background thread
        does, at the 3s cadence, so no offline segment can outrun the 120s
        freshness budget. Exits when the daemon stops.

        _verifying counts too (T-C1, 2026-07-30): a deep verify walks the NAS for
        HOURS on the main thread without touching state.json, so without this the
        GUI's staleness heuristic reported "守护进程无响应" for the whole run."""
        while not self._stop.is_set():
            self._stop.wait(self._progress_hb_interval)
            if self._stop.is_set():
                break
            if (self._syncing.is_set() or self._prefetching.is_set()
                    or self._verifying.is_set()):
                with self._progress_lock:
                    # Re-check under the lock: if the pass/prefetch/verify just
                    # cleared progress (inactive) we must not re-stamp an active
                    # snapshot.
                    if not (self._syncing.is_set() or self._prefetching.is_set()
                            or self._verifying.is_set()):
                        continue
                    last = self._progress_last
                    if last is None:
                        continue
                    snap = dict(last)
                    # The snapshot is from the last THROTTLED write; the
                    # transfer stamp moves on every rsync line (and --progress
                    # redraw) in between.
                    if (snap.get("phase") == "transferring"
                            and self._last_transfer_at):
                        snap["last_transfer_at"] = self._last_transfer_at
                    try:
                        self.state.update(progress=snap)
                    except Exception:
                        pass
            elif self._offline_active.is_set() or self._main_busy.is_set():
                # Offline/backoff liveness, or post-pass work: no progress
                # snapshot to re-stamp, just keep updated_at fresh so a slow
                # segment never looks like a dead daemon.
                self._idle_touch()

    def _clear_progress(self):
        # type: () -> None
        # Under the lock so a concurrent heartbeat re-stamp cannot land AFTER
        # this inactive write and leave a stale "active" progress on screen.
        with self._progress_lock:
            self._progress_last = None
            try:
                self.state.update(progress={"active": False})
            except Exception:
                pass

    def _clear_stale_liveness_on_start(self):
        # type: () -> None
        """Neutralize a prior run's progress/prefetch liveness on startup.

        Called once at the top of run() before the heartbeat starts, so a
        leftover progress={active:true, phase:"downloading"} or prefetch=
        {active:true} from a killed run cannot look live after a restart.
        """
        self._clear_progress()
        try:
            pf = self.state.get().get("prefetch")
            if isinstance(pf, dict) and pf.get("active"):
                updated = dict(pf)
                updated["active"] = False
                self.state.update(prefetch=updated)
        except Exception:
            pass

    def _idle_touch(self):
        # type: () -> None
        """Refresh state.json's updated_at without changing any field (A2).

        Called from the MAIN loop while no sync is in flight, so it can never
        race the progress heartbeat's writes (which only run while syncing) nor a
        folder_update: every state write funnels through StateStore on this same
        thread. The GUI's staleness heuristic keys off updated_at, so a quiet
        MOUNTED_IDLE stretch with nothing to sync must still show liveness.
        """
        try:
            self.state.update()
        except Exception:
            pass

    def _offline_wait(self, seconds):
        # type: (float) -> None
        """Interruptible offline/backoff wait that keeps state.json fresh.

        Every offline stretch -- backoff between mount attempts, recovery
        remount loops, a blocked/idle prefetch window -- can span up to the 600s
        backoff cap, and NEITHER the main-loop idle-touch (MOUNTED_IDLE only) NOR
        the progress heartbeat (syncing/prefetching only) covers it. So a plain
        ``_stop.wait(600)`` let updated_at go stale for minutes, tripping the
        GUI's "守护进程无响应" banner and any external freshness watchdog. This
        instead touches updated_at at least every _IDLE_TOUCH_INTERVAL (30s) --
        including immediately at entry and exit -- so freshness never lapses.

        Runs on the caller's (main) thread during offline stretches when neither
        _syncing nor _prefetching is set, so the touch never races the heartbeat.
        Returns promptly when stop is set.
        """
        remaining = max(0.0, float(seconds))
        while True:
            self._idle_touch()
            if remaining <= 0 or self._stop.is_set():
                return
            chunk = min(remaining, _IDLE_TOUCH_INTERVAL)
            self._stop.wait(chunk)
            remaining -= chunk

    # -- source enumeration (directory vs single-file) ---------------------

    def _scan_roots(self):
        # type: () -> Any
        """Split configured sources into watch roots for change detection.

        Returns (dir_roots, shallow_roots, parent_to_files):
          dir_roots        recursive snapshot/kqueue roots (directory sources)
          shallow_roots    the PARENT dir of each file source, watched
                           non-recursively (its mtime bumps on child churn)
          parent_to_files  abspath(parent) -> [str(file src), ...] for mapping a
                           dirty parent back onto the file spec(s) it covers
        """
        dir_roots = []  # type: List[Any]
        shallow = []  # type: List[str]
        parent_to_files = {}  # type: Dict[str, List[str]]
        for folder in self.cfg.folders:
            if getattr(folder, "is_file", False):
                parent = os.path.dirname(os.path.abspath(os.fspath(folder.src)))
                shallow.append(parent)
                parent_to_files.setdefault(parent, []).append(str(folder.src))
            else:
                dir_roots.append(folder.src)
        return dir_roots, shallow, parent_to_files

    def _build_scope(self, pending):
        # type: (set) -> Dict[str, List[str]]
        """Fold dirty dirs into a per-source scope, mapping file parents to files.

        Directory sources go through scanner.collapse_dirty as before; a dirty
        file-source parent directory is mapped to that file's src (scope value
        ["."], since a single file has no subtree).
        """
        dir_srcs = [f.src for f in self.cfg.folders
                    if not getattr(f, "is_file", False)]
        scope = scanner.collapse_dirty(pending, dir_srcs)
        _dirs, _shallow, parent_to_files = self._scan_roots()
        if parent_to_files:
            dirty_real = set()
            for path in pending:
                try:
                    dirty_real.add(os.path.realpath(os.fspath(path)))
                except Exception:
                    continue
            for parent, files in parent_to_files.items():
                if os.path.realpath(parent) in dirty_real:
                    for src in files:
                        scope.setdefault(src, ["."])
        return scope

    def _now(self):
        # type: () -> datetime
        return datetime.now(timezone.utc)

    def _set_state(self, value):
        # type: (str) -> None
        if value in ("MOUNTED_IDLE", "SYNCING"):
            # Online again: retire the persisted guard-failure mechanism so the
            # GUI's permission/offline guidance disappears with the cause.
            self.state.update(state=value, guard=None)
        else:
            self.state.update(state=value)

    def _log(self, level, msg, *args):
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(msg, *args)

    # -- startup: lock, capability probe, online ---------------------------

    def _acquire_lock(self):
        # type: () -> bool
        """Take an exclusive non-blocking flock and record our PID.

        Returns False if another instance already holds it (a clean exit that
        launchd, configured KeepAlive={SuccessfulExit:false}, will not respawn).
        """
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            self._log("error", "cannot open lock file %s: %s", self.lock_path, exc)
            # Not "another instance is running": exit non-zero so launchd's
            # KeepAlive retries instead of leaving the backup silently down.
            self._lock_error = True
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            os.close(fd)
            self._log("info", "another MyCloud daemon holds the lock; exiting cleanly")
            return False
        try:
            os.ftruncate(fd, 0)
            os.write(fd, ("%d\n" % os.getpid()).encode("ascii"))
            os.fsync(fd)
        except OSError:
            pass
        self._lock_fd = fd
        return True

    def _release_lock(self):
        # type: () -> None
        if self._lock_fd is None:
            return
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._lock_fd)
        except OSError:
            pass
        self._lock_fd = None

    def _upload_selftest(self):
        # type: () -> None
        """Run the real upload argv once on a local scratch tree at startup.

        The capability probe below uses its own argv, so it stayed green on
        the gateway while every real upload failed with "unknown option".
        A failure is logged and shown in the app (last_error) right away."""
        selftest = getattr(self.engine, "batch_selftest", None)
        if not callable(selftest):
            return
        try:
            with tempfile.TemporaryDirectory(dir=self.runtime_dir) as tmp:
                problem = selftest(tmp)
        except Exception as exc:  # never crash startup over a self-test
            self._log("warning", "upload self-test could not run: %s", exc)
            return
        if isinstance(problem, str) and problem:
            self._log("error", "upload self-test FAILED: %s -- uploads cannot "
                               "work until this is fixed", problem)
            # Kept until an upload batch really succeeds: a pass with nothing
            # to upload proves nothing about the argv.
            self._upload_broken = _USAGE_MSG % problem
            try:
                self.state.update(last_error=self._upload_broken)
            except Exception:
                pass
        else:
            self._upload_broken = None

    def _probe_dangling_links(self):
        # type: () -> None
        """Decide whether dangling symlinks can ride in upload batches.

        On the gateway (rsync 2.6.9) they copy as links; under openrsync one of
        them drops the rest of its batch, so they stay out (and are listed as
        failing). Decided by running the real upload argv, never by version."""
        probe = getattr(self.engine, "dangling_link_selftest", None)
        ok = False
        if callable(probe):
            try:
                with tempfile.TemporaryDirectory(dir=self.runtime_dir) as tmp:
                    ok = probe(tmp) is True
            except Exception as exc:  # never crash startup over a probe
                self._log("warning", "dangling-symlink probe could not run: %s", exc)
                ok = False
        self._dangling_links_ok = ok
        self._log("info", "dangling symlinks: %s",
                  "backed up as links (this rsync copies them)" if ok else
                  "kept out of uploads (this rsync drops the rest of a batch on one)")

    def _capability_check(self):
        # type: () -> None
        """Probe openrsync behavior and fail closed on a fingerprint change.

        First run stores the fingerprint. A stored key whose probed value
        CHANGED disables deletion propagation (Phase 2) until a human clears
        it, while copying (Phase 1) continues. A probe key the stored
        fingerprint does not know yet (a NEW probe added by an upgrade, e.g.
        inplace_updates_ok on 2026-07-21) is recorded silently -- it is a first
        observation, not a behavior change, so it must not freeze deletions on
        every already-deployed machine.

        Also gates the PLAN-15 --inplace fast path: inplace uploads run only
        while the probe demonstrates them working (fail-closed to the -b path).
        """
        self._upload_selftest()
        self._probe_dangling_links()
        data = self.state.get()
        self.deletion_disabled = bool(data.get("deletion_disabled", False))
        stored = data.get("capability_fingerprint")

        fingerprint = None
        try:
            with tempfile.TemporaryDirectory(dir=self.runtime_dir) as tmp:
                probe = self.engine.capability_probe(tmp, self.engine.rsync_bin)
            fingerprint = json.dumps(probe, sort_keys=True)
        except Exception as exc:  # probe must never crash startup
            self._log("warning", "capability probe failed: %s", exc)
            self._inplace_capable = False
            return

        self._inplace_capable = bool(probe.get("inplace_updates_ok", False))
        try:
            self.state.update(inplace_capable=self._inplace_capable)
        except Exception:
            pass
        if not self._inplace_capable:
            self._log("info",
                      "openrsync --inplace probe failed; new-file batches will "
                      "use the -b path (slower, equally safe)")

        if stored is None:
            self.state.update(capability_fingerprint=fingerprint)
            return

        stored_map = None
        try:
            parsed = json.loads(stored)
            if isinstance(parsed, dict):
                stored_map = parsed
        except (TypeError, ValueError):
            pass
        if stored_map is None:
            # Unparseable stored fingerprint (should not happen; we wrote it).
            # Treat as a first run rather than freezing deletions on junk.
            self._log("warning",
                      "stored capability fingerprint is unreadable; re-recording")
            self.state.update(capability_fingerprint=fingerprint)
            return

        changed = sorted(
            key for key in stored_map
            if key in probe and probe[key] != stored_map[key])
        vanished = sorted(key for key in stored_map if key not in probe)
        added = sorted(key for key in probe if key not in stored_map)

        if changed or vanished:
            self.deletion_disabled = True
            self.state.update(deletion_disabled=True)
            self.state.journal({
                "event": "capability_fingerprint_changed",
                "stored": stored,
                "current": fingerprint,
                "changed_keys": changed + vanished,
                "action": "deletion_propagation_disabled",
            })
            self.notifier.notify(
                "capability",
                "MyCloud disabled deletion propagation",
                "openrsync behavior changed; deletions are frozen until a human "
                "runs 'mycloudctl doctor'. Copying continues.",
                force=True,
            )
        elif added:
            # New probes only: known behavior is unchanged; record the wider
            # fingerprint so the next start compares all keys.
            self.state.update(capability_fingerprint=fingerprint)

    # -- mount guard: failing-layer logging + Local-Network soft-pass ------

    def _guard_check(self, child_timeout=10.0):
        # type: (float) -> bool
        """Effective mount guard used by every "can we sync now?" decision.

        Returns True when it is safe to sync. A plain guard_ok() pass returns
        True immediately. On a failure the failing layer is logged (rate-limited)
        via _report_guard_failure. The one special case is the macOS「本地网络」
        denial signature -- tcp445 the only failing layer while the share is
        still mounted -- which is soft-passed when the SMB session is provably
        alive (see the module docstring).

        child_timeout is the per-layer child-process timeout (default 10s); the
        guard watchdog raises it to 30s so a slow-under-load router does not read
        as a dead mount (A1).
        """
        if self.mount.guard_ok(timeout=child_timeout):
            return True

        layer = getattr(self.mount, "last_failure", "") or "unknown"
        if layer == "tcp445" and self._is_mounted_safe(child_timeout):
            soft = self._session_provably_alive(child_timeout)
            self._report_guard_failure("tcp445_local_network", soft_pass=soft)
            return soft

        self._report_guard_failure(layer, soft_pass=False)
        return False

    def _is_mounted_safe(self, child_timeout=10.0):
        # type: (float) -> bool
        try:
            return bool(self.mount.is_mounted(timeout=child_timeout))
        except Exception:
            return False

    def _session_provably_alive(self, child_timeout=10.0):
        # type: (float) -> bool
        """True iff the sentinel read and statshares both prove a live session.

        These two layers exercise real I/O over the mount (the child-cat sentinel
        read) and a live SMB session (statshares), so if they pass while only the
        in-process tcp445 probe fails, the mount is genuinely healthy and the
        failure is the Local-Network denial, not a dead session.
        """
        try:
            return bool(self.mount.sentinel_ok(timeout=child_timeout)
                        and self.mount.session_ok(timeout=child_timeout))
        except Exception:
            return False

    def _report_guard_failure(self, layer, soft_pass):
        # type: (str, bool) -> None
        """Rate-limited guard-failure logging (layer change or every 10 min).

        For the Local-Network-denial layer also emit a WARNING with the exact
        remediation hint and a 24h-throttled notification (category
        "local_network"); on soft-pass, say syncing continues rather than imply
        an outage.
        """
        # Persist the failing layer + mechanism for the GUI before the log
        # rate-limit: a sentinel `cat` denied with "Operation not permitted"
        # means Full Disk Access is missing for /usr/bin/python3, and the GUI
        # turns that into a button that opens the right settings pane
        # (2026-09-12: all operations must be completable from the GUI).
        try:
            self.state.update(guard={
                "layer": layer,
                "detail": getattr(self.mount, "last_failure_detail", ""),
                "soft_pass": bool(soft_pass),
            })
        except Exception:
            # Observability only: an unwritable state.json must not turn a guard
            # failure into an exception that escapes _main_loop and crash-loops
            # the daemon under launchd.
            pass
        now = time.monotonic()
        changed = (layer != self._guard_last_layer)
        if not changed and (now - self._guard_last_log_mono) < _GUARD_LOG_INTERVAL:
            return
        self._guard_last_layer = layer
        self._guard_last_log_mono = now

        if layer == "tcp445_local_network":
            # Log-only, deliberately NO notification. There is nothing the user
            # can do: macOS never lists a bare launchd python3 in the Local
            # Network pane (no entry, no + button -- 2026-07-31 user report
            # after hunting for it), and the block is already bypassed in code
            # (probe_reachable's smbutil fallback / this very soft-pass). A
            # toast that asks for an impossible action is worse than none.
            self._log("info", "%s", _LOCAL_NETWORK_HINT)
            if soft_pass:
                self._log("info",
                          "mount guard soft-pass: only the tcp445 probe failed "
                          "while the SMB session is alive (sentinel + statshares "
                          "OK); syncing continues")
            else:
                self._log("info",
                          "mount guard failing at layer 'tcp445' (share mounted "
                          "but the SMB session could not be verified); staying "
                          "offline / recovering")
            return

        detail = getattr(self.mount, "last_failure_detail", "")
        self._log("info",
                  "mount guard failing at layer '%s'%s; staying offline / recovering",
                  layer, " (%s)" % detail if detail else "")

    def _report_denial_recheck_success(self):
        # type: () -> None
        """A denial-recheck mount SUCCEEDED while the tcp445 probe was failing --
        the macOS Local-Network permission-denial signature (the kernel
        mount_smbfs path works while python3's socket probe is refused).

        Log-only, deliberately NO notification: the user cannot act on it --
        macOS never lists a bare launchd python3 in the Local Network pane
        (2026-07-31 user report after searching for the switch), and this very
        code path IS the working bypass. Backups continue; nothing is wrong.
        """
        self._log("info",
                  "denial-recheck: tcp445 probe blocked but mount_smbfs "
                  "SUCCEEDED -- macOS「本地网络」(Local Network) permission-denial "
                  "signature; the kernel mount path works while python3's socket "
                  "probe is refused. Backups continue over the mounted share.")

    def _try_online_gated(self):
        # type: () -> bool
        """One gated mount+guard attempt from an offline loop. Returns True iff
        the share is now mounted AND the guard passes (ready for MOUNTED_IDLE).

        Gate (away-from-home stability): invoke mount_smbfs only when the
        reachability probe succeeds -- EXCEPT a Local-Network-denial recheck on
        every _DENIAL_RECHECK_EVERY-th consecutive probe failure, where even the
        brokered probe reported unreachable but the kernel mount may still work.
        Safe because util.run can no longer wedge. Any successful probe OR mount
        resets the streak; a recheck mount that succeeds logs the denial
        signature and fires the local_network hint. The caller performs its own
        success action (return / full_pass) when this is True.

        probe_reachable (not the bare socket probe) is the gate: on a machine
        whose launchd python3 is blocked from direct LAN sockets by macOS 26
        "Local Network" privacy, the brokered smbutil fallback still detects the
        NAS, so the mount is attempted on the FIRST offline iteration instead of
        waiting ~18 min for the every-5th-failure recheck.
        """
        via_recheck = False
        try:
            probed = bool(self.mount.probe_reachable())
        except Exception:
            probed = False
        if probed:
            self._probe_fail_streak = 0
        else:
            self._probe_fail_streak += 1
            if (self._probe_fail_streak % _DENIAL_RECHECK_EVERY) != 0:
                return False   # probe failed and not a recheck iteration -> gated off
            via_recheck = True
        if not self.mount.mount():
            detail = getattr(self.mount, "last_mount_error", "")
            if detail:
                # Tell the GUI WHY it cannot come online (e.g. "the share is
                # already mounted at /Volumes/SHARE ..."), not just OFFLINE.
                try:
                    self.state.update(guard={"layer": "mount", "detail": detail,
                                             "soft_pass": False})
                except Exception:
                    pass
            return False
        # A successful mount proves the connection works -> reset the streak.
        self._probe_fail_streak = 0
        if via_recheck:
            self._report_denial_recheck_success()
        ok = self._guard_check()
        if (not ok and getattr(self.mount, "last_mount_note", "") == "already_mounted"
                and self._guard_failure_is_session_level()):
            # mount() found our share already in place, but the session behind
            # it is dead (statshares/sentinel time out). Nothing else in this
            # loop would ever replace it -- drop it so the next attempt mounts
            # fresh. (A TCC "Operation not permitted" or a sentinel UUID
            # mismatch is not a session problem; remounting cannot help those.)
            self._log("info", "existing mount has a dead SMB session; unmounting "
                              "so the next attempt reconnects")
            self.mount.force_unmount()
        return ok

    def _guard_failure_is_session_level(self):
        # type: () -> bool
        layer = getattr(self.mount, "last_failure", "")
        detail = getattr(self.mount, "last_failure_detail", "") or ""
        if layer == "session":
            return True
        return layer == "sentinel" and "timed out" in detail

    def ensure_online(self):
        # type: () -> bool
        """Ensure the guard passes; recover a wedged mount or back off to remount.

        In --once mode only a single attempt is made. Returns True once online,
        False if giving up (only possible in --once mode).
        """
        if self._guard_check():
            self._set_state(MOUNTED_IDLE)
            return True

        # Mounted but unhealthy -> recover the wedged session first.
        try:
            mounted = self.mount.is_mounted()
        except Exception:
            mounted = False
        if mounted:
            self.recovery()
            if self._guard_check():
                # A request recovery()'s own pass raised is answered by this
                # healthy guard: left set, the initial pass aborted and
                # remounted the mount just verified.
                self._recovery_needed.clear()
                self._set_state(MOUNTED_IDLE)
                return True

        self._set_state(OFFLINE)
        self._reset_prefetch()
        self._probe_fail_streak = 0
        # Mark the whole offline loop so the progress heartbeat keeps updated_at
        # fresh through segments the main thread cannot touch during (the recheck
        # mount_smbfs, a wedged os.listdir in the bare-dir precheck). Cleared on
        # every exit path.
        self._offline_active.set()
        try:
            attempt = 0
            while not self._stop.is_set():
                # Touch liveness before the (up to 60s) mount attempt too.
                self._idle_touch()
                # NEVER mount over stray local data: if files were written into
                # the bare mount point while the share was offline, halt instead
                # of hiding them under a fresh mount (PLAN 6.2, "绝不删除").
                if not self._bare_dir_precheck():
                    return False
                # Gated mount: only after a live tcp445 probe, except the periodic
                # Local-Network-denial recheck (see _try_online_gated). A plain
                # probe-failed iteration falls straight through to the backoff wait.
                if self._try_online_gated():
                    self._recovery_needed.clear()
                    self._set_state(MOUNTED_IDLE)
                    self._finish_prefetch_going_online()
                    return True
                self._maybe_offline_notify()
                if self.once:
                    return False
                # Fill the idle backoff window with iCloud prefetch when
                # applicable (M4); otherwise this is the plain backoff wait.
                self._offline_interval(attempt)
                attempt += 1
            return False
        finally:
            self._offline_active.clear()

    def _bare_dir_precheck(self):
        # type: () -> bool
        """True if it is safe to mount; False (and halt) on stray local writes.

        Persists critical_halt in state.json so status shows DEGRADED and a
        restart re-detects the condition (this method runs on every mount
        attempt, including startup, so it never mounts over stray data). A clean
        bare dir clears any previously persisted halt.
        """
        try:
            if self.mount.is_mounted():
                return True
        except Exception:
            pass
        try:
            self.mount.ensure_bare_dir_safe()
        except CriticalLocalWriteDetected as exc:
            self._log("error", "critical local write detected before mount: %s", exc)
            self._degraded_halt = True
            self._set_state(DEGRADED)
            try:
                self.state.update(critical_halt=True)
            except Exception:
                pass
            self.notifier.notify(
                "critical", "MyCloud stopped: local write detected",
                "Files were written to the backup mount point while it was "
                "offline. Syncing is stopped to protect them; see the log.",
                force=True)
            return False
        except (MountError, OSError):
            # Still mounted, or the bare dir is missing/unreadable: no stray data
            # to protect here; let the mount attempt proceed (it will fail if the
            # destination is truly unreachable).
            return True
        if self.state.get().get("critical_halt"):
            try:
                self.state.update(critical_halt=False)
            except Exception:
                pass
        return True

    def _backoff_delay(self, attempt):
        # type: (int) -> float
        base = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
        return base * self.rng.uniform(0.8, 1.2)

    def _backoff_wait(self, attempt):
        # type: (int) -> None
        self._stop.wait(self._backoff_delay(attempt))

    def _maybe_offline_notify(self):
        # type: () -> None
        """Stay silent until offline_notify_hours since last success, then daily."""
        data = self.state.get()
        last_ok = data.get("last_sync_ok_at")
        if not last_ok:
            return
        try:
            last_dt = datetime.fromisoformat(last_ok)
        except (TypeError, ValueError):
            return
        elapsed_h = (self._now() - last_dt).total_seconds() / 3600.0
        if elapsed_h < self.cfg.offline_notify_hours:
            return
        self.notifier.notify(
            "offline",
            "MyCloud backup is offline",
            "The backup destination has been unreachable for %d hours." % int(elapsed_h),
            min_hours=24.0,
        )

    # -- offline iCloud prefetch (PLAN section 14 M4) ----------------------

    def _reset_prefetch(self):
        # type: () -> None
        """Start a fresh prefetch bookkeeping state for a new offline period."""
        self._prefetch_session = None
        self._prefetch_complete = False
        self._prefetch_done_notified = False

    def _finish_prefetch_going_online(self):
        # type: () -> None
        """Reconnect won: mark prefetch inactive so the GUI/status stop showing a
        download in flight before the follow-up upload pass takes over."""
        self._prefetching.clear()
        session = self._prefetch_session
        if session is not None:
            self._write_prefetch_state(session, active=False)
        # Only clear the live progress if it is currently a prefetch snapshot;
        # full_pass re-seeds its own progress immediately after this returns.
        with self._progress_lock:
            last = self._progress_last
            if isinstance(last, dict) and last.get("phase") == "downloading":
                self._progress_last = None
                try:
                    self.state.update(progress={"active": False})
                except Exception:
                    pass

    def _offline_interval(self, attempt):
        # type: (int) -> None
        """Wait out one reconnect-backoff window, prefetching iCloud files during
        the idle time when icloud_policy=download (M4).

        When prefetch does not apply (policy=skip, nothing pending, or already
        complete) this is exactly the old plain ``_stop.wait(backoff)``. When it
        does apply, the window is spent materializing dataless files a batch at a
        time; the NAS is re-probed (cheap tcp445) after every batch and a
        reachable NAS returns at once so the caller reconnects. The backoff
        deadline still bounds the window, so a mount retry is delayed by at most
        one in-flight batch -- reconnection is never starved by downloads.
        """
        delay = self._backoff_delay(attempt)
        deadline = time.monotonic() + delay
        if (getattr(self.cfg, "icloud_policy", "skip") != "download"
                or self._prefetch_complete
                or self._materialize_blocked_now()):
            # Not applicable (skip policy), already done, or materialization is
            # blocked by the launchd restriction -> plain idle backoff (kept fresh).
            self._offline_wait(delay)
            return
        if self._prefetch_session is None:
            self._prefetch_session = self._enumerate_prefetch()
        session = self._prefetch_session
        if session.exhausted:
            # Nothing (left) to prefetch this offline period.
            self._maybe_finish_prefetch(session)
            self._offline_wait(max(0.0, deadline - time.monotonic()))
            return

        self._prefetching.set()
        terminal = False
        try:
            while not self._stop.is_set():
                outcome = self._run_prefetch_batch(session)
                if outcome == "reconnect":
                    return  # reconnect immediately (no idle wait)
                if outcome == "blocked":
                    # Launchd iCloud restriction latched: stop prefetch for this
                    # session (subsequent windows fall to plain backoff above).
                    self._write_prefetch_state(session, active=False)
                    terminal = True
                    break
                if outcome == "halt":
                    # Disk floor / guard tripped: stop prefetch, idle the rest of
                    # the backoff, retry the disk check next window.
                    terminal = True
                    break
                if outcome == "done":
                    self._maybe_finish_prefetch(session)
                    terminal = True
                    break
                # outcome == "continue": more batches remain.
                if time.monotonic() >= deadline:
                    return  # next mount attempt is due (backoff kept on schedule)
        finally:
            self._prefetching.clear()
        if terminal:
            # Prefetch stopped/paused for this window (blocked / disk-halt / done):
            # drop the live "downloading" progress so the idle backoff does not
            # show an active download, then idle out the rest of the backoff.
            # _prefetching is now clear, so the progress heartbeat no longer covers
            # freshness -- _offline_wait keeps updated_at fresh on this thread with
            # no heartbeat race.
            self._clear_progress()
            self._offline_wait(max(0.0, deadline - time.monotonic()))

    def _enumerate_prefetch(self):
        # type: () -> _PrefetchSession
        """Enumerate pending dataless files under every configured source.

        Reuses the manifest's pure-lstat plan walk (never opens a file, so it
        cannot itself materialize anything) and its exclude handling; only the
        ``dataless`` set -- new/changed cloud-only files not yet backed up -- is
        collected. Single-file sources are probed directly. The result is a flat,
        batchable list plus the pending totals for progress.
        """
        entries = []  # type: List[Any]
        total_bytes = 0
        for folder in self.cfg.folders:
            if self._stop.is_set():
                break
            src = str(folder.src)
            if getattr(folder, "is_file", False):
                if (not os.path.lexists(src)
                        and manifest_mod.has_placeholder(src)):
                    # The NAS is unreachable here, so whether it is already
                    # backed up comes from the last online pass.
                    fst = self.state.get().get("folders", {}).get(folder.name, {})
                    isk = fst.get("icloud_skipped") if isinstance(fst, dict) else None
                    if not (isinstance(isk, dict) and isk.get("count")):
                        continue
                    entry = _cloud_stat(src)
                    size = entry[0] if entry is not None else 0
                    entries.append((folder.name, os.path.dirname(src),
                                    os.path.basename(src), src, size))
                    total_bytes += size
                    continue
                try:
                    st = os.lstat(src)
                    dl = bool(self.is_dataless_fn(src))
                except OSError:
                    continue
                except Exception:
                    dl = False
                if dl:
                    size = int(getattr(st, "st_size", 0))
                    entries.append((folder.name, os.path.dirname(src),
                                    os.path.basename(src), src, size))
                    total_bytes += size
                continue
            if not os.path.isdir(src):
                continue
            try:
                plan = self.manifest.plan_folder(
                    folder.name, src, self._effective_excludes(), self.dataless_check)
            except Exception as exc:
                self._log("warning", "prefetch enumerate failed for %s: %s",
                          folder.name, exc)
                continue
            for rel in plan.dataless:
                abspath = os.path.join(src, rel)
                entry = _cloud_stat(abspath)
                size = entry[0] if entry is not None else 0
                entries.append((folder.name, src, rel, abspath, size))
                total_bytes += size
        return _PrefetchSession(entries, total_bytes, self._now().isoformat())

    def _run_prefetch_batch(self, session):
        # type: (_PrefetchSession) -> str
        """Materialize the next <=200-file slice. Returns an outcome string:

          "halt"      disk floor / guard refused this batch (stop, idle backoff)
          "reconnect" the NAS came back (tcp445): stop, caller reconnects
          "done"      the session is exhausted (everything attempted)
          "continue"  more batches remain and the NAS is still unreachable
        """
        from . import icloud
        start = session.index
        end = min(start + _PREFETCH_BATCH_FILES, len(session.entries))
        batch = session.entries[start:end]
        if not batch:
            return "done"
        batch_bytes = sum(int(size) for (_n, _s, _r, _a, size) in batch)
        probe_src = batch[0][1] or self.runtime_dir

        # Absolute 25 GiB floor first, then the >=3x-batch guard. Either failing
        # halts prefetch (notified, rate-limited) without touching the disk.
        try:
            free = icloud.free_bytes(probe_src)
        except OSError:
            free = -1
        if free < 0 or free < _PREFETCH_MIN_FREE_BYTES:
            self.notifier.notify(
                "prefetch_disk",
                "MyCloud：本地磁盘空间不足，暂停 iCloud 预下载",
                "本地可用空间低于 25 GiB，已暂停后台预下载；腾出空间后会自动继续。",
                min_hours=24.0)
            return "halt"
        if not icloud.disk_guard_ok(probe_src, batch_bytes):
            self.notifier.notify(
                "prefetch_disk",
                "MyCloud：磁盘空间不足以下载该批 iCloud 文件",
                "下载该批 iCloud 文件需要至少 3 倍批体积的本地可用空间；已暂停预下载。",
                min_hours=24.0)
            return "halt"

        folder = batch[0][0]
        self._prefetching.set()
        self._write_prefetch_progress(session, folder)

        abspaths = [abspath for (_n, _s, _r, abspath, _sz) in batch]
        # Materialize OFF the offline-loop thread. A Python thread stuck in an
        # uninterruptible iCloud read cannot be cancelled, and util.run's timeout
        # reap (kill + blocking wait) can itself block on a wedged child, so
        # calling materialize_batch synchronously froze the whole reconnect loop
        # for an entire (huge, possibly infinite) batch -- the production hang.
        # Run it in a daemon thread and POLL for completion while staying
        # responsive to stop and to the NAS returning, so reconnection/shutdown
        # is delayed by at most one poll interval, never a whole batch. An
        # abandoned worker is a daemon thread: it finishes on its own (or dies
        # with the process) and can never block exit.
        result = {}  # type: Dict[str, Any]
        # Set when this batch is abandoned (NAS back / shutdown), so the worker
        # stops too instead of downloading on for the rest of its batch --
        # concurrently with the pass that has taken over.
        abandon = threading.Event()
        stop = _AnyEvent(self._stop, abandon)

        def _materialize():
            try:
                (result["mat"], result["failed"],
                 result["blocked"]) = self._materialize_probed(
                     abspaths, stop, workers=_PREFETCH_WORKERS)
            except Exception as exc:  # never let the worker die silently
                result["mat"], result["failed"] = [], list(abspaths)
                result["blocked"] = False
                result["error"] = exc

        worker = threading.Thread(target=_materialize,
                                  name="mycloud-prefetch-batch")
        worker.daemon = True
        worker.start()
        while True:
            worker.join(self._prefetch_poll_interval)
            if not worker.is_alive():
                break
            if self._stop.is_set():
                abandon.set()
                return "halt"       # daemon shutting down: abandon the worker.
            try:
                if self.mount.probe_tcp445(timeout=_PREFETCH_PROBE_TIMEOUT):
                    abandon.set()
                    return "reconnect"  # NAS back: uploads take priority now.
            except Exception:
                pass
        if result.get("error") is not None:
            self._log("warning", "prefetch materialize batch errored: %s",
                      result.get("error"))
        materialized = result.get("mat", [])
        failed = result.get("failed", [])

        session.index = end
        mat_set = set(materialized)
        batch_done_bytes = sum(
            int(size) for (_n, _s, _r, abspath, size) in batch
            if abspath in mat_set)
        session.done_files += len(materialized)
        session.done_bytes += batch_done_bytes
        session.batches += 1
        if failed:
            session.skipped += len(failed)
            self._record_prefetch_skipped(len(failed))

        self._write_prefetch_progress(session, folder)
        self._write_prefetch_state(session, active=True)
        self._log("info",
                  "prefetch batch done: %d files, %.1f MB (total %d/%d)",
                  len(materialized), batch_done_bytes / (1024.0 * 1024.0),
                  session.done_files, session.total_files)
        # One NDJSON checkpoint every N batches (and one at end, see finish).
        if session.batches % _PREFETCH_JOURNAL_EVERY == 0:
            self._journal_prefetch(session, "prefetch_progress")

        # Launchd iCloud restriction: a fully-skipped probe/batch means downloads
        # can never succeed here. Latch it and stop prefetch for this session.
        if result.get("blocked"):
            self._mark_materialize_blocked("offline prefetch")
            self._journal_prefetch(session, "prefetch_blocked")
            return "blocked"

        if self._stop.is_set():
            return "halt"
        # Re-probe the NAS BETWEEN batches: back? stop now and let uploads win.
        try:
            reachable = self.mount.probe_tcp445(timeout=_PREFETCH_PROBE_TIMEOUT)
        except Exception:
            reachable = False
        if reachable:
            return "reconnect"
        return "done" if session.exhausted else "continue"

    def _maybe_finish_prefetch(self, session):
        # type: (_PrefetchSession) -> None
        """Everything is downloaded while still offline: latch complete, mark the
        state inactive, clear progress, journal, and notify exactly once.

        A session that had nothing pending (no dataless files at all) only latches
        the complete flag -- it emits no state/journal/notification churn, so a
        download-policy daemon that is simply up to date and offline stays quiet.
        """
        self._prefetch_complete = True
        if session.total_files <= 0:
            return
        self._write_prefetch_state(session, active=False)
        self._clear_progress()
        self._journal_prefetch(session, "prefetch_done")
        if not self._prefetch_done_notified:
            self._prefetch_done_notified = True
            self.notifier.notify(
                "prefetch_done",
                "MyCloud：iCloud 预下载完成",
                "云端文件已全部下载完成，回到家庭网络后将自动开始上传",
                min_hours=24.0)

    def _write_prefetch_progress(self, session, folder):
        # type: (_PrefetchSession, str) -> None
        """Publish the live prefetch progress snapshot (phase "downloading")."""
        entry = {
            "active": True,
            "folder": folder,
            "phase": "downloading",
            "files_done": int(session.done_files),
            "total_files": int(session.total_files),
            "bytes_done": int(session.done_bytes),
            "bytes_total": int(session.total_bytes),
            "current": None,
            "started_at": session.started_at,
        }
        with self._progress_lock:
            self._progress_last = entry
            try:
                self.state.update(progress=dict(entry))
            except Exception:
                pass

    def _write_prefetch_state(self, session, active):
        # type: (_PrefetchSession, bool) -> None
        """Publish the compact state["prefetch"] summary for status/GUI.

        Carries the latched ``blocked`` flag so it is never dropped by a later
        active/inactive write once the launchd restriction has been detected.
        """
        payload = {
            "active": bool(active),
            "done": int(session.done_files),
            "total": int(session.total_files),
        }
        if self._materialize_blocked:
            payload["blocked"] = True
            at = self._materialize_blocked_at or self._now()
            payload["blocked_at"] = at.isoformat()
        try:
            self.state.update(prefetch=payload)
        except Exception:
            pass

    def _materialize_probed(self, abspaths, stop_event, workers=None):
        # type: (List[str], Any, Optional[int]) -> Any
        """icloud.materialize_batch, probe-first until a download has worked.

        Returns (materialized, failed, blocked). blocked means the probe (or,
        once proven, the whole batch) came back with nothing downloaded and
        essentially everything timed out: the launchd restriction's signature.
        When a probe says so, the rest of the batch is NOT attempted (and not
        counted as failed -- it simply stays pending). A stop never reads as
        blocked: files it cut short were not given their chance.
        """
        from . import icloud
        kwargs = dict(is_dataless_fn=self.is_dataless_fn, read_fn=self.read_fn,
                      stop_event=stop_event, run_cmd=self.run_cmd,
                      per_file_timeout=self._materialize_timeout)
        if workers is not None:
            kwargs["workers"] = workers
        materialized = []  # type: List[str]
        failed = []  # type: List[str]
        # Vanished since listed, or refused outright by brctl: neither says
        # anything about the launchd restriction, so both stay out of its
        # signature (a renamed folder of cloud-only files used to latch every
        # download off for 24 h).
        gone = []  # type: List[str]
        rejected = []  # type: List[str]
        kwargs["gone"] = gone
        kwargs["rejected"] = rejected

        def _attempted(batch):
            # type: (List[str]) -> int
            skip = set(gone) | set(rejected)
            return len([p for p in batch if p not in skip])

        rest = list(abspaths)
        if not self._materialize_proven and len(rest) > _MATERIALIZE_PROBE_FILES:
            head = rest[:_MATERIALIZE_PROBE_FILES]
            rest = rest[_MATERIALIZE_PROBE_FILES:]
            got, bad = icloud.materialize_batch(head, **kwargs)
            materialized.extend(got)
            failed.extend(bad)
            if got:
                self._materialize_proven = True
            elif (not stop_event.is_set()
                    and self._materialize_blocked_batch(
                        got, bad, _attempted(head))):
                return materialized, failed, True
        if rest:
            if stop_event.is_set():
                failed.extend(rest)
            else:
                got, bad = icloud.materialize_batch(rest, **kwargs)
                materialized.extend(got)
                failed.extend(bad)
        if materialized:
            self._materialize_proven = True
        blocked = (not stop_event.is_set()
                   and self._materialize_blocked_batch(
                       materialized, failed, _attempted(abspaths)))
        if gone:
            self._log("info", "%d cloud-only file(s) vanished before their "
                              "download (moved or deleted)", len(gone))
        return materialized, failed, blocked

    @staticmethod
    def _materialize_blocked_batch(materialized, failed, batch_size):
        # type: (List[str], List[str], int) -> bool
        """True iff a COMPLETED batch signals the launchd iCloud restriction.

        The signature is: nothing materialized while >=90% of the batch timed
        out/failed. Under the restriction every read times out, so a full batch
        comes back 0 materialized / all skipped -- a strong, unambiguous signal
        (a genuinely working environment materializes at several files/second).
        """
        if batch_size <= 0:
            return False
        return len(materialized) == 0 and len(failed) >= 0.9 * batch_size

    def _restore_materialize_blocked(self):
        # type: () -> bool
        """Read the persisted launchd-iCloud-restriction latch from state.json.

        Kept separate (and defensive) so a malformed/absent state document simply
        yields False -- a fresh probe -- rather than breaking startup. A latch
        older than _MATERIALIZE_BLOCKED_TTL_S, or one from before latches were
        timestamped, is dropped: re-probing costs one per-file timeout now, and
        the restriction it recorded may no longer apply (MaterializeDatalessFiles
        in the agent plist). It used to be permanent until edited by hand.
        """
        try:
            prefetch = self.state.get().get("prefetch")
            if not (isinstance(prefetch, dict) and prefetch.get("blocked")):
                return False
            stamp = prefetch.get("blocked_at")
            blocked_at = datetime.fromisoformat(stamp) if stamp else None
            if blocked_at is not None and blocked_at.tzinfo is None:
                blocked_at = blocked_at.replace(tzinfo=timezone.utc)
        except Exception:
            return False
        if (blocked_at is None or (self._now() - blocked_at).total_seconds()
                >= _MATERIALIZE_BLOCKED_TTL_S):
            self._clear_prefetch_blocked_state()
            return False
        self._materialize_blocked_at = blocked_at
        return True

    def _materialize_blocked_now(self):
        # type: () -> bool
        """The blocked latch, expiring _MATERIALIZE_BLOCKED_TTL_S after it was
        set (a daemon runs for weeks; a latch from its first day must not
        outlive a lifted restriction)."""
        if not self._materialize_blocked:
            return False
        at = self._materialize_blocked_at
        if at is not None and (self._now() - at).total_seconds() < _MATERIALIZE_BLOCKED_TTL_S:
            return True
        if at is None:
            # Latched by a caller that set the flag directly: stamp it now.
            self._materialize_blocked_at = self._now()
            return True
        self._materialize_blocked = False
        self._materialize_blocked_at = None
        self._clear_prefetch_blocked_state()
        self._log("info", "re-probing iCloud downloads: the blocked latch expired")
        return False

    def _clear_prefetch_blocked_state(self):
        # type: () -> None
        try:
            cur = self.state.get().get("prefetch")
            if isinstance(cur, dict) and cur.get("blocked"):
                payload = dict(cur)
                payload["blocked"] = False
                payload.pop("blocked_at", None)
                self.state.update(prefetch=payload)
        except Exception:
            pass

    def _mark_materialize_blocked(self, where):
        # type: (str) -> None
        """Latch the launchd iCloud restriction: warn ONCE, notify once, surface
        state["prefetch"]["blocked"]. Idempotent (guarded by the flag)."""
        if self._materialize_blocked:
            return
        self._materialize_blocked = True
        self._materialize_blocked_at = self._now()
        self._log("warning",
                  "iCloud materialization appears blocked in the launchd-agent "
                  "context (%s): a full batch timed out with 0 files downloaded, "
                  "yet the same read works from a user shell. This is a macOS "
                  "restriction -- fileproviderd refuses download requests from a "
                  "background agent's process tree -- not a MyCloud bug. Pausing "
                  "iCloud downloads; cloud-only files will back up once you open "
                  "or download them yourself.", where)
        self.notifier.notify(
            "prefetch_blocked",
            "MyCloud：iCloud 下载被系统限制",
            "系统限制：后台进程无法触发 iCloud 下载，云端文件将在您手动打开或另行下载后自动备份",
            min_hours=24.0)
        self._set_prefetch_blocked_state()

    def _set_prefetch_blocked_state(self):
        # type: () -> None
        """Merge blocked=true into state["prefetch"] without dropping other keys."""
        try:
            cur = self.state.get().get("prefetch")
            payload = dict(cur) if isinstance(cur, dict) else {}
            payload["blocked"] = True
            at = self._materialize_blocked_at or self._now()
            payload["blocked_at"] = at.isoformat()
            self.state.update(prefetch=payload)
        except Exception:
            pass

    def _record_prefetch_skipped(self, n):
        # type: (int) -> None
        """Increment the cumulative state["prefetch_skipped"] counter (M4 4c)."""
        if n <= 0:
            return
        try:
            cur = self.state.get().get("prefetch_skipped", 0)
            cur = int(cur) if isinstance(cur, (int, float)) else 0
        except Exception:
            cur = 0
        try:
            self.state.update(prefetch_skipped=cur + int(n))
        except Exception:
            pass

    def _journal_prefetch(self, session, event):
        # type: (_PrefetchSession, str) -> None
        try:
            self.state.journal({
                "event": event,
                "files_done": session.done_files,
                "total_files": session.total_files,
                "bytes_done": session.done_bytes,
                "bytes_total": session.total_bytes,
                "skipped": session.skipped,
                "batches": session.batches,
            })
        except Exception:
            pass

    def full_pass(self, scope=None):
        # type: (Optional[Dict[str, List[str]]]) -> bool
        """Run one two-phase pass over the configured folders.

        scope maps str(source root) -> subtree relpaths (from
        scanner.collapse_dirty); None means every folder, whole. Returns True
        iff every processed folder synced cleanly. Raises SyncFailure on a hard
        Phase 1 failure so the caller can enter recovery().
        """
        all_ok = True
        aborted = False
        completed = False
        self._pass_batches_ok = 0
        # _syncing is set INSIDE the try: anything raising between "a pass is
        # running" and the finally used to leave it set forever -- the GUI then
        # showed 同步中 indefinitely, the idle liveness touch stayed suppressed,
        # and the watchdog kept ticking against a pass that no longer existed.
        try:
            self._syncing.set()
            self._set_state(SYNCING)
            self._ensure_dest_layout()
            stamp = util.make_stamp(self._now())
            # Consume any deletion-approval token ONCE for the whole pass, so
            # every held folder is released together and a stale/unused token
            # can never silently pre-approve a future, unrelated mass deletion.
            self._approval_active = self._take_approval()
            # Seed live-progress bookkeeping for this pass (started_at is
            # pass-level; folder/files_done are updated per transfer by the
            # engine callback).
            self._progress_started_at = self._now().isoformat()
            self._progress_last_write = 0.0
            # Directory sources go through the tcc self-test (one scandir per
            # source triggers a TCC denial). File sources are handled separately:
            # scandir on a file raises NotADirectoryError, and a DELETED file
            # must still reach _sync_file so its mirror is archived -- the tcc
            # path would misfile it as sourceMissing and skip that archival.
            in_scope = []  # type: List[Any]
            file_specs = []  # type: List[Any]
            for folder in self.cfg.folders:
                if scope is not None and str(folder.src) not in scope:
                    continue
                if getattr(folder, "is_file", False):
                    file_specs.append(folder)
                else:
                    in_scope.append(folder)

            from . import notify as notify_mod
            accessible, denied = notify_mod.tcc_selftest(
                in_scope, self.notifier, self.logger, self._settings_opened)
            for folder in denied:
                if not os.path.exists(str(folder.src)):
                    self.state.folder_update(folder.name, state=F_SOURCE_MISSING)
                else:
                    self.state.folder_update(folder.name, state=F_NEEDS_PERMISSION)
                all_ok = False

            # File sources are always processed (copy when present, archive when
            # gone); _sync_file applies its own parent-dir / st_dev guards.
            for folder in list(accessible) + file_specs:
                subtrees = ["."]
                if scope is not None:
                    subtrees = list(scope.get(str(folder.src), ["."]))
                self._raise_if_recovery_needed(
                    "pass stopped before %s" % getattr(folder, "name", "?"))
                gate = self._free_space_gate()
                if gate == "stop":
                    aborted = True
                    break
                try:
                    ok = self._sync_folder(folder, subtrees, stamp)
                except OSError as exc:
                    # A bad destination layout (e.g. dest_rel, or a folder name,
                    # collides with an EXISTING FILE on the share, so
                    # data_root/<name> cannot be created) surfaces as
                    # NotADirectoryError out of rsync's os.makedirs. Degrade this
                    # one folder rather than let the OSError escape full_pass and
                    # crash the daemon into a launchd respawn loop.
                    self._log("warning",
                              "sync of %s failed with an OS error (destination "
                              "layout problem?): %s",
                              getattr(folder, "name", "?"), exc)
                    try:
                        self.state.folder_update(folder.name, state=F_DEGRADED)
                    except Exception:
                        pass
                    all_ok = False
                    continue
                if not ok:
                    all_ok = False
            completed = True
        finally:
            self._syncing.clear()
            # Pass over (success OR failure): mark progress inactive.
            self._clear_progress()
            if not completed:
                self._end_interrupted_pass()

        if not aborted:
            self._main_busy.set()
            try:
                self._post_pass(scope, all_ok)
            finally:
                self._main_busy.clear()
        self._set_state(MOUNTED_IDLE)
        return all_ok and not aborted

    def _end_interrupted_pass(self):
        # type: () -> None
        """A pass that raised must not leave 同步中 behind. Every failure used
        to go through recovery(), which rewrote the state; a failure that now
        keeps the mount (backoff) left the daemon SYNCING and the interrupted
        folder 'syncing' indefinitely -- the GUI showed a working sync while
        every upload failed."""
        try:
            folders = self.state.get().get("folders", {})
            if isinstance(folders, dict):
                for fname, entry in folders.items():
                    if isinstance(entry, dict) and entry.get("state") == F_SYNCING:
                        self.state.folder_update(fname, state=F_DEGRADED)
            # Not _set_state: that also retires the guard-failure reason the
            # watchdog just saved, which the GUI shows once recovery goes
            # OFFLINE. Under MOUNTED_IDLE the GUI ignores it.
            self.state.update(state=MOUNTED_IDLE)
        except Exception as exc:  # never mask the exception being raised
            self._log("warning", "could not reset state after a failed pass: %s",
                      exc)

    def _raise_if_recovery_needed(self, where):
        # type: (str) -> None
        """End the pass once the watchdog has asked for recovery (mount gone,
        guard dead, transfer wedged). Only rsync stages used to notice -- by
        failing -- so a pass in any other stage (downloads, renames, deletions,
        NAS verification, the next folder's plan) kept working against a dead
        mount, and recovery waited behind it."""
        if self._recovery_needed.is_set():
            raise SyncFailure("%s: the watchdog requested recovery "
                              "(mount lost or wedged)" % where, kind="recovery")

    def _retry_backup_dir(self, backup_dir, name):
        # type: (str, str) -> str
        """A fresh <trash>/<stamp>/<name> for re-running a -b batch.

        Re-running -b into the SAME backup dir re-sends files that already
        landed (no -t, so their NAS mtimes differ) and rsync then moves the
        just-written copy over the old version the first attempt saved: the
        old version is gone (reproduced on rsync 2.6.9 and openrsync). A new
        stamp keeps both, and it still parses for retention pruning.
        """
        first = os.path.basename(os.path.dirname(backup_dir))
        stamp = util.make_stamp(self._now())
        if stamp == first:
            time.sleep(1.1)
            stamp = util.make_stamp(self._now())
        return os.path.join(self.trash_root, stamp, name)

    def _backing_off(self):
        # type: () -> bool
        return time.monotonic() < self._pass_backoff_until

    def _pass_failed(self, exc, where):
        # type: (SyncFailure, str) -> None
        """Route a failed pass. recovery() -- unmount, remount, re-plan
        everything -- only helps when the mount is the problem. It used to run
        for EVERY failure: when the gateway's rsync rejected an option on every
        upload, that meant an unmount/remount (「离线」) every few minutes for two
        months, and the reason never reached the app."""
        kind = getattr(exc, "kind", None)
        if kind in _TRANSPORT_KINDS:
            self._log("warning", "%s failed, entering recovery: %s", where, exc)
            self.recovery()
            return
        if not self._guard_check():
            self._log("warning", "%s failed and the mount guard fails too, "
                                 "entering recovery: %s", where, exc)
            self.recovery()
            return
        self._note_pass_failure(exc, where)

    def _note_pass_failure(self, exc, where):
        # type: (SyncFailure, str) -> None
        """A failure a remount cannot fix: keep the mount, say why, back off."""
        self._pass_fail_streak += 1
        # Bounded exponent: 2 ** 1024 overflows a float (a 43-day outage).
        delay = min(_PASS_FAIL_BACKOFF_MAX, _PASS_FAIL_BACKOFF_BASE
                    * 2 ** min(self._pass_fail_streak - 1, 16))
        self._pass_backoff_until = time.monotonic() + delay
        if getattr(exc, "kind", None) == "usage":
            msg = _USAGE_MSG % exc
            self._upload_broken = msg
        else:
            msg = ("上传失败（%s）。NAS 挂载正常，%d 分钟后自动重试；详情见日志。"
                   % (exc, int(delay // 60)))
        self._pass_error_msg = msg
        self._log("error", "%s failed (%s; mount healthy, not remounting); next "
                           "scheduled pass in %d s", where, exc, int(delay))
        try:
            self.state.update(last_error=msg)
        except Exception:
            pass

    def _note_pass_completed(self, scope):
        # type: (Optional[Dict[str, List[str]]]) -> bool
        """A pass finished without a SyncFailure: end the failure streak, the
        backoff and the message they wrote -- if the pass could have exercised
        what failed. A full pass did (whatever was pending was planned); a
        scoped pass only if it uploaded something. A rejected upload argv
        clears only once an upload batch actually landed. Held, missing or
        degraded folders do not matter here: their own state reports them.
        Returns True when the failure counts as over."""
        if self._upload_broken:
            # The argv itself was rejected: any landed upload batch proves it.
            proven = self._pass_batches_ok > 0
        else:
            # Anything else may be specific to one file or folder (a file the
            # NAS cannot take): only a full pass re-planned it. A scoped pass
            # elsewhere used to clear it every few minutes -- the message
            # flickered, the backoff never grew, 上次成功同步 stayed fresh.
            proven = scope is None
        if not proven:
            return False
        stale = set(m for m in (self._pass_error_msg, self._upload_broken) if m)
        self._pass_fail_streak = 0
        self._pass_backoff_until = 0.0
        self._pass_error_msg = None
        self._upload_broken = None
        if stale:
            try:
                if self.state.get().get("last_error") in stale:
                    self.state.update(last_error=None)
            except Exception:
                pass
        return True

    def _take_approval(self):
        # type: () -> bool
        """Consume the approval token (always removing it) and report validity.

        The token carries the ISO timestamp mycloudctl wrote; a token older than
        _APPROVAL_MAX_AGE_H is rejected. Removing it unconditionally means a
        token that never lines up with an over-threshold pass cannot linger on
        disk and auto-approve a later deletion the user never saw (PLAN 6.4).
        """
        try:
            with open(self.approval_path, "r", encoding="utf-8") as handle:
                content = handle.read().strip()
        except OSError:
            return False
        try:
            os.remove(self.approval_path)
        except OSError:
            pass
        try:
            ts = datetime.fromisoformat(content)
        except (TypeError, ValueError):
            # Unparseable token (e.g. a hand-written marker): honor once.
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_h = (self._now() - ts).total_seconds() / 3600.0
        if age_h > _APPROVAL_MAX_AGE_H:
            self._log("warning", "approval token is stale (%.1fh old); ignoring", age_h)
            return False
        return True

    def _free_space_gate(self):
        # type: () -> str
        """Check destination free space; notify and return the level."""
        try:
            level, _free = self.trash_mgr.free_space(self.mountpoint)
        except OSError:
            return "ok"
        if level == "stop":
            self.notifier.notify(
                "space", "MyCloud backup paused: low disk space",
                "The backup destination is nearly full; syncing is paused.",
                force=True)
        elif level == "warn":
            self.notifier.notify(
                "space", "MyCloud backup: low disk space",
                "The backup destination is running low on free space.")
        return level

    def _post_pass(self, scope, all_ok):
        # type: (Optional[Dict[str, List[str]]], bool) -> None
        """Snapshot, heartbeat, orphan marking, and the daily prune."""
        recovered = self._note_pass_completed(scope)
        if all_ok:
            try:
                dir_roots, shallow, _ = self._scan_roots()
                snap, _errs = scanner.snapshot_walk(
                    dir_roots, self._effective_excludes(), shallow_roots=shallow)
                self._snapshot = snap
                util.atomic_write_json(
                    os.path.join(self.runtime_dir, "snapshot.json"), snap)
            except Exception as exc:
                self._log("warning", "snapshot persist failed: %s", exc)
            # A scoped pass that uploaded nothing while a failure is still
            # outstanding is not a successful sync (it masked the gateway's
            # outage once already).
            if recovered or scope is None or (
                    self._pass_fail_streak == 0 and not self._upload_broken):
                self.state.update(last_sync_ok_at=self._now().isoformat())

        self._write_heartbeat()
        self._mark_orphans()
        # Pruning is the ONLY code path that truly destroys NAS data, so it runs
        # only when the whole round succeeded AND nothing is held/fail-closed
        # (PLAN 6.4: "每日一次、仅在整轮成功且四层守卫通过后执行"). Never destroy
        # aged history while the system is in a known-bad state.
        if all_ok and not self.deletion_disabled and not self._any_folder_held():
            self._daily_prune()

    def _any_folder_held(self):
        # type: () -> bool
        folders = self.state.get().get("folders", {})
        if not isinstance(folders, dict):
            return False
        for entry in folders.values():
            if isinstance(entry, dict) and entry.get("state") == F_DELETIONS_HELD:
                return True
        return False

    def _write_heartbeat(self):
        # type: () -> None
        """Write last_sync.json onto the NAS; never fatal."""
        data = self.state.get()
        counts = {"copied": 0, "trashed": 0}
        for entry in data.get("folders", {}).values():
            if isinstance(entry, dict):
                counts["copied"] += int(entry.get("files_copied", 0) or 0)
                counts["trashed"] += int(entry.get("files_trashed", 0) or 0)
        payload = {
            "host": platform.node(),
            "last_sync_ok_at": data.get("last_sync_ok_at"),
            "counts": counts,
        }
        try:
            util.atomic_write_json(self.heartbeat_path, payload)
        except Exception:
            try:
                with open(self.heartbeat_path, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload))
            except Exception as exc:
                self._log("warning", "heartbeat write failed: %s", exc)

    def _mark_orphans(self):
        # type: () -> None
        """Mark data-root entries not matching any configured folder; never touch."""
        names = set(f.name for f in self.cfg.folders)
        try:
            entries = os.listdir(self.data_root)
        except OSError:
            return
        for entry in entries:
            if entry in names:
                continue
            if not os.path.isdir(os.path.join(self.data_root, entry)):
                continue
            self.state.folder_update(entry, orphaned=True)

    def _daily_prune(self):
        # type: () -> None
        """Run trash pruning at most once per calendar date."""
        today = self._now().date().isoformat()
        data = self.state.get()
        if data.get("last_prune_date") == today:
            return
        errors = []  # type: List[str]
        try:
            result = self.trash_mgr.prune(guard_cb=self.mount.guard_ok,
                                          now_utc=self._now())
            errors = list(getattr(result, "errors", []) or [])
        except Exception as exc:
            self._log("warning", "prune failed: %s", exc)
        if errors:
            self._log("warning", "trash prune: %s", "; ".join(errors[:3]))
        # A prune the guard cut short did not happen: retry on the next pass
        # instead of skipping the rest of the day.
        if not any(e.startswith(("guard_failed", "guard_error")) for e in errors):
            self.state.update(last_prune_date=today)

    def _phase1_failure_msg(self, phase, name, rc):
        # type: (str, str, object) -> str
        """Build the SyncFailure message for a hard Phase-1 failure (A3).

        When the failure was caused by our OWN guard-watchdog killpg, name the
        kill reason ("killed by watchdog (reason: <layer>)") instead of a bare
        rc, so the recovery log line explains WHY rather than leaving an
        inscrutable "rc=-15"/"rc=20" that looks like an rsync bug.
        """
        return _phase1_text(phase, name, rc, self._consume_kill_reason())

    def _phase1_failure(self, phase, name, rc, kind):
        # type: (str, str, object, Optional[str]) -> SyncFailure
        """The SyncFailure for a hard Phase-1 failure. A pending watchdog kill
        reason means our own watchdog ended it: a transport failure (remount),
        whatever exit code rsync managed on the way out."""
        reason = self._consume_kill_reason()
        if reason and kind not in _TRANSPORT_KINDS:
            kind = "killed"
        return SyncFailure(_phase1_text(phase, name, rc, reason), kind=kind)

    def _sync_folder(self, folder, subtrees, stamp):
        # type: (Any, List[str], str) -> bool
        """Sync one folder via the v2 manifest pipeline (PLAN section 14).

        plan -> renames -> batched uploads (+ iCloud download when policy=download)
        -> manifest-fed deletions. Zero remote I/O for change computation; every
        committed batch advances the manifest so a crash resumes at the remainder.
        Returns True on a clean sync. Raises SyncFailure on a hard Phase-1 failure.
        """
        if getattr(folder, "is_file", False):
            return self._sync_file(folder, stamp)
        name = folder.name
        src = str(folder.src)
        fstate = self.state.get().get("folders", {}).get(name, {})
        if not isinstance(fstate, dict):
            fstate = {}

        # -- source device / emptiness guards (PLAN 6.4, unchanged) ---------
        try:
            st = os.stat(src)
        except OSError:
            self.state.folder_update(name, state=F_SOURCE_MISSING)
            return False
        stored_dev = fstate.get("st_dev")
        if stored_dev is not None and stored_dev != st.st_dev:
            # A changed st_dev is NOT proof the source is gone. APFS data
            # volumes / the iCloud FileProvider overlay can get a fresh device
            # minor on reboot -- observed 2026-08-01: ALL THREE sources flipped
            # 16777231 -> 16777234 after a restart, every folder wedged in
            # sourceMissing, and backups silently stopped until a human dug in.
            # The guard exists to protect the mirror from an unmounted external
            # disk's EMPTY stub directory (PLAN 6.4), so probe what actually
            # matters: a readable, NON-EMPTY source is safe to sync whatever
            # its device number says. Adopt the new id (written back by
            # _record_folder_success) and continue; only an unreadable or
            # empty source keeps the conservative skip.
            if self._dir_readable(src) and not self._source_empty(src):
                self._log("info",
                          "source device for %s changed (%s -> %s) but the "
                          "source is readable and non-empty -- likely a reboot "
                          "remount; adopting the new device id and continuing",
                          name, stored_dev, st.st_dev)
            else:
                self.notifier.notify(
                    "source_device", "MyCloud skipped a folder",
                    "The source device for %s changed and the source looks "
                    "empty/unreadable; skipping to protect the mirror." % name)
                self.state.folder_update(name, state=F_SOURCE_MISSING)
                return False
        if self._source_empty(src) and self._mirror_nonempty(name):
            self.notifier.notify(
                "source_device", "MyCloud skipped a folder",
                "Source %s is empty but its mirror is not; skipping to avoid a "
                "mass deletion." % name)
            self.state.folder_update(name, state=F_SOURCE_MISSING)
            return False

        self.state.folder_update(name, state=F_SYNCING)
        self._begin_folder_progress(name)   # phase "planning"
        dst_root = os.path.join(self.data_root, name)
        backup_dir = os.path.join(self.trash_root, stamp, name)

        start = time.time()
        plan = self.manifest.plan_folder(
            name, src, self._effective_excludes(), self.dataless_check, subtrees=subtrees)

        policy = getattr(self.cfg, "icloud_policy", "skip")
        upload_list = list(plan.uploads)
        dataless_list = list(plan.dataless)
        dataless_bytes = int(plan.totals["dataless_bytes"])
        deletion_list = list(plan.deletions)
        if plan.forget:
            # Rows for ".X.icloud" placeholders an older version backed up as
            # files. Manifest only: each placeholder was just seen on disk, and
            # deep verify keeps its NAS copy while it is (and trashes it once
            # the file is downloaded and the placeholder goes away).
            self.manifest.commit_deleted(name, list(plan.forget))
            self._log("info", "%s: forgot %d placeholder (.icloud) row(s)",
                      name, len(plan.forget))
        if plan.deferred:
            self._log("info",
                      "%s: holding back %d deletion(s) while an evicted iCloud "
                      "file may have moved there (e.g. %s)", name,
                      len(plan.deferred), plan.deferred[0])
        whole = list(subtrees) == ["."]
        if whole:
            # Whole-folder passes own the count (a scoped one sees only part).
            self.state.folder_update(name, deferred_deletions={
                "count": len(plan.deferred),
                "samples": list(plan.deferred)[:_ICLOUD_SAMPLE_CAP]})
        if whole:
            # A file that kept failing inside a package that is now evicted is
            # still not backed up: fetch / report the package itself.
            patterns = [p.rstrip("/") for p in self._effective_excludes()]
            seen_parts = {}  # type: Dict[str, bool]
            for rel in fstate.get("failed_files") or []:
                if not isinstance(rel, str) or os.path.lexists(os.path.join(src, rel)):
                    continue
                root = manifest_mod.placeholder_root(src, rel)
                if (root and root != rel and root not in dataless_list
                        and not manifest_mod._path_excluded(root, patterns, seen_parts)):
                    dataless_list.append(root)
                    entry = _cloud_stat(os.path.join(src, root))
                    dataless_bytes += entry[0] if entry is not None else 0

        # -- walk-error guard (PLAN 16.2 T-B1) ------------------------------
        # A swallowed scandir/lstat error means part of the local tree is simply
        # ABSENT from this plan's walk, and every manifest row under it reads as
        # "vanished" -- which is exactly how a deletion (and, from the same
        # vanished set, a rename) is derived. On 2026-07-30 an iCloud
        # fileproviderd hiccup did that to 1000 live files; the circuit breaker
        # held those, but a sub-threshold batch would have been trashed for real.
        # Uploads stay on: a missing entry can only under-report work, never
        # destroy anything. Deletions and renames wait for a clean walk (the next
        # pass, seconds to minutes later).
        suppress_deletions = plan.walk_errors > 0
        if suppress_deletions:
            self._log("warning",
                      "%s: %d local walk error(s) this pass; suppressing %d "
                      "deletion(s) and %d rename(s) until a clean walk",
                      name, plan.walk_errors, len(plan.deletions),
                      len(plan.renames))
            self.state.journal({
                "event": "deletions_suppressed",
                "folder": name,
                "walk_errors": int(plan.walk_errors),
                "deletions": len(plan.deletions),
                "renames": len(plan.renames),
            })

        # Fix 4a: surface the per-folder pending-download set at PLAN time and
        # keep it fresh as batches complete. The old code only wrote
        # icloud_skipped in _record_folder_success at the very END of the pass,
        # so during a download-policy pass over tens of thousands of files the
        # payload stayed null for the pass's (possibly multi-hour) duration --
        # the GUI/status had nothing to show. Seeded from the plan totals (no
        # per-file lstat, so this is cheap even for 200k+ files).
        if dataless_list:
            self.state.folder_update(name, icloud_skipped=self._icloud_skipped_live(
                dataless_list, set(), dataless_bytes, 0))

        # -- renames first (NAS-side os.rename; fall back to upload+delete) --
        renamed = 0
        if not suppress_deletions:
            renamed, fb_uploads, fb_deletes = self._execute_renames(
                name, src, dst_root, plan.renames)
            if fb_uploads:
                upload_list = sorted(set(upload_list) | set(fb_uploads))
            if fb_deletes:
                deletion_list = sorted(set(deletion_list) | set(fb_deletes))

        # Progress totals: everything we intend to transfer this pass.
        want_dataless = (policy == "download")
        self._pass_total_files = len(upload_list) + (
            len(dataless_list) if want_dataless else 0)
        self._pass_total_bytes = (
            plan.totals["upload_bytes"]
            + (dataless_bytes if want_dataless else 0))

        agg = {"copied": 0, "failed": [], "degraded": False,
               "downloaded": 0, "evicted": 0, "files_done": 0,
               "dataless_pending": [], "verified": set()}

        # -- uploads (non-dataless) in batches ------------------------------
        self._upload_relpaths(name, src, dst_root, backup_dir,
                              upload_list, agg, phase="transferring",
                              new_set=plan.new_set)

        # -- iCloud download-then-back-up (policy=download) -----------------
        if want_dataless and dataless_list:
            self._download_and_upload(
                name, src, dst_root, backup_dir, dataless_list, dataless_bytes,
                agg, new_set=plan.new_set)
        self._raise_if_recovery_needed("sync of %s" % name)

        # -- deletions via manifest diff + circuit breaker ------------------
        trashed = 0
        held = False
        if not self.deletion_disabled and deletion_list and not suppress_deletions:
            trashed, held = self._apply_manifest_deletions(
                name, src, dst_root, backup_dir, deletion_list, fstate)

        # -- iCloud surfacing (PLAN M5): dataless files not backed up -------
        # skip policy: all dataless files (left out of the backup).
        # download policy: only those still pending download after this pass.
        if policy == "skip":
            skipped_relpaths = list(dataless_list)
        else:
            skipped_relpaths = list(agg["dataless_pending"])
        icloud_skipped = self._icloud_skipped_payload(src, skipped_relpaths)

        duration = time.time() - start
        verified_now = agg["verified"]

        def _resolved(rel):
            # type: (str) -> bool
            # A listed failure is over once the file landed (verified this
            # pass) or no longer exists locally (deleted/renamed away).
            # A file under an evicted package is not gone, only cloud-only.
            return rel in verified_now or not _source_has(src, rel)

        self._record_folder_success(
            name, st.st_dev, agg["copied"], trashed, agg["failed"],
            agg["degraded"], held, dst_root, icloud_skipped=icloud_skipped,
            renamed=renamed, resolved=_resolved)
        self.state.journal({
            "folder": name, "stamp": stamp, "copied": agg["copied"],
            "trashed": trashed, "renamed": renamed,
            "downloaded": agg["downloaded"], "evicted": agg["evicted"],
            "icloud_skipped": icloud_skipped.get("count", 0),
            "duration": round(duration, 3),
            "status": ("held" if held else "degraded" if agg["degraded"] else "ok"),
        })
        return not (agg["degraded"] or held)

    # -- v2 pipeline helpers ----------------------------------------------

    @staticmethod
    def _within(path, root):
        # type: (str, str) -> bool
        """True iff realpath(path) is root itself or lives under root."""
        real_root = os.path.realpath(root)
        real_path = os.path.realpath(path)
        return real_path == real_root or real_path.startswith(real_root + os.sep)

    def _stat_batches(self, src, relpaths, on_broken=None):
        # type: (str, List[str], Optional[Any]) -> Any
        """Yield (batch, batch_bytes) split at _BATCH_MAX_FILES / _BATCH_MAX_BYTES.

        batch is a list of (relpath, size, mtime_ns, inode). lstat only (never
        opens -> never materializes a dataless file). A vanished file is dropped;
        a still-dataless file is SKIPPED (handing it to rsync would abort the
        whole batch), so it stays pending and is retried/surfaced next pass.

        DANGLING SYMLINKS are poison pills and are kept out of the batch:
        openrsync stat()s (follows) every --files-from entry and silently DROPS
        the remainder of the batch after the first failure -- verified
        2026-08-01 with [good, broken-link, good2]: only `good` transfers. On
        the real machine 61 dangling venv/ links wedged 911 innocent files
        behind them in a permanent retry loop (batch order is stable, so the
        same pill blocked the same tail every pass). A skipped pill is reported
        through on_broken(rel) so it lands in the named failed_files disclosure
        instead of silently re-planning forever; the check re-runs every pass,
        so a repaired link resumes syncing on its own. Where the startup probe
        showed this rsync copies them as links (rsync 2.6.9: the gateway), they
        ride in the batch like any file (_dangling_links_ok).
        """
        batch = []  # type: List[Any]
        batch_bytes = 0
        for rel in relpaths:
            path = os.path.join(src, rel)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat_mod.S_ISDIR(st.st_mode):
                # rsync -r would recurse into it from --files-from, outside
                # the -b / exclude rules (a package whose placeholder and
                # directory briefly coexist).
                continue
            if stat_mod.S_ISLNK(st.st_mode) and not self._dangling_links_ok:
                try:
                    os.stat(path)  # follows the link; raises if dangling
                except OSError:
                    if on_broken is not None:
                        try:
                            on_broken(rel)
                        except Exception:
                            pass
                    continue
            try:
                if self.is_dataless_fn(path):
                    continue
            except Exception:
                pass
            size = int(st.st_size)
            if batch and (len(batch) >= _BATCH_MAX_FILES
                          or batch_bytes + size > _BATCH_MAX_BYTES):
                yield batch, batch_bytes
                batch = []
                batch_bytes = 0
            batch.append((rel, size, int(st.st_mtime_ns), int(st.st_ino)))
            batch_bytes += size
        if batch:
            yield batch, batch_bytes

    @staticmethod
    def _verify_batch_on_nas(dst_root, batch, should_abort=None):
        # type: (str, List[Any], Optional[Any]) -> set
        """Return the relpaths of `batch` that are provably ON the NAS.

        The trust anchor for a PARTIAL batch (PLAN 16.2 T-A1). rsync exits 23/24
        when some files failed, but naming them means parsing stderr, and the
        real-machine audit of 2026-07-30 proved that parse unreliable: the paths
        it yields are not normalized to the batch's relpath shape and some
        failures are never printed at all. Committing "everything rsync did not
        complain about" therefore recorded ~4,200 files as backed up that had
        never reached the NAS -- the manifest lied. So we ask the destination
        instead of the transcript.

        A file passes iff lstat succeeds and the size matches what we planned.
        Anything else -- absent, wrong size, ANY OSError (a wedged mount raises
        rather than lying) -- fails, fail-safe: a file left out of the manifest
        merely gets re-planned next pass, while a wrongly committed one is lost
        silently, which is exactly the failure mode being fixed.

        Size only, on purpose. Under the default rsync_no_times=true the NAS
        mtime is the write time and carries no information; and even under
        preserve-times this check answers "did the bytes arrive?", not "are they
        bit-identical" -- content assurance is the deep verify's job (and the
        2026-07-30 audit found zero content divergence in what DID land).
        Symlinks compare correctly for free: lstat size is the length of the
        target string on both sides.

        Stops early -- the remainder simply stays unverified, i.e. uncommitted
        -- when should_abort() says so (a lost mount) or a single lstat takes
        longer than _NAS_STAT_SLOW_S: up to 500 lstats against a dying
        soft-mounted share, each waiting out its timeout, could hold the pass
        for hours.
        """
        verified = set()  # type: set
        for i, row in enumerate(batch):
            if should_abort is not None and i % 25 == 0 and should_abort():
                break
            rel = row[0]
            began = time.monotonic()
            try:
                st = os.lstat(os.path.join(dst_root, rel))
            except OSError:
                if time.monotonic() - began > _NAS_STAT_SLOW_S:
                    break
                continue
            if int(st.st_size) == int(row[1]):
                verified.add(rel)
            if time.monotonic() - began > _NAS_STAT_SLOW_S:
                break
        return verified

    def _note_upload_verification(self, name, rels, verified):
        # type: (str, List[str], set) -> List[str]
        """Track consecutive verification failures; return the chronic relpaths.

        Returns the files that have now failed verification _UPLOAD_FAIL_DISCLOSE
        times in a row and so deserve to be named in failed_files. Verified files
        drop their counter, which is why a clean (rc=0) batch calling this with
        verified == set(rels) also clears the folder's stale entries.

        Callers hold the batch lock: parallel upload workers share this dict.
        """
        chronic = []  # type: List[str]
        for rel in rels:
            key = (name, rel)
            if rel in verified:
                self._upload_retry_counts.pop(key, None)
                continue
            count = self._upload_retry_counts.get(key, 0) + 1
            self._upload_retry_counts[key] = count
            if count >= _UPLOAD_FAIL_DISCLOSE:
                chronic.append(rel)
        return chronic

    def _upload_relpaths(self, name, src, dst_root, backup_dir, relpaths, agg,
                         phase="transferring", new_set=None):
        # type: (str, str, str, str, List[str], Dict[str, Any], str, Optional[set]) -> None
        """Upload relpaths in batches over a small rsync worker pool (PLAN 15 P0).

        Up to cfg.upload_concurrency batches run as concurrent rsync processes
        (parallel streams measured near-linear against this router: 1/2/4 ->
        5.2/9.3/17.9 files/s). Each successful batch still commits the manifest
        at once (Manifest serializes internally), so a crash resumes at the
        uncommitted remainder exactly as in the serial pipeline (PLAN 14.3).

        NEW files (relpaths in new_set: no manifest row, hence no mirror entry)
        ride the --inplace fast path when the capability probe demonstrated it
        working -- nothing exists on the NAS to version, so dropping -b there
        loses nothing. Changed files always keep the -b overwrite-versioning
        path, because --inplace + -b is destructive on this openrsync (verified
        2026-07-21, PLAN 15).

        Only NEW-file (inplace) batches run on the pool. Concurrent -b batches
        race on shared backup-dir mkdirs -- openrsync's mk_backup_dir is not
        EEXIST-tolerant (verified 2026-07-21: "backup_to_dir ... File exists" +
        aborted transfers), while the plain data-tree mkdir race IS tolerated
        (5x8-way stress clean). Changed-file batches therefore run serially;
        an incremental pass rarely has more than one anyway, and the win the
        pool exists for is the first backup, which is all new files.

        A hard batch failure stops dispatch, lets in-flight batches drain, and
        raises SyncFailure -- except rc=20 (rsync's own I/O timeout), which is
        retried once in place first (PLAN 16.2 T-A2). A partial batch marks the
        folder degraded and commits only the files _verify_batch_on_nas can
        prove landed (PLAN 16.2 T-A1); the rest stay out of the manifest and are
        re-planned next pass, needing no retry bookkeeping of their own.
        """
        if not relpaths:
            return  # hot passes are mostly empty; never build a pool for that
        # Re-baseline the stall clock: the watchdog measures "no upload progress
        # for _TRANSFER_STALL_KILL", and without this the (possibly many minutes
        # of) planning that just ran, or a stale stamp from the previous pass,
        # would be charged against this pass's very first batch.
        self._last_upload_mono = time.monotonic()
        inplace_on = (self._inplace_capable
                      and bool(getattr(self.cfg, "rsync_inplace", True)))
        no_times = bool(getattr(self.cfg, "rsync_no_times", True))
        if inplace_on and new_set:
            new_rels = [rel for rel in relpaths if rel in new_set]
            chg_rels = [rel for rel in relpaths if rel not in new_set]
        else:
            new_rels = []
            chg_rels = list(relpaths)

        concurrency = max(
            1, int(getattr(self.cfg, "upload_concurrency", 1) or 1))
        # Coarse batch total for the UI (an UPPER bound: _stat_batches also splits
        # on the byte cap, and vanished/dataless files drop out, so the real count
        # can be higher for huge files or lower after skips). It exists to give a
        # "batch 3/12" heartbeat, not an exact denominator.
        est_batches = ((len(new_rels) + _BATCH_MAX_FILES - 1) // _BATCH_MAX_FILES
                       + (len(chg_rels) + _BATCH_MAX_FILES - 1) // _BATCH_MAX_FILES)
        self._batches_total = max(1, est_batches)
        self._batches_done = 0
        lock = threading.Lock()
        # Files transferred by IN-FLIGHT batches (their itemize lines), on top
        # of agg["files_done"] (batches fully accounted). shown = done + inflight
        # is monotonic: a finishing batch moves its lines from one term to the
        # other and len(rels) >= itemize lines (skipped files emit none).
        prog = {"inflight": 0}
        failures = []  # type: List[BaseException]

        def _run_batch(batch, inplace):
            # type: (List[Any], bool) -> None
            rels = [b[0] for b in batch]
            last = [0]

            def _cb(done, current):
                # type: (int, str) -> None
                with lock:
                    prog["inflight"] += done - last[0]
                    last[0] = done
                    shown = agg["files_done"] + prog["inflight"]
                self._on_progress(name, shown, current, phase)

            res = self.engine.phase1_batch(
                src, dst_root, backup_dir, rels, timeout=None, on_progress=_cb,
                inplace=inplace, no_times=no_times, on_alive=self._on_transfer_alive)
            first_kind = getattr(res, "kind", None) if res.status == "failure" else None
            if (first_kind == "io_timeout"
                    and not self._recovery_needed.is_set()):
                # T-A2: rsync's own I/O timeout -- retry ONCE, identical args.
                # Classified by the engine: exit 30 on rsync 2.6.9, exit 20 +
                # "poll: timeout" on openrsync. A bare exit 20 is a SIGTERM (our
                # watchdog) -- retrying that just runs into the dead mount again.
                # The retried rsync re-emits its itemize lines from zero, so the
                # first attempt's in-flight credit must be handed back first or
                # _cb would double-count every file it re-reports.
                with lock:
                    prog["inflight"] -= last[0]
                    last[0] = 0
                self._log("warning",
                          "batch for %s hit the rsync I/O timeout (rc=%s); "
                          "retrying it once in %.1fs", name, res.rc,
                          _BATCH_RETRY_SLEEP)
                time.sleep(_BATCH_RETRY_SLEEP)
                res = self.engine.phase1_batch(
                    src, dst_root, self._retry_backup_dir(backup_dir, name),
                    rels, timeout=None,
                    on_progress=_cb, inplace=inplace, no_times=no_times,
                    on_alive=self._on_transfer_alive)
            if res.status == "failure":
                kind = getattr(res, "kind", None)
                if first_kind == "io_timeout" and kind != "usage":
                    # The transfer already died once on this mount: whatever the
                    # retry ran into (e.g. its mkdir -p hanging), the remedy is
                    # still a remount, never a backoff on a wedged share.
                    kind = "io_timeout"
                raise self._phase1_failure("phase1_batch", name, res.rc, kind)
            if res.status == "partial":
                # T-A1: rc=23/24 means SOME files did not make it, and rsync's
                # stderr does not reliably say which (see _verify_batch_on_nas).
                # Ask the NAS itself and commit only what is provably there.
                verified = self._verify_batch_on_nas(
                    dst_root, batch,
                    should_abort=lambda: (self._stop.is_set()
                                          or self._recovery_needed.is_set()))
                self._log("info",
                          "partial batch: verified %d/%d files on the NAS; "
                          "%d left uncommitted for retry",
                          len(verified), len(batch), len(batch) - len(verified))
                if res.failed_paths:
                    self._log("debug",
                              "rsync stderr named %d failed path(s) (reference "
                              "only, not a commit decision): %s",
                              len(res.failed_paths), res.failed_paths[:5])
            else:
                # rc=0: rsync transferred every listed file. Trusted as-is --
                # re-lstat'ing an entire successful batch over SMB would cost
                # more round trips than the transfer itself.
                verified = set(rels)
            commit_rows = [(rel, size, mtime, inode)
                           for (rel, size, mtime, inode) in batch
                           if rel in verified]
            self.manifest.commit_uploaded(name, commit_rows)
            with lock:
                agg.setdefault("verified", set()).update(verified)
                if verified:
                    self._pass_batches_ok += 1
            ok_bytes = sum(size for (rel, size, _m, _i) in batch
                           if rel in verified)
            with lock:
                agg["copied"] += res.files_copied
                # Under the lock: parallel batches share the counter dict.
                chronic = self._note_upload_verification(name, rels, verified)
                if res.status == "partial":
                    # Files that already failed _UPLOAD_FAIL_DISCLOSE passes in
                    # a row are named in failed_files; they alone no longer
                    # fail the pass. One file the NAS can never take (a name
                    # its filesystem rejects, > 4 GB on FAT32) used to freeze
                    # 上次成功同步 and fire the 48 h alert forever.
                    if (set(rels) - verified) - set(chronic):
                        agg["degraded"] = True
                    agg["failed"].extend(chronic)
                prog["inflight"] -= last[0]
                agg["files_done"] += len(rels)
                self._bytes_done += ok_bytes
                self._batches_done += 1
                shown = agg["files_done"] + prog["inflight"]
            self._set_progress_phase(name, phase, shown)

        def _worker(batch, inplace):
            # type: (List[Any], bool) -> None
            try:
                _run_batch(batch, inplace)
            except BaseException as exc:  # collected; re-raised on the sync thread
                with lock:
                    failures.append(exc)

        # Dangling-symlink poison pills skipped by the batcher: disclose them
        # in the named failed list (they never reach rsync, so nothing else
        # would surface them) -- see _stat_batches for the openrsync behavior.
        broken = []  # type: List[str]

        def _on_broken(rel):
            # type: (str) -> None
            with lock:
                broken.append(rel)
                agg["failed"].append(rel)

        # Changed-file (-b) batches: strictly serial (backup-dir mkdir race).
        for batch, _bytes in self._stat_batches(src, chg_rels,
                                                on_broken=_on_broken):
            if self._stop.is_set():
                return
            self._raise_if_recovery_needed("upload of %s" % name)
            _run_batch(batch, False)

        if not new_rels:
            if broken:
                self._log("warning",
                          "%d dangling symlink(s) kept out of the upload "
                          "batches (openrsync would drop the batch remainder); "
                          "first few: %s", len(broken), broken[:3])
            return

        # New-file (--inplace) batches: the parallel pool.
        jobs = self._stat_batches(src, new_rels, on_broken=_on_broken)
        pending = set()  # type: set
        pool = ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="mycloud-upload")
        try:
            while True:
                with lock:
                    failed_now = bool(failures)
                if (failed_now or self._stop.is_set()
                        or self._recovery_needed.is_set()):
                    break
                job = next(jobs, None)
                if job is None:
                    break
                pending.add(pool.submit(_worker, job[0], True))
                if len(pending) >= concurrency:
                    _done, pending = wait(pending, return_when=FIRST_COMPLETED)
            if pending:
                wait(pending)
        finally:
            pool.shutdown(wait=True)

        if broken:
            self._log("warning",
                      "%d dangling symlink(s) kept out of the upload batches "
                      "(openrsync would drop the batch remainder); first few: %s",
                      len(broken), broken[:3])

        if failures:
            for exc in failures:
                if isinstance(exc, SyncFailure):
                    raise exc
            raise failures[0]
        self._raise_if_recovery_needed("upload of %s" % name)

    def _download_and_upload(self, name, src, dst_root, backup_dir,
                             dataless_list, dataless_bytes, agg, new_set=None):
        # type: (str, str, str, str, List[str], int, Dict[str, Any], Optional[set]) -> None
        """icloud_policy=download: materialize each batch, upload it, then evict.

        Disk guard: refuse a batch unless local free space >= 3x its bytes; on
        refusal notify, degrade the folder for this pass, and stop (PLAN M4).
        Per batch it logs one INFO line (fix 4b -- the stage used to log nothing,
        so a multi-hour first batch looked frozen), counts materialization
        failures as prefetch_skipped (fix 4c), and refreshes the folder's
        icloud_skipped surfacing (fix 4a) so status shrinks toward zero live.
        """
        from . import icloud
        # Materialization known-blocked (launchd restriction, detected earlier):
        # skip the whole download stage. The dataless set stays pending (surfaced
        # via icloud_skipped) and the already-uploaded non-dataless files stand.
        if self._materialize_blocked_now():
            agg["dataless_pending"] = sorted(dataless_list)
            return
        # Stop for shutdown AND for a lost mount: there is nothing to upload the
        # downloads to, and recovery must not wait behind them.
        abort = _AnyEvent(self._stop, self._recovery_needed)
        done = set()  # type: set
        done_bytes = 0
        total = len(dataless_list)
        for batch, batch_bytes in self._stat_batches_raw(src, dataless_list):
            if abort.is_set():
                break
            if not icloud.disk_guard_ok(src, batch_bytes):
                self.notifier.notify(
                    "icloud_disk", "MyCloud：磁盘空间不足以下载 iCloud 文件",
                    "下载该 iCloud 批次需要至少 3 倍批体积的本地可用空间；本轮"
                    "该文件夹降级为跳过。", force=True)
                agg["degraded"] = True
                break
            rels = [b[0] for b in batch]
            abspaths = [os.path.join(src, r) for r in rels]
            self._set_progress_phase(name, "downloading", agg["files_done"])
            materialized, failed, blocked = self._materialize_probed(abspaths, abort)
            # Fix 4c: a file that failed / timed out is skipped and counted
            # (its per-file timeout is now genuinely bounded by the subprocess
            # read), never retried forever inside the batch.
            if failed and not abort.is_set():
                self._record_prefetch_skipped(len(failed))
            # Launchd iCloud restriction: a fully-skipped probe/batch means
            # downloads cannot succeed here. Latch it and stop the download stage
            # for this pass WITHOUT degrading the folder or failing the pass --
            # the dataless remainder simply stays pending (icloud_skipped), and
            # every already-materialized file stays backed up.
            if blocked:
                self._mark_materialize_blocked("online download of %s" % name)
                break
            mat_rels = sorted(os.path.relpath(p, src) for p in materialized)
            mat_set = set(mat_rels)
            mat_bytes = sum(int(size) for (rel, size, _m, _i) in batch
                            if rel in mat_set)
            # Fix 4b: one INFO line per download/materialize batch.
            self._log("info",
                      "download batch done: %d files, %.1f MB (total %d/%d)",
                      len(mat_rels), mat_bytes / (1024.0 * 1024.0),
                      agg["downloaded"] + len(mat_rels), total)
            if mat_rels:
                agg["downloaded"] += len(mat_rels)
                upload_rels = []  # type: List[str]
                pkg_files = {}  # type: Dict[str, List[str]]
                for rel in mat_rels:
                    path = os.path.join(src, rel)
                    if os.path.isdir(path) and not os.path.islink(path):
                        # A macOS <= 13 package came back as a directory:
                        # upload its files now, in this batch.
                        pkg_files[rel] = self._package_files(name, src, rel)
                        upload_rels.extend(pkg_files[rel])
                        # Planned as one entry; it is uploaded as its files.
                        if self._pass_total_files is not None:
                            self._pass_total_files += max(0, len(pkg_files[rel]) - 1)
                    else:
                        upload_rels.append(rel)
                self._upload_relpaths(
                    name, src, dst_root, backup_dir, upload_rels, agg,
                    phase="transferring", new_set=new_set)
                # Done (and evictable) only once it verifiably reached the NAS:
                # evicting the rest re-downloaded it every pass, and a package
                # read as done before any of its files was uploaded.
                landed = agg.get("verified", set())
                complete = set(
                    rel for rel in mat_rels
                    if (all(k in landed for k in pkg_files[rel])
                        if rel in pkg_files else rel in landed))
                done.update(complete)
                done_bytes += sum(int(size) for (rel, size, _m, _i) in batch
                                  if rel in complete)
                if getattr(self.cfg, "icloud_evict_after_backup", True):
                    evictable = set(rel for rel in complete
                                    if rel not in pkg_files
                                    or self._package_backed_up(name, rel, landed))
                    agg["evicted"] += self._evict_materialized(
                        [p for p in materialized
                         if os.path.relpath(p, src) in evictable])
            # Fix 4a: refresh the pending-download surfacing as batches complete
            # (in-memory counters only -- never an lstat storm per batch).
            self.state.folder_update(name, icloud_skipped=self._icloud_skipped_live(
                dataless_list, done, dataless_bytes, done_bytes))
        # Whatever we did NOT get onto the NAS this pass is still pending-download,
        # surfaced via icloud_skipped under the download policy (PLAN M5).
        agg["dataless_pending"] = sorted(set(dataless_list) - done)

    def _package_backed_up(self, name, rel, landed):
        # type: (str, str, set) -> bool
        """Whether the next plan reads a downloaded package as backed up once
        it is evicted again: it has rows, none pending, and no file listed as
        failing in it is still unlanded. Evicting one the plan keeps selecting
        (a pending row for a file no longer in it, an empty or all-excluded
        package, a stale failure) downloaded it again on every pass."""
        rows = self.manifest._rows_in_scope(name, [rel])
        if not rows or any(row["pending"] for row in rows.values()):
            return False
        entry = self.state.get().get("folders", {}).get(name, {})
        failed = entry.get("failed_files") if isinstance(entry, dict) else None
        for frel in failed or []:
            if (isinstance(frel, str) and frel.startswith(rel + os.sep)
                    and frel not in landed):
                return False
        return True

    def _package_files(self, name, src, rel):
        # type: (str, str, str) -> List[str]
        """The files of a downloaded package directory that need uploading:
        excluded names pruned the way the plan walk prunes them, and files
        whose manifest row already matches skipped."""
        patterns = [p.rstrip("/") for p in self._effective_excludes()]
        rows = self.manifest._rows_in_scope(name, [rel])
        out = []  # type: List[str]
        for dirpath, dirs, files in os.walk(os.path.join(src, rel)):
            dirs[:] = [d for d in dirs if not _excluded_basename(d, patterns)]
            # A symlink to a directory is listed with the directories but is
            # one entry to back up (the plan walk records it as a file).
            links = [d for d in dirs if os.path.islink(os.path.join(dirpath, d))]
            dirs[:] = [d for d in dirs if d not in links]
            for fname in list(files) + links:
                if _excluded_basename(fname, patterns):
                    continue
                path = os.path.join(dirpath, fname)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if stat_mod.S_ISDIR(st.st_mode):
                    continue
                frel = os.path.relpath(path, src)
                row = rows.get(frel)
                if (row is not None and not row["pending"]
                        and row["size"] == st.st_size
                        and row["mtime_ns"] == st.st_mtime_ns):
                    continue
                out.append(frel)
        return sorted(out)

    @staticmethod
    def _icloud_skipped_live(dataless_list, done, total_bytes, done_bytes):
        # type: (List[str], set, int, int) -> Dict[str, Any]
        """Cheap (in-memory) icloud_skipped payload for the plan-time / per-batch
        updates. Unlike _icloud_skipped_payload it never lstats -- byte totals
        come from the plan and the running downloaded-bytes counter, so it is
        safe to call once per batch even over a 200k-file folder."""
        remaining = sorted(set(dataless_list) - done)
        return {
            "count": len(remaining),
            "bytes": max(0, int(total_bytes) - int(done_bytes)),
            "samples": remaining[:_ICLOUD_SAMPLE_CAP],
        }

    def _stat_batches_raw(self, src, relpaths):
        # type: (str, List[str]) -> Any
        """Like _stat_batches but WITHOUT the dataless skip (download path).

        The download path deliberately targets dataless files (it materializes
        them), so it must not filter them out before materialization. A macOS
        <= 13 cloud-only file has no entry of its own, only a placeholder: it
        is kept (sized from the placeholder) instead of silently dropped.
        """
        batch = []  # type: List[Any]
        batch_bytes = 0
        for rel in relpaths:
            path = os.path.join(src, rel)
            try:
                if stat_mod.S_ISDIR(os.lstat(path).st_mode):
                    continue
            except OSError:
                pass
            entry = _cloud_stat(path)
            if entry is None:
                continue
            size, mtime_ns, inode = entry
            if batch and (len(batch) >= _BATCH_MAX_FILES
                          or batch_bytes + size > _BATCH_MAX_BYTES):
                yield batch, batch_bytes
                batch = []
                batch_bytes = 0
            batch.append((rel, size, mtime_ns, inode))
            batch_bytes += size
        if batch:
            yield batch, batch_bytes

    def _evict_materialized(self, abspaths):
        # type: (List[str]) -> int
        """brctl-evict files WE materialized; a no-op (logged once) if absent."""
        from . import icloud
        if self._evict_available is None:
            self._evict_available = icloud.evict_available(self.run_cmd)
            # Surface availability in status (PLAN M4): status --json dumps the
            # whole state document, so this field rides along for the GUI.
            try:
                self.state.update(icloud_evict_available=bool(self._evict_available))
            except Exception:
                pass
        if not self._evict_available:
            if not self._evict_warned:
                self._evict_warned = True
                self._log("info",
                          "brctl evict is unavailable on this system; downloaded "
                          "iCloud files are left materialized (see status)")
            return 0
        evicted, errors = icloud.evict_paths(abspaths, self.run_cmd, self.logger)
        if errors:
            self._log("warning", "brctl evict reported %d error(s)", len(errors))
        return len(evicted)

    def _apply_manifest_deletions(self, name, src, dst_root, backup_dir,
                                  relpaths, fstate):
        # type: (str, str, str, str, List[str], Dict[str, Any]) -> Any
        """Circuit-breaker + phase2_execute for manifest-derived deletions.

        On execution, commit_deleted only the relpaths actually moved to trash
        (a recreated/unsafe file is not committed, so its row survives). Returns
        (files_trashed, held).
        """
        moved, held, moved_relpaths = self._apply_deletions(
            name, src, dst_root, backup_dir, relpaths, fstate)
        if moved_relpaths:
            self.manifest.commit_deleted(name, moved_relpaths)
        return moved, held

    def _execute_renames(self, name, src, dst_root, pairs):
        # type: (str, str, str, List[Any]) -> Any
        """Rename mirror entries old->new inside dst_root (realpath-checked).

        Any pair that cannot be renamed safely (outside the mirror root, missing
        old mirror, or an OSError) falls back to upload(new)+delete(old). Commits
        the successful pairs to the manifest. Returns (renamed, fb_uploads,
        fb_deletes).
        """
        renamed = 0
        fb_uploads = []  # type: List[str]
        fb_deletes = []  # type: List[str]
        ok_pairs = []  # type: List[Any]
        pairs = list(pairs)
        for old_rel, new_rel in pairs:
            if self._stop.is_set() or self._recovery_needed.is_set():
                fb_uploads.append(new_rel)
                fb_deletes.append(old_rel)
                continue
            old_m = os.path.join(dst_root, old_rel)
            new_m = os.path.join(dst_root, new_rel)
            if not (self._within(old_m, dst_root) and self._within(new_m, dst_root)):
                self._log("warning", "rename skipped (outside mirror root): %s -> %s",
                          old_rel, new_rel)
                fb_uploads.append(new_rel)
                fb_deletes.append(old_rel)
                continue
            if not os.path.lexists(old_m) or os.path.lexists(new_m):
                # Nothing to move -- or something already sits at the new
                # path, which os.rename would replace with no trash version
                # (the upload's -b keeps it instead).
                fb_uploads.append(new_rel)
                fb_deletes.append(old_rel)
                continue
            try:
                os.makedirs(os.path.dirname(new_m), exist_ok=True)
                os.rename(old_m, new_m)
                ok_pairs.append((old_rel, new_rel))
                renamed += 1
            except OSError as exc:
                self._log("warning",
                          "rename %s -> %s failed (%s); upload+delete fallback",
                          old_rel, new_rel, exc)
                fb_uploads.append(new_rel)
                fb_deletes.append(old_rel)
        if ok_pairs:
            self.manifest.commit_renamed(name, ok_pairs)
        # An evicted file that moved (macOS <= 13 placeholder) cannot be
        # uploaded from its new path: deleting the old copy would leave it
        # with no backup at all. Its row stays; the next pass pairs it again.
        cloud_new = dict((new_rel, old_rel) for old_rel, new_rel in pairs
                         if not os.path.lexists(os.path.join(src, new_rel))
                         and manifest_mod.placeholder_covers(src, new_rel))
        if cloud_new:
            keep = set(cloud_new[n] for n in fb_uploads if n in cloud_new)
            fb_deletes = [rel for rel in fb_deletes if rel not in keep]
            fb_uploads = [rel for rel in fb_uploads if rel not in cloud_new]
        return renamed, fb_uploads, fb_deletes

    def _icloud_skipped_payload(self, src, relpaths):
        # type: (str, List[str]) -> Dict[str, Any]
        """Build the folders[name].icloud_skipped surfacing dict (PLAN M5)."""
        total = 0
        for rel in relpaths:
            entry = _cloud_stat(os.path.join(src, rel))
            if entry is not None:
                total += entry[0]
        return {
            "count": len(relpaths),
            "bytes": total,
            "samples": sorted(relpaths)[:_ICLOUD_SAMPLE_CAP],
        }

    def _sync_file(self, folder, stamp):
        # type: (Any, str) -> bool
        """Sync one single-file source: phase1_file mirror + phase2 archival.

        The mirror is data_root/<name> (rsync preserves the basename). Overwrites
        are versioned by rsync's -b into <trash>/<stamp>/<name>; a locally-deleted
        source is archived to the same place via phase2_execute. The st_dev guard
        keys off the file's PARENT directory (PLAN 12.1). Raises SyncFailure on a
        hard Phase 1 failure.
        """
        name = folder.name
        src_file = str(folder.src)
        parent = os.path.dirname(src_file)
        fstate = self.state.get().get("folders", {}).get(name, {})
        if not isinstance(fstate, dict):
            fstate = {}

        try:
            pst = os.stat(parent)
        except OSError:
            self.state.folder_update(name, state=F_SOURCE_MISSING)
            return False
        stored_dev = fstate.get("st_dev")
        if stored_dev is not None and stored_dev != pst.st_dev:
            # Same reboot-remount tolerance as the folder guard (2026-08-01):
            # a readable parent directory means the device-id change is a
            # remount, not a vanished disk. The file itself may legitimately be
            # ABSENT (deletion archival must still run), so readability of the
            # parent -- not existence of the file -- is the safety test here.
            if self._dir_readable(parent):
                self._log("info",
                          "device for file source %s changed (%s -> %s) but its "
                          "parent directory is readable -- likely a reboot "
                          "remount; adopting the new device id and continuing",
                          name, stored_dev, pst.st_dev)
            else:
                self.notifier.notify(
                    "source_device", "MyCloud skipped a file",
                    "The device holding %s changed and its directory is "
                    "unreadable; skipping to protect the mirror." % name)
                self.state.folder_update(name, state=F_SOURCE_MISSING)
                return False

        self.state.folder_update(name, state=F_SYNCING)
        self._begin_folder_progress(name)
        # Backup-dir and archival dir share <trash>/<stamp>, so an overwritten
        # old version and a deleted file both land at <trash>/<stamp>/<name>.
        trash_folder_dir = os.path.join(self.trash_root, stamp)

        copied = 0
        failed = []  # type: List[str]
        degraded = False
        held = False
        icloud_skipped = {"count": 0, "bytes": 0, "samples": []}  # type: Dict[str, Any]
        start = time.time()
        policy = getattr(self.cfg, "icloud_policy", "skip")
        materialized_by_us = False
        # macOS <= 13 evicts the file to a hidden .X.icloud placeholder: the
        # source is cloud-only, NOT deleted -- archiving the mirror would put
        # the only backup in the trash for retention to prune.
        cloud_only = (not os.path.lexists(src_file)
                      and manifest_mod.has_placeholder(src_file))
        backed_up = False
        if cloud_only:
            # Already backed up when the NAS copy has the size the placeholder
            # records. Decided BEFORE any download: downloading it anyway, then
            # evicting it after a no-op copy, looped every pass (the evict
            # rewrites the watched parent directory).
            real = _cloud_stat(src_file)
            try:
                mirror_size = os.lstat(os.path.join(self.data_root, name)).st_size
            except OSError:
                mirror_size = None
            backed_up = bool(real is not None and real[0] and real[0] == mirror_size)
        if cloud_only and policy == "download" and not backed_up:
            materialized_by_us = self._materialize_one(src_file)
            cloud_only = not (materialized_by_us and os.path.lexists(src_file))
        if cloud_only:
            if not backed_up:
                icloud_skipped = self._icloud_skipped_payload(parent, [name])
        elif os.path.lexists(src_file):
            # A dataless single-file source must never be handed to rsync (it
            # would abort the run). Apply the same skip/download policy as folders.
            dataless = False
            try:
                dataless = bool(self.is_dataless_fn(src_file))
            except Exception:
                dataless = False
            if dataless and policy == "download" and not materialized_by_us:
                materialized_by_us = self._materialize_one(src_file)
                dataless = not materialized_by_us
            if dataless:
                icloud_skipped = self._icloud_skipped_payload(parent, [name])
            else:
                res = self.engine.phase1_file(
                    src_file, self.data_root, trash_folder_dir, timeout=None,
                    on_progress=self._make_progress(name),
                    on_alive=self._on_transfer_alive)
                copied += res.files_copied
                if res.status == "failure":
                    raise self._phase1_failure(
                        "phase1_file", name, res.rc, getattr(res, "kind", None))
                if res.status == "partial":
                    degraded = True
                    failed.extend(res.failed_paths)
                elif res.files_copied > 0 and not self._upload_broken:
                    # Only a file that actually moved proves anything; and
                    # phase1_file's argv lacks the batch options (--files-from,
                    # --from0, -r, --inplace) a rejected upload argv involves.
                    self._pass_batches_ok += 1
                # Evict a file WE downloaded, now that it is backed up (M4).
                if (materialized_by_us and res.status != "partial"
                        and getattr(self.cfg, "icloud_evict_after_backup", True)):
                    self._evict_materialized([src_file])

        trashed = 0
        if not self.deletion_disabled and not cloud_only:
            trashed, held = self._phase2_file(name, src_file, trash_folder_dir, fstate)

        duration = time.time() - start
        # A clean copy (or a source that is gone) ends every earlier failure of
        # this one-file source.
        copied_clean = os.path.lexists(src_file) and not degraded and not failed
        cleared = copied_clean or (not os.path.lexists(src_file) and not cloud_only)
        self._record_folder_success(
            name, pst.st_dev, copied, trashed, failed, degraded, held,
            self.data_root, icloud_skipped=icloud_skipped,
            resolved=(lambda _rel: True) if cleared else None)
        self.state.journal({
            "folder": name, "stamp": stamp, "copied": copied, "trashed": trashed,
            "icloud_skipped": icloud_skipped.get("count", 0),
            "duration": round(duration, 3), "is_file": True,
            "status": ("held" if held else "degraded" if degraded else "ok"),
        })
        return not (degraded or held)

    def _materialize_one(self, abspath):
        # type: (str) -> bool
        """Download one dataless file (policy=download); evict after if enabled.

        Returns True if the file is now materialized (safe for rsync). Disk-guarded
        like a batch (needs 3x the file's size free locally).
        """
        from . import icloud
        if self._materialize_blocked_now():
            return False
        entry = _cloud_stat(abspath)
        if entry is None:
            return False
        need = entry[0]
        # statvfs of the folder: a placeholder-backed file has no path yet.
        if not icloud.disk_guard_ok(os.path.dirname(abspath), need):
            self.notifier.notify(
                "icloud_disk", "MyCloud：磁盘空间不足以下载 iCloud 文件",
                "下载该 iCloud 文件需要至少 3 倍其体积的本地可用空间；本轮跳过。",
                force=True)
            return False
        materialized, _failed = icloud.materialize_batch(
            [abspath], is_dataless_fn=self.is_dataless_fn, read_fn=self.read_fn,
            stop_event=_AnyEvent(self._stop, self._recovery_needed),
            run_cmd=self.run_cmd, per_file_timeout=self._materialize_timeout)
        if materialized:
            self._materialize_proven = True
        return bool(materialized)

    def _phase2_file(self, name, src_file, trash_folder_dir, fstate):
        # type: (str, str, str, Dict[str, Any]) -> Any
        """Archive a file mirror when its source is gone (a 1-item deletion).

        Routes through the SAME circuit-breaker accounting as folders, but with
        mirror_count=0 so the percentage rule (undefined for a single-file
        source) never applies; the count rule (max_delete_files) still does, so
        a lone deletion always propagates. Returns (files_trashed, held).
        """
        mirror = os.path.join(self.data_root, name)
        if os.path.lexists(src_file) or not os.path.lexists(mirror):
            return 0, False
        if manifest_mod.has_placeholder(src_file):
            return 0, False   # cloud-only (macOS <= 13), not deleted
        # src_root is the file's parent so phase2_execute's recreation guard
        # checks the right path (parent/<name> == the source file).
        src_parent = os.path.dirname(src_file)
        moved, held, _moved_relpaths = self._apply_deletions(
            name, src_parent, self.data_root, trash_folder_dir, [name], fstate,
            mirror_count=0)
        return moved, held

    def _apply_deletions(self, name, src, dst_root, trash_folder_dir, relpaths,
                         fstate, mirror_count=None):
        # type: (str, str, str, str, List[str], Dict[str, Any], Optional[int]) -> Any
        """Circuit-breaker accounting shared by folder and file deletions.

        Returns (files_trashed, held: bool, moved_relpaths). mirror_count=0
        disables the pct rule. Deletions are fed from the manifest diff (folders)
        or a single vanished file (file sources) -- never a remote dry-run.
        """
        if not relpaths:
            return 0, False, []

        if mirror_count is None:
            mirror_count = fstate.get("mirror_count")
            if mirror_count is None:
                mirror_count = self._mirror_count(name, dst_root, allow_walk=True)
                self.state.folder_update(name, mirror_count=mirror_count)

        over = len(relpaths) > self.cfg.max_delete_files
        if mirror_count > 0:
            pct_limit = self.cfg.max_delete_pct / 100.0 * mirror_count
            if len(relpaths) > pct_limit:
                over = True

        # The token was already consumed (once) for the whole pass in full_pass.
        approval = self._approval_active
        if over and not approval:
            # The list is capped for state.json's size; held_total is the real
            # count -- an approval releases ALL of them, and the GUI's confirm
            # dialog used to say "1000" for a 60k-file deletion.
            self.state.folder_update(
                name, state=F_DELETIONS_HELD,
                held_deletions=relpaths[:_HELD_CAP], held_total=len(relpaths))
            self._set_state(DELETIONS_HELD)
            self.notifier.notify(
                "deletions_held", "MyCloud is holding a large deletion",
                "%d files would be deleted from the %s mirror. Run "
                "'mycloudctl approve-deletions' to release them." % (len(relpaths), name),
                min_hours=24.0)
            return 0, True, []

        result = self.engine.phase2_execute(src, dst_root, trash_folder_dir, relpaths)
        return result.moved, False, result.moved_relpaths

    def _record_folder_success(self, name, st_dev, copied, trashed, failed,
                               degraded, held, dst_root, icloud_skipped=None,
                               renamed=0, resolved=None):
        # type: (str, int, int, int, List[str], bool, bool, str, Optional[Dict[str, Any]], int, Optional[Any]) -> None
        """Persist one folder's pass outcome.

        failed_files keeps the earlier entries that are still failing plus this
        pass's; resolved(rel) -> True drops an entry. The list used to only ever
        grow: a file that failed once stayed "多次同步失败" forever, after it
        synced or was deleted -- a permanent warning nobody could clear.
        """
        cur = self.state.get().get("folders", {}).get(name, {})
        if not isinstance(cur, dict):
            cur = {}
        previous = list(cur.get("failed_files", []) or [])
        if resolved is not None:
            kept = []
            for rel in previous:
                try:
                    if resolved(rel):
                        continue
                except Exception:
                    pass
                kept.append(rel)
            previous = kept
        merged_failed = list(dict.fromkeys(previous + list(failed)))
        fields = {
            "st_dev": st_dev,
            "files_copied": int(cur.get("files_copied", 0) or 0) + copied,
            "files_trashed": int(cur.get("files_trashed", 0) or 0) + trashed,
            "files_renamed": int(cur.get("files_renamed", 0) or 0) + int(renamed),
            "failed_files": merged_failed[:_FAILED_CAP],
            "mirror_count": self._mirror_count(name),
            # PLAN M5: always present so the GUI can render it unconditionally.
            "icloud_skipped": icloud_skipped or {"count": 0, "bytes": 0, "samples": []},
        }
        if held:
            fields["state"] = F_DELETIONS_HELD
        elif degraded:
            fields["state"] = F_DEGRADED
        else:
            fields["state"] = F_UP_TO_DATE
            fields["last_ok"] = self._now().isoformat()
        if not held:
            # The hold is released (deletions executed, or nothing over-threshold
            # this pass): clear the stale held list so status/approve-deletions
            # stop reporting it.
            fields["held_deletions"] = []
            fields["held_total"] = 0
        self.state.folder_update(name, **fields)

    # -- filesystem helpers ------------------------------------------------

    def _dir_readable(self, path):
        # type: (str) -> bool
        """True iff a scandir of path succeeds (readability, not emptiness)."""
        try:
            with os.scandir(path) as it:
                next(it, None)
            return True
        except OSError:
            return False

    def _source_empty(self, src):
        # type: (str) -> bool
        try:
            with os.scandir(src) as it:
                return next(it, None) is None
        except OSError:
            return False

    def _mirror_nonempty(self, name):
        # type: (str) -> bool
        try:
            with os.scandir(os.path.join(self.data_root, name)) as it:
                return next(it, None) is not None
        except OSError:
            return False

    def _mirror_count(self, name, dst_root=None, allow_walk=False):
        # type: (str, Optional[str], bool) -> int
        """Number of files in this folder's NAS mirror.

        Answered from the MANIFEST, which is authoritative: the daemon is the
        only writer of the mirror (PLAN section 14), so its row count IS the
        mirror's file count -- one indexed SQL COUNT instead of an os.walk over
        SMB. That walk was the real cost behind the apparent "stuck in planning"
        hang: _record_folder_success ran it for every folder on every pass, and
        over a large mirror on this router it took 10+ minutes while no progress
        was written (diagnosed 2026-07-28 via a SIGINFO stack dump).

        Using the manifest is also SAFER for the circuit breaker that divides by
        this number: an os.walk counts Finder litter (.DS_Store, ._*) we never
        uploaded, inflating the denominator and loosening the mass-deletion
        threshold.

        allow_walk=True permits ONE bounded fallback walk when the manifest has
        no rows, and only the circuit-breaker path asks for it -- there the
        denominator affects a safety decision. Even then the walk stops at
        _MIRROR_WALK_CAP files: "at least this many" is all the percentage rule
        needs, and an unbounded walk is exactly the trap this fix removes (a
        mirror can hold tens of thousands of files the manifest never tracked --
        e.g. 71443 left behind by a source whose local contents moved elsewhere).
        Display-only callers pass allow_walk=False and get 0 rather than paying
        for remote I/O; mirror_count=0 simply disables the percentage rule, and
        the absolute max_delete_files threshold still guards deletions.
        """
        try:
            rows = int(self.manifest.counts(name).get("files", 0) or 0)
        except Exception:
            rows = 0
        if rows > 0:
            return rows
        if dst_root is None or not allow_walk:
            return 0
        total = 0
        try:
            for _dirpath, _dirnames, filenames in os.walk(dst_root):
                total += len(filenames)
                # Checked per directory (os.walk hands over one directory's
                # file list at a time), so a single huge directory is still read
                # in full -- that is one readdir, not a tree traversal.
                if total >= _MIRROR_WALK_CAP:
                    self._log("info",
                              "mirror count for %s stopped at the %d-file cap "
                              "(manifest has no rows); the percentage rule uses "
                              "that lower bound", name, _MIRROR_WALK_CAP)
                    return total
        except OSError:
            return 0
        return total

    # -- deep verify (PLAN 14.6 / M3) --------------------------------------

    def deep_verify(self):
        # type: () -> Dict[str, Any]
        """Reconcile the manifest against the NAS mirror. Returns the summary.

        DATALESS-SAFE by design: it reads ONLY the NAS side (os.walk/lstat on the
        mount) and never touches local source files. The v1 mechanism -- a
        full-tree `rsync -n` over the SOURCE -- was verified on 2026-07-20 to
        MATERIALIZE iCloud dataless files (SF_DATALESS clears, the file
        downloads), so it must never run over the source; this manifest-vs-NAS
        walk replaces it and also matches v2's "manifest is authoritative" model.

        Missing / drifted mirror files are marked pending (re-uploaded by the
        follow-up pass); NAS-extra files (no source, no manifest row) route
        through the existing phase2/breaker/trash flow -- never a direct delete.

        ABORT SEMANTICS (T-C1, 2026-07-30): a VerifyAborted from the walk guard
        (stop requested or mount gone) ends the ENTIRE verify -- not just that
        folder -- because every remaining folder would walk a dead mount and read
        as empty. The summary then carries aborted=True and last_deep_verify_at
        is left untouched (in the manifest AND in state), so the weekly cadence
        retries instead of recording a reconciliation that never happened.
        """
        started = self._now().isoformat()
        self.state.update(verify={
            "running": True, "started_at": started, "finished_at": None,
            "missing_uploaded": 0, "extra_found": 0, "drift_reuploaded": 0,
            "aborted": False, "errors": []})
        self._verifying.set()
        # Never inherit a leftover approval flag: NAS-extra removal during verify
        # must independently respect the circuit breaker (a large extra set holds).
        self._approval_active = False
        missing = 0
        drift = 0
        extra = 0
        aborted = False
        errors = []  # type: List[str]
        try:
            for folder in self.cfg.folders:
                if getattr(folder, "is_file", False):
                    continue
                name = folder.name
                src = str(folder.src)
                dst_root = os.path.join(self.data_root, name)
                self._pass_total_files = None
                self._pass_total_bytes = None
                self._bytes_done = 0
                self._set_progress_phase(name, "verifying")
                try:
                    m, d, e, errs = self._verify_folder(name, src, dst_root)
                except VerifyAborted as exc:
                    # NOT a per-folder error: the guard fired, so the mount is
                    # gone (or we are stopping) and every remaining folder would
                    # walk a dead tree. Stop here -- do NOT continue.
                    aborted = True
                    errors.append("%s: 深度校验已中止（%s）" % (name, exc))
                    self._log("warning",
                              "deep verify aborted while verifying %s: %s",
                              name, exc)
                    break
                except Exception as exc:  # never let one folder abort the verify
                    errors.append("%s: %s" % (name, exc))
                    continue
                missing += m
                drift += d
                extra += e
                errors.extend(errs)
        finally:
            self._verifying.clear()
            self._clear_progress()

        finished = self._now().isoformat()
        if not aborted:
            # An aborted verify reconciled nothing trustworthy, so it must not
            # count as "last deep verify": leaving both stamps alone keeps the
            # weekly cadence (and the doctor's deep_verify_age row) honest.
            self.manifest.set_meta("last_deep_verify_at", finished)
        summary = {
            "running": False, "started_at": started, "finished_at": finished,
            "missing_uploaded": missing, "extra_found": extra,
            "drift_reuploaded": drift, "aborted": aborted,
            "errors": errors[:_FAILED_CAP],
        }
        if aborted:
            self.state.update(verify=summary)
        else:
            self.state.update(verify=summary, last_deep_verify_at=finished)
        self.state.journal({
            "event": "deep_verify_aborted" if aborted else "deep_verify",
            "missing_uploaded": missing, "extra_found": extra,
            "drift_reuploaded": drift, "errors": len(errors)})
        return summary

    def _verify_guard(self):
        # type: () -> None
        """Raise VerifyAborted if the daemon is stopping or the mount is gone.

        Called periodically from inside the NAS walk (T-C1, 2026-07-30): an
        unguarded walk over a dead SMB mount silently yields a truncated tree,
        and acting on that fabricates thousands of "missing" rows.
        """
        if self._stop.is_set():
            raise VerifyAborted("守护进程正在停止")
        if not self._is_mounted_safe():
            raise VerifyAborted("备份共享已不再挂载")

    def _verify_folder(self, name, src, dst_root):
        # type: (str, str, str) -> Any
        """Compare one folder's manifest rows against its NAS mirror.

        Returns (missing, drift, extra, errors). Missing/drift rows are marked
        pending (re-upload next pass). Extra NAS files with NO source counterpart
        are routed through the circuit-breaker + trash flow; an extra whose source
        still exists is left alone (the next plan re-uploads it and re-adds the
        manifest row).

        The NAS walk honors the effective excludes: Finder browsing the share
        litters mirrors with .DS_Store/._* (AppleDouble) that we never uploaded;
        treating that litter as "extra" would archive it into the trash every
        week, forever. Excluded names are simply invisible to the verify.

        With rsync_no_times enabled (PLAN 15 P0) uploads drop -t, so the NAS
        mtime means "backup time" and cannot be compared against the manifest;
        drift detection then uses size alone (the manifest still quick-checks
        the LOCAL mtime every pass, so local edits are always caught).

        Raises VerifyAborted (T-C1, 2026-07-30) if the walk guard trips, always
        BEFORE any mark_pending/extra work, so an aborted folder is a no-op.
        """
        compare_mtime = not bool(getattr(self.cfg, "rsync_no_times", True))
        patterns = [p.rstrip("/") for p in self._effective_excludes()]
        rows = self.manifest.rows(name)
        nas = {}  # type: Dict[str, Any]
        dirs_since_guard = 0
        last_guard = time.monotonic()
        # os.walk drops an unreadable directory SILENTLY. Its files would read
        # as missing (a mass re-upload) and survivors as extra, so any walk
        # error voids this folder's verdict. Exception: the mirror root simply
        # not existing -- with the mount healthy that is a genuinely missing
        # mirror, and re-uploading it is exactly right.
        walk_errors = []  # type: List[OSError]

        def _on_walk_error(exc):
            # type: (OSError) -> None
            if (isinstance(exc, FileNotFoundError)
                    and os.path.normpath(getattr(exc, "filename", "") or "")
                    == os.path.normpath(dst_root)):
                return
            walk_errors.append(exc)

        for dirpath, dirs, files in os.walk(dst_root, onerror=_on_walk_error):
            # Abort guard (T-C1): every _VERIFY_GUARD_EVERY_DIRS directories or
            # _VERIFY_GUARD_EVERY_S seconds, whichever comes first. Raising here
            # is what makes the abort side-effect-free: mark_pending and the
            # extra-file handling below only run once the walk COMPLETED.
            dirs_since_guard += 1
            now_mono = time.monotonic()
            if (dirs_since_guard >= _VERIFY_GUARD_EVERY_DIRS
                    or (now_mono - last_guard) >= _VERIFY_GUARD_EVERY_S):
                dirs_since_guard = 0
                last_guard = now_mono
                self._verify_guard()
            dirs[:] = [d for d in dirs if not _excluded_basename(d, patterns)]
            for fname in files:
                if _excluded_basename(fname, patterns):
                    continue
                path = os.path.join(dirpath, fname)
                rel = os.path.relpath(path, dst_root)
                try:
                    st = os.lstat(path)
                except OSError as exc:
                    walk_errors.append(exc)
                    continue
                nas[rel] = (int(st.st_size), int(st.st_mtime_ns))

        if walk_errors:
            # The mount check first: a dead share is an abort, not a folder error.
            self._verify_guard()
            return 0, 0, 0, ["%s: NAS 目录读取出错 %d 次（例：%s），本文件夹本次不作结论"
                             % (name, len(walk_errors), walk_errors[0])]
        if not os.path.isdir(dst_root):
            self._verify_guard()

        missing_pending = []  # type: List[str]
        drift_pending = []  # type: List[str]
        for rel, row in rows.items():
            if rel not in nas:
                missing_pending.append(rel)
                continue
            size, mtime = nas[rel]
            if size != row["size"]:
                drift_pending.append(rel)
            elif (compare_mtime
                    and abs(mtime - row["mtime_ns"]) > _VERIFY_MTIME_TOLERANCE_NS):
                drift_pending.append(rel)

        extra = []  # type: List[str]
        for rel in nas:
            if rel in rows:
                continue
            if not _source_has(src, rel):
                extra.append(rel)

        if missing_pending or drift_pending:
            self.manifest.mark_pending(name, missing_pending + drift_pending)

        errs = []  # type: List[str]
        if extra and not self.deletion_disabled:
            stamp = util.make_stamp(self._now())
            backup_dir = os.path.join(self.trash_root, stamp, name)
            fstate = self.state.get().get("folders", {}).get(name, {})
            if not isinstance(fstate, dict):
                fstate = {}
            _moved, held, _rels = self._apply_deletions(
                name, src, dst_root, backup_dir, sorted(extra), fstate)
            if held:
                errs.append("%s: extra-file removal held by circuit breaker" % name)
        return len(missing_pending), len(drift_pending), len(extra), errs

    def _safe_deep_verify(self):
        # type: () -> None
        """Run a deep verify, then a follow-up pass if it flagged re-uploads."""
        awake = self._hold_awake()
        try:
            summary = self.deep_verify()
        except SyncFailure as exc:
            self._pass_failed(exc, "deep verify")
            return
        except OSError as exc:
            self._log("error", "deep verify hit an OS error: %s", exc)
            return
        finally:
            self._release_awake(awake)
        if summary.get("aborted"):
            # The follow-up below is still safe: the counts belong to folders
            # that COMPLETED, so any pending row they flagged is genuine.
            self._log("warning",
                      "deep verify was aborted early; the counts cover only the "
                      "folders it finished and the cadence will retry it")
        pending = (summary.get("missing_uploaded", 0)
                   + summary.get("drift_reuploaded", 0))
        if pending > 0:
            self._safe_full_pass(None)

    def _consume_marker(self, path):
        # type: (str) -> bool
        try:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
                return True
        except OSError:
            pass
        return False

    def _prune_now(self):
        # type: () -> None
        """Prune the trash on request, only behind a passing guard (pruning is
        the one operation that truly destroys NAS data)."""
        if not self._guard_check():
            self._log("warning", "trash prune request ignored: mount guard failed")
            return
        self._main_busy.set()
        try:
            result = self.trash_mgr.prune(guard_cb=self.mount.guard_ok,
                                          now_utc=self._now())
            self._log("info", "trash pruned on request: %d batch(es) removed",
                      len(getattr(result, "removed_stamps", []) or []))
            errors = list(getattr(result, "errors", []) or [])
            if errors:
                self._log("warning", "trash prune: %s", "; ".join(errors[:3]))
        except Exception as exc:
            self._log("warning", "trash prune on request failed: %s", exc)
        finally:
            self._main_busy.clear()

    def _consume_verify_request(self):
        # type: () -> bool
        """True (and remove the marker) iff a verify.request file is present."""
        try:
            if os.path.exists(self.verify_request_path):
                try:
                    os.remove(self.verify_request_path)
                except OSError:
                    pass
                return True
        except OSError:
            pass
        return False

    def _weekly_verify_due(self):
        # type: () -> bool
        """True iff the last deep verify is older than the weekly cadence."""
        last = self.manifest.get_meta("last_deep_verify_at")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except (TypeError, ValueError):
            return True
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return (self._now() - last_dt).total_seconds() >= _DEEP_VERIFY_INTERVAL_S

    def recovery(self):
        # type: () -> None
        """Recover a wedged/dropped mount: unmount, verify bare dir, remount.

        If local data was written into the dead mountpoint we DEGRADE and stop
        syncing (fail safe): the loop keeps running and guards keep failing, but
        we never delete the stray local data. A human must intervene.

        Every step runs inside the retry loop. A wedged SMB session can refuse
        to unmount for a while; that used to end recovery on the spot (one
        warning, state left as it was -- the GUI still showed a healthy daemon)
        until some later pass happened to try again. Now the daemon says
        OFFLINE and keeps retrying the release on the normal backoff.
        """
        # This IS the recovery any pending request asked for. Left set, the
        # request fired a SECOND recovery on the next loop tick -- unmounting the
        # fresh, healthy mount and re-running a full pass after every
        # watchdog-triggered recovery. A request raised during the pass below is
        # a new one and stays set.
        self._recovery_needed.clear()
        # A dropped mount away from home is the same "offline, could be
        # prefetching" situation as startup (e.g. lid opened on a different
        # network): mark OFFLINE and prefetch during the reconnect backoff (M4).
        self._set_state(OFFLINE)
        self._reset_prefetch()
        self._probe_fail_streak = 0
        # Offline-loop liveness (see ensure_online): the heartbeat keeps
        # updated_at fresh through the mount/precheck segments. Cleared on exit.
        self._offline_active.set()
        release_warned = False
        try:
            attempt = 0
            while not self._stop.is_set():
                # Keep updated_at fresh across the (up to 60s) mount attempt too.
                self._idle_touch()
                self.mount.force_unmount()
                try:
                    self.mount.ensure_bare_dir_safe()
                except CriticalLocalWriteDetected as exc:
                    self._log("error", "critical local write detected: %s", exc)
                    self._degraded_halt = True
                    self._set_state(DEGRADED)
                    try:
                        self.state.update(critical_halt=True)
                    except Exception:
                        pass
                    self.notifier.notify(
                        "critical", "MyCloud stopped: local write detected",
                        "Files were written to the backup mount point while it "
                        "was offline. Syncing is stopped to protect them; see "
                        "the log.", force=True)
                    return
                except MountError as exc:
                    if not release_warned:
                        self._log("warning",
                                  "recovery: the old mount will not release yet "
                                  "(%s); staying OFFLINE and retrying on the "
                                  "backoff schedule", exc)
                        release_warned = True
                    if self.once:
                        return
                    self._offline_wait(self._backoff_delay(attempt))
                    attempt += 1
                    continue

                # Bare dir is clean: clear any persisted critical halt.
                self._degraded_halt = False
                if self.state.get().get("critical_halt"):
                    try:
                        self.state.update(critical_halt=False)
                    except Exception:
                        pass

                # Gated mount (see ensure_online / _try_online_gated): probe-gated,
                # with the periodic Local-Network-denial recheck.
                if self._try_online_gated():
                    self._set_state(MOUNTED_IDLE)
                    self._finish_prefetch_going_online()
                    awake = self._hold_awake()
                    try:
                        self.full_pass(None)
                    except SyncFailure as exc:
                        self._log("warning",
                                  "full pass after recovery failed: %s", exc)
                        if getattr(exc, "kind", None) not in _TRANSPORT_KINDS:
                            if not self._guard_check():
                                # The mount died again during the pass: the
                                # main loop recovers on its next tick (never
                                # recurse into recovery() from here).
                                self._log("warning",
                                          "the mount guard fails again after "
                                          "the recovery pass; recovering on the "
                                          "next tick")
                                self._recovery_needed.set()
                            else:
                                self._note_pass_failure(
                                    exc, "full pass after recovery")
                    except OSError as exc:
                        # recovery() runs inside callers' exception handlers;
                        # an OSError escaping here would crash the daemon into
                        # a launchd respawn loop.
                        self._log("error",
                                  "full pass after recovery hit an OS error: %s",
                                  exc)
                    finally:
                        self._release_awake(awake)
                    return
                if self.once:
                    return
                self._offline_interval(attempt)
                attempt += 1
        finally:
            self._offline_active.clear()

    def run(self):
        # type: () -> int
        """Startup sequence + (unless --once) the main loop. Returns an exit code."""
        self._degraded_halt = False
        self._lock_error = False
        if not self._acquire_lock():
            return _EXIT_TEMPFAIL if self._lock_error else 0

        # Announce this process before anything else. Without it, the state a
        # PREVIOUS run left behind (often MOUNTED_IDLE) read as "the restarted
        # daemon is online" to mycloudctl's post-restart wait -- even when the
        # new process could not connect at all (no FDA, a wrong new password).
        try:
            self.state.update(state=STARTING, daemon_pid=os.getpid(),
                              daemon_started_at=self._now().isoformat(),
                              guard=None, last_error=None)
        except Exception:
            pass

        # Clear any stale progress/prefetch snapshot left in state.json by a
        # PREVIOUS run before anything can read it. The heartbeat only re-stamps
        # while syncing/prefetching, so a leftover progress={phase:"downloading"}
        # (or prefetch={active:true}) from a crashed/killed run would otherwise
        # masquerade as a live download until the next pass overwrote it.
        self._clear_stale_liveness_on_start()

        caffeinate = None
        try:
            self._install_signal_handlers()
            self._capability_check()
            # Absorb any exclude-list change BEFORE the first plan, or newly
            # excluded but still-tracked files would be planned as deletions
            # (PLAN 16.2 T-B2).
            self._forget_excluded_rows()
            # Progress heartbeat covers the initial full pass too (the biggest
            # transfer, where a frozen progress bar is most visible).
            self._start_progress_heartbeat()

            if not self.ensure_online():
                # --once and the destination is unreachable.
                return 2 if self.once else 0

            self._estimate_initial()
            # Watch the initial pass too: it is the longest and most exposed one.
            self._start_watchdog()
            caffeinate = self._start_caffeinate()

            initial_ok = True
            try:
                initial_ok = self.full_pass(None)
            except SyncFailure as exc:
                initial_ok = False
                self._pass_failed(exc, "initial pass")
            except OSError as exc:
                self._log("error", "initial pass hit an OS error: %s", exc)
                initial_ok = False
            finally:
                # caffeinate holds off idle sleep only for the first full sync
                # (PLAN 6.3); release it before entering the steady-state loop so
                # the Mac can sleep normally afterwards.
                if caffeinate is not None:
                    self._stop_caffeinate(caffeinate)
                    caffeinate = None
            self.state.update(initial_sync=True)

            if self.once:
                return 0 if initial_ok else 2

            self._main_loop()
            return 0
        finally:
            if caffeinate is not None:
                self._stop_caffeinate(caffeinate)
            # Stop the heartbeat thread and mark a terminal state so the GUI does
            # not render stale "syncing"/"idle" forever after a clean exit.
            self._stop.set()
            self._join_progress_heartbeat()
            self.state.update(state="stopped")
            try:
                self.manifest.close()
            except Exception:
                pass
            self._release_lock()

    def _start_watchdog(self):
        # type: () -> None
        """Start the guard watchdog once, idempotently.

        Called before the INITIAL pass (not just from _main_loop as it used to
        be): that pass is the longest-running and most failure-prone one -- the
        first full backup -- and leaving it unwatched meant a mount that died
        mid-transfer wedged the daemon with nothing to kill rsync and recover
        (2026-07-28). The loop no-ops unless self._syncing is set, so starting it
        early is harmless.
        """
        if self._watchdog_thread is not None:
            return
        t = threading.Thread(target=self._watchdog_loop)
        t.daemon = True
        self._watchdog_thread = t
        t.start()

    def _start_progress_heartbeat(self):
        # type: () -> None
        if self._hb_thread is not None:
            return
        t = threading.Thread(target=self._progress_heartbeat_loop)
        t.daemon = True
        self._hb_thread = t
        t.start()

    def _join_progress_heartbeat(self):
        # type: () -> None
        t = self._hb_thread
        if t is None:
            return
        try:
            t.join(timeout=2.0)
        except Exception:
            pass
        self._hb_thread = None

    def _hold_awake(self):
        # type: () -> Any
        """Keep the Mac from idle-sleeping while a pass or verify runs.

        Only the first-ever sync used to be covered, so a gateway MacBook on
        default power settings slept mid-pass: the SMB session died, the pass
        failed, and every wake started a full re-plan. -w ties the assertion to
        this process, so it is released even if the daemon is killed. This
        does not keep an IDLE gateway awake -- that is a power setting
        (doctor reports it).
        """
        try:
            return subprocess.Popen(
                [paths.CAFFEINATE, "-i", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self._log("debug", "could not start caffeinate: %s", exc)
            return None

    def _release_awake(self, proc):
        # type: (Any) -> None
        if proc is not None:
            self._stop_caffeinate(proc)

    def _start_caffeinate(self):
        # type: () -> Optional[subprocess.Popen]
        try:
            return subprocess.Popen([paths.CAFFEINATE, "-i"])
        except Exception as exc:
            self._log("warning", "could not start caffeinate: %s", exc)
            return None

    def _stop_caffeinate(self, proc):
        # type: (subprocess.Popen) -> None
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        except Exception:
            pass

    def _estimate_initial(self):
        # type: () -> None
        """Seed initial_sync totals from a manifest plan (NOT an rsync dry-run).

        The v1 pre-estimate ran `rsync -n` over each source, which -- verified
        2026-07-20 -- MATERIALIZES iCloud dataless files (SF_DATALESS clears, the
        file downloads). The manifest plan uses a pure lstat walk, which never
        materializes, so it is the dataless-safe way to count the initial work.
        Dataless files are counted separately and excluded from the upload total
        under the default skip policy.

        SKIPPED once the manifest holds rows (i.e. not a first run). This walk is
        a FULL local traversal of every source, and the full_pass that follows
        immediately re-walks the very same trees: on a 292k-file iCloud source
        behind fileproviderd that is 10+ minutes each, so a purely cosmetic total
        cost double the time before the first byte moved -- with no progress
        written the whole time, which is exactly what made a healthy daemon look
        hung (diagnosed 2026-07-28). Per-folder totals still reach the UI from
        each pass's own plan (progress.total_files / total_bytes).
        """
        want_dataless = getattr(self.cfg, "icloud_policy", "skip") == "download"
        candidates = [f for f in self.cfg.folders
                      if not getattr(f, "is_file", False)
                      and os.path.isdir(str(f.src))]
        if self._manifest_has_rows(candidates):
            self._log("info",
                      "skipping the initial-total estimate: the manifest already "
                      "has rows, so the pass below computes the same plan anyway")
            return

        total_files = 0
        total_bytes = 0
        for folder in candidates:
            if self._stop.is_set():
                return
            src = str(folder.src)
            # Publish progress DURING the estimate: on a first run over a huge
            # iCloud tree this is many minutes of pure scanning, and a silent
            # progress snapshot here reads as a hang.
            self._begin_folder_progress(folder.name)
            try:
                plan = self.manifest.plan_folder(
                    folder.name, src, self._effective_excludes(), self.dataless_check)
            except Exception:
                continue
            total_files += plan.totals["upload_files"]
            total_bytes += plan.totals["upload_bytes"]
            if want_dataless:
                total_files += plan.totals["dataless_files"]
                total_bytes += plan.totals["dataless_bytes"]
        self._clear_progress()
        try:
            self.state.update(initial_sync={"total_files": total_files,
                                            "total_bytes": total_bytes})
        except Exception:
            pass

    def _manifest_has_rows(self, folders):
        # type: (List[Any]) -> bool
        """True iff the manifest already tracks any file for these folders.

        Cheap (one indexed COUNT per folder) compared with the full local walk it
        guards. A manifest error is treated as "no rows" so a first run still
        gets its estimate.
        """
        for folder in folders:
            try:
                if self.manifest.counts(folder.name).get("files", 0) > 0:
                    return True
            except Exception:
                continue
        return False

    # -- signals -----------------------------------------------------------

    def _install_signal_handlers(self):
        # type: () -> None
        """Install SIGTERM/SIGUSR1/SIGUSR2 handlers (no-op if not main thread).

        Also arms SIGINFO (Ctrl-T / `kill -INFO <pid>`) to dump every thread's
        PYTHON stack to stderr, i.e. into launchd.err.log. Without it, a daemon
        that appears stuck can only be inspected with `sample`, which shows C
        frames and cannot say which Python call is blocking -- that gap cost hours
        of guesswork on 2026-07-28. SIGINFO neither kills nor interrupts the
        process, so it is safe to arm permanently.
        """
        try:
            signal.signal(signal.SIGTERM, self._on_sigterm)
            signal.signal(signal.SIGUSR1, self._on_sigusr1)
            signal.signal(signal.SIGUSR2, self._on_sigusr2)
        except (ValueError, OSError):
            # Not on the main thread (e.g. under a test harness); skip.
            pass
        siginfo = getattr(signal, "SIGINFO", None)  # BSD/macOS only
        if siginfo is not None:
            try:
                faulthandler.register(siginfo, all_threads=True, chain=False)
            except (ValueError, OSError, RuntimeError):
                pass

    def _on_sigterm(self, signum, frame):
        self._stop.set()

    def _on_sigusr1(self, signum, frame):
        self._sync_requested.set()
        self._stop_waiting()

    def _on_sigusr2(self, signum, frame):
        self._reload_requested.set()
        self._stop_waiting()

    def _stop_waiting(self):
        # type: () -> None
        # Briefly pulse the stop event so a waiting tick returns promptly, then
        # clear it (only a real SIGTERM leaves it set).
        pass

    # -- main loop ---------------------------------------------------------

    def _main_loop(self):
        # type: () -> None
        """Tick every ~5s: wake detection, watcher debounce, tier-2 scheduling."""
        self._start_watcher()
        self._start_watchdog()

        last_mono = time.monotonic()
        last_wall = time.time()
        last_full = time.monotonic()
        last_hot_scan = time.monotonic()
        last_idle_touch = time.monotonic()
        pending = set()  # type: set
        debounce_armed_at = None  # type: Optional[float]

        while not self._stop.is_set():
            self._stop.wait(_TICK_SECONDS)
            if self._stop.is_set():
                break
            try:
                last_mono, last_wall, last_full, last_hot_scan, last_idle_touch, \
                    pending, debounce_armed_at = self._main_loop_tick(
                        last_mono, last_wall, last_full, last_hot_scan,
                        last_idle_touch, pending, debounce_armed_at)
            except Exception as exc:
                # An unexpected error in one tick must not take the daemon down:
                # a crash means a launchd respawn and a full re-plan of ~270k
                # files, and a bug on a periodic path would repeat that forever
                # ("守护进程时常失败"). Log the traceback (rate-limited), back off,
                # keep serving.
                self._log_loop_exception(exc)
                self._stop.wait(_LOOP_ERROR_BACKOFF)
                last_mono = time.monotonic()
                last_wall = time.time()

        self._shutdown_children()
        if self._watcher is not None:
            self._watcher.close()

    def _log_loop_exception(self, exc):
        # type: (BaseException) -> None
        now = time.monotonic()
        key = "%s: %s" % (type(exc).__name__, exc)
        if key == self._loop_error_key and (now - self._loop_error_mono) < _GUARD_LOG_INTERVAL:
            return
        self._loop_error_key = key
        self._loop_error_mono = now
        if self.logger is not None:
            try:
                self.logger.exception("main loop tick failed; continuing: %s", exc)
            except Exception:
                pass

    def _main_loop_tick(self, last_mono, last_wall, last_full, last_hot_scan,
                        last_idle_touch, pending, debounce_armed_at):
        # type: (float, float, float, float, float, set, Optional[float]) -> Any
        """One main-loop iteration; returns the updated loop bookkeeping."""
        now_mono = time.monotonic()
        now_wall = time.time()

        # Idle freshness (A2): while no pass is running, keep updated_at fresh
        # so the GUI never mistakes a quiet daemon for a dead one. The sync
        # heartbeat covers the syncing case, so skip then to avoid double
        # writes. Done on the main thread (no folder_update race).
        if not self._syncing.is_set():
            if (now_mono - last_idle_touch) >= _IDLE_TOUCH_INTERVAL:
                self._idle_touch()
                last_idle_touch = now_mono

        # Config reload request.
        if self._reload_requested.is_set():
            self._reload_requested.clear()
            self._handle_reload()

        # Explicit request (SIGUSR1): a verify.request marker means deep
        # verify (mycloudctl verify / the run_deep_verify fix); otherwise a
        # plain sync-now. approve-deletions also raises SIGUSR1 but leaves no
        # verify marker, so it falls through to the full pass below.
        if self._sync_requested.is_set():
            self._sync_requested.clear()
            if self._consume_marker(self.prune_request_path):
                # mycloudctl's "立即清理回收站": pruning happens HERE, on the
                # thread that writes the NAS, never concurrently with a batch.
                self._prune_now()
            elif self._consume_verify_request() and self._guard_check():
                self._safe_deep_verify()
            else:
                self._safe_full_pass(None)
            last_full = time.monotonic()

        if self._degraded_halt:
            return (last_mono, last_wall, last_full, last_hot_scan,
                    last_idle_touch, pending, debounce_armed_at)

        # Wake detection: wall advanced far more than monotonic -> slept.
        slept = (now_wall - last_wall) - (now_mono - last_mono)
        if slept > _WAKE_THRESHOLD:
            # Logged so mycloud.log lines up with `pmset -g log`, and state is
            # touched at once: the GUI otherwise read the pre-sleep mtime as
            # a dead daemon until the first post-wake write.
            self._log("info", "system wake detected (asleep ~%d s)", int(slept))
            self._idle_touch()
            # The backoff deadline is on the monotonic clock, which stops while
            # asleep: without this, an overnight sleep left the whole backoff
            # (up to 1 h of awake time) still to run, and the wake pass -- the
            # natural moment to retry -- was skipped.
            if self._pass_backoff_until:
                self._pass_backoff_until = max(
                    0.0, self._pass_backoff_until - slept)
            if self._guard_check_after_wake():
                if not self._backing_off():
                    self._safe_full_pass(None)
                    last_full = time.monotonic()
            else:
                self._recovery_needed.set()
        last_mono = now_mono
        last_wall = now_wall

        # Watchdog flagged a guard failure mid-sync.
        if self._recovery_needed.is_set():
            self._recovery_needed.clear()
            self.recovery()
            last_full = time.monotonic()
            return (last_mono, last_wall, last_full, last_hot_scan,
                    last_idle_touch, pending, debounce_armed_at)

        # Gather dirty directories: the kqueue watcher is polled every tick
        # (near-realtime), but the Tier-1 hot scan only runs every
        # cfg.hot_interval seconds (PLAN 6.3), not on every 5s tick.
        if self._watcher is not None and self._watcher.active:
            dirty = self._watcher.poll(0)
            if self._watcher.degraded and not self._reported_degraded:
                self._reported_degraded = True
                try:
                    self.state.update(watcher_degraded=True)
                except Exception:
                    pass
        else:
            hot = getattr(self.cfg, "hot_interval", 30) or 30
            if now_mono - last_hot_scan >= hot:
                dirty = self._scan_dirty()
                last_hot_scan = now_mono
            else:
                dirty = set()
        if dirty:
            if debounce_armed_at is None:
                debounce_armed_at = now_mono
            pending |= dirty

        # Fire the debounced scoped pass (not while backing off: a scoped pass
        # over the failing file fails again, and each edit would retry the
        # whole failing upload; the dirty set keeps accumulating meanwhile).
        if debounce_armed_at is not None and not self._backing_off():
            since_first = now_mono - debounce_armed_at
            idle = not dirty
            if since_first >= _DEBOUNCE_HARD_CAP or (idle and since_first >= _DEBOUNCE_ARM):
                scope = self._build_scope(pending)
                pending = set()
                debounce_armed_at = None
                if scope:
                    self._safe_full_pass(scope)

        # Tier-2 adaptive full pass.
        effective = max(self.cfg.full_interval, 2 * self._last_full_duration)
        if now_mono - last_full >= effective and not self._backing_off():
            self._safe_full_pass(None)
            last_full = time.monotonic()

        # Weekly deep verify (PLAN 14.6). The due-check is a cheap manifest
        # meta read, so it gates the (expensive) guard probe via short-circuit
        # and only actually fires about once a week.
        if self._weekly_verify_due() and self._guard_check():
            self._safe_deep_verify()
            last_full = time.monotonic()

        return (last_mono, last_wall, last_full, last_hot_scan, last_idle_touch,
                pending, debounce_armed_at)

    def _guard_check_after_wake(self):
        # type: () -> bool
        """The guard, with a little patience right after a wake: Wi-Fi and the
        SMB client's own reconnect can need tens of seconds, and failing at once
        meant an unmount plus a full re-plan of a mount that would have been
        fine. updated_at is kept fresh while waiting."""
        for attempt in range(_WAKE_GUARD_TRIES):
            if self._guard_check():
                return True
            if attempt + 1 < _WAKE_GUARD_TRIES:
                self._idle_touch()
                if self._stop.wait(_WAKE_GUARD_WAIT):
                    return False
        return False

    def _safe_full_pass(self, scope):
        # type: (Optional[Dict[str, List[str]]]) -> None
        # Check the mount BEFORE the pass. The watchdog only runs while a pass
        # is in flight, so a mount that died while idle (NAS reboot, Wi-Fi blip)
        # used to be discovered by the pass itself: a multi-minute local plan
        # walk, then main-thread makedirs/statvfs straight into the dead share
        # -- which on a WEDGED SMB vnode blocks uninterruptibly and livelocks the
        # daemon. The guard runs every layer in timed child processes, so it is
        # the safe way to ask; a failure goes straight to recovery.
        if not self._guard_check():
            self._log("info", "mount guard failed before a scheduled pass; "
                              "recovering instead of syncing into a dead mount")
            self.recovery()
            return
        start = time.monotonic()
        awake = self._hold_awake()
        failure = None  # type: Optional[SyncFailure]
        try:
            self.full_pass(scope)
        except SyncFailure as exc:
            failure = exc
        except OSError as exc:
            # Defense in depth: full_pass already degrades per-folder OSErrors,
            # but an OSError raised outside the folder loop must never kill the
            # daemon (a fatal exit here launchd-respawn-loops).
            self._log("error", "full pass hit an OS error; skipping this pass: %s", exc)
        finally:
            self._release_awake(awake)
            # The pass alone: recovery() below can sit OFFLINE for hours, and
            # 2x that used to push the next Tier-2 safety-net pass out by 2x
            # the outage.
            if scope is None:
                self._last_full_duration = time.monotonic() - start
        if failure is not None:
            self._pass_failed(failure, "full pass")

    def _scan_dirty(self):
        # type: () -> set
        """Tier-1 hot scan: diff a fresh directory-mtime snapshot vs. the last."""
        try:
            dir_roots, shallow, _ = self._scan_roots()
            new_snap, _errs = scanner.snapshot_walk(
                dir_roots, self._effective_excludes(), shallow_roots=shallow)
        except Exception:
            return set()
        dirty = scanner.diff_snapshots(self._snapshot, new_snap)
        self._snapshot = new_snap
        return dirty

    def _start_watcher(self):
        # type: () -> None
        mode = getattr(self.cfg, "watch_mode", "auto")
        self._reported_degraded = False
        if mode == "scan":
            # Explicit scan mode is a deliberate choice, not a degradation.
            self._watcher = None
            self._set_watcher_degraded(False)
            self._seed_scan_snapshot()
            return
        try:
            dir_roots, shallow, _ = self._scan_roots()
            watcher = KqueueWatcher(
                dir_roots, self._effective_excludes(), logger=self.logger,
                shallow_roots=shallow)
            if watcher.start():
                self._watcher = watcher
                self._reported_degraded = bool(watcher.degraded)
                self._set_watcher_degraded(bool(watcher.degraded))
                return
            watcher.close()
        except Exception as exc:
            self._log("warning", "kqueue watcher unavailable: %s", exc)
        # Requested auto/kqueue but fell back to scan (over fd budget or
        # unavailable): surface the degradation in status (PLAN 6.3 / T7).
        self._watcher = None
        self._reported_degraded = True
        self._set_watcher_degraded(True)
        self._seed_scan_snapshot()

    def _set_watcher_degraded(self, value):
        # type: (bool) -> None
        try:
            self.state.update(watcher_degraded=bool(value))
        except Exception:
            pass

    def _seed_scan_snapshot(self):
        # type: () -> None
        """Seed the scan snapshot so the first Tier-1 diff is meaningful."""
        try:
            dir_roots, shallow, _ = self._scan_roots()
            self._snapshot, _errs = scanner.snapshot_walk(
                dir_roots, self._effective_excludes(), shallow_roots=shallow)
        except Exception:
            self._snapshot = {}

    def _handle_reload(self):
        # type: () -> None
        """Reload config, rewrite excludes, rebuild folders, restart watcher."""
        from . import config as config_mod
        try:
            new_cfg = config_mod.load_config(paths.CONFIG)
        except Exception as exc:
            self._log("error", "config reload failed: %s", exc)
            return
        self.cfg = new_cfg
        # dest_rel may have changed: re-point data/trash/heartbeat roots (and the
        # trash manager) before the next pass. The dest_changed notification is
        # emitted lazily by _ensure_dest_layout on that pass.
        self._recompute_roots()
        try:
            config_mod.write_excludes(new_cfg, paths.EXCLUDES)
        except Exception as exc:
            self._log("warning", "excludes rewrite failed: %s", exc)
        # The reload may have ADDED exclude patterns; forget the rows they now
        # cover before the next pass can call them deletions (PLAN 16.2 T-B2).
        self._forget_excluded_rows()
        if self._watcher is not None:
            self._watcher.close()
            self._watcher = None
        self._start_watcher()

    # -- guard watchdog thread --------------------------------------------

    def _watchdog_loop(self):
        # type: () -> None
        """Re-check the guard every 60s while syncing; kill rsync only reluctantly.

        The kill policy (A1) lives in _watchdog_tick; this loop just paces it and
        resets the consecutive-failure counter whenever no sync is in flight.
        """
        while not self._stop.is_set():
            self._stop.wait(_WATCHDOG_INTERVAL)
            if self._stop.is_set():
                break
            if not self._syncing.is_set():
                self._watchdog_consecutive = 0
                continue
            try:
                self._watchdog_tick()
            except Exception as exc:
                # This thread is the only thing that can break a transfer wedged
                # against a dead mount; an uncaught error would end it silently
                # and leave every later outage unwatched.
                self._log("warning", "watchdog tick failed; continuing: %s", exc)

    def _watchdog_tick(self):
        # type: () -> None
        """One watchdog evaluation while a sync is running (A1).

        Kills the live rsync process group (and requests recovery) iff:
          (a) the mount is truly gone from the mount table (is_mounted False --
              cheap and load-independent), immediately; or
          (b) the soft-pass-aware guard fails on _WATCHDOG_CONSECUTIVE_KILL
              checks IN A ROW.
        Anything short of that (a single/transient failure, or a Local-Network
        soft-pass) does NOT kill: eager killing is unnecessary for safety (see
        the module docstring) and under sync load a slow router intermittently
        fails one guard layer without the mount actually being dead. Sentinel/
        statshares child timeouts are widened to 30s for these checks.
        """
        # (a) Mount gone from the table -> a wedged transfer against a dead mount;
        # end it now so recovery can remount.
        mounted = self._mount_table_state(_WATCHDOG_SYNC_CHILD_TIMEOUT)
        if mounted is None:
            # Could not read the mount table this tick: say nothing, kill
            # nothing -- the next tick asks again.
            return
        if not mounted:
            # One warning per mount-loss episode. The main thread may be busy in
            # a stage that only returns to the loop later; re-logging every
            # 60s (174 identical lines in one field outage) buried the log and
            # repeated a kill that had nothing left to kill.
            if not self._watchdog_gone_reported:
                self._log("warning",
                          "watchdog: mount is gone from the mount table "
                          "(is_mounted False); killing the rsync group and "
                          "recovering")
                self._watchdog_gone_reported = True
            self._watchdog_consecutive = 0
            self._kill_sync_procs(reason="is_mounted")
            self._recovery_needed.set()
            return
        self._watchdog_gone_reported = False

        # (b) Full guard (soft-pass aware). A pass/soft-pass resets the streak.
        try:
            ok = self._guard_check(child_timeout=_WATCHDOG_SYNC_CHILD_TIMEOUT)
        except Exception:
            ok = False
        if ok:
            self._watchdog_consecutive = 0
            # (b'') A HEALTHY guard does not prove the transfer is moving: an
            # rsync child can sit in uninterruptible kernel I/O against a wedged
            # SMB server while the sentinel still reads fine (verified on real
            # hardware 2026-07-28: two rsync --server processes at 0% CPU in
            # state UN for 14 minutes, guard green, daemon waiting forever).
            # Break that deadlock here -- this is the only place that can, since
            # every guard-based rule above has already passed.
            if self._transfer_stalled():
                self._log("warning",
                          "watchdog: uploads have made no progress for over "
                          "%.0f min while the mount looks healthy; killing the "
                          "rsync group so the batch retries",
                          _TRANSFER_STALL_KILL / 60.0)
                self._kill_sync_procs(reason="transfer_stall")
                self._recovery_needed.set()
            return

        # (b') Load-tolerance: the mount table showed us mounted just above (a
        # load-INDEPENDENT read), and if rsync transferred a file within the last
        # _WATCHDOG_PROGRESS_GRACE seconds the SMB session is provably alive and
        # writable. Under several parallel upload streams the sentinel/statshares
        # reads get starved of bandwidth and time out -- a FALSE "mount dead" that
        # used to make this watchdog kill rsync and thrash. Do NOT count a
        # starved-read failure toward the kill threshold while uploads progress; a
        # genuinely wedged session stops emitting itemize lines, so progress goes
        # stale past the grace and the normal kill path below resumes.
        if (self._syncing.is_set()
                and self._last_upload_mono > 0.0
                and (time.monotonic() - self._last_upload_mono)
                    < _WATCHDOG_PROGRESS_GRACE):
            self._watchdog_consecutive = 0
            self._log("info",
                      "watchdog: guard layer '%s' timed out under upload load but "
                      "rsync is still transferring (mount present); not killing",
                      getattr(self.mount, "last_failure", "") or "unknown")
            return

        self._watchdog_consecutive += 1
        layer = getattr(self.mount, "last_failure", "") or "unknown"
        if self._watchdog_consecutive >= _WATCHDOG_CONSECUTIVE_KILL:
            self._log("warning",
                      "watchdog: guard failed on %d consecutive checks (failing "
                      "layer: %s); killing the rsync group and recovering",
                      self._watchdog_consecutive, layer)
            self._watchdog_consecutive = 0
            self._kill_sync_procs(reason=layer)
            self._recovery_needed.set()
            return
        # Transient (not yet at the kill threshold): log once at INFO,
        # rate-limited to a layer change or once per 10 minutes.
        self._log_watchdog_transient(layer, self._watchdog_consecutive)

    def _mount_table_state(self, timeout):
        # type: (float) -> Optional[bool]
        """True/False per the mount table, None when it cannot be read."""
        state_fn = getattr(self.mount, "mount_state", None)
        try:
            if state_fn is not None:
                return state_fn(timeout=timeout)
            return bool(self.mount.is_mounted(timeout=timeout))
        except Exception:
            return None

    def _transfer_stalled(self):
        # type: () -> bool
        """True iff a live rsync has made zero progress past _TRANSFER_STALL_KILL.

        Requires an actually-running rsync child, so a long planning phase (no
        children yet) can never trip it. _last_upload_mono is re-baselined when
        uploads start (_upload_relpaths), so planning time is not counted against
        the stall budget either.
        """
        if not self._syncing.is_set():
            return False
        if self._last_upload_mono <= 0.0:
            return False
        if (time.monotonic() - self._last_upload_mono) < _TRANSFER_STALL_KILL:
            return False
        registry = getattr(self.engine, "proc_registry", None)
        if not registry:
            return False
        # Ask the process GROUP, not the client: after rsync's own --timeout the
        # client exits (poll() says "done") while the child it forked sits stuck
        # in SMB I/O -- exactly the stall this rule exists to break.
        return any(_group_alive(proc.pid) for proc in list(registry))

    def _log_watchdog_transient(self, layer, consecutive):
        # type: (str, int) -> None
        now = time.monotonic()
        changed = (layer != self._watchdog_log_layer)
        if not changed and (now - self._watchdog_log_mono) < _GUARD_LOG_INTERVAL:
            return
        self._watchdog_log_layer = layer
        self._watchdog_log_mono = now
        self._log("info",
                  "watchdog: transient guard failure (layer: %s, %d/%d "
                  "consecutive); not killing the rsync group yet",
                  layer, consecutive, _WATCHDOG_CONSECUTIVE_KILL)

    def _record_kill_reason(self, reason):
        # type: (str) -> None
        """Stash why the watchdog just killed rsync so _sync_folder can attribute
        the resulting phase1 failure to us instead of logging a bare rc (A3)."""
        self._kill_reason = reason
        self._kill_reason_mono = time.monotonic()

    def _consume_kill_reason(self):
        # type: () -> Optional[str]
        """Return-and-clear a recent watchdog kill reason (within its TTL)."""
        reason = self._kill_reason
        self._kill_reason = None
        if reason is None:
            return None
        if (time.monotonic() - self._kill_reason_mono) > _KILL_REASON_TTL:
            return None
        return reason

    def _kill_sync_procs(self, reason=None):
        # type: (Optional[str]) -> None
        """SIGTERM every live rsync process group, give them ONE shared grace
        window, then SIGKILL the stragglers.

        Two-phase on purpose: with upload_concurrency workers there may be
        several rsync groups in flight, and terminating them one-at-a-time
        (each with its own grace wait) would stretch a guard-failure response
        to workers x _GUARD_TERM_GRACE seconds against a dead mount.
        """
        if reason is not None:
            self._record_kill_reason(reason)
        registry = getattr(self.engine, "proc_registry", None)
        if not registry:
            return
        # Every rsync runs in its own session, so its group id is the client's
        # pid -- valid while ANY member lives. os.getpgid(pid) stops working the
        # moment the client is reaped, which left its stuck children unkillable.
        pgids = []
        for proc in list(registry):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                continue
            pgids.append(proc.pid)
        if not pgids:
            return
        deadline = time.monotonic() + _GUARD_TERM_GRACE
        while time.monotonic() < deadline:
            pgids = [g for g in pgids if _group_alive(g)]
            if not pgids:
                return
            time.sleep(0.2)
        for pgid in pgids:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass

    def _shutdown_children(self):
        # type: () -> None
        self._kill_sync_procs()


_STDERR_LOG_MAX = 8 * 2 ** 20


def _phase1_text(phase, name, rc, kill_reason):
    # type: (str, str, object, Optional[str]) -> str
    if kill_reason:
        return "%s for %s killed by watchdog (reason: %s)" % (phase, name, kill_reason)
    return "%s failed for %s (rc=%s)" % (phase, name, rc)


def _source_has(src, rel):
    # type: (str, str) -> bool
    """rel exists in the source -- or only in iCloud: macOS <= 13 replaces an
    evicted iCloud Drive file X with a hidden .X.icloud placeholder, and an
    evicted package (a directory) with one placeholder for all of it."""
    if os.path.lexists(os.path.join(src, rel)):
        return True
    return manifest_mod.placeholder_covers(src, rel)


def _cloud_stat(path):
    # type: (str) -> Optional[Any]
    """(size, mtime_ns, inode) of path -- or, when only its macOS <= 13
    placeholder exists, of the placeholder with the real size from its plist
    (0 if unreadable) and inode 0. None when there is neither."""
    try:
        st = os.lstat(path)
        return int(st.st_size), int(st.st_mtime_ns), int(st.st_ino)
    except OSError:
        pass
    stub = manifest_mod.placeholder_path(path)
    try:
        sst = os.lstat(stub)
    except OSError:
        return None
    if not manifest_mod._is_stub_stat(sst):
        return None
    size = manifest_mod.stub_logical_size(stub)
    return int(size or 0), int(sst.st_mtime_ns), 0


def _trim_launchd_stderr(logger):
    # type: (Any) -> None
    """launchd.err.log (our stderr under launchd) is never rotated by anyone.
    It now only receives tracebacks and SIGINFO dumps, but an older install
    mirrored the whole log into it; start fresh once it passes 8 MB."""
    import sys
    try:
        if sys.stderr.isatty():
            return
        fd = sys.stderr.fileno()
        if os.fstat(fd).st_size > _STDERR_LOG_MAX:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            logger.info("launchd.err.log exceeded %d MB; truncated",
                        _STDERR_LOG_MAX // 2 ** 20)
    except (OSError, ValueError, AttributeError):
        pass


def _group_alive(pgid):
    # type: (int) -> bool
    try:
        os.killpg(pgid, 0)
        return True
    except OSError:
        return False


def run_daemon(config_path, once):
    # type: (str, bool) -> int
    """Wire real components from paths.py + config and run the daemon.

    Exit codes: 0 clean / duplicate-lock / SIGTERM; 1 fatal (config error, with a
    human message on stderr); in --once, 2 if any folder failed to sync.
    """
    import sys
    from . import config as config_mod
    from . import notify as notify_mod
    from .state import StateStore
    from .mount import MountManager
    from .engine import SyncEngine
    from .trash import TrashManager

    for directory in (paths.APP, paths.RUNTIME, paths.LOGDIR):
        try:
            os.makedirs(os.fspath(directory), exist_ok=True)
        except OSError as exc:
            sys.stderr.write("MyCloud: cannot create %s: %s\n" % (directory, exc))
            return 1

    logger = notify_mod.setup_logging(paths.LOGDIR)
    _trim_launchd_stderr(logger)

    try:
        cfg = config_mod.load_config(config_path)
    except config_mod.ConfigError as exc:
        sys.stderr.write("MyCloud configuration error: %s\n" % exc)
        logger.error("configuration error: %s", exc)
        # Say it where the GUI looks. It only reached launchd.err.log, and the
        # app showed the previous run's state (or "unresponsive") for a daemon
        # that launchd restarts into the same error every 30s. The next
        # successful start's STARTING write clears it.
        try:
            StateStore(paths.RUNTIME, logger=logger).update(
                state="stopped", daemon_pid=None, guard=None,
                last_error="配置文件有误，守护进程无法启动：%s" % exc)
        except Exception:
            pass
        return 1
    for warning in cfg.warnings:
        logger.warning("config: %s", warning)

    try:
        config_mod.write_excludes(cfg, paths.EXCLUDES)
    except OSError as exc:
        logger.warning("could not write excludes file: %s", exc)

    state = StateStore(paths.RUNTIME, logger=logger)
    proc_registry = set()
    engine = SyncEngine(paths.RSYNC, paths.EXCLUDES, logger, proc_registry=proc_registry)
    mount = MountManager(cfg.host, cfg.share, cfg.mount_username, paths.MNT,
                         cfg.sentinel_uuid, logger, guest=cfg.anonymous)
    trash_mgr = TrashManager(
        os.path.join(os.fspath(paths.MNT), paths.trash_rel(cfg.dest_rel)),
        cfg.retention_days, cfg.trash_max_gb, logger)
    notifier = notify_mod.Notifier(state, logger=logger)

    daemon = Daemon(cfg, state, mount, engine, trash_mgr, notifier, logger, once=once)
    try:
        return daemon.run()
    except Exception as exc:  # last-resort: log and report fatal
        logger.exception("daemon crashed: %s", exc)
        return 1
