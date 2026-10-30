"""Shared utilities: subprocess runner, atomic file writes, timestamp helpers.

Everything here targets Python 3.9 stdlib only.
"""

import json
import logging
import errno
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections import namedtuple
from datetime import datetime
from typing import Any, List, Optional

RunResult = namedtuple("RunResult", "rc out err timed_out")

# Stamp format: %Y-%m-%dT%H%M%S (no colons), e.g. "2026-07-18T153000".
_STAMP_FMT = "%Y-%m-%dT%H%M%S"
_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}$")

_LOG = logging.getLogger("mycloud.util")

# Unwedgeable-timeout policy. subprocess.run's timeout path does kill() then a
# BLOCKING wait(), which hangs forever on an uninterruptible/D-state child -- the
# exact hazard behind the incident where mount_smbfs to an unreachable host
# wedged the daemon (state.json stale 949s) and left forked orphans alive for
# 13+ minutes. Instead we Popen with start_new_session=True (its own process
# group, so internal forks -- e.g. mount_smbfs's -- die with a group kill), and
# on timeout SIGKILL the whole group then poll for the leader to die for at most
# _REAP_TIMEOUT. A child that still refuses to die is ABANDONED: run() returns at
# once (never blocking the caller) and the Popen is queued in _ABANDONED, swept
# by non-blocking poll() on later run() calls -- no dedicated thread, no zombie
# accumulation. run() may be called from several threads (the materialize
# workers), so _ABANDONED is lock-protected.
_REAP_TIMEOUT = 10.0
_REAP_POLL = 0.2
_ABANDONED = []  # type: List[Any]
_ABANDONED_LOCK = threading.Lock()


def _reap_abandoned():
    # type: () -> None
    """Non-blocking sweep of previously-abandoned children; drop the dead ones."""
    with _ABANDONED_LOCK:
        if not _ABANDONED:
            return
        survivors = []
        for proc in _ABANDONED:
            try:
                alive = proc.poll() is None
            except Exception:
                alive = False   # already reaped / unusable -> forget it
            if alive:
                survivors.append(proc)
        _ABANDONED[:] = survivors


def _kill_group(proc, sig=signal.SIGKILL):
    # type: (Any, int) -> None
    """SIGKILL the child's whole process group; fall back to the bare pid.

    Children run with start_new_session=True, so the group id IS the child's
    pid and stays valid while any member lives. os.getpgid(pid) fails as soon
    as the leader is a zombie, and the old fallback then killed only the leader
    -- leaking the helpers it had forked (mount_smbfs/smbutil do fork).
    """
    try:
        os.killpg(proc.pid, sig)
        return
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except (OSError, ProcessLookupError):
        pass


def _close_pipes(proc):
    # type: (Any) -> None
    """Close the child's pipe streams without reading (never blocks)."""
    for stream in (getattr(proc, "stdin", None),
                   getattr(proc, "stdout", None),
                   getattr(proc, "stderr", None)):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def _abandon(proc, cmd):
    # type: (Any, List[str]) -> None
    """Queue an unkillable child for later sweeping; warn with its pid."""
    with _ABANDONED_LOCK:
        _ABANDONED.append(proc)
    name = cmd[0] if cmd else "?"
    _LOG.warning(
        "util.run: child pid %s (%s) ignored SIGKILL for %.0fs; abandoning it to "
        "avoid blocking the caller (it is swept by a later util.run call)",
        getattr(proc, "pid", "?"), name, _REAP_TIMEOUT)


def run(cmd, timeout, input_text=None):
    # type: (List[str], float, Optional[str]) -> RunResult
    """Run a subprocess with a mandatory, UNWEDGEABLE timeout.

    Always captures text output. On a clean finish returns (rc, out, err,
    timed_out=False). On timeout the child's whole process group is SIGKILLed and
    reaped for up to _REAP_TIMEOUT; a child that still will not die is ABANDONED
    (run returns at once with timed_out=True and out/err="") and swept later, so a
    stuck/uninterruptible child can never block the caller. out/err on timeout are
    empty (best-effort; every caller checks timed_out first). Never shell=True. A
    spawn failure (e.g. missing binary) still raises, exactly as before.
    """
    # Sweep any lingering abandoned children first (cheap, non-blocking).
    _reap_abandoned()

    proc = subprocess.Popen(
        cmd,
        stdin=(subprocess.PIPE if input_text is not None else None),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # Output can carry NAS-side bytes (a sentinel file's content, a share
        # listing): strict UTF-8 decoding raised UnicodeDecodeError out of a
        # guard check -- a crash on the daemon's startup path.
        errors="replace",
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
        return RunResult(proc.returncode, out or "", err or "", False)
    except subprocess.TimeoutExpired:
        pass  # fall through to the unwedgeable kill/reap path

    _kill_group(proc)
    _close_pipes(proc)
    deadline = time.monotonic() + _REAP_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _kill_group(proc)   # the leader is gone; sweep what it left behind
            return RunResult(None, "", "", True)   # group died -> reaped
        time.sleep(_REAP_POLL)
    # Still alive after the bound: abandon it and return immediately.
    _abandon(proc, cmd)
    return RunResult(None, "", "", True)


def atomic_write_text(path, text):
    # type: (Any, str) -> None
    """Write text atomically: temp file in the same dir + fsync + os.replace."""
    path = os.fspath(path)
    directory = os.path.dirname(path) or "."
    fd = None
    tmp_name = None
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=directory,
        delete=False,
    )
    try:
        tmp_name = tmp.name
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if not tmp.closed:
            tmp.close()
        if tmp_name is not None and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def atomic_write_json(path, obj):
    # type: (Any, Any) -> None
    """Serialize obj as pretty JSON and write it atomically."""
    atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True))


def read_json(path, default=None):
    # type: (Any, Any) -> Any
    """Read JSON, returning default on missing or corrupt files."""
    try:
        with open(os.fspath(path), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def make_stamp(dt):
    # type: (datetime) -> str
    """Format a datetime as a colon-free timestamp string."""
    return dt.strftime(_STAMP_FMT)


def parse_stamp(name):
    # type: (str) -> Optional[datetime]
    """Parse a strict %Y-%m-%dT%H%M%S stamp; return None for anything else.

    This regex gates which .trash directories retention pruning may ever touch,
    so it must reject any name that is not exactly the expected shape.
    """
    if not isinstance(name, str) or not _STAMP_RE.match(name):
        return None
    try:
        return datetime.strptime(name, _STAMP_FMT)
    except ValueError:
        return None


_DIR_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_subdir(name, parent_fd, root_dev, where):
    # type: (str, int, int, str) -> int
    """Open a subdirectory relative to its parent's fd and prove, ON THE OPENED
    FD, that it is still on the root's device. Deciding on the fd leaves no
    window between the check and the deletes that follow: if a mount lands on
    this directory later, the fd still names the covered local directory."""
    fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
    try:
        if os.fstat(fd).st_dev != root_dev:
            raise OSError(errno.EBUSY, "refusing to cross a mountpoint", where)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _walk_fd(dir_fd, root_dev, where, delete):
    # type: (int, int, str, bool) -> None
    with os.scandir(dir_fd) as it:
        entries = list(it)
    for entry in entries:
        sub_where = os.path.join(where, entry.name)
        if entry.is_dir(follow_symlinks=False):
            sub_fd = _open_subdir(entry.name, dir_fd, root_dev, sub_where)
            try:
                _walk_fd(sub_fd, root_dev, sub_where, delete)
            finally:
                os.close(sub_fd)
            if delete:
                os.rmdir(entry.name, dir_fd=dir_fd)
        elif delete:
            try:
                os.unlink(entry.name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass


def remove_local_tree(path):
    # type: (str) -> None
    """Recursively delete a LOCAL directory tree, refusing at any mountpoint.

    Deliberately NOT the shutil recursive-delete helper, which test_invariants
    confines to trash.py (the one place allowed to destroy data on the NAS);
    that scanner matches the helper's name anywhere in a source file, so this
    docstring avoids it too. This primitive exists for the installer and the
    uninstaller: <APP> contains the daemon's private mountpoint, so a recursive
    delete that crossed mounts could walk into a live share.

    Two passes over directory FDs (symlinks never followed): pass 1 surveys the
    whole tree and raises EBUSY before a single byte is removed if any
    directory sits on another device; pass 2 deletes bottom-up with the same
    per-FD device check. The previous path-based walk had a window between
    "checked" and "deleted" in which an in-flight mount_smbfs could land and
    the delete would empty the share (reproduced with a disk image,
    2026-09-25). An unreadable directory aborts rather than being skipped (it
    could hide a mountpoint). A missing path is not an error.
    """
    path = os.path.abspath(path)
    if os.path.islink(path):
        os.unlink(path)
        return
    if not os.path.lexists(path):
        return
    if not os.path.isdir(path):
        os.unlink(path)
        return
    if os.path.ismount(path):
        raise OSError(errno.EBUSY, "refusing to remove a mountpoint", path)
    root_fd = os.open(path, _DIR_OPEN_FLAGS)
    try:
        root_dev = os.fstat(root_fd).st_dev
        _walk_fd(root_fd, root_dev, path, delete=False)
        _walk_fd(root_fd, root_dev, path, delete=True)
    finally:
        os.close(root_fd)
    os.rmdir(path)
