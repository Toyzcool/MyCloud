"""Trash retention pruning, size cap, and free-space gates (PLAN 6.4).

This module is the ONLY place in src/ where shutil.rmtree may appear: pruning
is the single code path that ever truly destroys data on the NAS. An invariant
test in tests/test_trash.py asserts that property.

Python 3.9 stdlib only.
"""

import os
import shutil
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

from mycloud import util

PruneResult = namedtuple("PruneResult", "removed_stamps freed_bytes errors")

_GIB = 2 ** 30

# Free-space thresholds (bytes).
_STOP_BELOW = 1 * _GIB
_WARN_BELOW = 5 * _GIB

# The size cap never removes a batch younger than this. One large pass (an
# approved mass deletion, a big overwrite) could exceed trash_max_gb by itself,
# and the cap then destroyed that very batch at the end of the same pass: the
# only copies of files deleted minutes earlier, gone -- exactly what the trash
# exists to prevent. Over-cap young batches wait; the NAS free-space gate stops
# syncing (and notifies) long before the share could fill.
_CAP_MIN_AGE = timedelta(hours=24)


class TrashManager(object):
    def __init__(self, trash_root, retention_days, trash_max_gb, logger):
        # type: (object, int, int, object) -> None
        self.trash_root = os.fspath(trash_root)
        self.retention_days = retention_days
        self.trash_max_gb = trash_max_gb
        self.logger = logger

    def _candidates(self):
        # type: () -> List[Tuple[str, datetime]]
        """Immediate children of trash_root whose name parses as a strict stamp.

        Anything that does not parse is ignored and never touched. Returns a
        list of (name, parsed_datetime) sorted oldest-first.
        """
        result = []  # type: List[Tuple[str, datetime]]
        try:
            entries = os.listdir(self.trash_root)
        except OSError:
            return result
        for name in entries:
            stamp = util.parse_stamp(name)
            if stamp is None:
                continue
            if not os.path.isdir(os.path.join(self.trash_root, name)):
                continue
            result.append((name, stamp))
        result.sort(key=lambda item: item[1])
        return result

    def _dir_size(self, path):
        # type: (str) -> int
        total = 0
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                try:
                    total += os.lstat(fpath).st_size
                except OSError:
                    continue
        return total

    def prune(self, guard_cb, now_utc):
        # type: (Callable[[], bool], datetime) -> PruneResult
        """Remove aged-out and over-cap trash batches.

        Age rule: parsed stamp < now_utc - retention_days.
        Size-cap rule: while total size > trash_max_gb GiB, add oldest batches
        to the removal set until under the cap -- never one younger than
        _CAP_MIN_AGE; a cap that cannot be met without them is reported in
        errors ("cap_unmet") instead.

        Before each rmtree, guard_cb() is re-checked (mount may drop mid-prune)
        and the target's realpath is verified to lie inside the trash root.
        """
        # parse_stamp yields naive-UTC datetimes; the daemon passes an aware UTC
        # now_utc. Normalize to naive UTC so the comparison below never raises
        # (otherwise daily pruning silently TypeErrors and never runs).
        if now_utc.tzinfo is not None:
            now_utc = now_utc.astimezone(timezone.utc).replace(tzinfo=None)

        candidates = self._candidates()
        sizes = {}  # type: dict
        for name, _stamp in candidates:
            sizes[name] = self._dir_size(os.path.join(self.trash_root, name))

        cutoff = now_utc - timedelta(days=self.retention_days)
        to_remove = []  # type: List[str]
        remove_set = set()

        # Age-based removals.
        for name, stamp in candidates:
            if stamp < cutoff:
                to_remove.append(name)
                remove_set.add(name)

        # Size-cap removals: oldest-first among survivors until under cap.
        cap = self.trash_max_gb * _GIB
        remaining_total = sum(
            sizes[name] for name, _s in candidates if name not in remove_set
        )
        young_after = now_utc - _CAP_MIN_AGE
        for name, stamp in candidates:
            if remaining_total <= cap:
                break
            if name in remove_set:
                continue
            if stamp > young_after:
                break          # candidates are oldest-first: the rest are younger
            to_remove.append(name)
            remove_set.add(name)
            remaining_total -= sizes[name]

        removed_stamps = []  # type: List[str]
        freed_bytes = 0
        errors = []  # type: List[str]
        if remaining_total > cap:
            errors.append("cap_unmet: %.1f GiB of trash younger than %d h exceeds "
                          "the %d GiB cap; kept"
                          % (remaining_total / float(_GIB),
                             _CAP_MIN_AGE.total_seconds() // 3600,
                             self.trash_max_gb))
        trash_real = os.path.realpath(self.trash_root)

        for name in to_remove:
            # Mount may drop mid-prune; abort remaining on guard failure.
            try:
                if not guard_cb():
                    errors.append("guard_failed: aborting before %s" % name)
                    break
            except Exception as exc:  # guard callback misbehaving
                errors.append("guard_error: %s" % exc)
                break

            target = os.path.join(self.trash_root, name)
            target_real = os.path.realpath(target)
            prefix = trash_real + os.sep
            if not (target_real == trash_real or target_real.startswith(prefix)):
                errors.append("outside_root: %s" % name)
                continue

            try:
                shutil.rmtree(target)
                removed_stamps.append(name)
                freed_bytes += sizes.get(name, 0)
            except OSError as exc:
                errors.append("rmtree_failed: %s: %s" % (name, exc))

        return PruneResult(removed_stamps, freed_bytes, errors)

    def free_space(self, mountpoint):
        # type: (object) -> Tuple[str, int]
        """Return (level, free_bytes) for the filesystem at mountpoint.

        level: "stop" if < 1 GiB, "warn" if < 5 GiB, else "ok".
        """
        st = os.statvfs(os.fspath(mountpoint))
        free_bytes = st.f_bavail * st.f_frsize
        if free_bytes < _STOP_BELOW:
            level = "stop"
        elif free_bytes < _WARN_BELOW:
            level = "warn"
        else:
            level = "ok"
        return level, free_bytes
