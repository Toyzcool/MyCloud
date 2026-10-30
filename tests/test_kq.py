import tests._path  # noqa: F401  (prepends <root>/src to sys.path)

import os
import shutil
import tempfile
import time
import unittest

from mycloud import kq


def _mk(*parts):
    path = os.path.join(*parts)
    os.makedirs(path)
    return path


def _touch(path):
    with open(path, "w") as handle:
        handle.write("x")


class KqueueWatcherTest(unittest.TestCase):
    def setUp(self):
        # realpath: /var -> /private/var on macOS; the watcher normalizes too.
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="mycloud-kq-"))
        self.watcher = None

    def tearDown(self):
        if self.watcher is not None:
            self.watcher.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _settle(self, seconds=0.2):
        time.sleep(seconds)

    def _drain(self, timeout=0.3):
        return self.watcher.poll(timeout)

    def test_file_create_marks_parent_dirty(self):
        sub = _mk(self.root, "sub")
        self.watcher = kq.KqueueWatcher([self.root], [])
        self.assertTrue(self.watcher.start())
        self._drain()  # consume any startup noise

        _touch(os.path.join(sub, "note.txt"))
        self._settle()
        dirty = self._drain()
        self.assertIn(sub, dirty)

    def test_new_subdir_gets_watched_and_reported(self):
        self.watcher = kq.KqueueWatcher([self.root], [])
        self.assertTrue(self.watcher.start())
        self._drain()
        base_count = self.watcher.fd_count

        # Create a new subdirectory: parent goes dirty, subdir gets registered.
        newsub = os.path.join(self.root, "newsub")
        os.mkdir(newsub)
        self._settle()
        dirty = self._drain()
        self.assertIn(self.root, dirty)
        self.assertEqual(self.watcher.fd_count, base_count + 1)

        # A file created inside the freshly-watched subdir must be reported.
        _touch(os.path.join(newsub, "inside.txt"))
        self._settle()
        dirty = self._drain()
        self.assertIn(newsub, dirty)

    def test_nested_new_subtree_registered_recursively(self):
        self.watcher = kq.KqueueWatcher([self.root], [])
        self.assertTrue(self.watcher.start())
        self._drain()

        # Build a nested tree in one shot, then let one poll reconcile it.
        deep = os.path.join(self.root, "a", "b", "c")
        os.makedirs(deep)
        self._settle()
        self._drain()  # reconcile root -> registers a, then a -> b, etc.

        # The deepest directory should now be watched: a file there is reported.
        _touch(os.path.join(deep, "leaf.txt"))
        self._settle()
        dirty = self._drain()
        self.assertIn(deep, dirty)

    def test_delete_watched_dir_releases_fd(self):
        sub = _mk(self.root, "victim")
        self.watcher = kq.KqueueWatcher([self.root], [])
        self.assertTrue(self.watcher.start())
        self._drain()
        before = self.watcher.fd_count
        self.assertTrue(before >= 2)  # root + victim

        shutil.rmtree(sub)
        self._settle()
        dirty = self._drain()  # must not crash
        self.assertIn(self.root, dirty)
        self.assertEqual(self.watcher.fd_count, before - 1)

    def test_exclude_patterns_skip_dirs(self):
        _mk(self.root, "node_modules")
        _mk(self.root, "keep")
        self.watcher = kq.KqueueWatcher([self.root], ["node_modules", "*.tmp"])
        self.assertTrue(self.watcher.start())
        watched = set(self.watcher._path_to_fd.keys())
        self.assertIn(os.path.join(self.root, "keep"), watched)
        self.assertNotIn(os.path.join(self.root, "node_modules"), watched)

    def test_over_budget_start_returns_false_no_leak(self):
        # Wider tree than the budget of 3 (root + 4 subdirs = 5 dirs).
        for i in range(4):
            _mk(self.root, "d%d" % i)
        self.watcher = kq.KqueueWatcher([self.root], [], max_fds=3)
        self.assertFalse(self.watcher.start())
        self.assertFalse(self.watcher.active)
        self.assertEqual(self.watcher.fd_count, 0)
        # poll on a non-started watcher is a harmless no-op.
        self.assertEqual(self.watcher.poll(0.05), set())

    def test_incremental_overflow_sets_degraded(self):
        self.watcher = kq.KqueueWatcher([self.root], [], max_fds=2)
        # root + one existing subdir = 2 fds, exactly at budget.
        _mk(self.root, "first")
        self.assertTrue(self.watcher.start())
        self.assertEqual(self.watcher.fd_count, 2)
        self.assertFalse(self.watcher.degraded)

        # A new subdir cannot be registered -> degraded flips true.
        os.mkdir(os.path.join(self.root, "second"))
        self._settle()
        self._drain()
        self.assertTrue(self.watcher.degraded)
        self.assertEqual(self.watcher.fd_count, 2)

    def test_close_is_idempotent(self):
        _mk(self.root, "x")
        self.watcher = kq.KqueueWatcher([self.root], [])
        self.assertTrue(self.watcher.start())
        self.watcher.close()
        self.assertEqual(self.watcher.fd_count, 0)
        self.assertFalse(self.watcher.active)
        self.watcher.close()  # second call must be safe
        self.assertEqual(self.watcher.fd_count, 0)

    def test_shallow_root_reports_child_churn_without_descending(self):
        # A file source's parent dir is watched non-recursively: a file created
        # directly in it marks it dirty, but its subdirectory is NOT registered.
        parent = _mk(self.root, "parent")
        _mk(parent, "unrelated_subdir")
        self.watcher = kq.KqueueWatcher(
            [], [], shallow_roots=[parent])
        self.assertTrue(self.watcher.start())
        # Only the shallow root itself is watched (no descent into its subdir).
        self.assertEqual(set(self.watcher._path_to_fd.keys()), {parent})
        self._drain()

        _touch(os.path.join(parent, "note.txt"))
        self._settle()
        dirty = self._drain()
        self.assertIn(parent, dirty)
        # Reconciling the shallow root must not have registered its subdir.
        self.assertEqual(set(self.watcher._path_to_fd.keys()), {parent})

    def test_multiple_roots(self):
        root2 = os.path.realpath(tempfile.mkdtemp(prefix="mycloud-kq2-"))
        self.addCleanup(shutil.rmtree, root2, ignore_errors=True)
        sub2 = _mk(root2, "s2")
        self.watcher = kq.KqueueWatcher([self.root, root2], [])
        self.assertTrue(self.watcher.start())
        self._drain()

        _touch(os.path.join(sub2, "f.txt"))
        self._settle()
        dirty = self._drain()
        self.assertIn(sub2, dirty)


if __name__ == "__main__":
    unittest.main()
