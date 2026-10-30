"""Optional kqueue near-realtime directory watcher (watch_mode=auto, PLAN 6.3).

Watches every backup directory via one open fd (O_EVTONLY) + EVFILT_VNODE and
reports the set of directories that changed since the last poll. New
subdirectories are registered incrementally; vanished ones are released.

If the directory count exceeds the fd budget the watcher declines to start so
the caller falls back to periodic scan mode. Everything here is Python 3.9
stdlib only and macOS-specific (kqueue).
"""

import errno
import fnmatch
import os
import select
from typing import Dict, List, Optional, Set

# Open a directory for event notification only (no read/write). Absent from the
# os module in the CLT 3.9 build; 0x8000 is the fixed Darwin value.
_O_EVTONLY = getattr(os, "O_EVTONLY", 0x8000)

_VNODE_FFLAGS = (
    select.KQ_NOTE_WRITE
    | select.KQ_NOTE_DELETE
    | select.KQ_NOTE_RENAME
    | select.KQ_NOTE_EXTEND
)

# Upper bound on drain iterations, guarding against a busy tree that keeps
# generating events faster than we read them.
_MAX_DRAIN = 4096


class KqueueWatcher(object):
    """Watch backup directories with kqueue/EVFILT_VNODE.

    active: True once start() has succeeded and the watcher owns live fds.
    degraded: True if the fd budget was hit during incremental registration
    (some new subtree is not being watched; a periodic full scan still covers
    it). Both are surfaced in status.
    """

    def __init__(self, roots, exclude_patterns, max_fds=4096, logger=None,
                 shallow_roots=None):
        # type: (List[object], List[str], int, object, Optional[List[object]]) -> None
        self.roots = [self._norm(r) for r in roots]
        self.exclude_patterns = list(exclude_patterns or [])
        self.max_fds = max_fds
        self.logger = logger
        # Shallow roots are watched NON-RECURSIVELY (one fd each, no subtree
        # registration): used for the parent directory of a single-file source,
        # whose vnode fires on any create/rename/delete of a direct child.
        self._shallow = set(self._norm(r) for r in (shallow_roots or []))
        self._kq = None  # type: Optional[select.kqueue]
        self._fd_to_path = {}  # type: Dict[int, str]
        self._path_to_fd = {}  # type: Dict[str, int]
        self.active = False
        self.degraded = False
        self._overflow_logged = False

    # -- lifecycle --------------------------------------------------------

    def start(self):
        # type: () -> bool
        """Enumerate and register every directory; return False if over budget.

        On False nothing is left open, so the caller can safely fall back to
        scan mode.
        """
        self.close()
        self.degraded = False
        self._overflow_logged = False

        dirs = set(self._enumerate_dirs())
        shallow = set(p for p in self._shallow if os.path.isdir(p))
        all_dirs = dirs | shallow
        if len(all_dirs) > self.max_fds:
            self._log(
                "kqueue watch disabled: %d directories exceed fd budget %d; "
                "falling back to scan mode" % (len(all_dirs), self.max_fds)
            )
            return False

        self._kq = select.kqueue()
        for path in dirs:
            self._register(path)
        for path in shallow:
            self._register(path)
        self.active = True
        return True

    def close(self):
        # type: () -> None
        """Release all fds and the kqueue. Idempotent."""
        for fd in list(self._fd_to_path.keys()):
            try:
                os.close(fd)
            except OSError:
                pass
        self._fd_to_path.clear()
        self._path_to_fd.clear()
        if self._kq is not None:
            try:
                self._kq.close()
            except OSError:
                pass
            self._kq = None
        self.active = False

    @property
    def fd_count(self):
        # type: () -> int
        return len(self._fd_to_path)

    # -- polling ----------------------------------------------------------

    def poll(self, timeout_s):
        # type: (float) -> Set[str]
        """Block up to timeout_s for events, then drain, and return dirty dirs.

        For each dirty directory, incrementally registers newly created
        subdirectories (recursively) and unregisters ones that vanished.
        Returns the set of dirty directory paths (absolute strings).
        """
        dirty = set()  # type: Set[str]
        if not self.active or self._kq is None:
            return dirty

        idents = set()  # type: Set[str]
        timeout = timeout_s
        for _ in range(_MAX_DRAIN):
            try:
                events = self._kq.control(None, 64, timeout)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    timeout = 0
                    continue
                break
            if not events:
                break
            for ev in events:
                path = self._fd_to_path.get(ev.ident)
                if path is not None:
                    idents.add(path)
            # Subsequent reads only drain what is already queued.
            timeout = 0

        for path in idents:
            dirty.add(path)
            self._reconcile(path)
        return dirty

    # -- registration -----------------------------------------------------

    def _register(self, path):
        # type: (str) -> bool
        """Open path O_EVTONLY and add a vnode filter. Honors the fd budget."""
        if path in self._path_to_fd:
            return True
        if len(self._fd_to_path) >= self.max_fds:
            self.degraded = True
            if not self._overflow_logged:
                self._log(
                    "kqueue fd budget (%d) exceeded during incremental "
                    "registration; watcher degraded, relying on full scan"
                    % self.max_fds
                )
                self._overflow_logged = True
            return False
        try:
            fd = os.open(path, _O_EVTONLY)
        except OSError:
            return False
        ev = select.kevent(
            fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=_VNODE_FFLAGS,
        )
        try:
            self._kq.control([ev], 0, 0)
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            return False
        self._fd_to_path[fd] = path
        self._path_to_fd[path] = fd
        return True

    def _unregister(self, fd):
        # type: (int) -> None
        """Close one fd (closing removes it from the kqueue automatically)."""
        path = self._fd_to_path.pop(fd, None)
        if path is not None:
            self._path_to_fd.pop(path, None)
        try:
            os.close(fd)
        except OSError:
            pass

    def _unregister_subtree(self, path):
        # type: (str) -> None
        prefix = path + os.sep
        for watched in list(self._path_to_fd.keys()):
            if watched == path or watched.startswith(prefix):
                fd = self._path_to_fd.get(watched)
                if fd is not None:
                    self._unregister(fd)

    def _register_subtree(self, path):
        # type: (str) -> None
        """Register every not-yet-watched descendant directory of path."""
        stack = [path]
        while stack:
            current = stack.pop()
            for child in self._child_dirs(current):
                if child not in self._path_to_fd:
                    if not self._register(child):
                        # Budget hit or open failed; do not descend further.
                        continue
                stack.append(child)

    def _reconcile(self, path):
        # type: (str) -> None
        """Rescan a dirty dir: register new subdirs, release vanished ones."""
        if path in self._shallow:
            # Non-recursive watch: the dirtiness itself is already reported by
            # poll(); never descend into or register this dir's children. Only
            # release the fd if the watched directory itself has disappeared.
            if not os.path.isdir(path):
                self._unregister_subtree(path)
            return
        try:
            entries = list(os.scandir(path))
        except OSError:
            # The dirty directory itself is gone; drop it and its subtree.
            self._unregister_subtree(path)
            return

        present = set()  # type: Set[str]
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if self._excluded(entry.name):
                continue
            child = entry.path
            present.add(child)
            if child not in self._path_to_fd:
                if self._register(child):
                    self._register_subtree(child)

        # Release watched immediate children that no longer exist.
        for watched in list(self._path_to_fd.keys()):
            if os.path.dirname(watched) == path and watched not in present:
                self._unregister_subtree(watched)

    # -- enumeration & helpers -------------------------------------------

    def _enumerate_dirs(self):
        # type: () -> Set[str]
        dirs = self._enumerate_via_scanner()
        if dirs is None:
            dirs = self._enumerate_local()
        return dirs

    def _enumerate_via_scanner(self):
        # type: () -> Optional[Set[str]]
        """Reuse scanner.snapshot_walk when available for identical semantics.

        Returns None if the scanner module (built in parallel) is absent or its
        result cannot be interpreted, so the caller falls back to a local walk.
        """
        try:
            from . import scanner  # noqa: F401
        except ImportError:
            return None
        walk = getattr(scanner, "snapshot_walk", None)
        if walk is None:
            return None
        try:
            result = walk(self.roots, self.exclude_patterns)
        except Exception:
            return None
        if isinstance(result, dict):
            items = result.keys()
        else:
            try:
                items = list(result)
            except TypeError:
                return None
        dirs = set()  # type: Set[str]
        for item in items:
            if isinstance(item, (tuple, list)) and item:
                item = item[0]
            try:
                dirs.add(self._norm(item))
            except TypeError:
                return None
        return dirs

    def _enumerate_local(self):
        # type: () -> Set[str]
        dirs = set()  # type: Set[str]
        for root in self.roots:
            if not os.path.isdir(root):
                continue
            dirs.add(root)
            stack = [root]
            while stack:
                current = stack.pop()
                for child in self._child_dirs(current):
                    dirs.add(child)
                    stack.append(child)
        return dirs

    def _child_dirs(self, path):
        # type: (str) -> List[str]
        """Immediate, non-excluded, non-symlink child directories of path."""
        result = []
        try:
            entries = list(os.scandir(path))
        except OSError:
            return result
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if self._excluded(entry.name):
                continue
            result.append(entry.path)
        return result

    def _excluded(self, name):
        # type: (str) -> bool
        for pat in self.exclude_patterns:
            p = pat.strip()
            if not p:
                continue
            p = p.rstrip("/")
            if p.startswith("/"):
                p = p.lstrip("/")
            if not p:
                continue
            if fnmatch.fnmatch(name, p):
                return True
        return False

    @staticmethod
    def _norm(path):
        # type: (object) -> str
        return os.path.realpath(os.fspath(path))

    def _log(self, message):
        # type: (str) -> None
        if self.logger is not None:
            try:
                self.logger.warning(message)
            except Exception:
                pass
