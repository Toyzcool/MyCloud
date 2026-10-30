import tests._path  # noqa: F401

import os
import shutil
import tempfile
import unittest
from datetime import datetime

from mycloud import trash
from mycloud.trash import TrashManager, PruneResult


class _NullLogger(object):
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _make_batch(root, name, files):
    """Create root/name/<file> for each (relpath, size) in files."""
    base = os.path.join(root, name)
    for rel, size in files:
        fpath = os.path.join(base, rel)
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "wb") as h:
            h.write(b"\0" * size)
    os.makedirs(base, exist_ok=True)
    return base


class TrashPruneTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.logger = _NullLogger()

    def _mgr(self, retention_days=30, trash_max_gb=50):
        return TrashManager(self.dir, retention_days, trash_max_gb, self.logger)

    def test_age_cutoff_removes_only_old(self):
        _make_batch(self.dir, "2026-01-01T120000", [("a.txt", 10)])  # old
        _make_batch(self.dir, "2026-07-17T120000", [("b.txt", 10)])  # within
        now = datetime(2026, 7, 18, 0, 0, 0)
        res = self._mgr(retention_days=30).prune(lambda: True, now)
        self.assertEqual(res.removed_stamps, ["2026-01-01T120000"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "2026-01-01T120000")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "2026-07-17T120000")))

    def test_returns_prune_result_namedtuple(self):
        res = self._mgr().prune(lambda: True, datetime(2026, 7, 18))
        self.assertIsInstance(res, PruneResult)
        self.assertEqual(res.removed_stamps, [])
        self.assertEqual(res.freed_bytes, 0)
        self.assertEqual(res.errors, [])

    def test_freed_bytes_reported(self):
        _make_batch(self.dir, "2026-01-01T120000", [("a.txt", 100), ("sub/b.txt", 50)])
        now = datetime(2026, 7, 18)
        res = self._mgr(retention_days=30).prune(lambda: True, now)
        self.assertEqual(res.removed_stamps, ["2026-01-01T120000"])
        self.assertEqual(res.freed_bytes, 150)

    def test_malformed_names_never_removed(self):
        # Date-only, junk, and near-miss stamp shapes must all be ignored.
        for bad in ["2026-07-18", "evil", "2026-07-18T15300", "2026-07-18T1530000",
                    "2026-13-01T120000", "not-a-stamp"]:
            os.makedirs(os.path.join(self.dir, bad), exist_ok=True)
        _make_batch(self.dir, "2020-01-01T000000", [("x", 1)])  # old, valid
        now = datetime(2026, 7, 18)
        res = self._mgr(retention_days=30).prune(lambda: True, now)
        self.assertEqual(res.removed_stamps, ["2020-01-01T000000"])
        for bad in ["2026-07-18", "evil", "2026-07-18T15300", "2026-07-18T1530000",
                    "2026-13-01T120000", "not-a-stamp"]:
            self.assertTrue(os.path.exists(os.path.join(self.dir, bad)), bad)

    def test_files_at_root_ignored(self):
        # A plausibly-named FILE (not dir) must not be treated as a batch.
        with open(os.path.join(self.dir, "2020-01-01T000000"), "w") as h:
            h.write("x")
        res = self._mgr(retention_days=30).prune(lambda: True, datetime(2026, 7, 18))
        self.assertEqual(res.removed_stamps, [])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "2020-01-01T000000")))

    def test_guard_false_aborts_remaining(self):
        # Two old batches; guard returns False after the first removal.
        _make_batch(self.dir, "2020-01-01T000000", [("a", 1)])
        _make_batch(self.dir, "2020-02-01T000000", [("b", 1)])
        calls = {"n": 0}

        def guard():
            calls["n"] += 1
            return calls["n"] <= 1

        res = self._mgr(retention_days=30).prune(guard, datetime(2026, 7, 18))
        self.assertEqual(res.removed_stamps, ["2020-01-01T000000"])
        self.assertTrue(any("guard_failed" in e for e in res.errors))
        # Second (oldest-remaining) batch survives the abort.
        self.assertTrue(os.path.exists(os.path.join(self.dir, "2020-02-01T000000")))

    def test_size_cap_removes_oldest_first(self):
        # trash_max_gb=0 -> cap is 0 bytes; everything is over cap, oldest first.
        _make_batch(self.dir, "2026-07-10T120000", [("a", 100)])
        _make_batch(self.dir, "2026-07-11T120000", [("b", 100)])
        _make_batch(self.dir, "2026-07-12T120000", [("c", 100)])
        now = datetime(2026, 7, 18)
        # retention large enough that age rule removes nothing.
        res = self._mgr(retention_days=3650, trash_max_gb=0).prune(lambda: True, now)
        self.assertEqual(
            res.removed_stamps,
            ["2026-07-10T120000", "2026-07-11T120000", "2026-07-12T120000"],
        )

    def test_size_cap_partial(self):
        # Cap admits some batches; only the oldest excess is removed.
        gib = 2 ** 30
        # Fake sizes via patched _dir_size to avoid writing gigabytes.
        mgr = self._mgr(retention_days=3650, trash_max_gb=2)
        for name in ["2026-07-10T120000", "2026-07-11T120000", "2026-07-12T120000"]:
            os.makedirs(os.path.join(self.dir, name), exist_ok=True)
        sizes = {
            "2026-07-10T120000": gib,
            "2026-07-11T120000": gib,
            "2026-07-12T120000": gib,
        }
        mgr._dir_size = lambda p: sizes[os.path.basename(p)]
        res = mgr.prune(lambda: True, datetime(2026, 7, 18))
        # Total 3 GiB, cap 2 GiB -> remove oldest 1 batch to get under.
        self.assertEqual(res.removed_stamps, ["2026-07-10T120000"])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "2026-07-11T120000")))

    def test_age_and_size_combined_no_double_count(self):
        _make_batch(self.dir, "2020-01-01T000000", [("a", 100)])  # aged out
        _make_batch(self.dir, "2026-07-16T120000", [("b", 100)])  # 36h: cap may take it
        res = self._mgr(retention_days=30, trash_max_gb=0).prune(
            lambda: True, datetime(2026, 7, 18)
        )
        # Both removed but the aged one appears exactly once.
        self.assertEqual(res.removed_stamps.count("2020-01-01T000000"), 1)
        self.assertIn("2026-07-16T120000", res.removed_stamps)

    def test_size_cap_never_removes_a_batch_younger_than_a_day(self):
        # One big pass exceeded the cap by itself: the cap used to destroy that
        # very batch -- the only copies of files deleted minutes earlier.
        _make_batch(self.dir, "2026-07-15T000000", [("old", 100)])
        _make_batch(self.dir, "2026-07-17T230000", [("fresh", 100)])  # 1h old
        res = self._mgr(retention_days=30, trash_max_gb=0).prune(
            lambda: True, datetime(2026, 7, 18)
        )
        self.assertIn("2026-07-15T000000", res.removed_stamps)
        self.assertNotIn("2026-07-17T230000", res.removed_stamps)
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "2026-07-17T230000")))
        self.assertTrue(any(e.startswith("cap_unmet") for e in res.errors), res.errors)

    def test_symlinks_not_followed_in_size(self):
        base = _make_batch(self.dir, "2020-01-01T000000", [("real", 40)])
        outside = os.path.join(self.dir, "outside_big")
        with open(outside, "wb") as h:
            h.write(b"\0" * 10000)
        try:
            os.symlink(outside, os.path.join(base, "link"))
        except OSError:
            self.skipTest("symlinks unsupported")
        # lstat of the symlink itself, not its 10000-byte target.
        size = self._mgr()._dir_size(base)
        self.assertLess(size, 10000)

    def test_realpath_escape_rejected(self):
        # A stamp-named symlink pointing outside trash must not be rmtree'd.
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        with open(os.path.join(outside, "precious"), "w") as h:
            h.write("keep me")
        link = os.path.join(self.dir, "2020-01-01T000000")
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("symlinks unsupported")
        res = self._mgr(retention_days=30).prune(lambda: True, datetime(2026, 7, 18))
        self.assertNotIn("2020-01-01T000000", res.removed_stamps)
        self.assertTrue(any("outside_root" in e for e in res.errors))
        self.assertTrue(os.path.exists(os.path.join(outside, "precious")))

    def test_empty_trash_root(self):
        res = self._mgr().prune(lambda: True, datetime(2026, 7, 18))
        self.assertEqual(res, PruneResult([], 0, []))

    def test_missing_trash_root(self):
        mgr = TrashManager(os.path.join(self.dir, "nope"), 30, 50, self.logger)
        res = mgr.prune(lambda: True, datetime(2026, 7, 18))
        self.assertEqual(res.removed_stamps, [])


class FreeSpaceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.mgr = TrashManager(self.dir, 30, 50, _NullLogger())
        self._orig = os.statvfs

    def tearDown(self):
        os.statvfs = self._orig

    def _fake_statvfs(self, free_bytes):
        gib = 2 ** 30

        class _S(object):
            f_frsize = 4096
            f_bavail = free_bytes // 4096

        os.statvfs = lambda p: _S()

    def test_stop_below_1gib(self):
        self._fake_statvfs(512 * 1024 * 1024)
        level, free = self.mgr.free_space(self.dir)
        self.assertEqual(level, "stop")

    def test_warn_below_5gib(self):
        self._fake_statvfs(3 * (2 ** 30))
        level, free = self.mgr.free_space(self.dir)
        self.assertEqual(level, "warn")

    def test_ok_above_5gib(self):
        self._fake_statvfs(10 * (2 ** 30))
        level, free = self.mgr.free_space(self.dir)
        self.assertEqual(level, "ok")

    def test_boundary_exactly_1gib_is_warn(self):
        self._fake_statvfs(2 ** 30)
        level, _ = self.mgr.free_space(self.dir)
        self.assertEqual(level, "warn")

    def test_boundary_exactly_5gib_is_ok(self):
        self._fake_statvfs(5 * (2 ** 30))
        level, _ = self.mgr.free_space(self.dir)
        self.assertEqual(level, "ok")

    def test_free_bytes_value(self):
        self._fake_statvfs(10 * (2 ** 30))
        _, free = self.mgr.free_space(self.dir)
        self.assertEqual(free, 10 * (2 ** 30))


class InvariantTests(unittest.TestCase):
    def test_rmtree_only_in_trash_module(self):
        # shutil.rmtree may appear ONLY in src/mycloud/trash.py.
        src_root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "mycloud",
        )
        offenders = []
        for dirpath, _dirs, files in os.walk(src_root):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(dirpath, fname)
                with open(fpath, encoding="utf-8") as h:
                    if "rmtree" in h.read() and fname != "trash.py":
                        offenders.append(fpath)
        self.assertEqual(offenders, [], "rmtree found outside trash.py: %s" % offenders)


if __name__ == "__main__":
    unittest.main()
