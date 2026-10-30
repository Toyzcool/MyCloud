"""Two-phase sync engine (the heart of MyCloud).

Phase 1 mirrors + versions overwritten files via rsync's own --backup-dir.
Phase 2 computes deletions with a dry-run and executes them in Python by
renaming mirror entries into the trash (never rsync --delete in write mode).

Why two phases: on this machine /usr/bin/rsync is Apple openrsync, which
SILENTLY no-ops the entire run when -b/--backup-dir is combined with any
--delete* flag (verified 2026-07-18, PLAN section 2). Therefore a write-mode
rsync invocation must NEVER carry a --delete* flag anywhere in this codebase.

Python 3.9 stdlib only. No third-party deps.
"""

import os
import re
import signal
import subprocess
import threading
import time
from collections import namedtuple
from typing import Dict, List, Optional

from . import paths, util
from .manifest import placeholder_covers

# kind (failures only): "io_timeout" -- rsync's own --timeout fired (worth one
# retry); "killed" -- a signal ended it (our watchdog, or a wedged run we gave
# up on: never retry into the same dead mount); "usage" -- rsync rejected the
# command line itself (an option this rsync does not have): nothing about the
# NAS, and nothing a retry or a remount can change; "error" -- anything else.
SyncResult = namedtuple("SyncResult",
                        "status rc files_copied bytes_copied failed_paths kind",
                        defaults=(None,))

# After the rsync client exits, children it forked (the local "server" side)
# may still hold stderr -- blocked in uninterruptible I/O against a wedged SMB
# share. Wait this long for the drain, then SIGKILL the group and wait a little
# more, then give up on them instead of blocking the sync thread forever.
_DRAIN_GRACE = 30.0
_REAP_GRACE = 10.0
# At most one liveness callback per second while output is flowing.
_ALIVE_MIN_INTERVAL = 1.0

# Both implementations exit 20 when a SIGINT/SIGTERM/SIGHUP ends them. rsync
# 2.6.9 reports its own --timeout as exit 30 ("io timeout after N seconds");
# openrsync reports it as exit 20 with "poll: timeout" on stderr.
_IO_TIMEOUT_RE = re.compile(
    r"io timeout after \d+ seconds|poll: timeout|timeout in data send/receive")
_QUOTED_RE = re.compile(r'"([^"]+)"')
# rsync refusing its own command line. rsync 2.6.9: "rsync: --x: unknown
# option" + "rsync error: syntax or usage error (code 1)"; openrsync (getopt):
# "rsync: unrecognized option `--x'" / "rsync: invalid option -- x" / its usage
# banner. Every alternative is anchored to the start of rsync's own line, so a
# FILE NAME in a per-file error ("rsync(<pid>): error: invalid option
# notes.bin: write: No space left on device", 'write failed on ".../unknown
# option list.txt"') can never match. Applies only with exit 1 (both rsyncs
# exit 1 on a usage error; openrsync also exits 1 for runtime failures, hence
# the text match too).
_USAGE_RE = re.compile(
    r"^rsync: --?[^\s:]+(?:=\S*)?: unknown option$"
    r"|^rsync error: syntax or usage error \(code 1\)"
    r"|^rsync: unrecognized option [`']-"
    r"|^rsync: (?:invalid|illegal) option -- \S"
    r"|^usage: rsync ", re.MULTILINE)
# _ensure_dir's reason when mkdir -p hangs: the share stopped answering.
_DEST_TIMEOUT = "mkdir -p timed out"

# Every option a write-mode argv may carry. Each one exists in BOTH rsyncs
# MyCloud meets: Apple's samba rsync 2.6.9 (/usr/bin/rsync on macOS <= 14,
# apple-oss-distributions/rsync tag rsync-55 options.c) and openrsync (macOS
# 15+). The gateway's 2.6.9 rejected --contimeout (an rsync 3.0 option) with
# "unknown option", exit 1, on every upload for two months (2026-08-02 ..
# 10-02) -- invisible on the openrsync dev machine, where the flag is accepted.
# Adding an option here needs evidence it exists in rsync 2.6.9 too.
_PORTABLE_LONG = frozenset((
    "--backup-dir", "--inplace", "--modify-window", "--timeout", "--progress",
    "--itemize-changes", "--stats", "--from0", "--files-from", "--exclude-from",
))
_PORTABLE_SHORT = frozenset("rltDb")


def _killpg(pgid, sig):
    # type: (int, int) -> bool
    """Signal a whole process group by id. Every rsync is spawned with
    start_new_session=True, so its group id IS the client's pid -- valid for as
    long as any member lives, and never reused meanwhile. (os.getpgid(pid)
    stops working the moment the client is reaped, while its children, the
    ones actually stuck, live on.)"""
    try:
        os.killpg(pgid, sig)
        return True
    except OSError:
        return False


def assert_portable_argv(argv):
    # type: (List[str]) -> None
    """ABSOLUTE RULE: a write-mode argv uses only options that exist in rsync
    2.6.9 AND openrsync (see _PORTABLE_LONG). Positionals (paths) pass."""
    for arg in argv[1:]:
        if not arg.startswith("-") or arg == "-":
            continue
        if arg.startswith("--"):
            name = arg.split("=", 1)[0]
            assert name in _PORTABLE_LONG, (
                "rsync option %s is not known to exist in both rsync 2.6.9 "
                "(macOS 12-14) and openrsync: %s" % (name, arg))
        else:
            for ch in arg[1:]:
                assert ch in _PORTABLE_SHORT, (
                    "rsync short option -%s is not in the portable set: %s"
                    % (ch, arg))


def stderr_summary(err, limit=400):
    # type: (str, int) -> str
    """Head + tail of rsync's stderr. The tail alone lost the cause: Apple's
    2.6.9 puts "unknown option" on the FIRST line and a ~190-char build path
    on the last."""
    text = (err or "").strip()
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + " ... " + text[-half:]


def _failure_kind(rc, err, timed_out):
    # type: (Optional[int], str, bool) -> str
    if timed_out or rc is None or rc < 0:
        return "killed"
    if rc == 1 and _USAGE_RE.search(err or ""):
        return "usage"
    if rc == 30 or _IO_TIMEOUT_RE.search(err or ""):
        return "io_timeout"
    if rc == 20:
        return "killed"
    return "error"
Phase2Result = namedtuple(
    "Phase2Result", "moved skipped_recreated skipped_unsafe errors moved_relpaths")

_DELETING_PREFIX_B = b"*deleting "

# Itemize lines that denote an actual file transfer begin with ">" (received)
# or "<" (sent); openrsync uses the same first-column convention as GNU rsync.
# The flags field is whitespace-free, so the relpath is everything after the
# first space (filenames may themselves contain spaces, so split only once).
_XFER_FIRST_B = (b">", b"<")

# openrsync escapes non-ASCII / control bytes in itemize output as "\#ooo"
# (three octal digits). We unescape at the byte level before decoding.
_ESC_RE = re.compile(rb"\\#([0-7]{3})")

# Tolerant stats parsing (openrsync wording differs from GNU rsync).
_FILES_RE = re.compile(r"Number of files transferred:\s*([\d,]+)")
_BYTES_RE = re.compile(r"Total transferred file size:\s*([\d,]+)")


def _unescape_rsync(raw):
    # type: (bytes) -> bytes
    """Turn openrsync's "\\#ooo" octal escapes back into the original bytes."""
    return _ESC_RE.sub(lambda m: bytes([int(m.group(1), 8)]), raw)


def _transfer_relpath_bytes(line):
    # type: (bytes) -> Optional[str]
    """Return the relpath of an itemize transfer line, or None if not one.

    Operates on raw bytes so openrsync's "\\#ooo" octal escapes round-trip; the
    result is decoded with 'replace' because it is display-only (state.json is
    JSON, which cannot carry surrogate bytes).
    """
    if line[:1] not in _XFER_FIRST_B:
        return None
    parts = line.split(b" ", 1)
    if len(parts) != 2:
        return None
    rel = _unescape_rsync(parts[1]).rstrip(b"\r\n")
    if rel == b"":
        return None
    return rel.decode("utf-8", "replace")


def _to_int(text):
    # type: (str) -> int
    try:
        return int(text.replace(",", ""))
    except (TypeError, ValueError):
        return 0


def _norm_subtree(subtree):
    # type: (Optional[str]) -> Optional[str]
    """Return a cleaned subtree relpath, or None for whole-root ('.' or None)."""
    if subtree is None:
        return None
    stripped = subtree.strip("/")
    if stripped == "" or stripped == ".":
        return None
    return stripped


def _is_within(path, root):
    # type: (str, str) -> bool
    """True iff realpath(path) is root itself or lives under root (realpath)."""
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(path)
    if real_path == real_root:
        return True
    return real_path.startswith(real_root + os.sep)


class SyncEngine(object):
    def __init__(self, rsync_bin, excludes_file, logger, proc_registry=None):
        # type: (str, object, object, Optional[set]) -> None
        self.rsync_bin = str(rsync_bin)
        self.excludes_file = str(excludes_file)
        self.logger = logger
        # The daemon watchdog kills live rsync process groups via os.killpg on
        # a guard failure; we register each Popen here before waiting on it.
        self.proc_registry = proc_registry

    # -- Phase 1: copy + overwrite versioning ------------------------------

    def phase1(self, src_root, dst_root, backup_dir, subtree=None, timeout=None,
               on_progress=None):
        # type: (object, object, object, Optional[str], Optional[float], Optional[object]) -> SyncResult
        sub = _norm_subtree(subtree)
        src_root = str(src_root)
        dst_root = str(dst_root)
        backup_dir = str(backup_dir)
        if sub is None:
            eff_src = src_root
            eff_dst = dst_root
            eff_backup = backup_dir
        else:
            eff_src = os.path.join(src_root, sub)
            eff_dst = os.path.join(dst_root, sub)
            eff_backup = os.path.join(backup_dir, sub)

        problem = self._ensure_dir(eff_dst)
        if problem:
            return self._dest_failure(problem)

        # Trailing slashes on both endpoints are semantically required (rsync
        # copies contents-of-src into dst rather than nesting src under dst).
        argv = [
            self.rsync_bin,
            "-rltD",
            "-b",
            "--backup-dir=" + eff_backup,
            "--modify-window=2",
            "--timeout=180",
            "--itemize-changes",
            "--stats",
            "--exclude-from=" + self.excludes_file,
            eff_src + "/",
            eff_dst + "/",
        ]

        self._assert_no_delete(argv)
        assert_portable_argv(argv)
        rc, out, err, timed_out = self._run(argv, timeout, on_progress=on_progress)
        return self._finish(rc, out, err, timed_out)

    # -- Phase 1 (file variant): single-file copy + overwrite versioning ---

    def phase1_file(self, src_file, dst_dir, backup_dir, timeout=None,
                    on_progress=None, on_alive=None):
        # type: (object, object, object, Optional[float], Optional[object], Optional[object]) -> SyncResult
        """Mirror ONE file into dst_dir/ (no -r), versioning any overwrite.

        rsync preserves the source basename, so the file lands at
        dst_dir/<basename>; an overwritten prior version is moved by rsync's own
        -b/--backup-dir into backup_dir/<basename>. No --exclude-from applies to
        a single named file. Same status/registry/no-delete machinery as phase1.
        """
        src_file = str(src_file)
        dst_dir = str(dst_dir)
        backup_dir = str(backup_dir)
        problem = self._ensure_dir(dst_dir)
        if problem:
            return self._dest_failure(problem)

        argv = [
            self.rsync_bin,
            "-ltD",
            "-b",
            "--backup-dir=" + backup_dir,
            "--modify-window=2",
            "--timeout=180",
            "--progress",
            "--itemize-changes",
            "--stats",
            src_file,
            dst_dir + "/",
        ]

        self._assert_no_delete(argv)
        assert_portable_argv(argv)
        rc, out, err, timed_out = self._run(argv, timeout, on_progress=on_progress,
                                            on_alive=on_alive)
        return self._finish(rc, out, err, timed_out)

    # -- Phase 1 (batch variant): explicit file list via --files-from --------

    def phase1_batch(self, src_root, dst_root, backup_dir, relpaths,
                     timeout=None, on_progress=None, inplace=False,
                     no_times=False, on_alive=None):
        # type: (object, object, object, List[str], Optional[float], Optional[object], bool, bool, Optional[object]) -> SyncResult
        """Mirror an EXPLICIT list of relpaths (PLAN 14.3 batched pipeline).

        The batch is fed to rsync as a NUL-delimited list on stdin
        (--from0 --files-from=-), which is newline-safe and CJK-safe and needs no
        temp file (verified against this openrsync 2026-07-20). Same no-delete
        invariant as phase1; no --exclude-from because the relpaths are already
        the post-exclude plan. rsync creates parent directories for each listed
        file automatically.

        Per-file round-trip slimming (PLAN section 15 P0, verified 2026-07-21):

        inplace=True writes each file in place (--inplace, no temp+rename round
        trip) and MUST only be used for batches of NEW files (no mirror entry):
        --inplace combined with -b/--backup-dir is DESTRUCTIVE on this openrsync
        (the destination file vanishes and the NEW content lands in the backup
        dir -- verified 2026-07-21), so an inplace argv never carries -b. A new
        file has no prior version to preserve, so dropping -b there loses
        nothing. Callers keep changed-file batches on the -b path (inplace=False).

        no_times=True drops -t (one SET_INFO round trip saved per file). The
        NAS mtime then means "backup time", which is fine because the truth
        lives in the local manifest; the deep verify compares size only in this
        mode (daemon._verify_folder).
        """
        src_root = str(src_root)
        dst_root = str(dst_root)
        backup_dir = str(backup_dir)
        problem = self._ensure_dir(dst_root)
        if problem:
            return self._dest_failure(problem)

        # "./" in front of every entry: --files-from treats a line starting with
        # "#" or ";" as a COMMENT and silently drops it (rc stays 0), so such
        # files were recorded as uploaded yet never reached the NAS, and every
        # deep-verify re-upload was dropped again. "./" does not change the
        # relative path (-R starts there anyway) and keeps itemize output clean.
        payload = b"".join(b"./" + os.fsencode(rel) + b"\0" for rel in relpaths)
        argv = [
            self.rsync_bin,
            "-rlD" if no_times else "-rltD",
        ]
        if inplace:
            argv.append("--inplace")
        else:
            argv.extend(["-b", "--backup-dir=" + backup_dir])
        argv.extend([
            "--modify-window=2",
            "--timeout=180",
            "--progress",
            "--itemize-changes",
            "--stats",
            "--from0",
            "--files-from=-",
            src_root + "/",
            dst_root + "/",
        ])

        self._assert_no_delete(argv)
        self._assert_inplace_backup_exclusive(argv)
        assert_portable_argv(argv)
        rc, out, err, timed_out = self._run(
            argv, timeout, on_progress=on_progress, stdin_bytes=payload,
            on_alive=on_alive)
        return self._finish(rc, out, err, timed_out)

    def batch_selftest(self, work_dir):
        # type: (str) -> Optional[str]
        """Run the REAL phase1_batch argv (both variants) on a tiny local tree.

        None when this rsync accepts and executes it, else a reason. The
        capability probe and the doctor used to run their OWN argv, so both
        stayed green on the gateway while every real upload died with "unknown
        option" (2026-08-02 .. 10-02). work_dir must be a scratch directory.
        """
        src = os.path.join(work_dir, "src")
        dst = os.path.join(work_dir, "dst")
        bak = os.path.join(work_dir, "bak")
        try:
            os.makedirs(src, exist_ok=True)
            os.makedirs(dst, exist_ok=True)
            for name in ("a.txt", "#b.txt"):
                with open(os.path.join(src, name), "w", encoding="utf-8") as h:
                    h.write("mycloud selftest\n")
        except OSError as exc:
            return "selftest scratch setup failed: %s" % exc
        for inplace in (True, False):
            res = self.phase1_batch(src, dst, bak, ["a.txt", "#b.txt"],
                                    timeout=60, inplace=inplace, no_times=True)
            if res.status != "ok":
                return ("rsync rejected MyCloud's upload command (%s, rc=%s, "
                        "kind=%s)" % ("--inplace" if inplace else "-b", res.rc,
                                      getattr(res, "kind", None)))
        for name in ("a.txt", "#b.txt"):
            if not os.path.exists(os.path.join(dst, name)):
                return "rsync exited 0 but did not copy %s" % name
        return None

    def dangling_link_selftest(self, work_dir):
        # type: (str) -> bool
        """True iff this rsync copies a dangling symlink inside a batch AS A
        LINK, and keeps going with the rest of the batch -- with the real
        phase1_batch argv, both variants.

        rsync 2.6.9 (macOS <= 14) lstat()s every --files-from entry, so a link
        whose target does not exist on this Mac (e.g. an absolute path on the
        Mac that created it, synced over iCloud) copies like any other.
        openrsync stat()s (follows) each entry, fails on the link and silently
        drops the rest of the batch (exit 23) -- which is why dangling links
        were kept out of every batch, and so were never backed up anywhere.
        work_dir must be a scratch directory.
        """
        target = "/nonexistent/mycloud-selftest-target"
        src = os.path.join(work_dir, "src")
        try:
            os.makedirs(src, exist_ok=True)
            for name in ("a.txt", "z.txt"):
                with open(os.path.join(src, name), "w", encoding="utf-8") as h:
                    h.write("mycloud selftest\n")
            os.symlink(target, os.path.join(src, "link"))
        except OSError:
            return False
        for inplace in (True, False):
            dst = os.path.join(work_dir, "dst-inplace" if inplace else "dst-b")
            res = self.phase1_batch(src, dst, os.path.join(work_dir, "bak"),
                                    ["a.txt", "link", "z.txt"], timeout=60,
                                    inplace=inplace, no_times=True)
            link = os.path.join(dst, "link")
            try:
                copied = os.path.islink(link) and os.readlink(link) == target
            except OSError:
                copied = False
            if (res.status != "ok" or not copied
                    or not os.path.exists(os.path.join(dst, "z.txt"))):
                return False
        return True

    @staticmethod
    def _assert_no_delete(argv):
        # type: (List[str]) -> None
        """ABSOLUTE RULE: openrsync silently no-ops the whole run when -b is
        combined with --delete*. A write-mode argv must never carry one."""
        for arg in argv:
            assert "--delete" not in arg, (
                "write-mode rsync argv must never contain a --delete* flag "
                "(openrsync silently no-ops the entire run when combined with "
                "-b): " + arg
            )

    @staticmethod
    def _assert_inplace_backup_exclusive(argv):
        # type: (List[str]) -> None
        """ABSOLUTE RULE: --inplace + -b/--backup-dir is destructive on this
        openrsync (destination file vanishes, new content lands in the backup
        dir -- verified 2026-07-21). An argv must never carry both."""
        if "--inplace" not in argv:
            return
        for arg in argv:
            assert arg != "-b" and not arg.startswith("--backup-dir"), (
                "rsync argv must never combine --inplace with -b/--backup-dir "
                "(openrsync destroys the destination file): " + arg
            )

    def _finish(self, rc, out, err, timed_out):
        # type: (int, str, str, bool) -> SyncResult
        """Map an rsync run's (rc, out, err, timed_out) into a SyncResult.

        A hard (non-zero, non-partial) exit is logged at WARNING with the rc and
        the last 200 chars of stderr (A3), so a failed Phase 1 is diagnosable
        from the log alone rather than surfacing only as a bare "rc=20" upstream.
        A signal death (rc < 0) is phrased as such -- that is the guard
        watchdog's killpg, and the daemon logs the human-readable kill reason
        adjacent (daemon._sync_folder).
        """
        files_copied, bytes_copied = self._parse_stats(out)
        failed_paths = []  # type: List[str]
        if timed_out:
            status = "failure"
        elif rc == 0:
            status = "ok"
        elif rc in (23, 24):
            status = "partial"
            failed_paths = self._parse_failed_paths(err)
        else:
            # Any other exit code, or signal death (rc < 0).
            status = "failure"
        if status == "failure":
            tail = stderr_summary(err)
            if timed_out:
                self._log("warning",
                          "phase1 rsync timed out (killed); last stderr: %s", tail)
            elif rc is not None and rc < 0:
                self._log("warning",
                          "phase1 rsync terminated by signal %s; last stderr: %s",
                          -rc, tail)
            else:
                self._log("warning",
                          "phase1 rsync exited rc=%s; last stderr: %s", rc, tail)
        kind = _failure_kind(rc, err, timed_out) if status == "failure" else None
        return SyncResult(status, rc, files_copied, bytes_copied, failed_paths, kind)

    def _ensure_dir(self, path):
        # type: (str) -> Optional[str]
        """mkdir -p in a timed child process: an in-thread os.makedirs against a
        wedged SMB share blocks uninterruptibly, before any rsync exists for the
        watchdog to see. None on success, else a reason."""
        try:
            res = util.run([paths.MKDIR, "-p", path], 30)
        except OSError as exc:
            return "mkdir could not run: %s" % exc
        if res.timed_out:
            return "%s (destination not responding): %s" % (_DEST_TIMEOUT, path)
        if res.rc != 0:
            return "mkdir -p failed (rc=%s): %s" % (res.rc, (res.err or "").strip()[:160])
        return None

    def _dest_failure(self, problem):
        # type: (str) -> SyncResult
        """A destination that stopped answering is a transport failure (the
        daemon remounts); a layout problem (mkdir rc != 0) is not."""
        self._log("warning", "phase1: destination unusable: %s", problem)
        kind = "dest_timeout" if problem.startswith(_DEST_TIMEOUT) else "error"
        return SyncResult("failure", None, 0, 0, [], kind)

    # -- Phase 2 compute: dry-run deletion detection -----------------------

    def phase2_compute(self, src_root, dst_root, subtree=None, timeout=1800):
        # type: (object, object, Optional[str], Optional[float]) -> List[str]
        sub = _norm_subtree(subtree)
        src_root = str(src_root)
        dst_root = str(dst_root)
        if sub is None:
            eff_src = src_root
            eff_dst = dst_root
        else:
            eff_src = os.path.join(src_root, sub)
            eff_dst = os.path.join(dst_root, sub)

        # Dry-run (-n) makes --delete-during safe here: no writes occur. This
        # exact combination was verified to emit "*deleting <relpath>" lines.
        argv = [
            self.rsync_bin,
            "-rltD",
            "-n",
            "--itemize-changes",
            "--delete-during",
            "--exclude-from=" + self.excludes_file,
            eff_src + "/",
            eff_dst + "/",
        ]

        # Parse at the BYTE level: openrsync escapes non-ASCII (and control)
        # bytes in itemize output as "\#ooo" octal, so a replace-decoded string
        # would corrupt CJK filenames. We unescape, then decode with
        # surrogateescape so the relpath round-trips through os.* filesystem
        # calls unchanged even for bytes that are not valid UTF-8.
        rc, out, _err, timed_out = self._run_raw(argv, timeout)

        relpaths = []  # type: List[str]
        # FAIL-SAFE: a dry-run that timed out (SIGKILLed mid-write by the guard
        # watchdog) or exited abnormally may have emitted a TRUNCATED final
        # "*deleting " line -- a prefix of a real path -- which phase2_execute
        # would then rename the wrong (shorter-named) mirror entry for. Never
        # derive deletions from an incomplete computation: propagate nothing this
        # round. rc 24 (some source files vanished during the scan) is tolerated.
        if timed_out or rc not in (0, 24):
            self._log("warning",
                      "phase2 dry-run did not complete cleanly (rc=%s, timed_out=%s); "
                      "skipping deletion propagation this pass", rc, timed_out)
            return relpaths
        if not out:
            return relpaths
        lines = out.split(b"\n")
        # If output does not end in a newline the last element is a partial line
        # (a fragment of a filename); drop it rather than risk a wrong rename.
        if not out.endswith(b"\n"):
            lines = lines[:-1]
        for line in lines:
            if not line.startswith(_DELETING_PREFIX_B):
                continue
            # Do NOT strip other whitespace: filenames may begin/end with spaces.
            rel_bytes = line[len(_DELETING_PREFIX_B):].rstrip(b"\r")
            if rel_bytes == b"":
                continue
            rel = _unescape_rsync(rel_bytes).decode("utf-8", "surrogateescape")
            if sub is not None:
                rel = sub + "/" + rel
            relpaths.append(rel)
        return relpaths

    # -- Phase 2 execute: rename into trash --------------------------------

    def phase2_execute(self, src_root, dst_root, trash_folder_dir, relpaths):
        # type: (object, object, object, List[str]) -> Phase2Result
        src_root = str(src_root)
        dst_root = str(dst_root)
        trash_folder_dir = str(trash_folder_dir)

        moved = 0
        skipped_recreated = 0
        skipped_unsafe = 0
        errors = []  # type: List[str]
        moved_mirrors = []  # type: List[str]
        moved_relpaths = []  # type: List[str]

        # Deeper paths first so files move before their parent directories.
        ordered = sorted(
            relpaths,
            key=lambda r: r.rstrip("/").count(os.sep),
            reverse=True,
        )

        for rel in ordered:
            rel_clean = rel.rstrip("/")
            if rel_clean == "":
                continue
            mirror = os.path.join(dst_root, rel_clean)

            # SAFETY: the mirror path must resolve to inside dst_root.
            if not _is_within(mirror, dst_root):
                skipped_unsafe += 1
                self._log("warning", "phase2 skipped unsafe path outside dst: %s", rel)
                continue

            # RACE GUARD: the file may have been recreated locally after the
            # dry-run; if the source still has it, do not propagate a delete.
            # Nor when it is only cloud-only: on macOS <= 13 an evicted file,
            # or a whole evicted package above it, is a .X.icloud placeholder.
            if (os.path.lexists(os.path.join(src_root, rel_clean))
                    or placeholder_covers(src_root, rel_clean)):
                skipped_recreated += 1
                continue

            # Mirror already gone (e.g. removed by a deeper move, or by hand):
            # nothing to move, and the deletion is complete -- report it so
            # the caller drops the row. Kept, the row was planned as a deletion
            # (and counted against the circuit breaker) on every pass forever.
            # Only while the mirror root itself is there: a missing root may
            # be a mount problem, not a finished deletion.
            if not os.path.lexists(mirror):
                if os.path.isdir(dst_root):
                    moved_relpaths.append(rel_clean)
                continue

            dest = os.path.join(trash_folder_dir, rel_clean)
            if not _is_within(dest, trash_folder_dir):
                skipped_unsafe += 1
                self._log("warning", "phase2 skipped unsafe trash dest: %s", rel)
                continue

            try:
                if (
                    os.path.isdir(mirror)
                    and not os.path.islink(mirror)
                    and os.path.exists(dest)
                ):
                    # The dest was already populated by earlier (deeper) child
                    # moves, so this mirror dir is now redundant. Remove it if
                    # empty; a non-empty dir here is a genuine conflict (raises).
                    os.rmdir(mirror)
                else:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    os.rename(mirror, dest)  # same volume -> instant
                moved += 1
                moved_mirrors.append(mirror)
                moved_relpaths.append(rel_clean)
            except OSError as exc:
                errors.append("%s: %s" % (rel_clean, exc))
                continue

        self._prune_empty_dirs(moved_mirrors, dst_root)

        return Phase2Result(
            moved, skipped_recreated, skipped_unsafe, errors, moved_relpaths)

    def _prune_empty_dirs(self, moved_mirrors, dst_root):
        # type: (List[str], str) -> None
        """Best-effort bottom-up removal of dirs emptied by our moves.

        Only ancestors of moved entries are considered, and never dst_root
        itself, so unrelated (or source-present) directories are untouched.
        """
        real_dst = os.path.realpath(dst_root)
        for mirror in moved_mirrors:
            parent = os.path.dirname(mirror)
            while True:
                if not parent:
                    break
                real_parent = os.path.realpath(parent)
                if real_parent == real_dst:
                    break
                if not real_parent.startswith(real_dst + os.sep):
                    break
                try:
                    os.rmdir(parent)
                except OSError:
                    break  # not empty (or gone) -> stop climbing this branch
                parent = os.path.dirname(parent)

    # -- Capability probe --------------------------------------------------

    @staticmethod
    def capability_probe(tmp_dir, rsync_bin):
        # type: (object, str) -> Dict[str, bool]
        """Verify openrsync behavior fingerprints in a throwaway sandbox.

        This is the ONLY place a --delete* flag may co-exist with -b, and only
        against local temp dirs. The daemon runs this every start and fails
        closed (disables deletion propagation) if a fingerprint changes.
        """
        tmp_dir = str(tmp_dir)
        rsync_bin = str(rsync_bin)
        excludes = os.path.join(tmp_dir, "excludes.txt")
        with open(excludes, "w", encoding="utf-8") as handle:
            handle.write("")

        result = {
            "backup_moves_overwritten": False,
            "delete_suppressed_with_backup": False,
            "deleting_line_format_ok": False,
            "inplace_updates_ok": False,
        }

        # (A) delete_suppressed_with_backup: write-mode -b + --delete-during
        #     must NOT delete a dst-only file.
        a_src = os.path.join(tmp_dir, "a", "src")
        a_dst = os.path.join(tmp_dir, "a", "dst")
        a_bak = os.path.join(tmp_dir, "a", "backup")
        os.makedirs(a_src)
        os.makedirs(a_dst)
        _write(os.path.join(a_src, "keep.txt"), "x")
        extra = os.path.join(a_dst, "extra.txt")
        _write(extra, "dst-only")
        _run_probe(
            [
                rsync_bin, "-rltD", "-b", "--backup-dir=" + a_bak,
                "--modify-window=2", "--partial", "--delete-during",
                "--itemize-changes", "--exclude-from=" + excludes,
                a_src + "/", a_dst + "/",
            ]
        )
        result["delete_suppressed_with_backup"] = os.path.exists(extra)

        # (B) backup_moves_overwritten: an overwritten file's old version must
        #     land under the backup dir.
        b_src = os.path.join(tmp_dir, "b", "src")
        b_dst = os.path.join(tmp_dir, "b", "dst")
        b_bak = os.path.join(tmp_dir, "b", "backup")
        os.makedirs(b_src)
        os.makedirs(b_dst)
        _write(os.path.join(b_src, "file.txt"), "original")
        _run_probe(
            [
                rsync_bin, "-rltD", "-b", "--backup-dir=" + b_bak,
                "--modify-window=2", "--partial", "--itemize-changes",
                "--exclude-from=" + excludes, b_src + "/", b_dst + "/",
            ]
        )
        _write(os.path.join(b_src, "file.txt"), "changed-content")
        _run_probe(
            [
                rsync_bin, "-rltD", "-b", "--backup-dir=" + b_bak,
                "--modify-window=2", "--partial", "--itemize-changes",
                "--exclude-from=" + excludes, b_src + "/", b_dst + "/",
            ]
        )
        result["backup_moves_overwritten"] = os.path.exists(
            os.path.join(b_bak, "file.txt")
        )

        # (C) deleting_line_format_ok: dry-run must emit a "*deleting " line
        #     naming the dst-only file.
        c_src = os.path.join(tmp_dir, "c", "src")
        c_dst = os.path.join(tmp_dir, "c", "dst")
        os.makedirs(c_src)
        os.makedirs(c_dst)
        _write(os.path.join(c_dst, "gone.txt"), "dst-only")
        out = _run_probe(
            [
                rsync_bin, "-rltD", "-n", "--itemize-changes", "--delete-during",
                "--exclude-from=" + excludes, c_src + "/", c_dst + "/",
            ]
        )
        for line in out.splitlines():
            if line.startswith("*deleting ") and "gone.txt" in line:
                result["deleting_line_format_ok"] = True
                break

        # (D) inplace_updates_ok: --inplace (no -b anywhere near it) must
        #     create a new file and update an existing one IN PLACE (same
        #     inode, correct content). Guards the PLAN-15 fast path against a
        #     future openrsync changing --inplace semantics; on failure the
        #     daemon falls back to the -b path for every batch (fail-closed).
        d_src = os.path.join(tmp_dir, "d", "src")
        d_dst = os.path.join(tmp_dir, "d", "dst")
        os.makedirs(d_src)
        os.makedirs(d_dst)
        d_file = os.path.join(d_src, "file.txt")
        d_mirror = os.path.join(d_dst, "file.txt")
        _write(d_file, "first")
        _run_probe(
            [
                rsync_bin, "-rltD", "--inplace", "--modify-window=2",
                "--partial", "--itemize-changes", d_src + "/", d_dst + "/",
            ]
        )
        try:
            inode_before = os.stat(d_mirror).st_ino
        except OSError:
            inode_before = None
        _write(d_file, "second-longer-content")
        _run_probe(
            [
                rsync_bin, "-rltD", "--inplace", "--modify-window=2",
                "--partial", "--itemize-changes", d_src + "/", d_dst + "/",
            ]
        )
        try:
            with open(d_mirror, "r", encoding="utf-8") as handle:
                content_ok = handle.read() == "second-longer-content"
            inode_after = os.stat(d_mirror).st_ino
        except OSError:
            content_ok = False
            inode_after = None
        result["inplace_updates_ok"] = bool(
            content_ok
            and inode_before is not None
            and inode_before == inode_after
        )

        return result

    # -- Internals ---------------------------------------------------------

    def _run(self, argv, timeout, on_progress=None, stdin_bytes=None,
             on_alive=None):
        # type: (List[str], Optional[float], Optional[object], Optional[bytes], Optional[object]) -> tuple
        """Run rsync and return decoded (rc, out_str, err_str, timed_out).

        If on_progress is given it is invoked once per itemize transfer line as
        on_progress(files_done_so_far, current_relpath): live (per line) on the
        streaming path, and after the fact (per parsed line) on the timeout
        path. on_alive() is called (at most once a second) whenever output
        arrives on the streaming path -- including --progress redraws while one
        large file transfers. Callback exceptions never break the run.
        stdin_bytes, when given, is written to the child's stdin (the
        --files-from=- batch list). timed_out is also True when the run was
        abandoned as wedged (see _stream).
        """
        rc, out_b, err_b, timed_out = self._run_raw(
            argv, timeout, on_progress=on_progress, stdin_bytes=stdin_bytes,
            on_alive=on_alive)
        out = _decode(out_b)
        err = _decode(err_b)
        if out:
            self._log("debug", "rsync stdout:\n%s", out.rstrip("\n"))
        if err:
            self._log("debug", "rsync stderr:\n%s", err.rstrip("\n"))
        return rc, out, err, timed_out

    def _run_raw(self, argv, timeout, on_progress=None, stdin_bytes=None,
                 on_alive=None):
        # type: (List[str], Optional[float], Optional[object], Optional[bytes], Optional[object]) -> tuple
        """Spawn rsync in its own session; return (rc, out_bytes, err_bytes, timed_out).

        Uses binary pipes so callers that must recover exact filenames (Phase 2)
        can unescape openrsync's byte-level output themselves. Registers the
        Popen in proc_registry before waiting so the daemon watchdog can kill the
        process group on a guard failure; discards after. stdin_bytes, when given,
        opens a stdin pipe and feeds the child that payload (batch --files-from=-).
        """
        self._log("debug", "rsync exec: %s", " ".join(argv))
        proc = subprocess.Popen(
            argv,
            stdin=(subprocess.PIPE if stdin_bytes is not None else None),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if self.proc_registry is not None:
            self.proc_registry.add(proc)

        timed_out = False
        try:
            if timeout is not None:
                try:
                    out_b, err_b = proc.communicate(
                        input=stdin_bytes, timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    self._kill_group(proc)
                    try:
                        out_b, err_b = proc.communicate(timeout=_REAP_GRACE)
                    except subprocess.TimeoutExpired:
                        # Still holding the pipes after SIGKILL: uninterruptible
                        # I/O. Never block the caller on it.
                        util._close_pipes(proc)
                        util._abandon(proc, argv)
                        out_b, err_b = b"", b""
                # No live stream on the timeout path: replay progress after.
                self._emit_progress(out_b or b"", on_progress)
            else:
                out_b, err_b, wedged = self._stream(
                    proc, on_progress, stdin_bytes, on_alive)
                timed_out = wedged
        finally:
            if self.proc_registry is not None:
                self.proc_registry.discard(proc)

        return proc.returncode, out_b or b"", err_b or b"", timed_out

    def _stream(self, proc, on_progress=None, stdin_bytes=None, on_alive=None):
        # type: (subprocess.Popen, Optional[object], Optional[bytes], Optional[object]) -> tuple
        """Drain stdout (logging + live progress) while draining stderr.

        stdout is read in raw chunks and split on "\n"; with --progress, rsync
        redraws a progress frame with "\r" while one large file transfers, and
        that redraw is the only sign of life for the whole file. A line-based
        reader saw nothing until the file finished, so the watchdog's stall kill
        fired on any single file taking longer than 20 minutes -- and every retry
        restarted it from zero. Now any arriving chunk counts as liveness
        (on_alive, rate-limited); only "\n"-terminated lines are parsed, and of a
        redraw sequence only its last frame is kept.

        When stdin_bytes is given, a writer thread feeds and closes the child's
        stdin concurrently, so a batch list larger than the pipe buffer cannot
        deadlock against our stdout reads.

        Returns (out_bytes, err_bytes, wedged). wedged is True when the client
        exited but a child it forked still held stderr after the grace window
        and a SIGKILL of the whole group -- a transfer stuck in uninterruptible
        SMB I/O. Waiting for it used to hang the sync thread indefinitely (the
        client's pid was already reaped, so the watchdog could no longer find
        the group to kill, and saw "no live rsync" to call a stall).
        """
        err_chunks = []  # type: List[bytes]

        if stdin_bytes is not None and proc.stdin is not None:
            def _feed_stdin(pipe, data):
                try:
                    pipe.write(data)
                    pipe.flush()
                except (OSError, ValueError):
                    pass
                finally:
                    try:
                        pipe.close()
                    except (OSError, ValueError):
                        pass
            stdin_thread = threading.Thread(
                target=_feed_stdin, args=(proc.stdin, stdin_bytes))
            stdin_thread.daemon = True
            stdin_thread.start()

        def _drain_err(pipe, sink):
            try:
                for line in iter(pipe.readline, b""):
                    sink.append(line)
            finally:
                pipe.close()

        err_thread = threading.Thread(
            target=_drain_err, args=(proc.stderr, err_chunks)
        )
        err_thread.daemon = True
        err_thread.start()

        out_lines = []  # type: List[bytes]
        done = 0
        last_alive = 0.0
        if proc.stdout is not None:
            fd = proc.stdout.fileno()
            buf = b""
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                now = time.monotonic()
                if on_alive is not None and now - last_alive >= _ALIVE_MIN_INTERVAL:
                    last_alive = now
                    self._safe_call(on_alive)
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = buf[:nl + 1]
                    buf = buf[nl + 1:]
                    if b"\r" in line:
                        line = line.rsplit(b"\r", 1)[-1]   # last redraw frame
                    out_lines.append(line)
                    self._log("debug", "rsync| %s", _decode(line).rstrip("\n"))
                    if on_progress is not None:
                        rel = _transfer_relpath_bytes(line)
                        if rel is not None:
                            done += 1
                            self._safe_progress(on_progress, done, rel)
                # Completed redraw frames need no keeping (bounded memory).
                cr = buf.rfind(b"\r")
                if cr >= 0:
                    buf = buf[cr + 1:]
            if buf:
                out_lines.append(buf)
            proc.stdout.close()

        wedged = False
        try:
            proc.wait(timeout=_REAP_GRACE)
        except subprocess.TimeoutExpired:
            _killpg(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=_REAP_GRACE)
            except subprocess.TimeoutExpired:
                util._abandon(proc, [self.rsync_bin])
                wedged = True
        err_thread.join(_DRAIN_GRACE)
        if err_thread.is_alive():
            self._log("warning",
                      "rsync client exited but its children (group %d) still "
                      "hold stderr after %.0fs; killing the group", proc.pid,
                      _DRAIN_GRACE)
            _killpg(proc.pid, signal.SIGKILL)
            err_thread.join(_REAP_GRACE)
            if err_thread.is_alive():
                self._log("warning",
                          "rsync children (group %d) are stuck in uninterruptible "
                          "I/O; giving up on them so the sync can recover",
                          proc.pid)
                wedged = True
        return b"".join(out_lines), b"".join(err_chunks), wedged

    def _emit_progress(self, out_b, on_progress):
        # type: (bytes, Optional[object]) -> None
        """Replay progress from a completed run's captured stdout bytes."""
        if on_progress is None:
            return
        done = 0
        for line in out_b.split(b"\n"):
            rel = _transfer_relpath_bytes(line)
            if rel is None:
                continue
            done += 1
            self._safe_progress(on_progress, done, rel)

    def _safe_call(self, fn):
        # type: (object) -> None
        try:
            fn()
        except Exception as exc:  # observability only; must never break the sync
            self._log("debug", "liveness callback raised: %s", exc)

    def _safe_progress(self, on_progress, done, rel):
        # type: (object, int, str) -> None
        """Invoke a progress callback, swallowing (and DEBUG-logging) any error."""
        try:
            on_progress(done, rel)
        except Exception as exc:  # observability only; must never break the sync
            self._log("debug", "on_progress callback raised: %s", exc)

    def _kill_group(self, proc):
        # type: (subprocess.Popen) -> None
        _killpg(proc.pid, signal.SIGKILL)

    def _parse_stats(self, out):
        # type: (str) -> tuple
        """Parse files/bytes transferred tolerantly; never raise on parse failure."""
        files = 0
        byts = 0
        try:
            match = _FILES_RE.search(out)
            if match:
                files = _to_int(match.group(1))
            match = _BYTES_RE.search(out)
            if match:
                byts = _to_int(match.group(1))
            if files == 0:
                # Fallback: count itemize lines that look like transfers.
                for line in out.splitlines():
                    if line[:1] in (">", "<"):
                        files += 1
        except Exception:  # observability only; must not fail the sync
            self._log("debug", "stats parse failed; counts left at 0")
        return files, byts

    def _parse_failed_paths(self, err):
        # type: (str) -> List[str]
        """Best-effort extraction of per-file failure paths from stderr.

        Display only (commit decisions come from the NAS itself). Handles both
        implementations: openrsync "rsync(pid): error: <path>: <op> ..." and
        rsync 2.6.9 'rsync: <op> "<path>" failed: <why> (n)'. The 2.6.9 summary
        line ("rsync error: some files could not be transferred (code 23) at
        main.c(977) [sender=2.6.9]") names no file and used to be shown to the
        user AS a failed path.
        """
        found = []  # type: List[str]
        if not err:
            return found
        for line in err.splitlines():
            s = line.strip()
            if not s:
                continue
            low = s.lower()
            if low.startswith("rsync error:") or low.startswith("rsync warning:"):
                continue
            quoted = _QUOTED_RE.search(s)
            if quoted and low.startswith("rsync:"):
                found.append(quoted.group(1))
                continue
            if "error:" not in s:
                continue
            after = s.split("error:", 1)[1].strip()
            # openrsync form: "<path>: <op> (errno) in <cwd>: <reason>".
            found.append(after.split(": ", 1)[0].strip() if ": " in after else after)
        return found

    def _log(self, level, msg, *args):
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.debug)(msg, *args)


def _decode(raw):
    # type: (Optional[bytes]) -> str
    """Decode rsync output bytes tolerantly (openrsync may emit raw non-UTF-8)."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", "replace")


def _write(path, text):
    # type: (str, str) -> None
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _run_probe(argv):
    # type: (List[str]) -> str
    """Run a probe rsync against local temp dirs; return combined stdout."""
    proc = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    return _decode(proc.stdout)
