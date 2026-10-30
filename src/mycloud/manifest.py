"""Local sync-state manifest database (PLAN section 14, M1).

The NAS mirror is a SINGLE-WRITER store: only this daemon ever writes it. That
lets a local SQLite manifest fully replace remote stat storms -- every pass is a
cheap local os.walk/lstat diff against the table, with ZERO remote I/O for change
computation. This is the v2 architecture that ends v1's "re-walk 316k files and
remote-stat the whole mirror every 15 minutes" cost.

Schema (PLAN 14.1)::

    files(folder TEXT, relpath TEXT, size INTEGER, mtime_ns INTEGER,
          inode INTEGER, synced_at TEXT, pending INTEGER,
          PRIMARY KEY(folder, relpath))
    meta(key TEXT PRIMARY KEY, value TEXT)

Quick-check equality is size + mtime_ns (the same guarantee level as rsync's -t
quick check; SMB/exFAT's 2s mtime granularity is absorbed by --modify-window=2
at transfer time, and drift is caught by the weekly deep verify).

Threading: opened with check_same_thread=False and every operation is serialized
under a single instance lock, so the daemon's main-loop thread and a recovery
call (which may run on the same or a helper thread) never corrupt WAL state. The
progress-heartbeat thread never touches the manifest.

Python 3.9 stdlib only (sqlite3 IS stdlib).
"""

import os
import re
import sqlite3
import threading
from collections import namedtuple
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

# A computed pass plan for one folder. All list elements are folder-relative
# POSIX-ish relpaths (str, surrogateescape-decoded so non-UTF-8 names round-trip
# back through os.fsencode at transfer time). renames is a list of (old, new).
# new_set is the set of relpaths that APPEARED this pass (no manifest row, so
# no mirror entry on the NAS): the daemon routes those through the --inplace
# fast path, which is only safe when there is no prior version to preserve
# (PLAN section 15 P0; --inplace + -b is destructive, verified 2026-07-21).
# walk_errors counts the OSErrors the local walk swallowed: >0 means the walk
# may be INCOMPLETE, so deletions/renames (both derived from "vanished") are not
# trustworthy this pass (PLAN 16.2 T-B1).
# forget lists rows to drop from the manifest only (no NAS action): rows for
# ".X.icloud" placeholders that <= 1.3.0 backed up as ordinary files. deferred
# lists vanished rows whose deletion was held back because an evicted file may
# have moved there (see _defer_for_placeholders).
Plan = namedtuple(
    "Plan", "uploads deletions renames dataless totals new_set walk_errors "
            "forget deferred")
Plan.__new__.__defaults__ = ((), ())

_SCHEMA_VERSION = "1"

# macOS <= 13 (before iCloud Drive moved to FileProvider in Sonoma) replaces an
# evicted iCloud file X with a hidden placeholder ".X.icloud" (a < 200-byte
# plist) instead of an SF_DATALESS file. Seen as plain files, X looked DELETED
# -- its NAS copy went to the trash and was pruned 30 days later -- and the
# useless placeholder got backed up. The size cap keeps a real file that just
# happens to be named like one from being taken for a placeholder.
_ICLOUD_STUB_RE = re.compile(r"^\.(.+)\.icloud$", re.DOTALL)
_ICLOUD_STUB_MAX = 4096
# Placeholder plists parsed per Manifest instance, keyed by the placeholder's
# own identity; cleared wholesale past this many entries.
_STUB_CACHE_MAX = 200000


def placeholder_path(path):
    # type: (str) -> str
    """Where macOS <= 13 puts the placeholder of an evicted iCloud item."""
    head, tail = os.path.split(os.fspath(path).rstrip(os.sep))
    return os.path.join(head, "." + tail + ".icloud")


def stub_target(name):
    # type: (str) -> Optional[str]
    """The item a placeholder file name stands for, or None."""
    match = _ICLOUD_STUB_RE.match(name)
    if match is None or match.group(1) in (".", ".."):
        return None
    return match.group(1)


def _is_stub_stat(st):
    # type: (object) -> bool
    import stat as _stat
    return _stat.S_ISREG(st.st_mode) and st.st_size <= _ICLOUD_STUB_MAX


def has_placeholder(path):
    # type: (str) -> bool
    """True iff a macOS <= 13 placeholder stands in for path (cloud-only)."""
    try:
        return _is_stub_stat(os.lstat(placeholder_path(path)))
    except OSError:
        return False


def placeholder_root(src_root, rel):
    # type: (str, str) -> Optional[str]
    """The relpath whose placeholder makes rel cloud-only -- rel itself, or a
    directory above it -- or None. An evicted package (Notes.rtfd, a
    .key/.pages bundle) becomes ONE placeholder for the whole directory, so
    its files have no placeholder of their own."""
    rel = os.path.normpath(rel)
    while rel not in ("", ".", os.sep):
        if has_placeholder(os.path.join(src_root, rel)):
            return rel
        rel = os.path.dirname(rel)
    return None


def placeholder_covers(src_root, rel):
    # type: (str, str) -> bool
    """True iff rel, or a directory above it, is cloud-only."""
    return placeholder_root(src_root, rel) is not None


def stub_logical_size(path):
    # type: (str) -> Optional[int]
    """The evicted item's real size (NSURLFileSizeKey) from its placeholder.

    A placeholder is a small local binary plist, never itself cloud-only, so
    reading it cannot start a download. None when unreadable or absent."""
    import plistlib
    try:
        with open(path, "rb") as handle:
            data = plistlib.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("NSURLFileSizeKey")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


# Finder's names for a second item with a taken name: "Keep Both" on a move
# ("report 2.pdf") and Duplicate ("report copy.pdf", 「报告 副本.pdf」).
_FINDER_DUP_RE = re.compile(r"^(.+?) (?:copy(?: \d+)?|副本(?: \d+)?|拷贝(?: \d+)?|\d+)$",
                            re.DOTALL)


def _names_before(name):
    # type: (str) -> Tuple[str, ...]
    """name, plus the name it had before Finder renamed it, if it looks so."""
    stem, ext = os.path.splitext(name)
    match = _FINDER_DUP_RE.match(stem)
    if match is None:
        return (name,)
    return (name, match.group(1) + ext)


def _real_size(rel, size, stubs):
    # type: (str, int, _StubScan) -> int
    """Byte count to plan for a dataless entry: a placeholder's own size is a
    few hundred bytes, the file it stands for may be gigabytes."""
    if rel in stubs.targets:
        return int(stubs.targets[rel] or 0)
    return size


class _StubScan(object):
    """Placeholders one plan walk consumed (see Manifest._walk_scope)."""

    __slots__ = ("seen", "targets")

    def __init__(self):
        # type: () -> None
        self.seen = set()  # type: set
        self.targets = {}  # type: Dict[str, Optional[int]]


def _utc_now_iso():
    # type: () -> str
    return datetime.now(timezone.utc).isoformat()


class Manifest(object):
    """SQLite-backed per-folder file manifest with plan/commit operations."""

    def __init__(self, db_path):
        # type: (object) -> None
        self.db_path = os.fspath(db_path)
        self._lock = threading.RLock()
        self._stub_cache = {}  # type: Dict[Tuple, Optional[int]]
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    # -- lifecycle ---------------------------------------------------------

    def _init_db(self):
        # type: () -> None
        """Create the schema (idempotent) and enable WAL."""
        with self._lock:
            cur = self._conn.cursor()
            # WAL gives a durable single-writer/many-reader journal; NORMAL sync
            # is safe under WAL and far faster on the many small commits a batch
            # pipeline makes.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS files ("
                "folder TEXT NOT NULL, relpath TEXT NOT NULL, size INTEGER, "
                "mtime_ns INTEGER, inode INTEGER, synced_at TEXT, "
                "pending INTEGER DEFAULT 0, "
                "PRIMARY KEY(folder, relpath))"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS meta ("
                "key TEXT PRIMARY KEY, value TEXT)"
            )
            # Rename detection queries by (folder, inode); index it.
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_files_inode "
                "ON files(folder, inode)"
            )
            self._conn.commit()
            self.set_meta("schema_version", _SCHEMA_VERSION)

    def close(self):
        # type: () -> None
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # -- meta --------------------------------------------------------------

    def get_meta(self, key, default=None):
        # type: (str, Optional[str]) -> Optional[str]
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if row is None:
                return default
            return row["value"]

    def set_meta(self, key, value):
        # type: (str, str) -> None
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))
            self._conn.commit()

    # -- planning ----------------------------------------------------------

    def plan_folder(self, folder_name, src_root, exclude_patterns,
                    dataless_check, subtrees=None):
        # type: (str, object, List[str], Callable, Optional[List[str]]) -> Plan
        """Diff a local walk of src_root against the manifest; return a Plan.

        One local scandir/lstat pass (NEVER open/read: lstat can't materialize an
        iCloud dataless file, verified 2026-07-20) diffed against the table:

          uploads   new + changed non-dataless files (relpaths)
          deletions manifest rows whose local path vanished (minus renames)
          renames   (old, new) pairs matched by (inode, size, mtime_ns), size>0
          dataless  new + changed files that are iCloud-dataless (kept OUT of
                    uploads; the caller applies the icloud_policy to them)
          totals    dict of counts/bytes for progress + surfacing
          walk_errors count of OSErrors the walk swallowed (see _walk_scope)

        Change quick-check is size + mtime_ns equality (rsync -t level). A row
        with pending=1 (set by deep verify) re-uploads even if size/mtime match.

        subtrees narrows BOTH the walk AND the manifest rows considered, so a
        scoped pass over one dirty subtree never mis-reads rows outside it as
        deletions. None or ["."] means the whole folder.
        """
        src_root = os.fspath(src_root)
        scopes = _normalize_subtrees(subtrees)
        patterns = [p.rstrip("/") for p in (exclude_patterns or [])]

        # Local walk: relpath -> (size, mtime_ns, inode, is_dataless).
        local = {}  # type: Dict[str, Tuple[int, int, int, bool]]
        stubs = _StubScan()
        walk_errors = 0
        for scope in scopes:
            walk_errors += self._walk_scope(
                src_root, scope, patterns, dataless_check, local, stubs)

        # Manifest rows in scope: relpath -> row.
        rows = self._rows_in_scope(folder_name, scopes)

        # Rows for placeholder files themselves (<= 1.3.0 uploaded ".X.icloud"
        # as an ordinary file). The walk no longer records them, so they read
        # as vanished on every pass -- yet the placeholder is still on disk, so
        # phase 2 skipped them and the row was never removed: a phantom
        # deletion every pass, enough of them a permanent hold.
        forget = sorted(rel for rel in rows if rel in stubs.seen)
        for rel in forget:
            del rows[rel]

        local_set = set(local)
        row_set = set(rows)
        vanished = row_set - local_set        # deletion / rename-old candidates
        appeared = local_set - row_set        # upload / rename-new candidates
        common = local_set & row_set

        # An evicted package: its rows are files UNDER the placeholder's target.
        # They are cloud-only, not deleted, and the package is already backed
        # up -- so the target is not also planned as a one-file download.
        covered_targets = set()  # type: set
        if stubs.targets:
            pending_targets = set()  # type: set
            for rel in list(vanished):
                parent = os.path.dirname(rel)
                while parent:
                    if parent in stubs.targets:
                        vanished.discard(rel)
                        covered_targets.add(parent)
                        if rows[rel]["pending"]:
                            pending_targets.add(parent)
                        break
                    parent = os.path.dirname(parent)
            # A package with a file deep verify found missing on the NAS is
            # NOT backed up: it stays a cloud-only arrival (reported; fetched
            # under the download policy).
            for target in covered_targets - pending_targets:
                if target in appeared:
                    appeared.discard(target)
                    local_set.discard(target)

        uploads = []    # type: List[str]
        dataless = []   # type: List[str]
        upload_bytes = 0
        dataless_bytes = 0
        unchanged = 0

        # Changed-in-place files (same relpath, differing size/mtime or pending).
        for rel in common:
            size, mtime_ns, _inode, is_dl = local[rel]
            row = rows[rel]
            if (not row["pending"]
                    and row["size"] == size and row["mtime_ns"] == mtime_ns):
                unchanged += 1
                continue
            if (rel in stubs.targets and not row["pending"]
                    and stubs.targets[rel] is not None
                    and stubs.targets[rel] == row["size"]):
                # Evicted after it was backed up: the placeholder records the
                # same size the NAS copy has. Cloud-only, but not "not backed up".
                unchanged += 1
                continue
            if is_dl:
                dataless.append(rel)
                dataless_bytes += _real_size(rel, size, stubs)
            else:
                uploads.append(rel)
                upload_bytes += size

        # Rename detection between vanished (old) and appeared (new).
        renames, consumed_old, consumed_new = self._detect_renames(
            rows, local, vanished, appeared)
        if stubs.targets:
            for detect in (self._detect_stub_renames, self._detect_package_moves):
                more, more_old, more_new = detect(
                    src_root, rows, stubs.targets, vanished - consumed_old,
                    appeared - consumed_new - covered_targets)
                if more:
                    renames = sorted(renames + more)
                    consumed_old |= more_old
                    consumed_new |= more_new

        deletions = sorted(vanished - consumed_old)
        deferred = []  # type: List[str]
        if stubs.targets and deletions:
            # Arrivals that may explain a vanished row: unpaired cloud-only
            # targets, plus one dropped onto a path that already had a row
            # (Finder "Replace") -- its placeholder size differs from the row.
            replaced = set(
                rel for rel in common
                if rel in stubs.targets and stubs.targets[rel] is not None
                and rows[rel]["size"] != stubs.targets[rel])
            deletions, deferred = self._defer_for_placeholders(
                src_root, rows, stubs.targets, deletions,
                (appeared - consumed_new) | replaced)

        # A scoped pass sees only the placeholders inside its scope, so it
        # cannot tell a deletion from an evicted file moved elsewhere: in a
        # folder that has placeholders, deletions wait for a whole pass.
        flag = "placeholders:" + folder_name
        waiting = 0
        if scopes == ["."]:
            want = "1" if stubs.seen else "0"
            if self.get_meta(flag) != want:
                self.set_meta(flag, want)
        else:
            if stubs.seen and self.get_meta(flag) != "1":
                self.set_meta(flag, "1")
            if deletions and self.get_meta(flag) == "1":
                waiting = len(deletions)
                deletions = []

        new_set = set()  # type: set
        for rel in appeared:
            if rel in consumed_new:
                continue
            new_set.add(rel)
            size, _m, _i, is_dl = local[rel]
            if is_dl:
                dataless.append(rel)
                dataless_bytes += _real_size(rel, size, stubs)
            else:
                uploads.append(rel)
                upload_bytes += size

        uploads.sort()
        dataless.sort()
        totals = {
            "local_files": len(local),
            "upload_files": len(uploads),
            "upload_bytes": upload_bytes,
            "dataless_files": len(dataless),
            "dataless_bytes": dataless_bytes,
            "deletion_files": len(deletions),
            "rename_files": len(renames),
            "unchanged_files": unchanged,
            "deferred_files": len(deferred),
            "deletions_waiting_whole_pass": waiting,
        }
        return Plan(uploads, deletions, renames, dataless, totals, new_set,
                    walk_errors, forget, deferred)

    def _walk_scope(self, src_root, scope, patterns, dataless_check, out,
                    stubs=None):
        # type: (str, str, List[str], Callable, Dict, Optional[_StubScan]) -> int
        """scandir-walk one scope under src_root, filling out[relpath] = tuple.

        Explicit stack (no recursion). Excluded dirs are pruned wholesale. A file
        whose basename matches an exclude is skipped. lstat only -- never open.

        Returns the number of OSErrors swallowed. Swallowing stays deliberate --
        one unreadable directory must not abort a whole pass -- but it is NOT
        free: everything under a failed scandir is simply absent from `out`, and
        the caller reads absence as "vanished", i.e. a DELETION (and, from the
        same vanished set, a rename). Real incident 2026-07-30: an iCloud
        fileproviderd hiccup failed one scandir and put 1000 live files into a
        deletion plan. The count rides out in Plan.walk_errors so the daemon can
        suppress deletion/rename propagation for that pass (PLAN 16.2 T-B1).

        stubs (optional) collects the macOS <= 13 placeholders the walk
        consumed: their own relpaths (seen) and, for each cloud-only target
        recorded in out, the real size from the placeholder (targets).
        """
        if stubs is None:
            stubs = _StubScan()
        errors = 0
        base = src_root if scope == "." else os.path.join(src_root, scope)
        try:
            st = os.lstat(base)
        except OSError:
            return 1
        # A single-file scope (rare) records just that file.
        if not _stat_is_dir(st):
            rel = "." if scope == "." else scope
            self._record_file(src_root, base, st, dataless_check, out)
            return errors

        stack = [base]
        while stack:
            current = stack.pop()
            found = []  # type: List[Tuple[str, object, str]]
            try:
                with os.scandir(current) as scan:
                    for entry in scan:
                        name = entry.name
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            errors += 1
                            continue
                        if not is_dir:
                            target = stub_target(name)
                            if target is not None:
                                try:
                                    sst = entry.stat(follow_symlinks=False)
                                except OSError:
                                    errors += 1
                                    continue
                                if _is_stub_stat(sst):
                                    found.append((target, sst, entry.path))
                                    continue
                        if _excluded(name, patterns):
                            continue
                        if is_dir:
                            stack.append(entry.path)
                            continue
                        try:
                            est = entry.stat(follow_symlinks=False)
                        except OSError:
                            errors += 1
                            continue
                        self._record_file(
                            src_root, entry.path, est, dataless_check, out)
            except OSError:
                errors += 1
                continue
            for target, sst, stub_path in found:
                stubs.seen.add(os.path.relpath(stub_path, src_root))
                if _excluded(target, patterns):
                    continue
                target_path = os.path.join(current, target)
                if os.path.lexists(target_path):
                    # The real item wins when both exist: a file mid-download,
                    # or a package directory whose files the walk records.
                    continue
                rel = os.path.relpath(target_path, src_root)
                if rel in out:
                    continue
                # Cloud-only: planned as dataless (skipped or downloaded per
                # icloud_policy), never as vanished. inode 0 keeps it out of
                # inode rename matching (_detect_stub_renames pairs it by name
                # and the real size instead).
                out[rel] = (int(sst.st_size), int(sst.st_mtime_ns), 0, True)
                stubs.targets[rel] = self._stub_size(stub_path, sst)
        return errors

    def _stub_size(self, stub_path, sst):
        # type: (str, object) -> Optional[int]
        """stub_logical_size, memoized on the placeholder's identity: a large
        evicted library would otherwise re-read every placeholder each pass."""
        key = (stub_path, int(sst.st_ino), int(sst.st_size), int(sst.st_mtime_ns))
        if key in self._stub_cache:
            return self._stub_cache[key]
        size = stub_logical_size(stub_path)
        if len(self._stub_cache) >= _STUB_CACHE_MAX:
            self._stub_cache.clear()
        self._stub_cache[key] = size
        return size

    @staticmethod
    def _detect_stub_renames(src_root, rows, targets, vanished, appeared):
        # type: (str, Dict, Dict[str, Optional[int]], set, set) -> Tuple[List, set, set]
        """Pair a vanished row with an appeared cloud-only target: an evicted
        file moved in Finder. Its placeholder has a new inode and its own
        size/mtime, so inode matching cannot see the move, and the old path's
        NAS copy -- the only backup -- went to the trash. The key is
        (basename, real size from the placeholder); unique 1:1 matches only.
        A pending row (deep verify found its NAS copy missing) is never paired:
        there is nothing on the NAS to move, and the arrival must be fetched."""
        old_index = {}  # type: Dict[Tuple[str, int], List[str]]
        for rel in vanished:
            size = rows[rel]["size"]
            if size is None or size <= 0 or rows[rel]["pending"]:
                continue
            old_index.setdefault((os.path.basename(rel), int(size)), []).append(rel)
        new_index = {}  # type: Dict[Tuple[str, int], List[str]]
        for rel in appeared:
            size = targets.get(rel)
            if rel not in targets or size is None or size <= 0:
                continue
            new_index.setdefault((os.path.basename(rel), int(size)), []).append(rel)
        pairs = []  # type: List[Tuple[str, str]]
        consumed_old = set()  # type: set
        consumed_new = set()  # type: set
        for key, olds in old_index.items():
            news = new_index.get(key)
            if not news or len(olds) != 1 or len(news) != 1:
                continue
            pairs.append((olds[0], news[0]))
            consumed_old.add(olds[0])
            consumed_new.add(news[0])
        return sorted(pairs), consumed_old, consumed_new

    @staticmethod
    def _detect_package_moves(src_root, rows, targets, vanished, appeared):
        # type: (str, Dict, Dict[str, Optional[int]], set, set) -> Tuple[List, set, set]
        """Pair the files of a vanished package directory with an appeared
        cloud-only target of the same name: an evicted package (one
        placeholder for the whole directory) moved, or a folder above it
        renamed. Its rows are the package's files, whose names never match
        the package's own, so file pairing cannot see it. Every row under the
        old directory maps to the same place under the new one; unique 1:1
        directory matches only."""
        by_name = {}  # type: Dict[str, List[str]]
        for rel in appeared:
            if rel in targets:
                by_name.setdefault(os.path.basename(rel), []).append(rel)
        if not by_name:
            return [], set(), set()
        groups = {}  # type: Dict[str, List[str]]
        for rel in vanished:
            if rows[rel]["pending"]:
                continue           # nothing on the NAS to move (see above)
            parent = os.path.dirname(rel)
            while parent:
                if (os.path.basename(parent) in by_name
                        and not os.path.lexists(os.path.join(src_root, parent))):
                    groups.setdefault(parent, []).append(rel)
                    break
                parent = os.path.dirname(parent)
        olds_by_name = {}  # type: Dict[str, List[str]]
        for old_dir in groups:
            olds_by_name.setdefault(os.path.basename(old_dir), []).append(old_dir)
        pairs = []  # type: List[Tuple[str, str]]
        consumed_old = set()  # type: set
        consumed_new = set()  # type: set
        for base, old_dirs in olds_by_name.items():
            news = by_name.get(base, [])
            if len(old_dirs) != 1 or len(news) != 1:
                continue
            old_dir, new_dir = old_dirs[0], news[0]
            for rel in groups[old_dir]:
                pairs.append((rel, new_dir + rel[len(old_dir):]))
                consumed_old.add(rel)
            consumed_new.add(new_dir)
        return sorted(pairs), consumed_old, consumed_new

    @staticmethod
    def _defer_for_placeholders(src_root, rows, targets, deletions, arrivals):
        # type: (str, Dict, Dict[str, Optional[int]], List[str], set) -> Tuple[List[str], List[str]]
        """Hold back deletions that an unpaired cloud-only arrival may explain.

        A vanished row is kept (no NAS action, row stays) when an arrival
        - has its name and its size (or an unreadable size): a move the 1:1
          pairing could not prove (ambiguous, or the NAS copy was missing);
        - sits in its directory with exactly its size: a rename in place;
        - is a package whose vanished directory above the row has its name, or
          sits beside it with the same extension (a package renamed in place).
        Matching on size alone across the folder held unrelated deletions
        back for as long as any never-downloaded arrival existed. Once the
        arrival is downloaded and backed up, or goes away, the deletion
        proceeds normally."""
        by_name = {}  # type: Dict[str, List[Optional[int]]]
        by_dir = {}  # type: Dict[str, set]
        pkg_names = set()  # type: set
        pkg_dir_ext = set()  # type: set
        for rel in arrivals:
            if rel not in targets:
                continue
            size = targets[rel]
            for base in _names_before(os.path.basename(rel)):
                by_name.setdefault(base, []).append(size)
            if size is not None and size > 0:
                by_dir.setdefault(os.path.dirname(rel), set()).add(int(size))
            ext = os.path.splitext(rel)[1]
            if ext:
                pkg_dir_ext.add((os.path.dirname(rel), ext))
        # A package's files may have gone to ANY placeholder of that name --
        # including one that already has rows (Finder "Replace" onto a
        # backed-up package, or a package move that stopped part-way).
        for rel in targets:
            pkg_names.update(_names_before(os.path.basename(rel)))
        if not by_name and not pkg_names:
            return deletions, []
        kept = []  # type: List[str]
        deferred = []  # type: List[str]
        for rel in deletions:
            size = rows[rel]["size"] if rel in rows else None
            base = os.path.basename(rel)
            hit = any(s is None or (size is not None and s == size)
                      for s in by_name.get(base, ()))
            if not hit and size:
                hit = int(size) in by_dir.get(os.path.dirname(rel), ())
            if not hit:
                parent = os.path.dirname(rel)
                while parent and not hit:
                    if not os.path.lexists(os.path.join(src_root, parent)):
                        pbase = os.path.basename(parent)
                        pext = os.path.splitext(pbase)[1]
                        hit = (pbase in pkg_names or (
                            pext and (os.path.dirname(parent), pext) in pkg_dir_ext))
                    parent = os.path.dirname(parent)
            (deferred if hit else kept).append(rel)
        return kept, deferred

    @staticmethod
    def _record_file(src_root, path, st, dataless_check, out):
        # type: (str, str, object, Callable, Dict) -> None
        rel = os.path.relpath(path, src_root)
        try:
            is_dl = bool(dataless_check(path, st))
        except Exception:
            is_dl = False
        out[rel] = (int(st.st_size), int(st.st_mtime_ns), int(st.st_ino), is_dl)

    @staticmethod
    def _detect_renames(rows, local, vanished, appeared):
        # type: (Dict, Dict, set, set) -> Tuple[List, set, set]
        """Pair a vanished old path with an appeared new path by identity.

        Identity is (inode, size, mtime_ns) with size>0: a macOS rename keeps the
        inode, and copies mtime, so a moved file matches its old row exactly. A
        key with more than one candidate on EITHER side is ambiguous and is left
        to fall back to upload+delete (never guess a rename). Returns
        (pairs, consumed_old, consumed_new).
        """
        old_index = {}  # type: Dict[Tuple[int, int, int], List[str]]
        for rel in vanished:
            row = rows[rel]
            inode = row["inode"]
            size = row["size"]
            if not inode or not size or size <= 0:
                continue
            key = (int(inode), int(size), int(row["mtime_ns"]))
            old_index.setdefault(key, []).append(rel)

        new_index = {}  # type: Dict[Tuple[int, int, int], List[str]]
        for rel in appeared:
            size, mtime_ns, inode, _dl = local[rel]
            if not inode or size <= 0:
                continue
            key = (int(inode), int(size), int(mtime_ns))
            new_index.setdefault(key, []).append(rel)

        pairs = []  # type: List[Tuple[str, str]]
        consumed_old = set()  # type: set
        consumed_new = set()  # type: set
        for key, olds in old_index.items():
            news = new_index.get(key)
            if not news:
                continue
            if len(olds) != 1 or len(news) != 1:
                continue  # ambiguous -> upload + delete
            old_rel, new_rel = olds[0], news[0]
            pairs.append((old_rel, new_rel))
            consumed_old.add(old_rel)
            consumed_new.add(new_rel)
        pairs.sort()
        return pairs, consumed_old, consumed_new

    def _rows_in_scope(self, folder, scopes):
        # type: (str, List[str]) -> Dict[str, sqlite3.Row]
        """Return {relpath: row} for folder, limited to the given scopes."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT relpath, size, mtime_ns, inode, pending FROM files "
                "WHERE folder=?", (folder,))
            all_rows = cur.fetchall()
        if scopes == ["."]:
            return dict((r["relpath"], r) for r in all_rows)
        result = {}  # type: Dict[str, sqlite3.Row]
        for r in all_rows:
            rel = r["relpath"]
            for scope in scopes:
                if rel == scope or rel.startswith(scope + os.sep):
                    result[rel] = r
                    break
        return result

    # -- commits -----------------------------------------------------------

    def commit_uploaded(self, folder, rows):
        # type: (str, List[Tuple[str, int, int, int]]) -> None
        """Upsert (relpath, size, mtime_ns, inode) rows as synced (pending=0).

        Called after each batch's rsync succeeds, so a crash mid-pass loses at
        most the last uncommitted batch and a restart re-plans only the remainder
        (PLAN 14.3 resume).
        """
        if not rows:
            return
        now = _utc_now_iso()
        payload = [
            (folder, rel, int(size), int(mtime_ns), int(inode), now)
            for (rel, size, mtime_ns, inode) in rows
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO files(folder, relpath, size, mtime_ns, inode, "
                "synced_at, pending) VALUES(?,?,?,?,?,?,0) "
                "ON CONFLICT(folder, relpath) DO UPDATE SET "
                "size=excluded.size, mtime_ns=excluded.mtime_ns, "
                "inode=excluded.inode, synced_at=excluded.synced_at, pending=0",
                payload)
            self._conn.commit()

    def commit_deleted(self, folder, relpaths):
        # type: (str, List[str]) -> None
        """Remove rows for relpaths whose deletion was actually propagated."""
        if not relpaths:
            return
        with self._lock:
            self._conn.executemany(
                "DELETE FROM files WHERE folder=? AND relpath=?",
                [(folder, rel) for rel in relpaths])
            self._conn.commit()

    def commit_renamed(self, folder, pairs):
        # type: (str, List[Tuple[str, str]]) -> None
        """Move rows old->new after a NAS-side rename (content unchanged).

        A rename preserves size/mtime/inode, so only the key changes. The old row
        is deleted and the new one written (pending carried over). Any pre-existing row at
        the new key is overwritten (it was an 'appeared' path with no prior row).
        """
        if not pairs:
            return
        now = _utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            for old_rel, new_rel in pairs:
                row = cur.execute(
                    "SELECT size, mtime_ns, inode, pending FROM files "
                    "WHERE folder=? AND relpath=?", (folder, old_rel)).fetchone()
                if row is None:
                    continue
                cur.execute(
                    "DELETE FROM files WHERE folder=? AND relpath=?",
                    (folder, old_rel))
                # pending moves with the row: a NAS copy deep verify found
                # missing or drifted is still that after a rename.
                cur.execute(
                    "INSERT INTO files(folder, relpath, size, mtime_ns, inode, "
                    "synced_at, pending) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(folder, relpath) DO UPDATE SET "
                    "size=excluded.size, mtime_ns=excluded.mtime_ns, "
                    "inode=excluded.inode, synced_at=excluded.synced_at, "
                    "pending=excluded.pending",
                    (folder, new_rel, row["size"], row["mtime_ns"],
                     row["inode"], now, 1 if row["pending"] else 0))
            self._conn.commit()

    def mark_pending(self, folder, relpaths):
        # type: (str, List[str]) -> int
        """Flag existing rows pending=1 so the next pass re-uploads them.

        Used by deep verify for NAS-missing / drifted files. Returns the number
        of rows actually updated (a relpath with no row is ignored -- the next
        plan walk uploads it naturally as an 'appeared' file).
        """
        if not relpaths:
            return 0
        with self._lock:
            cur = self._conn.cursor()
            cur.executemany(
                "UPDATE files SET pending=1 WHERE folder=? AND relpath=?",
                [(folder, rel) for rel in relpaths])
            self._conn.commit()
            return cur.rowcount if cur.rowcount is not None else 0

    def forget_folder(self, folder):
        # type: (str) -> None
        """Drop every row for a folder (config removal / source-device change)."""
        with self._lock:
            self._conn.execute("DELETE FROM files WHERE folder=?", (folder,))
            self._conn.commit()

    def forget_excluded(self, folder, patterns):
        # type: (str, List[str]) -> int
        """Drop rows the current excludes would never walk again; return the count.

        WHY (PLAN 16.2 T-B2, 2026-07-30): the exclude list grows over time (that
        day BUILTIN_EXCLUDES gained the developer-litter patterns, ~1/3 of the
        real-machine backup volume). A row for a now-excluded path disappears
        from the next local walk, and the planner reads that absence as
        "vanished" -- so one exclude edit would plan a mass DELETION of files
        that are all perfectly alive. Forgetting the rows FIRST turns the edit
        into a no-op: those files simply stop being tracked. Their NAS-side
        copies are left in place as harmless orphan litter on purpose -- the
        local files still exist, so deep verify never classifies them as extra
        (an explicit clean-up command is deferred to PLAN 16.4).

        Matching mirrors _walk_scope: a pattern hitting ANY path component
        removes the whole subtree under that component (that is exactly what
        pruning an excluded directory does), and a pattern hitting the basename
        removes the file. Idempotent -- a second call finds nothing left.
        """
        patterns = [p.rstrip("/") for p in (patterns or [])]
        if not patterns:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "SELECT relpath FROM files WHERE folder=?", (folder,))
            relpaths = [r["relpath"] for r in cur.fetchall()]
        # Filtering in Python (not SQL) because fnmatch semantics have no SQL
        # equivalent; 300k+ rows is a sub-second pass done once per start/reload.
        cache = {}  # type: Dict[str, bool]
        doomed = [rel for rel in relpaths
                  if _path_excluded(rel, patterns, cache)]
        if not doomed:
            return 0
        with self._lock:
            self._conn.executemany(
                "DELETE FROM files WHERE folder=? AND relpath=?",
                [(folder, rel) for rel in doomed])
            self._conn.commit()
        return len(doomed)

    # -- introspection -----------------------------------------------------

    def counts(self, folder):
        # type: (str) -> Dict[str, int]
        """Return {files, pending, bytes} totals for a folder."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, "
                "COALESCE(SUM(CASE WHEN pending THEN 1 ELSE 0 END), 0) AS p, "
                "COALESCE(SUM(size), 0) AS b FROM files WHERE folder=?",
                (folder,)).fetchone()
        return {"files": int(row["n"]), "pending": int(row["p"]),
                "bytes": int(row["b"])}

    def rows(self, folder):
        # type: (str) -> Dict[str, sqlite3.Row]
        """Return {relpath: row} for every row of a folder (deep verify)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT relpath, size, mtime_ns, inode, pending, synced_at "
                "FROM files WHERE folder=?", (folder,))
            return dict((r["relpath"], r) for r in cur.fetchall())


def _normalize_subtrees(subtrees):
    # type: (Optional[List[str]]) -> List[str]
    """Clean a subtree list into scope relpaths; whole-folder collapses to ['.'].

    Any '.'/''/None among the entries means the whole folder, which subsumes all
    narrower scopes.
    """
    if not subtrees:
        return ["."]
    cleaned = set()  # type: set
    for sub in subtrees:
        if sub is None:
            return ["."]
        stripped = sub.strip("/")
        if stripped == "" or stripped == ".":
            return ["."]
        cleaned.add(stripped)
    return sorted(cleaned)


def _excluded(name, patterns):
    # type: (str, List[str]) -> bool
    """True if a basename matches any exclude pattern (fnmatch, like scanner)."""
    import fnmatch
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def _path_excluded(rel, patterns, cache):
    # type: (str, List[str], Dict[str, bool]) -> bool
    """True if ANY path component of rel matches a pattern (walk semantics).

    cache memoizes the per-component verdict: a real manifest holds hundreds of
    thousands of rows sharing a few thousand distinct directory names, so the
    fnmatch work collapses to one call per distinct name.
    """
    for part in rel.split(os.sep):
        hit = cache.get(part)
        if hit is None:
            hit = _excluded(part, patterns)
            cache[part] = hit
        if hit:
            return True
    return False


def _stat_is_dir(st):
    # type: (object) -> bool
    import stat as _stat
    return _stat.S_ISDIR(st.st_mode)
