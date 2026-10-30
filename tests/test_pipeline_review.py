import tests._path  # noqa: F401

"""Regressions from the 2026-09-26 sync-pipeline review ("守护进程时常失败导致
无法同步").

Each test pins one fix:
- iCloud downloads decide "blocked" from a 4-file probe, not a whole batch (a
  500-file online batch at 120s/file took ~4h to say so -- the 174-minute hang);
- a download stage stops for a lost mount, and the pass ends so recovery runs;
- recovery consumes the request that triggered it (no second, redundant
  unmount/remount/full pass on the next tick);
- failed_files drops entries that synced or vanished (it only ever grew);
- a NAS walk error voids a folder's deep-verify verdict (no false re-upload
  storm from a silently skipped directory);
- NAS verification of a partial batch stops when the mount is going away;
- the hot-scan walker records any OSError per subtree instead of losing the
  whole snapshot;
- rsync liveness keeps the GUI's last-transfer stamp moving;
- a prune the guard cut short is retried the same day.
"""

import errno
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from mycloud import config as config_mod
from mycloud import icloud
from mycloud import scanner
from mycloud.daemon import (
    Daemon, SyncFailure, _AnyEvent, _MATERIALIZE_PROBE_FILES)
from mycloud.trash import PruneResult

from tests.test_prefetch import PrefetchBase, _BIG_FREE
from tests.test_v2_daemon import V2Base


class AnyEventTests(unittest.TestCase):
    def test_set_when_any_member_is(self):
        a, b = threading.Event(), threading.Event()
        e = _AnyEvent(a, b)
        self.assertFalse(e.is_set())
        self.assertFalse(e.wait(0.05))
        threading.Timer(0.1, b.set).start()
        t0 = time.monotonic()
        self.assertTrue(e.wait(5))
        self.assertLess(time.monotonic() - t0, 2)
        self.assertTrue(e.is_set())


class ProbeFirstDownloadTests(PrefetchBase):
    def _spy(self, fail_all=False):
        sizes = []
        orig = icloud.materialize_batch

        def _fake(abspaths, **kw):
            sizes.append(len(abspaths))
            if fail_all:
                return [], list(abspaths)
            return orig(abspaths, **kw)

        icloud.materialize_batch = _fake
        self.addCleanup(setattr, icloud, "materialize_batch", orig)
        return sizes

    def test_blocked_is_decided_by_a_probe_not_a_whole_batch(self):
        self._patch_free(_BIG_FREE)
        for i in range(40):
            self._mk("f%02d.key" % i)
        sizes = self._spy(fail_all=True)
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        self.assertEqual(sizes, [_MATERIALIZE_PROBE_FILES])   # the other 36 untouched
        self.assertTrue(d._materialize_blocked)
        entry = self.state.get()["folders"]["Alpha"]
        self.assertEqual(entry["icloud_skipped"]["count"], 40)
        self.assertEqual(self.state.get().get("prefetch_skipped"),
                         _MATERIALIZE_PROBE_FILES)

    def test_a_working_probe_downloads_the_rest_then_skips_later_probes(self):
        self._patch_free(_BIG_FREE)
        for i in range(10):
            self._mk("f%02d.key" % i)
        sizes = self._spy()
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        self.assertEqual(sizes, [_MATERIALIZE_PROBE_FILES, 10 - _MATERIALIZE_PROBE_FILES])
        self.assertEqual(len(self._mirror_files()), 10)
        self.assertTrue(d._materialize_proven)
        for i in range(10, 20):
            self._mk("f%02d.key" % i)
        del sizes[:]
        self.assertTrue(d.full_pass(None))
        self.assertEqual(sizes, [10])                 # proven: no probe split

    def test_offline_prefetch_probes_too(self):
        self._patch_free(_BIG_FREE)
        for i in range(30):
            self._mk("f%02d.key" % i)
        sizes = self._spy(fail_all=True)
        d = self._daemon()
        session = d._enumerate_prefetch()
        self.assertEqual(d._run_prefetch_batch(session), "blocked")
        self.assertEqual(sizes, [_MATERIALIZE_PROBE_FILES])
        self.assertTrue(d._materialize_blocked)

    def test_download_stage_stops_for_a_lost_mount_and_the_pass_recovers(self):
        self._patch_free(_BIG_FREE)
        for i in range(12):
            self._mk("f%02d.key" % i)
        d = self._daemon()
        attempted = []

        def _read(path):
            attempted.append(path)
            d._recovery_needed.set()   # the watchdog: mount gone mid-download

        d.read_fn = _read
        d._materialize_timeout = 0.3
        with self.assertRaises(SyncFailure):
            d.full_pass(None)
        self.assertLessEqual(len(attempted), _MATERIALIZE_PROBE_FILES)
        self.assertFalse(d._materialize_blocked)      # a stop is not "blocked"
        self.assertIsNone(self.state.get().get("prefetch_skipped"))


class RecoveryRequestTests(V2Base):
    def _recoverable(self):
        d = self._daemon()
        d._try_online_gated = lambda: True
        return d

    def test_recovery_consumes_the_request_that_triggered_it(self):
        d = self._recoverable()
        d._recovery_needed.set()
        d.full_pass = lambda scope=None: True
        d.recovery()
        self.assertFalse(d._recovery_needed.is_set(),
                         "a stale request would unmount the fresh mount next tick")

    def test_a_request_raised_during_the_recovery_pass_survives(self):
        d = self._recoverable()

        def _pass(scope=None):
            d._recovery_needed.set()
            return True

        d.full_pass = _pass
        d.recovery()
        self.assertTrue(d._recovery_needed.is_set())

    def test_the_pass_ends_at_the_next_folder_once_recovery_is_requested(self):
        beta = os.path.join(self.tmp, "src", "Beta")
        os.makedirs(beta)
        with open(os.path.join(beta, "b.txt"), "w") as h:
            h.write("b")
        self.cfg.folders.append(config_mod.FolderSpec(Path(beta), "Beta"))
        self._mk("a.txt", "a")
        d = self._daemon()
        orig = d._sync_folder

        def _sync(folder, subtrees, stamp):
            ok = orig(folder, subtrees, stamp)
            d._recovery_needed.set()
            return ok

        d._sync_folder = _sync
        with self.assertRaises(SyncFailure):
            d.full_pass(None)
        self.assertTrue(os.path.exists(self._mirror("a.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.data_root, "Beta", "b.txt")))
        self.assertFalse(d._syncing.is_set())


class FailedFilesTests(V2Base):
    def test_resolved_failures_leave_the_list_and_live_ones_stay(self):
        self._mk("ok.txt", "fine")
        dead = os.path.join(self.src, "dead.lnk")
        os.symlink("/nonexistent/mycloud-target", dead)
        self.state.folder_update("Alpha",
                                 failed_files=["ok.txt", "gone.txt", "dead.lnk"])
        d = self._daemon()
        d.full_pass(None)
        failed = self.state.get()["folders"]["Alpha"]["failed_files"]
        # ok.txt landed, gone.txt no longer exists; the dangling link is still
        # a real, current problem.
        self.assertEqual(failed, ["dead.lnk"])


class DeepVerifyWalkErrorTests(V2Base):
    def test_an_unreadable_nas_directory_voids_the_folders_verdict(self):
        for i in range(3):
            self._mk("sub/f%d.txt" % i, "x")
        self._mk("top.txt", "t")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        sub = self._mirror("sub")
        os.chmod(sub, 0)
        self.addCleanup(os.chmod, sub, 0o755)
        summary = d.deep_verify()
        self.assertEqual(summary["missing_uploaded"], 0,
                         "files under an unreadable dir read as missing")
        self.assertTrue(any("NAS 目录读取出错" in e for e in summary["errors"]),
                        summary["errors"])
        self.assertEqual(self.manifest.counts("Alpha")["pending"], 0)

    def test_a_missing_mirror_root_is_still_a_missing_mirror(self):
        self._mk("a.txt", "a")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        shutil.rmtree(os.path.join(self.data_root, "Alpha"))
        summary = d.deep_verify()
        self.assertEqual(summary["missing_uploaded"], 1)


class NasVerificationAbortTests(unittest.TestCase):
    def test_verification_stops_when_told(self):
        batch = [("f%d" % i, 1, 0, 0) for i in range(100)]
        asks = []

        def _abort():
            asks.append(1)
            return len(asks) > 1

        with mock.patch("mycloud.daemon.os.lstat",
                        side_effect=OSError(errno.ENOENT, "gone")) as lst:
            verified = Daemon._verify_batch_on_nas("/nowhere", batch,
                                                   should_abort=_abort)
        self.assertEqual(verified, set())
        self.assertEqual(lst.call_count, 25)

    def test_a_stalling_lstat_ends_verification(self):
        batch = [("f%d" % i, 1, 0, 0) for i in range(50)]
        clock = {"t": 0.0}

        def _slow_lstat(path):
            clock["t"] += 30.0          # every call "takes" 30s
            raise OSError(errno.ETIMEDOUT, "timed out")

        with mock.patch("mycloud.daemon.os.lstat", side_effect=_slow_lstat) as lst, \
                mock.patch("mycloud.daemon.time.monotonic", side_effect=lambda: clock["t"]):
            Daemon._verify_batch_on_nas("/nowhere", batch)
        self.assertEqual(lst.call_count, 1)


class SnapshotWalkErrorTests(unittest.TestCase):
    def test_any_oserror_is_recorded_and_the_walk_continues(self):
        root = tempfile.mkdtemp(prefix="mycloud-scan-")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "good", "deeper"))
        os.makedirs(os.path.join(root, "bad"))
        real = os.scandir

        def _scandir(path):
            if os.fspath(path).endswith(os.sep + "bad"):
                raise OSError(errno.EIO, "Input/output error", path)
            return real(path)

        with mock.patch("mycloud.scanner.os.scandir", side_effect=_scandir):
            snap, errors = scanner.snapshot_walk([root], [])
        self.assertIn(os.path.join(root, "good", "deeper"), snap)
        self.assertTrue(any(e.endswith(os.sep + "bad") for e in errors), errors)


class TransferStampTests(V2Base):
    def test_rsync_liveness_moves_the_transfer_stamp(self):
        d = self._daemon()
        d._last_transfer_at = None
        d._on_transfer_alive()
        self.assertIsNotNone(d._last_transfer_at)

    def test_heartbeat_restamps_with_the_fresh_transfer_time(self):
        d = self._daemon()
        d._progress_hb_interval = 0.02
        d._progress_last = {"active": True, "phase": "transferring",
                            "last_transfer_at": "2026-01-01T00:00:00+00:00"}
        d._last_transfer_at = "2026-09-26T01:02:03+00:00"
        d._syncing.set()
        d._start_progress_heartbeat()
        try:
            deadline = time.monotonic() + 3
            seen = None
            while time.monotonic() < deadline:
                seen = (self.state.get().get("progress") or {}).get("last_transfer_at")
                if seen == d._last_transfer_at:
                    break
                time.sleep(0.02)
            self.assertEqual(seen, "2026-09-26T01:02:03+00:00")
        finally:
            d._stop.set()
            d._join_progress_heartbeat()


class DailyPruneRetryTests(V2Base):
    def test_a_prune_cut_short_by_the_guard_is_retried_the_same_day(self):
        d = self._daemon()
        d.trash_mgr.prune = lambda guard_cb, now_utc: PruneResult(
            [], 0, ["guard_failed: aborting before x"])
        d._daily_prune()
        self.assertIsNone(self.state.get().get("last_prune_date"))
        d.trash_mgr.prune = lambda guard_cb, now_utc: PruneResult([], 0, [])
        d._daily_prune()
        self.assertEqual(self.state.get().get("last_prune_date"),
                         d._now().date().isoformat())


class ConfigErrorReportingTests(unittest.TestCase):
    def test_a_config_error_reaches_state_json(self):
        import logging
        from mycloud import daemon as daemon_mod
        from mycloud import notify as notify_mod
        from mycloud import paths
        from mycloud.state import StateStore

        tmp = tempfile.mkdtemp(prefix="mycloud-cfgerr-")
        self.addCleanup(shutil.rmtree, tmp, True)
        app = os.path.join(tmp, "app")
        runtime = os.path.join(app, "runtime")
        bad = os.path.join(tmp, "config.ini")
        with open(bad, "w") as h:
            h.write("[backup]\nwatch_mode = sometimes\n")
        quiet = logging.getLogger("mycloud.test_cfgerr")
        quiet.addHandler(logging.NullHandler())
        quiet.propagate = False
        with mock.patch.object(paths, "APP", Path(app)), \
                mock.patch.object(paths, "RUNTIME", Path(runtime)), \
                mock.patch.object(paths, "LOGDIR", Path(os.path.join(tmp, "logs"))), \
                mock.patch.object(notify_mod, "setup_logging", lambda *a, **k: quiet), \
                mock.patch("sys.stderr"):
            rc = daemon_mod.run_daemon(bad, once=True)
        self.assertEqual(rc, 1)
        st = StateStore(runtime).get()
        self.assertEqual(st.get("state"), "stopped")
        self.assertIn("配置文件有误", st.get("last_error") or "")
        self.assertIn("watch_mode", st.get("last_error") or "")


if __name__ == "__main__":
    unittest.main()
