import tests._path  # noqa: F401  (prepends <root>/src to sys.path)

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from mycloud import scanner


def _touch(path, text="x"):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class SnapshotWalkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mycloud_scan_")
        self.root = os.path.join(self.tmp, "root")
        os.makedirs(os.path.join(self.root, "a", "b"))
        os.makedirs(os.path.join(self.root, "c"))
        _touch(os.path.join(self.root, "a", "f.txt"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_records_directories_only_including_root(self):
        snap, errors = scanner.snapshot_walk([Path(self.root)], [])
        self.assertEqual(errors, [])
        self.assertIn(self.root, snap)
        self.assertIn(os.path.join(self.root, "a"), snap)
        self.assertIn(os.path.join(self.root, "a", "b"), snap)
        self.assertIn(os.path.join(self.root, "c"), snap)
        # Files are never recorded.
        self.assertNotIn(os.path.join(self.root, "a", "f.txt"), snap)
        for value in snap.values():
            self.assertIsInstance(value, int)

    def test_touching_file_marks_parent_dirty(self):
        old, _ = scanner.snapshot_walk([Path(self.root)], [])
        time.sleep(0.01)
        # Create a new file inside a/ -> APFS-like: parent mtime bumps. We force
        # the mtime change explicitly to stay portable across filesystems.
        adir = os.path.join(self.root, "a")
        _touch(os.path.join(adir, "g.txt"))
        bumped = old[adir] + 1_000_000
        os.utime(adir, ns=(bumped, bumped))
        new, _ = scanner.snapshot_walk([Path(self.root)], [])
        dirty = scanner.diff_snapshots(old, new)
        self.assertIn(adir, dirty)
        self.assertNotIn(os.path.join(self.root, "c"), dirty)

    def test_mkdir_appears_as_dirty_new_key(self):
        old, _ = scanner.snapshot_walk([Path(self.root)], [])
        newdir = os.path.join(self.root, "c", "fresh")
        os.makedirs(newdir)
        new, _ = scanner.snapshot_walk([Path(self.root)], [])
        dirty = scanner.diff_snapshots(old, new)
        self.assertIn(newdir, dirty)

    def test_rmdir_marks_surviving_ancestor(self):
        old, _ = scanner.snapshot_walk([Path(self.root)], [])
        removed = os.path.join(self.root, "a", "b")
        os.rmdir(removed)
        new, _ = scanner.snapshot_walk([Path(self.root)], [])
        dirty = scanner.diff_snapshots(old, new)
        # b is gone; its nearest surviving ancestor a/ must be dirty.
        self.assertIn(os.path.join(self.root, "a"), dirty)
        self.assertNotIn(removed, dirty)

    def test_deleted_root_has_no_surviving_ancestor(self):
        old = {self.root: 1, os.path.join(self.root, "a"): 2}
        new = {}  # everything gone, nothing to anchor to
        dirty = scanner.diff_snapshots(old, new)
        self.assertEqual(dirty, set())

    def test_exclusion_skips_whole_subtree(self):
        nm = os.path.join(self.root, "a", "node_modules")
        os.makedirs(os.path.join(nm, "pkg", "deep"))
        snap, _ = scanner.snapshot_walk([Path(self.root)], ["node_modules/"])
        self.assertNotIn(nm, snap)
        self.assertNotIn(os.path.join(nm, "pkg"), snap)
        self.assertNotIn(os.path.join(nm, "pkg", "deep"), snap)
        # Non-excluded siblings still present.
        self.assertIn(os.path.join(self.root, "a", "b"), snap)

    def test_exclusion_glob_pattern(self):
        os.makedirs(os.path.join(self.root, "build_tmp"))
        snap, _ = scanner.snapshot_walk([Path(self.root)], ["build_*"])
        self.assertNotIn(os.path.join(self.root, "build_tmp"), snap)

    def test_permission_error_recorded_as_error(self):
        locked = os.path.join(self.root, "locked")
        os.makedirs(locked)
        os.chmod(locked, 0o000)
        try:
            snap, errors = scanner.snapshot_walk([Path(self.root)], [])
            # The directory itself is recorded (stat on it succeeds); scandir of
            # its contents fails and is captured in errors.
            self.assertIn(locked, errors)
        finally:
            os.chmod(locked, 0o755)

    def test_missing_root_recorded_as_error(self):
        missing = os.path.join(self.tmp, "does_not_exist")
        snap, errors = scanner.snapshot_walk([Path(missing)], [])
        self.assertIn(missing, errors)
        self.assertNotIn(missing, snap)

    def test_shallow_root_recorded_without_descent(self):
        # The file-source parent watch: record the dir's own mtime but never
        # pull its subtree into the snapshot.
        parent = os.path.join(self.root, "a")
        snap, errors = scanner.snapshot_walk([], [], shallow_roots=[Path(parent)])
        self.assertEqual(errors, [])
        self.assertIn(parent, snap)
        # a/b would be present under a recursive walk; as a shallow root it isn't.
        self.assertNotIn(os.path.join(parent, "b"), snap)

    def test_shallow_root_mtime_change_is_dirty(self):
        parent = os.path.join(self.root, "a")
        old, _ = scanner.snapshot_walk([], [], shallow_roots=[Path(parent)])
        bumped = old[parent] + 1_000_000
        os.utime(parent, ns=(bumped, bumped))
        new, _ = scanner.snapshot_walk([], [], shallow_roots=[Path(parent)])
        self.assertIn(parent, scanner.diff_snapshots(old, new))

    def test_file_as_recursive_root_does_not_crash(self):
        # A file mistakenly passed as a recursive root records itself, then its
        # scandir raises NotADirectoryError which is captured in errors.
        f = os.path.join(self.root, "a", "f.txt")
        snap, errors = scanner.snapshot_walk([Path(f)], [])
        self.assertIn(f, errors)


class DiffTests(unittest.TestCase):
    def test_changed_mtime_is_dirty(self):
        old = {"/r": 1, "/r/a": 5}
        new = {"/r": 1, "/r/a": 9}
        self.assertEqual(scanner.diff_snapshots(old, new), {"/r/a"})

    def test_only_in_new_is_dirty(self):
        old = {"/r": 1}
        new = {"/r": 1, "/r/a": 2}
        self.assertEqual(scanner.diff_snapshots(old, new), {"/r/a"})

    def test_deleted_nested_dir_promotes_to_nearest_present_ancestor(self):
        old = {"/r": 1, "/r/a": 2, "/r/a/b": 3, "/r/a/b/c": 4}
        new = {"/r": 1, "/r/a": 2}  # b and c deleted, a unchanged
        # Both b and c promote to a (nearest surviving ancestor).
        self.assertEqual(scanner.diff_snapshots(old, new), {"/r/a"})


class CollapseTests(unittest.TestCase):
    def setUp(self):
        self.root = "/data/src"

    def test_nested_dirty_collapse_to_ancestor(self):
        dirty = {"/data/src/a", "/data/src/a/b", "/data/src/a/b/c"}
        out = scanner.collapse_dirty(dirty, [Path(self.root)])
        self.assertEqual(out, {self.root: ["a"]})

    def test_root_dirty_swallows_all(self):
        dirty = {"/data/src", "/data/src/a", "/data/src/a/b"}
        out = scanner.collapse_dirty(dirty, [Path(self.root)])
        self.assertEqual(out, {self.root: ["."]})

    def test_siblings_both_kept(self):
        dirty = {"/data/src/a", "/data/src/b"}
        out = scanner.collapse_dirty(dirty, [Path(self.root)])
        self.assertEqual(out, {self.root: ["a", "b"]})

    def test_separator_guard_prefix_siblings(self):
        # "b" and "bc" share a string prefix but are not ancestor/descendant.
        dirty = {"/data/src/a/b", "/data/src/a/bc"}
        out = scanner.collapse_dirty(dirty, [Path(self.root)])
        self.assertEqual(out, {self.root: [os.path.join("a", "b"), os.path.join("a", "bc")]})

    def test_paths_outside_any_root_ignored(self):
        dirty = {"/data/src/a", "/elsewhere/x", "/data/srcXtra/y"}
        out = scanner.collapse_dirty(dirty, [Path(self.root)])
        self.assertEqual(out, {self.root: ["a"]})

    def test_multiple_roots_attributed_separately(self):
        r1, r2 = "/data/one", "/data/two"
        dirty = {"/data/one/a", "/data/two/b", "/data/two/b/c"}
        out = scanner.collapse_dirty(dirty, [Path(r1), Path(r2)])
        self.assertEqual(out, {r1: ["a"], r2: ["b"]})

    def test_nested_roots_prefer_most_specific(self):
        outer, inner = "/data/proj", "/data/proj/sub"
        dirty = {"/data/proj/sub/x"}
        out = scanner.collapse_dirty(dirty, [Path(outer), Path(inner)])
        # Deepest matching root wins.
        self.assertEqual(out, {inner: ["x"]})


class PerfSmokeTests(unittest.TestCase):
    def test_two_thousand_dirs_under_five_seconds(self):
        tmp = tempfile.mkdtemp(prefix="mycloud_perf_")
        try:
            root = os.path.join(tmp, "root")
            # 2000 directories in a moderate fan-out tree.
            for i in range(40):
                for j in range(50):
                    os.makedirs(os.path.join(root, "d%02d" % i, "s%02d" % j))
            start = time.monotonic()
            snap, errors = scanner.snapshot_walk([Path(root)], [])
            elapsed = time.monotonic() - start
            self.assertEqual(errors, [])
            self.assertGreaterEqual(len(snap), 2000)
            self.assertLess(elapsed, 5.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
