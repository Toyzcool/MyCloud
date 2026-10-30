"""Tier-1 change detection: directory-mtime snapshot scan + dirty-subtree collapse.

Pure functions, no classes. See PLAN section 6.3.

APFS bumps a directory's mtime whenever a direct child is created, deleted, or
renamed, and macOS apps overwhelmingly save via write-temp-then-rename. So a
cheap snapshot of directory mtimes (metadata only, no file stats) captures
creates/modifies/deletes/renames within one hot-scan cycle. In-place writes that
do not touch the parent mtime (SQLite, appended logs) are the Tier-2 full-verify
scan's job, not this module's.
"""

import fnmatch
import os
from typing import Dict, List, Optional, Set, Tuple


def snapshot_walk(roots, exclude_patterns, shallow_roots=None):
    # type: (List, List[str], Optional[List]) -> Tuple[Dict[str, int], List[str]]
    """Iteratively scandir every root, recording DIRECTORIES ONLY.

    Returns (snapshot, errors) where snapshot maps absolute directory path to
    stat(follow_symlinks=False).st_mtime_ns, including each root itself. A
    directory whose basename matches any exclude pattern (pattern trailing "/"
    stripped) is skipped along with its whole subtree. ANY OSError on a subtree
    appends str(path) to errors and continues (a file mistakenly passed as a
    recursive root records itself, then its scandir raises NotADirectoryError
    and is captured here). Only three errno classes used to be caught: EIO, a
    timeout, or iCloud's EDEADLK on one directory aborted the WHOLE snapshot, so
    the hot scan saw nothing at all for as long as that one directory failed.

    shallow_roots are recorded NON-RECURSIVELY: only each directory's own mtime
    is captured and its children are never descended into. This lets a caller
    watch a single file's PARENT directory (whose mtime bumps whenever a child
    is created/renamed/deleted) without pulling that parent's whole subtree into
    the snapshot. Absent/unreadable shallow roots go to errors like any root.

    The walk is an explicit stack (no recursion) so arbitrarily deep trees
    cannot exhaust the Python recursion limit.
    """
    snapshot = {}  # type: Dict[str, int]
    errors = []  # type: List[str]
    patterns = [p.rstrip("/") for p in exclude_patterns]

    for root in roots:
        root_path = os.path.abspath(os.fspath(root))
        try:
            snapshot[root_path] = os.stat(root_path, follow_symlinks=False).st_mtime_ns
        except OSError:
            errors.append(root_path)
            continue

        stack = [root_path]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as scan:
                    for entry in scan:
                        try:
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            # Race: entry vanished between listing and stat.
                            continue
                        if _excluded(entry.name, patterns):
                            continue
                        try:
                            mtime = entry.stat(follow_symlinks=False).st_mtime_ns
                        except OSError:
                            errors.append(entry.path)
                            continue
                        snapshot[entry.path] = mtime
                        stack.append(entry.path)
            except OSError:
                errors.append(current)
                continue

    for shallow in (shallow_roots or []):
        shallow_path = os.path.abspath(os.fspath(shallow))
        if shallow_path in snapshot:
            continue
        try:
            snapshot[shallow_path] = os.stat(
                shallow_path, follow_symlinks=False).st_mtime_ns
        except OSError:
            errors.append(shallow_path)

    return snapshot, errors


def diff_snapshots(old, new):
    # type: (Dict[str, int], Dict[str, int]) -> Set[str]
    """Return the set of dirty directory paths between two snapshots.

    Dirty = keys whose mtime changed + keys present only in new. For a key
    present only in old (a deleted directory) the nearest surviving ancestor
    that is in new is marked dirty instead, so its parent gets re-synced (which
    is what propagates the deletion downstream). If no ancestor survives in new,
    the deleted key is skipped.
    """
    dirty = set()  # type: Set[str]

    for path, mtime in new.items():
        if path not in old or old[path] != mtime:
            dirty.add(path)

    for path in old:
        if path in new:
            continue
        ancestor = _nearest_ancestor(path, new)
        if ancestor is not None:
            dirty.add(ancestor)

    return dirty


def collapse_dirty(dirty, roots):
    # type: (Set[str], List) -> Dict[str, List[str]]
    """Fold a dirty set into a minimal per-root subtree cover.

    Returns str(root) -> sorted list of subtree relpaths, where "." means the
    whole root and no returned path is a descendant of another returned path for
    the same root. A dirty path equal to a root collapses that root to ["."] and
    swallows all of its other dirty subtrees. Paths not under any root are
    ignored. Ancestor/descendant tests are os.sep-guarded so sibling names that
    share a prefix ("a/b" vs "a/bc") are never treated as related.
    """
    # (abspath, original str(root)); match most-specific (longest) root first so
    # a path under a nested root is attributed to the deeper one.
    pairs = []
    for root in roots:
        pairs.append((os.path.abspath(os.fspath(root)), str(root)))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)

    groups = {}  # type: Dict[str, Set[str]]
    for path in dirty:
        ap = os.path.abspath(os.fspath(path))
        for root_ap, root_str in pairs:
            if ap == root_ap or ap.startswith(root_ap + os.sep):
                rel = "." if ap == root_ap else os.path.relpath(ap, root_ap)
                groups.setdefault(root_str, set()).add(rel)
                break

    result = {}  # type: Dict[str, List[str]]
    for root_str, rels in groups.items():
        if "." in rels:
            result[root_str] = ["."]
        else:
            result[root_str] = sorted(_minimal_cover(rels))
    return result


def _excluded(name, patterns):
    # type: (str, List[str]) -> bool
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def _nearest_ancestor(path, present):
    # type: (str, Dict[str, int]) -> object
    current = path
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return None
        if parent in present:
            return parent
        current = parent


def _minimal_cover(rels):
    # type: (Set[str]) -> List[str]
    """Drop any rel that is a descendant of another rel in the set."""
    kept = []
    for candidate in rels:
        prefixed = False
        for other in rels:
            if candidate == other:
                continue
            if candidate.startswith(other + os.sep):
                prefixed = True
                break
        if not prefixed:
            kept.append(candidate)
    return kept
