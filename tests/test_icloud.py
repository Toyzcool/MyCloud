import tests._path  # noqa: F401

"""Unit tests for the iCloud policy primitives (PLAN section 14, M4).

Tests cannot create real dataless files, so the predicate is exercised with a
fake stat object, and materialization uses an injected read trigger + a
predicate that flips after N polls."""

import os
import shutil
import tempfile
import threading
import unittest

from mycloud import icloud
from mycloud import paths
from mycloud.util import RunResult


class _FakeStat(object):
    def __init__(self, flags):
        self.st_flags = flags


class TestIsDataless(unittest.TestCase):
    def test_dataless_bit_detected(self):
        self.assertTrue(icloud.is_dataless(
            "x", _FakeStat(icloud.SF_DATALESS | 0x40)))

    def test_not_dataless(self):
        self.assertFalse(icloud.is_dataless("x", _FakeStat(0x40)))

    def test_real_regular_file_is_not_dataless(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        p = os.path.join(tmp, "f")
        with open(p, "w") as h:
            h.write("hi")
        self.assertFalse(icloud.is_dataless(p))

    def test_dataless_check_adapter(self):
        self.assertTrue(icloud.dataless_check("x", _FakeStat(icloud.SF_DATALESS)))


class TestDiskGuard(unittest.TestCase):
    def test_zero_need_always_ok(self):
        self.assertTrue(icloud.disk_guard_ok("/", 0))

    def test_small_need_ok(self):
        self.assertTrue(icloud.disk_guard_ok("/", 1024))

    def test_huge_need_refused(self):
        # 3x an exabyte cannot fit; must refuse.
        self.assertFalse(icloud.disk_guard_ok("/", 10 ** 18))


class TestEvictAvailability(unittest.TestCase):
    def test_absent_when_help_lacks_evict(self):
        # This machine's real brctl: no evict subcommand.
        def _run(argv, timeout):
            return RunResult(0, "diagnose\nlog\ndump\nstatus\nquota\n", "", False)
        self.assertFalse(icloud.evict_available(_run))

    def test_present_when_help_lists_evict(self):
        def _run(argv, timeout):
            return RunResult(0, "evict [<path>]\n    evict a file\n", "", False)
        self.assertTrue(icloud.evict_available(_run))

    def test_absent_on_timeout(self):
        def _run(argv, timeout):
            return RunResult(None, "", "", True)
        self.assertFalse(icloud.evict_available(_run))

    def test_evict_paths_reports_errors(self):
        calls = []

        def _run(argv, timeout):
            calls.append(argv)
            # Fail the second path.
            if argv[-1].endswith("bad"):
                return RunResult(1, "", "no", False)
            return RunResult(0, "", "", False)

        evicted, errors = icloud.evict_paths(["/x/good", "/x/bad"], _run)
        self.assertEqual(evicted, ["/x/good"])
        self.assertEqual(len(errors), 1)


def _real_files(test, *names):
    """materialize_batch only counts a path that exists: create them."""
    tmp = tempfile.mkdtemp(prefix="mycloud-icloud-")
    test.addCleanup(shutil.rmtree, tmp, True)
    out = []
    for name in names:
        path = os.path.join(tmp, name)
        with open(path, "w") as handle:
            handle.write("x")
        out.append(path)
    return out


class TestMaterializeBatch(unittest.TestCase):
    def _flip_after(self, n):
        """Return an is_dataless_fn returning True for the first n polls/file."""
        counts = {}
        lock = threading.Lock()

        def _fn(path):
            with lock:
                c = counts.get(path, 0)
                counts[path] = c + 1
            return c < n
        return _fn

    def test_flips_after_n_polls(self):
        reads = []
        a, b = _real_files(self, "a", "b")
        mat, failed = icloud.materialize_batch(
            [a, b],
            is_dataless_fn=self._flip_after(2),
            read_fn=lambda p: reads.append(p),
            poll_interval=0, per_file_timeout=5.0)
        self.assertEqual(sorted(mat), [a, b])
        self.assertEqual(failed, [])
        self.assertEqual(sorted(reads), [a, b])  # each triggered once

    def test_missing_path_is_never_materialized(self):
        # is_dataless() reads a missing path as False; a macOS <= 13
        # placeholder target (X absent, .X.icloud present) used to count as
        # downloaded at once, without a byte moving.
        gone = []
        mat, failed = icloud.materialize_batch(
            ["/nonexistent/dir/report.pdf"], is_dataless_fn=icloud.is_dataless,
            read_fn=lambda p: None, poll_interval=0, per_file_timeout=0.05,
            gone=gone)
        self.assertEqual(mat, [])
        # Vanished since listed: neither downloaded nor a failed download (a
        # moved folder must not read as the launchd restriction).
        self.assertEqual(failed, [])
        self.assertEqual(gone, ["/nonexistent/dir/report.pdf"])

    def test_a_placeholder_target_that_never_lands_fails(self):
        tmp = tempfile.mkdtemp(prefix="mycloud-icloud-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, ".r.pdf.icloud"), "wb") as h:
            h.write(b"bplist00")
        target = os.path.join(tmp, "r.pdf")
        gone = []
        mat, failed = icloud.materialize_batch(
            [target], is_dataless_fn=icloud.is_dataless, read_fn=lambda p: None,
            poll_interval=0, per_file_timeout=0.05, gone=gone)
        self.assertEqual((mat, failed, gone), ([], [target], []))

    def test_brctl_refusing_a_placeholder_is_rejected_not_timed_out(self):
        tmp = tempfile.mkdtemp(prefix="mycloud-icloud-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, ".r.pdf.icloud"), "wb") as h:
            h.write(b"bplist00")
        target = os.path.join(tmp, "r.pdf")
        calls = []

        def run_cmd(argv, timeout):
            calls.append(argv[-1])
            return RunResult(1, "", "unknown item", False)

        rejected = []
        mat, failed = icloud.materialize_batch(
            [target], is_dataless_fn=icloud.is_dataless, run_cmd=run_cmd,
            poll_interval=0, per_file_timeout=5.0, rejected=rejected)
        self.assertEqual((mat, failed, rejected), ([], [target], [target]))
        self.assertEqual(calls, [target, os.path.join(tmp, ".r.pdf.icloud")])

    def test_never_clears_times_out_to_failed(self):
        stuck, = _real_files(self, "stuck")
        mat, failed = icloud.materialize_batch(
            [stuck],
            is_dataless_fn=lambda p: True,          # never materializes
            read_fn=lambda p: None,
            poll_interval=0, per_file_timeout=0.05)
        self.assertEqual(mat, [])
        self.assertEqual(failed, [stuck])

    def test_stop_event_aborts(self):
        stop = threading.Event()
        stop.set()
        mat, failed = icloud.materialize_batch(
            ["/a"],
            is_dataless_fn=lambda p: True,
            read_fn=lambda p: None,
            stop_event=stop, poll_interval=0, per_file_timeout=5.0)
        self.assertEqual(mat, [])
        self.assertEqual(failed, ["/a"])

    def test_empty_batch(self):
        self.assertEqual(icloud.materialize_batch([]), ([], []))


class TestDefaultReadTrigger(unittest.TestCase):
    """The production read trigger is a SUBPROCESS 1-byte read bounded by the
    per-file timeout (NOT an in-thread open/read that a kernel-stuck iCloud read
    could wedge forever, starving the worker pool -- the >2h-stall bug)."""

    def test_default_trigger_spawns_head_with_timeout(self):
        calls = []
        materialized = set()

        def run_cmd(argv, timeout):
            calls.append((list(argv), timeout))
            materialized.add(argv[-1])   # a successful 1-byte read = downloaded
            return RunResult(0, "x", "", False)

        a, b = _real_files(self, "a", "b")
        mat, failed = icloud.materialize_batch(
            [a, b], is_dataless_fn=lambda p: p not in materialized,
            run_cmd=run_cmd, poll_interval=0, per_file_timeout=5.0)
        self.assertEqual(sorted(mat), [a, b])
        self.assertEqual(failed, [])
        self.assertEqual(len(calls), 2)
        for argv, timeout in calls:
            self.assertEqual(argv[0], paths.HEAD)
            self.assertEqual(argv[1:3], ["-c", "1"])
            self.assertEqual(argv[3], argv[3])       # path is the last arg
            self.assertEqual(timeout, 5.0)           # bounded by per_file_timeout

    def test_default_trigger_timeout_marks_file_failed(self):
        # A wedged read: the subprocess times out and the file never clears -> it
        # is SKIPPED (failed), never retried forever and never blocking the pool.
        def run_cmd(argv, timeout):
            return RunResult(None, "", "", True)     # timed_out

        stuck, = _real_files(self, "stuck")
        mat, failed = icloud.materialize_batch(
            [stuck], is_dataless_fn=lambda p: True, run_cmd=run_cmd,
            poll_interval=0, per_file_timeout=0.05)
        self.assertEqual(mat, [])
        self.assertEqual(failed, [stuck])

    def test_placeholder_target_asks_brctl_to_download(self):
        # macOS <= 13: X does not exist, only .X.icloud -- reading X cannot
        # start a download, brctl can. The download lands X; then it counts.
        tmp = tempfile.mkdtemp(prefix="mycloud-icloud-")
        self.addCleanup(shutil.rmtree, tmp, True)
        target = os.path.join(tmp, "report.pdf")
        stub = os.path.join(tmp, ".report.pdf.icloud")
        with open(stub, "wb") as handle:
            handle.write(b"bplist00")
        calls = []

        def run_cmd(argv, timeout):
            calls.append(list(argv))
            if argv[:2] == [paths.BRCTL, "download"]:
                os.rename(stub, target)
            return RunResult(0, "", "", False)

        mat, failed = icloud.materialize_batch(
            [target], is_dataless_fn=icloud.is_dataless, run_cmd=run_cmd,
            poll_interval=0, per_file_timeout=5.0)
        self.assertEqual(calls, [[paths.BRCTL, "download", target]])
        self.assertEqual((mat, failed), ([target], []))


if __name__ == "__main__":
    unittest.main()
