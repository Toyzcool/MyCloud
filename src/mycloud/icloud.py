"""iCloud "download then back up" policy pipeline (PLAN section 14, M4).

macOS marks an evicted iCloud Drive file "dataless": its bytes live only in the
cloud and st_flags carries SF_DATALESS (0x40000000). os.lstat/os.stat NEVER
materialize such a file, but open()+read() DOES trigger a download -- and Apple's
openrsync mmap()s source files and ABORTS the whole run (rc=20, "mmap: Resource
deadlock avoided") on a dataless file. So a dataless file can never be handed to
rsync as-is.

Two policies (config [backup] icloud_policy):
  skip      dataless files stay out of the upload set and are surfaced.
  download  before a batch, materialize (open+read 1 byte, poll until the
            SF_DATALESS bit clears), transfer, then optionally brctl-evict the
            files WE materialized (never ones the user had local).

brctl evict availability is probed at runtime: on this macOS build `brctl help`
lists no evict subcommand, so eviction degrades to a logged no-op (PLAN M4).

Python 3.9 stdlib only. The dataless predicate and the per-file read/poll are
injectable so tests -- which cannot create real dataless files -- can fake them.
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional, Tuple

# st_flags bit set by macOS on an evicted (cloud-only) iCloud Drive file.
SF_DATALESS = 0x40000000

_DEFAULT_PER_FILE_TIMEOUT = 120.0
_DEFAULT_WORKERS = 4
_DEFAULT_POLL_INTERVAL = 0.5
# A read trigger's answer when the download was refused outright.
REJECTED = "rejected"


def is_dataless(path, st=None):
    # type: (str, object) -> bool
    """True iff path is an iCloud dataless (cloud-only) file. lstat only.

    Never opens the file, so this cannot trigger a download. A missing file or a
    platform without st_flags reads as not-dataless.
    """
    try:
        if st is None:
            st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS)


def dataless_check(path, st):
    # type: (str, object) -> bool
    """(path, st) adapter matching Manifest.plan_folder's dataless_check param."""
    return is_dataless(path, st)


def free_bytes(path):
    # type: (str) -> int
    """Free bytes available on the filesystem holding path (statvfs)."""
    st = os.statvfs(os.fspath(path))
    return st.f_bavail * st.f_frsize


def disk_guard_ok(local_path, need_bytes, factor=3):
    # type: (str, int, float) -> bool
    """True iff local free space >= factor * need_bytes (PLAN M4 disk guard).

    Refuse a download batch that would risk filling the boot disk: a 45GB iCloud
    library is materialized only a batch at a time, and each batch needs headroom
    for the downloaded bytes plus working space.
    """
    if need_bytes <= 0:
        return True
    try:
        return free_bytes(local_path) >= factor * need_bytes
    except OSError:
        # Cannot stat the volume: fail safe by refusing the download.
        return False


def evict_available(run_cmd):
    # type: (Callable) -> bool
    """True iff `brctl` exposes an `evict` subcommand on this machine.

    Parsed from `brctl help` output (a command block whose first token is
    'evict'). On this macOS build it does not -- so eviction is skipped.
    """
    from . import paths
    try:
        res = run_cmd([paths.BRCTL, "help"], 10)
    except Exception:
        return False
    if getattr(res, "timed_out", False):
        return False
    text = (getattr(res, "out", "") or "") + (getattr(res, "err", "") or "")
    for line in text.splitlines():
        token = line.strip().split(" ", 1)[0] if line.strip() else ""
        if token == "evict":
            return True
    return False


def evict_paths(abspaths, run_cmd, logger=None):
    # type: (List[str], Callable, object) -> Tuple[List[str], List[str]]
    """brctl-evict each path (best effort). Returns (evicted, errors).

    Caller must have confirmed evict_available() first; this still guards each
    call and never raises. Only files WE materialized should be passed in.
    """
    from . import paths
    evicted = []  # type: List[str]
    errors = []  # type: List[str]
    for path in abspaths:
        try:
            res = run_cmd([paths.BRCTL, "evict", path], 30)
        except Exception as exc:
            errors.append("%s: %s" % (path, exc))
            continue
        if getattr(res, "timed_out", False) or getattr(res, "rc", 1) != 0:
            errors.append("%s: rc=%s" % (path, getattr(res, "rc", "?")))
        else:
            evicted.append(path)
    return evicted, errors


def _make_default_read_trigger(per_file_timeout, run_cmd):
    # type: (float, Optional[Callable]) -> Callable
    """Build the production read trigger: a SUBPROCESS 1-byte read, time-bounded.

    A dataless file is downloaded by reading any byte of it. The classic
    implementation -- ``open(path,'rb').read(1)`` on the worker thread -- is the
    bug behind the observed >2h stall: an iCloud read that wedges in the kernel
    (uninterruptible sleep) cannot be interrupted from Python, so a Python thread
    blocked in that read is stuck forever, and four such files starve the whole
    pool. Instead we spawn ``/usr/bin/head -c 1 <path>`` via util.run with the
    per-file timeout: util.run waits on the child's pipes with an INTERRUPTIBLE
    poll and kills the child on expiry, so the worker thread is always freed
    within per_file_timeout even if the child itself is wedged. run_cmd is
    injectable so tests never spawn a real process.
    """
    from . import paths, util
    from .manifest import has_placeholder, placeholder_path
    runner = run_cmd or util.run

    def _brctl_ok(target):
        # type: (str) -> Optional[bool]
        """True: accepted; False: refused outright; None: timed out."""
        res = runner([paths.BRCTL, "download", target], per_file_timeout)
        if getattr(res, "timed_out", False):
            return None
        return getattr(res, "rc", 1) == 0

    def _trigger(path):
        # type: (str) -> Optional[str]
        if not os.path.lexists(path) and has_placeholder(path):
            # macOS <= 13: the file is a .X.icloud placeholder and X does not
            # exist, so there is nothing to read; ask the iCloud daemon.
            # (Absolute paths only ever reach here, so neither argument can
            # be read as an option.)
            first = _brctl_ok(path)
            if first is False:
                first = _brctl_ok(placeholder_path(path))
            if first is False:
                # Refused at once (a placeholder iCloud does not know, e.g.
                # copied out of iCloud Drive; a brctl without "download"):
                # not a timeout, so not evidence of the launchd restriction.
                return REJECTED
            return None
        runner([paths.HEAD, "-c", "1", path], per_file_timeout)
        return None
    return _trigger


def _gone(path):
    # type: (str) -> bool
    """Neither the file nor a placeholder for it (or for a package above it)
    exists: it was moved or deleted since it was listed."""
    from .manifest import has_placeholder
    if os.path.lexists(path):
        return False
    probe = path
    while probe and probe != os.sep:
        if has_placeholder(probe):
            return False
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return True


def materialize_batch(abspaths, is_dataless_fn=None, read_fn=None,
                      per_file_timeout=_DEFAULT_PER_FILE_TIMEOUT,
                      workers=_DEFAULT_WORKERS, stop_event=None,
                      poll_interval=_DEFAULT_POLL_INTERVAL, run_cmd=None,
                      gone=None, rejected=None):
    # type: (List[str], Optional[Callable], Optional[Callable], float, int, Optional[threading.Event], float, Optional[Callable], Optional[List[str]], Optional[List[str]]) -> Tuple[List[str], List[str]]
    """Download a batch of dataless files, up to `workers` at a time.

    For each path: trigger a download (read_fn), then poll is_dataless_fn(path)
    until it clears or per_file_timeout elapses. Returns (materialized, failed).
    Aborts promptly when stop_event is set (daemon shutdown), marking the
    unfinished paths as failed. is_dataless_fn/read_fn are injectable for tests.

    EACH FILE IS GENUINELY BOUNDED to ~per_file_timeout total: the per-file
    deadline is computed BEFORE the read trigger, and both the (subprocess) read
    and the subsequent poll are bounded by that single deadline. So a file whose
    read wedges for the whole timeout is marked failed and SKIPPED (surfaced by
    the caller) rather than retried forever or starving the pool. read_fn
    defaults to a subprocess 1-byte read (see _make_default_read_trigger);
    run_cmd is threaded into that default so tests can stub the subprocess.

    Two outcomes are reported apart, when the caller passes lists for them,
    because neither says anything about whether downloads work here: gone
    (the path vanished since it was listed -- in NEITHER result list) and
    rejected (brctl refused the placeholder outright -- also in failed). A
    missing path used to count as downloaded at once; waiting the full
    timeout on it instead would read a moved folder as the launchd
    restriction and latch every download off for 24 h.
    """
    if is_dataless_fn is None:
        is_dataless_fn = is_dataless  # type: ignore[assignment]
    if read_fn is None:
        read_fn = _make_default_read_trigger(per_file_timeout, run_cmd)  # type: ignore[assignment]
    if not abspaths:
        return [], []

    materialized = []  # type: List[str]
    failed = []  # type: List[str]
    lock = threading.Lock()

    def _one(path):
        # type: (str) -> None
        if stop_event is not None and stop_event.is_set():
            with lock:
                failed.append(path)
            return
        import time as _time
        # Checked once, before the trigger (with one recheck: iCloud may be
        # swapping the placeholder for the file on its own). Never inside the
        # poll: during a brctl download the placeholder can go before X lands.
        if _gone(path):
            if poll_interval > 0:
                _time.sleep(poll_interval)
            if _gone(path):
                if gone is not None:
                    with lock:
                        gone.append(path)
                return
        # Deadline BEFORE the read so a read that consumes the whole budget is
        # counted against this file's timeout (the poll below then breaks at
        # once and the file is marked failed -- total stays ~per_file_timeout).
        deadline = _time.monotonic() + per_file_timeout
        try:
            outcome = read_fn(path)
        except Exception:
            # A failed trigger is not fatal; the poll below still decides.
            outcome = None
        if outcome == REJECTED:
            with lock:
                failed.append(path)
                if rejected is not None:
                    rejected.append(path)
            return
        ok = False
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                # A path that does not exist is not downloaded, whatever the
                # predicate says (is_dataless reads a missing file as False):
                # a macOS <= 13 placeholder target used to count as done at once.
                still = bool(is_dataless_fn(path)) or not os.path.lexists(path)
            except Exception:
                still = True
            if not still:
                ok = True
                break
            if _time.monotonic() >= deadline:
                break
            wait = poll_interval
            if stop_event is not None:
                stop_event.wait(wait)
            elif wait > 0:
                _time.sleep(wait)
        with lock:
            (materialized if ok else failed).append(path)

    max_workers = max(1, min(int(workers or 1), len(abspaths)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(_one, abspaths))
    return materialized, failed
