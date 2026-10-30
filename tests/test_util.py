import tests._path  # noqa: F401

import json
import os
import signal
import subprocess
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from mycloud import util


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        for root, _dirs, files in os.walk(self.dir, topdown=False):
            for f in files:
                os.unlink(os.path.join(root, f))
            os.rmdir(root)

    def test_atomic_write_text_visible(self):
        path = os.path.join(self.dir, "a.txt")
        util.atomic_write_text(path, "hello")
        with open(path, encoding="utf-8") as h:
            self.assertEqual(h.read(), "hello")
        # No leftover temp files.
        self.assertEqual(os.listdir(self.dir), ["a.txt"])

    def test_atomic_write_text_replaces(self):
        path = os.path.join(self.dir, "a.txt")
        util.atomic_write_text(path, "one")
        util.atomic_write_text(path, "two")
        with open(path, encoding="utf-8") as h:
            self.assertEqual(h.read(), "two")
        self.assertEqual(os.listdir(self.dir), ["a.txt"])

    def test_atomic_write_json_roundtrip(self):
        path = os.path.join(self.dir, "a.json")
        obj = {"b": 2, "a": [1, 2, 3], "u": "中文"}
        util.atomic_write_json(path, obj)
        with open(path, encoding="utf-8") as h:
            self.assertEqual(json.load(h), obj)


class ReadJsonTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_missing_returns_default(self):
        path = os.path.join(self.dir, "missing.json")
        self.assertEqual(util.read_json(path, default={"x": 1}), {"x": 1})
        self.assertIsNone(util.read_json(path))

    def test_corrupt_returns_default(self):
        path = os.path.join(self.dir, "bad.json")
        with open(path, "w", encoding="utf-8") as h:
            h.write("{not valid json")
        self.assertEqual(util.read_json(path, default=[]), [])


class StampTests(unittest.TestCase):
    def test_make_stamp_format(self):
        dt = datetime(2026, 7, 18, 15, 30, 0)
        self.assertEqual(util.make_stamp(dt), "2026-07-18T153000")

    def test_roundtrip(self):
        dt = datetime(2026, 12, 1, 9, 5, 7)
        stamp = util.make_stamp(dt)
        parsed = util.parse_stamp(stamp)
        self.assertEqual(parsed, dt)

    def test_rejects_malformed(self):
        for bad in [
            "2026-7-18T153000",       # single-digit month
            "2026-07-18T15300",       # 5 digits
            "2026-07-18t153000",      # lowercase t
            "2026-07-18T153000extra",
            "prefix2026-07-18T153000",
            "2026-07-18 153000",
            "not-a-stamp",
            "26-07-18T153000",        # 2-digit year
            "",
        ]:
            self.assertIsNone(util.parse_stamp(bad), bad)

    def test_rejects_invalid_calendar(self):
        # Correct shape but not a real date.
        self.assertIsNone(util.parse_stamp("2026-13-01T000000"))


class RunTests(unittest.TestCase):
    def test_timeout_sets_timed_out(self):
        result = util.run(["/bin/sleep", "5"], timeout=0.5)
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.rc)

    def test_success(self):
        result = util.run(["/bin/echo", "hi"], timeout=5)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.out.strip(), "hi")

    def test_input_text(self):
        result = util.run(["/bin/cat"], timeout=5, input_text="payload")
        self.assertFalse(result.timed_out)
        self.assertEqual(result.out, "payload")

    def test_nonzero_rc(self):
        result = util.run(["/bin/sh", "-c", "exit 3"], timeout=5)
        self.assertEqual(result.rc, 3)

    def test_missing_binary_still_raises(self):
        # Spawn failure must still raise (unchanged contract), not return a
        # RunResult -- callers rely on it.
        with self.assertRaises(OSError):
            util.run(["/nonexistent/mycloud-binary-xyz"], timeout=5)


class _UnkillableFake(object):
    """A Popen that never dies: communicate() times out, poll() stays None."""

    def __init__(self, pid=424242):
        self.pid = pid
        self.returncode = None
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.poll_calls = 0

    def communicate(self, input=None, timeout=None):
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)

    def poll(self):
        self.poll_calls += 1
        return None      # uninterruptible / D-state: never reaps

    def kill(self):
        pass


class RunTimeoutKillTests(unittest.TestCase):
    """The unwedgeable-timeout path: killpg the group, bounded reap, ABANDON a
    child that ignores SIGKILL so the caller is never blocked."""

    def setUp(self):
        del util._ABANDONED[:]        # isolate module-global state per test

    def tearDown(self):
        del util._ABANDONED[:]

    def test_timeout_abandons_unkillable_child(self):
        fake = _UnkillableFake(pid=424242)
        killpg_calls = []
        with mock.patch.object(util.subprocess, "Popen", return_value=fake), \
             mock.patch.object(util.os, "getpgid", return_value=424242), \
             mock.patch.object(util.os, "killpg",
                               side_effect=lambda pg, sig: killpg_calls.append((pg, sig))), \
             mock.patch.object(util, "_REAP_TIMEOUT", 0.1), \
             mock.patch.object(util, "_REAP_POLL", 0.02):
            result = util.run(["/sbin/mount_smbfs", "//x/y", "/mnt"], timeout=0.05)
        # Returned at once with the timeout contract intact.
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.rc)
        self.assertEqual(result.out, "")
        self.assertEqual(result.err, "")
        # The whole GROUP was SIGKILLed (mount_smbfs forks die with it).
        self.assertEqual(killpg_calls, [(424242, signal.SIGKILL)])
        # It was abandoned (not blocked on), and the bounded reap actually polled.
        self.assertIn(fake, util._ABANDONED)
        self.assertGreaterEqual(fake.poll_calls, 1)

    def test_abandon_logs_warning_with_pid(self):
        fake = _UnkillableFake(pid=777001)
        logger = _RecLogger()
        with mock.patch.object(util.subprocess, "Popen", return_value=fake), \
             mock.patch.object(util.os, "getpgid", return_value=777001), \
             mock.patch.object(util.os, "killpg", side_effect=lambda pg, sig: None), \
             mock.patch.object(util, "_LOG", logger), \
             mock.patch.object(util, "_REAP_TIMEOUT", 0.06), \
             mock.patch.object(util, "_REAP_POLL", 0.02):
            util.run(["/sbin/mount_smbfs"], timeout=0.02)
        joined = " ".join(logger.warnings)
        self.assertIn("777001", joined)          # lingering pid named
        self.assertIn("abandoning", joined)

    def test_reap_abandoned_sweeps_dead_children(self):
        class _Dying(object):
            def __init__(self):
                self.alive = True

            def poll(self):
                return None if self.alive else 0

        f = _Dying()
        util._ABANDONED.append(f)
        util._reap_abandoned()
        self.assertIn(f, util._ABANDONED)        # still alive -> kept
        f.alive = False
        util._reap_abandoned()
        self.assertNotIn(f, util._ABANDONED)     # now dead -> swept

    def test_real_timeout_kills_group_and_reaps_not_abandoned(self):
        calls = []
        real_killpg = os.killpg

        def spy(pg, sig):
            calls.append((pg, sig))
            return real_killpg(pg, sig)

        with mock.patch.object(util.os, "killpg", side_effect=spy):
            result = util.run(["/bin/sleep", "5"], timeout=0.3)
        self.assertTrue(result.timed_out)
        self.assertTrue(calls)                       # killpg was invoked
        self.assertEqual(calls[0][1], signal.SIGKILL)
        self.assertEqual(util._ABANDONED, [])        # killable -> reaped, not abandoned


class _RecLogger(object):
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args):
        self.warnings.append((msg % args) if args else msg)


if __name__ == "__main__":
    unittest.main()


class RemoveLocalTreeTests(unittest.TestCase):
    """util.remove_local_tree: the installer/uninstaller's recursive delete.
    Not shutil.rmtree (confined to trash.py by test_invariants): it refuses,
    before deleting anything, if any directory in the tree is a mountpoint."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.tree = os.path.join(self.dir, "tree")
        os.makedirs(os.path.join(self.tree, "a", "b"))
        with open(os.path.join(self.tree, "a", "b", "f.txt"), "w") as h:
            h.write("x")
        self.outside = os.path.join(self.dir, "outside")
        os.makedirs(self.outside)
        with open(os.path.join(self.outside, "keep.txt"), "w") as h:
            h.write("keep")
        # a symlink to a directory OUTSIDE the tree must be unlinked, never followed
        os.symlink(self.outside, os.path.join(self.tree, "link"))

    def test_removes_tree_without_following_symlinks(self):
        util.remove_local_tree(self.tree)
        self.assertFalse(os.path.lexists(self.tree))
        self.assertTrue(os.path.isfile(os.path.join(self.outside, "keep.txt")))

    @unittest.skipUnless(os.path.exists("/usr/bin/hdiutil"), "needs hdiutil")
    def test_refuses_when_a_real_mount_is_inside_and_deletes_nothing(self):
        # A REAL mount (a tiny disk image) inside the tree -- the situation of
        # the daemon's share on <APP>/mnt. Nothing on either side may go.
        import subprocess
        img = os.path.join(self.dir, "t.dmg")
        subprocess.run(["/usr/bin/hdiutil", "create", "-size", "1m", "-fs", "HFS+",
                        "-volname", "T", "-layout", "NONE", img],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        mnt = os.path.join(self.tree, "a", "mnt")
        os.makedirs(mnt)
        subprocess.run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-mountpoint", mnt, img],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(subprocess.run, ["/usr/bin/hdiutil", "detach", mnt, "-force"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(os.path.join(mnt, "on-the-share.txt"), "w") as h:
            h.write("precious")
        with self.assertRaises(OSError):
            util.remove_local_tree(self.tree)
        self.assertTrue(os.path.isfile(os.path.join(mnt, "on-the-share.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.tree, "a", "b", "f.txt")))

    def test_refuses_when_root_itself_is_a_mountpoint(self):
        with mock.patch("os.path.ismount", return_value=True):
            with self.assertRaises(OSError):
                util.remove_local_tree(self.tree)
        self.assertTrue(os.path.isdir(self.tree))

    def test_missing_path_is_not_an_error(self):
        util.remove_local_tree(os.path.join(self.dir, "nope"))

    def test_unreadable_subtree_aborts_instead_of_being_skipped(self):
        secret = os.path.join(self.tree, "a", "secret")
        os.makedirs(secret)
        os.chmod(secret, 0o000)
        try:
            with self.assertRaises(OSError):
                util.remove_local_tree(self.tree)
            self.assertTrue(os.path.isfile(os.path.join(self.tree, "a", "b", "f.txt")))
        finally:
            os.chmod(secret, 0o700)
