import tests._path  # noqa: F401

"""Offline iCloud prefetch tests (PLAN section 14, M4).

The NAS is unreachable (the mount stub never mounts; tcp445 reachability is
controllable). With icloud_policy=download and dataless files pending, the
daemon materializes them from iCloud a batch at a time during the reconnect
backoff, then the normal pass uploads them once home. iCloud predicates are
injected (tests cannot create real dataless files); local free space is patched
so the 25 GiB floor and the >=3x-batch guard are deterministic regardless of the
test machine's real disk.

Also covers the interplay fixes on the ONLINE download pipeline (4a icloud_skipped
populated at plan time + updated per batch; 4b per-batch INFO log; 4c a
timed-out file is skipped and surfaced as prefetch_skipped)."""

import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mycloud import config as config_mod
from mycloud import icloud
from mycloud import paths
from mycloud.daemon import Daemon, _PREFETCH_BATCH_FILES
from mycloud.engine import SyncEngine
from mycloud.manifest import Manifest
from mycloud.state import StateStore
from mycloud.trash import TrashManager

_LOG = logging.getLogger("mycloud.test_prefetch")
_LOG.addHandler(logging.NullHandler())

_BIG_FREE = 500 * 2 ** 30  # comfortably above the 25 GiB prefetch floor.


class _PrefetchMount(object):
    """Offline mount: never mounts; tcp445 reachability is controllable."""

    def __init__(self, mountpoint, reachable=False):
        self.mountpoint = Path(mountpoint)
        self.reachable = reachable
        self.mount_calls = 0
        self._mounted = False

    def probe_tcp445(self, timeout=3.0):
        return self.reachable

    def probe_reachable(self, timeout=3.0):
        # _try_online_gated now gates on reachability (socket OR brokered SMB);
        # the fake models both with the single `reachable` flag.
        return self.reachable

    def guard_ok(self, timeout=10):
        return self._mounted

    def is_mounted(self, timeout=10):
        return self._mounted

    def sentinel_ok(self, timeout=10):
        return True

    def session_ok(self, timeout=10):
        return True

    def mount(self):
        self.mount_calls += 1
        return self._mounted

    def force_unmount(self):
        self._mounted = False

    def ensure_bare_dir_safe(self):
        pass


class _RecLogger(object):
    def __init__(self):
        self.records = []

    def _mk(self, level):
        def _f(msg, *args):
            try:
                self.records.append((level, (msg % args) if args else msg))
            except Exception:
                self.records.append((level, str(msg)))
        return _f

    def __getattr__(self, name):
        return self._mk(name)

    def msgs(self, level=None):
        return [m for lv, m in self.records if level is None or lv == level]


class _Notifier(object):
    def __init__(self):
        self.calls = []

    def notify(self, cls, title, body, min_hours=6.0, force=False):
        self.calls.append((cls, body))
        return True

    def classes(self):
        return [c for c, _b in self.calls]


class PrefetchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mycloud-prefetch-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, "src", "Alpha")
        os.makedirs(self.src)
        self.fakenas = os.path.join(self.tmp, "fakenas")
        os.makedirs(self.fakenas)
        self.runtime = os.path.join(self.tmp, "runtime")
        os.makedirs(self.runtime)
        self.excludes = os.path.join(self.runtime, "excludes.txt")

        self.cfg = config_mod.Config()
        # Serial uploads keep batch-order-dependent tests deterministic;
        # concurrency-specific tests raise this themselves.
        self.cfg.upload_concurrency = 1
        self.cfg.folders = [config_mod.FolderSpec(Path(self.src), "Alpha")]
        self.cfg.excludes = []
        self.cfg.icloud_policy = "download"
        config_mod.write_excludes(self.cfg, self.excludes)

        self.state = StateStore(self.runtime, logger=_LOG)
        self.engine = SyncEngine(paths.RSYNC, self.excludes, _LOG,
                                 proc_registry=set())
        self.mount = _PrefetchMount(self.fakenas)
        self.trash_root = os.path.join(self.fakenas, paths.TRASH_REL)
        self.trash_mgr = TrashManager(self.trash_root, 30, 50, _LOG)
        self.notifier = _Notifier()
        self.data_root = os.path.join(self.fakenas, paths.DATA_REL)
        self.manifest = Manifest(os.path.join(self.runtime, "manifest.db"))
        self.addCleanup(self.manifest.close)

        # A .key file is dataless until read_fn "downloads" it (adds to the set).
        self.materialized = set()

    # -- injected iCloud predicates ---------------------------------------
    def _dl_check(self, path, st):
        return path.endswith(".key") and path not in self.materialized

    def _is_dl(self, path):
        return path.endswith(".key") and path not in self.materialized

    def _read_fn(self, path):
        self.materialized.add(path)

    def _daemon(self, logger=None):
        return Daemon(self.cfg, self.state, self.mount, self.engine,
                      self.trash_mgr, self.notifier, logger or _LOG,
                      manifest=self.manifest, dataless_check=self._dl_check,
                      is_dataless_fn=self._is_dl, read_fn=self._read_fn)

    def _mk(self, rel, content="x"):
        path = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as h:
            h.write(content)
        return path

    def _mirror_files(self):
        root = os.path.join(self.data_root, "Alpha")
        out = []
        for dp, _d, fs in os.walk(root):
            for fn in fs:
                out.append(os.path.relpath(os.path.join(dp, fn), root))
        return sorted(out)

    def _patch_free(self, value):
        orig = icloud.free_bytes
        icloud.free_bytes = lambda p: value
        self.addCleanup(setattr, icloud, "free_bytes", orig)


class TestPrefetchTrigger(PrefetchBase):
    def test_enumerate_finds_pending_dataless_respecting_excludes(self):
        self._mk("a.key"); self._mk("b.key"); self._mk("normal.txt")
        self._mk("skip.key")
        self.cfg.excludes = ["skip.key"]
        d = self._daemon()
        session = d._enumerate_prefetch()
        rels = sorted(e[2] for e in session.entries)
        self.assertEqual(rels, ["a.key", "b.key"])  # normal excluded (not dataless), skip excluded (pattern)
        self.assertEqual(session.total_files, 2)
        self.assertGreater(session.total_bytes, 0)

    def test_no_dataless_yields_empty_session(self):
        self._mk("normal.txt")
        d = self._daemon()
        session = d._enumerate_prefetch()
        self.assertEqual(session.total_files, 0)
        self.assertTrue(session.exhausted)


class TestPrefetchBatchFlow(PrefetchBase):
    def test_200_file_batching_and_state_progress(self):
        self._patch_free(_BIG_FREE)
        for i in range(_PREFETCH_BATCH_FILES + 1):  # 201
            self._mk("f%03d.key" % i, "c%d" % i)
        d = self._daemon()
        session = d._enumerate_prefetch()
        self.assertEqual(session.total_files, _PREFETCH_BATCH_FILES + 1)

        out1 = d._run_prefetch_batch(session)
        self.assertEqual(out1, "continue")            # 200 done, 1 remains
        self.assertEqual(session.index, _PREFETCH_BATCH_FILES)
        self.assertEqual(session.done_files, _PREFETCH_BATCH_FILES)

        pf = self.state.get()["prefetch"]
        self.assertTrue(pf["active"])
        self.assertEqual(pf["done"], _PREFETCH_BATCH_FILES)
        self.assertEqual(pf["total"], _PREFETCH_BATCH_FILES + 1)

        prog = self.state.get()["progress"]
        self.assertEqual(prog["phase"], "downloading")
        self.assertEqual(prog["total_files"], _PREFETCH_BATCH_FILES + 1)
        self.assertIsNone(prog["current"])
        self.assertGreater(prog["bytes_total"], 0)

        out2 = d._run_prefetch_batch(session)
        self.assertEqual(out2, "done")                # last file, NAS still down
        self.assertEqual(session.done_files, _PREFETCH_BATCH_FILES + 1)

    def test_batch_logs_info_line(self):
        self._patch_free(_BIG_FREE)
        self._mk("a.key", "payload")
        log = _RecLogger()
        d = self._daemon(logger=log)
        session = d._enumerate_prefetch()
        d._run_prefetch_batch(session)
        self.assertIn("prefetch batch done", " ".join(log.msgs("info")))


class TestPrefetchReconnect(PrefetchBase):
    def test_nas_reachable_between_batches_returns_reconnect(self):
        self._patch_free(_BIG_FREE)
        self._mk("a.key"); self._mk("b.key")
        self.mount.reachable = True
        d = self._daemon()
        session = d._enumerate_prefetch()
        out = d._run_prefetch_batch(session)
        self.assertEqual(out, "reconnect")
        # The in-flight batch still completed before the reconnect (batch-atomic).
        self.assertEqual(session.done_files, 2)

    def test_ensure_online_prefetches_then_connects(self):
        self._patch_free(_BIG_FREE)
        self._mk("a.key"); self._mk("b.key")
        d = self._daemon()
        d._backoff_delay = lambda attempt: 0.01     # no real 30s wait
        # NAS unreachable at first (probe fails -> mount is gated OFF, prefetch
        # runs); once at least one file has materialized, the NAS becomes
        # reachable so the now-ungated mount is attempted and succeeds.
        def _bring_up():
            for _ in range(500):
                if self.materialized:
                    break
                time.sleep(0.005)
            self.mount.reachable = True
            self.mount._mounted = True      # mount() returns _mounted
        threading.Thread(target=_bring_up, daemon=True).start()
        self.assertTrue(d.ensure_online())
        self.assertGreaterEqual(self.mount.mount_calls, 1)
        # Prefetch ran during the offline window and downloaded both files.
        self.assertEqual(len(self.materialized), 2)
        # Reconnecting cleared the active prefetch flag for the GUI.
        self.assertFalse(self.state.get()["prefetch"]["active"])


class TestPrefetchDiskGuards(PrefetchBase):
    def test_disk_floor_halts_prefetch(self):
        self._patch_free(10 * 2 ** 30)              # 10 GiB < 25 GiB floor
        self._mk("a.key")
        d = self._daemon()
        session = d._enumerate_prefetch()
        out = d._run_prefetch_batch(session)
        self.assertEqual(out, "halt")
        self.assertEqual(session.done_files, 0)     # nothing downloaded
        self.assertEqual(len(self.materialized), 0)
        self.assertIn("prefetch_disk", self.notifier.classes())

    def test_three_x_batch_guard_halts_prefetch(self):
        self._patch_free(30 * 2 ** 30)              # above the floor...
        self._mk("a.key")
        d = self._daemon()
        orig = icloud.disk_guard_ok
        icloud.disk_guard_ok = lambda *a, **k: False   # ...but the 3x guard fails
        self.addCleanup(setattr, icloud, "disk_guard_ok", orig)
        session = d._enumerate_prefetch()
        out = d._run_prefetch_batch(session)
        self.assertEqual(out, "halt")
        self.assertEqual(session.done_files, 0)
        self.assertIn("prefetch_disk", self.notifier.classes())


class TestPrefetchCompletion(PrefetchBase):
    def test_completion_notifies_once_and_marks_inactive(self):
        self._patch_free(_BIG_FREE)
        self._mk("a.key")
        d = self._daemon()
        session = d._enumerate_prefetch()
        self.assertEqual(d._run_prefetch_batch(session), "done")
        d._maybe_finish_prefetch(session)
        d._maybe_finish_prefetch(session)            # idempotent: must not re-notify
        done = [b for c, b in self.notifier.calls if c == "prefetch_done"]
        self.assertEqual(len(done), 1)
        self.assertIn("云端文件已全部下载完成，回到家庭网络后将自动开始上传", done[0])
        self.assertFalse(self.state.get()["prefetch"]["active"])
        self.assertTrue(d._prefetch_complete)

    def test_offline_interval_runs_to_completion(self):
        self._patch_free(_BIG_FREE)
        self._mk("a.key"); self._mk("b.key")
        d = self._daemon()
        d._backoff_delay = lambda attempt: 0.01
        d._offline_interval(0)
        pf = self.state.get()["prefetch"]
        self.assertFalse(pf["active"])
        self.assertEqual(pf["done"], 2)
        self.assertEqual(pf["total"], 2)
        self.assertTrue(d._prefetch_complete)
        self.assertIn("prefetch_done", self.notifier.classes())

    def test_offline_interval_no_prefetch_under_skip_policy(self):
        self.cfg.icloud_policy = "skip"
        self._mk("a.key")
        d = self._daemon()
        d._backoff_delay = lambda attempt: 0.0
        d._offline_interval(0)
        self.assertIsNone(self.state.get().get("prefetch"))
        self.assertEqual(self.notifier.calls, [])


class TestPrefetchSkipPath(PrefetchBase):
    def test_timed_out_file_skipped_and_counted(self):
        self._patch_free(_BIG_FREE)
        self._mk("good.key"); self._mk("stuck.key")
        d = self._daemon()
        d._materialize_timeout = 0.05
        # good.key downloads; stuck.key never clears -> times out -> skipped.
        def _read(path):
            if path.endswith("good.key"):
                self.materialized.add(path)
        d.read_fn = _read
        session = d._enumerate_prefetch()
        d._run_prefetch_batch(session)
        self.assertEqual(session.done_files, 1)
        self.assertEqual(session.skipped, 1)
        self.assertEqual(self.state.get().get("prefetch_skipped"), 1)


class TestPrefetchResponsiveness(PrefetchBase):
    """Regression for the production hang: a slow/wedged materialize batch must
    NOT freeze the offline reconnect loop or daemon shutdown. A Python thread
    stuck in an uninterruptible iCloud read can't be cancelled, so the batch runs
    off-thread and the loop polls for stop / NAS-reachable at a bounded cadence.
    These tests FAIL (time out) against the old synchronous materialize."""

    def _block_materialize(self):
        """Replace icloud.materialize_batch with a stub that blocks until released
        (simulating a slow/wedged batch). Returns (started, release)."""
        import mycloud.icloud as icloud_mod
        started = threading.Event()
        release = threading.Event()
        orig = icloud_mod.materialize_batch

        def _blocking(abspaths, **kw):
            started.set()
            release.wait(10)
            return list(abspaths), []

        icloud_mod.materialize_batch = _blocking
        self.addCleanup(setattr, icloud_mod, "materialize_batch", orig)
        self.addCleanup(release.set)
        return started, release

    def test_slow_batch_does_not_block_reconnect(self):
        self._patch_free(_BIG_FREE)
        for i in range(3):
            self._mk("f%d.key" % i)
        d = self._daemon()
        d._backoff_delay = lambda a: 0.02
        d._prefetch_poll_interval = 0.02
        started, _release = self._block_materialize()

        # The NAS returns shortly after the (blocked) batch begins downloading.
        def _bring_nas_back():
            if started.wait(3):
                self.mount.reachable = True
                self.mount._mounted = True
        threading.Thread(target=_bring_nas_back, daemon=True).start()

        result = {}
        done = threading.Event()

        def _drive():
            try:
                result["ok"] = d.ensure_online()
            finally:
                done.set()
        threading.Thread(target=_drive, daemon=True).start()

        self.assertTrue(
            done.wait(5),
            "ensure_online hung: the offline loop blocked in the prefetch batch "
            "instead of reconnecting when the NAS returned")
        self.assertTrue(result.get("ok"))

    def test_stop_during_slow_batch_returns_promptly(self):
        self._patch_free(_BIG_FREE)
        for i in range(3):
            self._mk("f%d.key" % i)
        d = self._daemon()
        d._prefetch_poll_interval = 0.02
        started, _release = self._block_materialize()
        session = d._enumerate_prefetch()

        out = {}
        done = threading.Event()

        def _run():
            out["outcome"] = d._run_prefetch_batch(session)
            done.set()
        threading.Thread(target=_run, daemon=True).start()

        self.assertTrue(started.wait(3))
        d._stop.set()                         # daemon shutdown while batch blocks
        self.assertTrue(
            done.wait(3),
            "_run_prefetch_batch did not return after stop (batch blocked the "
            "offline loop / shutdown)")
        self.assertEqual(out["outcome"], "halt")


class TestStartupStaleLiveness(PrefetchBase):
    def test_startup_clears_stale_progress_and_prefetch(self):
        # A previous run left a "downloading" progress + active prefetch in
        # state.json; startup must neutralize both so they cannot masquerade as
        # live (requirement 3).
        self.state.update(
            progress={"active": True, "phase": "downloading", "files_done": 5,
                      "total_files": 100, "current": None},
            prefetch={"active": True, "done": 5, "total": 100})
        d = self._daemon()
        d._clear_stale_liveness_on_start()
        self.assertEqual(self.state.get()["progress"], {"active": False})
        self.assertFalse(self.state.get()["prefetch"]["active"])


class TestMaterializeBlocked(PrefetchBase):
    """Graceful degradation when the launchd-agent context cannot trigger iCloud
    materialization (fileproviderd refuses background download requests): a
    fully-skipped batch latches a blocked flag, warns/notifies once, surfaces
    state.prefetch.blocked, and stops attempting downloads -- offline and online."""

    def _stub_all_skipped(self):
        """Make icloud.materialize_batch return everything as failed/skipped."""
        import mycloud.icloud as icloud_mod
        orig = icloud_mod.materialize_batch
        calls = {"n": 0}

        def _all_skipped(abspaths, **kw):
            calls["n"] += 1
            return [], list(abspaths)

        icloud_mod.materialize_batch = _all_skipped
        self.addCleanup(setattr, icloud_mod, "materialize_batch", orig)
        return calls

    def test_offline_all_skipped_batch_marks_blocked(self):
        self._patch_free(_BIG_FREE)
        for i in range(3):
            self._mk("f%d.key" % i)
        log = _RecLogger()
        d = self._daemon(logger=log)
        self._stub_all_skipped()
        session = d._enumerate_prefetch()
        outcome = d._run_prefetch_batch(session)
        self.assertEqual(outcome, "blocked")
        self.assertTrue(d._materialize_blocked)
        self.assertTrue(self.state.get()["prefetch"]["blocked"])
        blocked = [b for c, b in self.notifier.calls if c == "prefetch_blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertIn("系统限制：后台进程无法触发 iCloud 下载，云端文件将在您手动打开"
                      "或另行下载后自动备份", blocked[0])
        warns = [m for m in log.msgs("warning")
                 if "launchd" in m or "fileproviderd" in m]
        self.assertEqual(len(warns), 1)   # exactly ONE explanatory warning

    def test_blocked_marks_once(self):
        d = self._daemon()
        d._mark_materialize_blocked("test")
        d._mark_materialize_blocked("test")   # idempotent
        blocked = [b for c, b in self.notifier.calls if c == "prefetch_blocked"]
        self.assertEqual(len(blocked), 1)

    def test_offline_interval_stops_prefetch_after_blocked(self):
        self._patch_free(_BIG_FREE)
        for i in range(3):
            self._mk("f%d.key" % i)
        d = self._daemon()
        d._backoff_delay = lambda a: 0.01
        calls = self._stub_all_skipped()
        d._offline_interval(0)                 # one batch -> blocked
        self.assertTrue(d._materialize_blocked)
        n = calls["n"]
        self.assertGreaterEqual(n, 1)
        d._offline_interval(1)                 # blocked -> plain backoff, no batch
        self.assertEqual(calls["n"], n)        # no further materialize attempts

    def test_online_all_skipped_marks_blocked_uploads_continue(self):
        self._patch_free(_BIG_FREE)
        self._mk("normal.txt", "hello")
        self._mk("a.key", "aa"); self._mk("b.key", "bbbb")
        d = self._daemon()
        self._stub_all_skipped()
        ok = d.full_pass(None)
        self.assertTrue(ok)                                    # pass NOT failed
        self.assertEqual(self._mirror_files(), ["normal.txt"])  # uploads continued
        self.assertTrue(d._materialize_blocked)
        self.assertTrue(self.state.get()["prefetch"]["blocked"])
        entry = self.state.get()["folders"]["Alpha"]
        self.assertEqual(entry["icloud_skipped"]["count"], 2)   # both .key pending
        self.assertEqual(entry["state"], "upToDate")            # not degraded

    def test_online_blocked_skips_download_stage_next_pass(self):
        self._patch_free(_BIG_FREE)
        self._mk("a.key", "aa")
        d = self._daemon()
        d._materialize_blocked = True          # already known blocked
        calls = self._stub_all_skipped()
        ok = d.full_pass(None)
        self.assertTrue(ok)
        self.assertEqual(calls["n"], 0)        # download stage skipped entirely
        entry = self.state.get()["folders"]["Alpha"]
        self.assertEqual(entry["icloud_skipped"]["count"], 1)   # a.key still pending

    def test_blocked_detection_criterion(self):
        d = self._daemon()
        # 0 materialized + all skipped -> blocked.
        self.assertTrue(d._materialize_blocked_batch([], ["a", "b", "c"], 3))
        # Any success -> not blocked.
        self.assertFalse(d._materialize_blocked_batch(["a"], ["b", "c"], 3))
        # Empty batch -> not blocked.
        self.assertFalse(d._materialize_blocked_batch([], [], 0))


class TestMountGate(PrefetchBase):
    """Fix 1: mount_smbfs is invoked ONLY after a live tcp445 probe succeeds
    (except the periodic denial-recheck), so an unreachable host never triggers a
    60s futile mount (or its wedge) on ordinary probe-failed iterations."""

    def test_gate_skips_mount_until_recheck(self):
        d = self._daemon()
        self.mount.reachable = False           # probe fails every time
        self.mount._mounted = False            # mount() returns False
        # Iterations 1-4: probe fails, NOT a recheck -> mount never attempted.
        for _ in range(4):
            self.assertFalse(d._try_online_gated())
        self.assertEqual(self.mount.mount_calls, 0)
        # 5th consecutive failure -> denial-recheck attempts a mount.
        self.assertFalse(d._try_online_gated())
        self.assertEqual(self.mount.mount_calls, 1)

    def test_offline_loop_mounts_after_probe_succeeds(self):
        self.cfg.icloud_policy = "skip"
        d = self._daemon()
        d._backoff_delay = lambda a: 0.01
        self.mount.reachable = False

        def _bring_up():
            time.sleep(0.05)
            self.mount.reachable = True        # NAS reachable -> probe passes
            self.mount._mounted = True         # mount() returns _mounted
        threading.Thread(target=_bring_up, daemon=True).start()

        self.assertTrue(d.ensure_online())
        self.assertGreaterEqual(self.mount.mount_calls, 1)   # mounted once reachable


class TestDenialRecheck(PrefetchBase):
    """Local-Network-denial recheck: every 5th consecutive probe failure attempts
    a mount despite the failed probe (this machine's probe is permanently blocked
    while the kernel mount works). Deterministic via _try_online_gated()."""

    def test_mount_attempted_on_5th_and_10th_only(self):
        d = self._daemon()
        self.mount.reachable = False           # probe always fails
        self.mount._mounted = False            # mount always fails -> never online
        per_iter = []
        for _ in range(10):
            before = self.mount.mount_calls
            d._try_online_gated()
            per_iter.append(self.mount.mount_calls - before)
        self.assertEqual(per_iter, [0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
        self.assertEqual(self.mount.mount_calls, 2)

    def test_recheck_success_logs_signature_without_notification(self):
        log = _RecLogger()
        d = self._daemon(logger=log)
        self.mount.reachable = False           # probe blocked (denial)

        def _mount():
            self.mount.mount_calls += 1
            self.mount._mounted = True          # kernel mount works despite the block
            return True
        self.mount.mount = _mount

        online = False
        for _ in range(5):
            online = d._try_online_gated()
        self.assertTrue(online)                        # 5th: recheck mount -> guard -> online
        self.assertEqual(self.mount.mount_calls, 1)    # mounted only on the 5th
        infos = " ".join(log.msgs("info"))
        self.assertIn("本地网络", infos)               # denial signature logged
        self.assertIn("mount_smbfs", infos)
        # Deliberately NO notification: the user cannot act on the denial
        # (macOS lists no switch for a launchd python3 in the Local Network
        # pane -- 2026-07-31 user report) and this code path IS the bypass.
        self.assertNotIn("local_network", self.notifier.classes())
        self.assertEqual(d._probe_fail_streak, 0)      # reset after successful mount

    def test_streak_resets_on_successful_probe(self):
        d = self._daemon()
        self.mount._mounted = False
        self.mount.reachable = False
        for _ in range(3):
            d._try_online_gated()              # 3 consecutive probe failures
        self.assertEqual(d._probe_fail_streak, 3)
        self.mount.reachable = True            # a successful probe...
        d._try_online_gated()                  # ...resets the streak (mount then fails)
        self.assertEqual(d._probe_fail_streak, 0)
        # A subsequent failure starts counting from 1 again (not from 4).
        self.mount.reachable = False
        d._try_online_gated()
        self.assertEqual(d._probe_fail_streak, 1)


class TestOfflineFreshness(PrefetchBase):
    """updated_at must be touched at least every _IDLE_TOUCH_INTERVAL during EVERY
    offline/backoff wait -- neither the main-loop idle-touch (MOUNTED_IDLE only)
    nor the progress heartbeat (syncing/prefetching only) covers a plain offline
    wait, so a long backoff used to let state.json go stale for minutes."""

    def _shrink_touch_interval(self, value=0.02):
        import mycloud.daemon as daemon_mod
        old = daemon_mod._IDLE_TOUCH_INTERVAL
        daemon_mod._IDLE_TOUCH_INTERVAL = value
        self.addCleanup(setattr, daemon_mod, "_IDLE_TOUCH_INTERVAL", old)

    def _count_touches(self, d):
        touches = []
        orig = d._idle_touch

        def _spy():
            touches.append(1)
            orig()
        d._idle_touch = _spy
        return touches

    def test_offline_wait_touches_periodically(self):
        self._shrink_touch_interval(0.02)
        d = self._daemon()
        touches = self._count_touches(d)
        d._offline_wait(0.1)                 # ~5 chunks of 0.02s
        self.assertGreaterEqual(len(touches), 3)   # entry + several + exit

    def test_offline_wait_refreshes_updated_at(self):
        d = self._daemon()
        self.state.update(state="OFFLINE")
        before = self.state.get()["updated_at"]
        time.sleep(0.01)
        d._offline_wait(0.0)                 # a single entry touch
        self.assertNotEqual(before, self.state.get()["updated_at"])

    def test_blocked_offline_interval_stays_fresh(self):
        # The exact production scenario: materialization blocked -> plain backoff
        # -> must keep touching state.json (was stale for up to ~600s).
        self._shrink_touch_interval(0.02)
        d = self._daemon()
        d._materialize_blocked = True
        d._backoff_delay = lambda a: 0.1
        touches = self._count_touches(d)
        d._offline_interval(0)
        self.assertGreaterEqual(len(touches), 3)

    def test_offline_wait_returns_promptly_on_stop(self):
        d = self._daemon()
        d._stop.set()
        start = time.monotonic()
        d._offline_wait(60.0)                # would block 60s without the stop check
        self.assertLess(time.monotonic() - start, 1.0)

    def test_liveness_covers_offline_segments_the_main_thread_cannot_touch(self):
        # Regression for the ~34-min staleness window. Some offline segments (the
        # recheck mount_smbfs attempt, a wedged os call in the bare-dir precheck)
        # block the MAIN thread with no touch and are not inside _offline_wait, so
        # only the background heartbeat can keep updated_at fresh through them.
        # In an always-probe-fail / always-mount-timeout loop the touch gap must
        # never approach the 120s budget on ANY iteration, recheck ones included.
        # Scaled for speed: the 60s mount -> 0.8s block, the 3s heartbeat -> 0.05s;
        # the gap staying far under the scaled budget proves the real 3s heartbeat
        # keeps the true 120s budget with vast margin.
        self.cfg.icloud_policy = "skip"          # plain offline backoff, no prefetch
        d = self._daemon()
        d._progress_hb_interval = 0.05           # fast liveness heartbeat
        d._backoff_delay = lambda a: 0.02        # tiny backoff between iterations
        self.mount.reachable = False             # probe always fails

        # Every recheck mount_smbfs "times out": it blocks the main thread with NO
        # touch, exactly like the real 60s attempt.
        blocker = threading.Event()              # never set -> wait() just times out
        def _mount():
            self.mount.mount_calls += 1
            blocker.wait(0.8)
            return False
        self.mount.mount = _mount

        # Record the monotonic time of every state write ("touch").
        touches = []
        tlock = threading.Lock()
        orig_update = d.state.update
        def _rec(**kw):
            with tlock:
                touches.append(time.monotonic())
            return orig_update(**kw)
        d.state.update = _rec

        # Stop after 12 iterations (rechecks fall on iterations 5 and 10).
        iters = {"n": 0}
        orig_interval = d._offline_interval
        def _count(attempt):
            iters["n"] += 1
            if iters["n"] >= 12:
                d._stop.set()
            return orig_interval(attempt)
        d._offline_interval = _count

        d._start_progress_heartbeat()
        try:
            d.ensure_online()
        finally:
            d._stop.set()
            blocker.set()
            d._join_progress_heartbeat()

        self.assertGreaterEqual(iters["n"], 12)
        self.assertGreaterEqual(self.mount.mount_calls, 2)   # recheck iterations ran
        self.assertGreater(len(touches), 2)
        stamps = sorted(touches)   # two threads append; order by time
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertLess(
            max(gaps), 0.4,
            "updated_at went stale for %.2fs during an offline segment; the "
            "liveness heartbeat did not cover it" % max(gaps))


class TestOnlineDownloadFixes(PrefetchBase):
    """Interplay fixes on the ONLINE (mounted) download pipeline."""

    def test_icloud_skipped_populated_at_plan_time(self):
        # Fix 4a: the per-folder icloud_skipped payload is populated at PLAN time
        # (before the download stage), not only at the end of the pass.
        self._patch_free(_BIG_FREE)
        self._mk("normal.txt"); self._mk("a.key", "aa"); self._mk("b.key", "bbbb")
        d = self._daemon()
        seen = {}
        orig = d._download_and_upload

        def _spy(name, src, dst_root, backup_dir, dataless_list, dataless_bytes,
                 agg, new_set=None):
            entry = self.state.get()["folders"].get(name, {})
            seen["entry"] = dict(entry.get("icloud_skipped") or {})
            return orig(name, src, dst_root, backup_dir, dataless_list,
                        dataless_bytes, agg, new_set=new_set)

        d._download_and_upload = _spy
        self.assertTrue(d.full_pass(None))
        self.assertEqual(seen["entry"].get("count"), 2)     # both .key pending at plan time
        self.assertGreater(seen["entry"].get("bytes", 0), 0)
        # After downloading everything it shrank to zero.
        self.assertEqual(
            self.state.get()["folders"]["Alpha"]["icloud_skipped"]["count"], 0)
        self.assertEqual(self._mirror_files(), ["a.key", "b.key", "normal.txt"])

    def test_online_download_logs_info_per_batch(self):
        # Fix 4b: the download/materialize stage now logs one INFO line per batch.
        self._patch_free(_BIG_FREE)
        self._mk("a.key", "payload")
        log = _RecLogger()
        d = self._daemon(logger=log)
        self.assertTrue(d.full_pass(None))
        self.assertIn("download batch done", " ".join(log.msgs("info")))

    def test_online_timeout_surfaced_as_prefetch_skipped(self):
        # Fix 4c: a file whose materialize times out is skipped + surfaced, not
        # retried forever (the subprocess read genuinely bounds each file).
        self._patch_free(_BIG_FREE)
        self._mk("stuck.key")
        d = self._daemon()
        d._materialize_timeout = 0.05
        d.read_fn = lambda path: None      # never clears -> times out
        d.full_pass(None)
        self.assertEqual(self.state.get().get("prefetch_skipped"), 1)
        self.assertEqual(
            self.state.get()["folders"]["Alpha"]["icloud_skipped"]["count"], 1)
        self.assertNotIn("stuck.key", self._mirror_files())


if __name__ == "__main__":
    unittest.main()


class TestMaterializeBlockedPersistence(PrefetchBase):
    """The launchd iCloud restriction is a property of the OS, not of this
    process. The latch was persisted but never read back, so every restart
    re-probed it -- ~120s per cloud-only file, i.e. ~32h for 968 files, during
    which the UI just showed "downloading" forever (reported 2026-07-30)."""

    def test_restored_from_state_on_startup(self):
        from datetime import datetime, timezone
        self.state.update(prefetch={"active": False, "blocked": True,
                                    "blocked_at": datetime.now(timezone.utc).isoformat()})
        d = self._daemon()
        self.assertTrue(d._materialize_blocked)

    def test_expired_or_unstamped_latch_is_reprobed(self):
        # The latch used to be permanent. A re-probe costs one per-file timeout
        # now, and the restriction may be gone (MaterializeDatalessFiles).
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        self.state.update(prefetch={"active": False, "blocked": True, "blocked_at": old})
        self.assertFalse(self._daemon()._materialize_blocked)
        self.assertFalse(self.state.get()["prefetch"]["blocked"])
        self.state.update(prefetch={"active": False, "blocked": True})   # pre-stamp latch
        self.assertFalse(self._daemon()._materialize_blocked)

    def test_latch_expires_in_a_long_running_daemon(self):
        from datetime import timedelta
        d = self._daemon()
        d._mark_materialize_blocked("test")
        self.assertTrue(d._materialize_blocked_now())
        self.assertIn("blocked_at", self.state.get()["prefetch"])
        real_now = d._now
        d._now = lambda: real_now() + timedelta(hours=25)
        self.assertFalse(d._materialize_blocked_now())
        self.assertFalse(d._materialize_blocked)

    def test_absent_or_false_means_fresh_probe(self):
        self.assertFalse(self._daemon()._materialize_blocked)
        self.state.update(prefetch={"active": False, "blocked": False})
        self.assertFalse(self._daemon()._materialize_blocked)

    def test_malformed_state_does_not_break_startup(self):
        self.state.update(prefetch="not-a-dict")
        self.assertFalse(self._daemon()._materialize_blocked)

    def test_blocked_daemon_skips_the_download_stage(self):
        # With the latch restored, the online download stage must not attempt a
        # single materialization (that attempt is what burns 120s per file).
        from datetime import datetime, timezone
        self.state.update(prefetch={"active": False, "blocked": True,
                                    "blocked_at": datetime.now(timezone.utc).isoformat()})
        d = self._daemon()
        agg = {"copied": 0, "failed": [], "degraded": False, "downloaded": 0,
               "evicted": 0, "files_done": 0, "dataless_pending": []}
        d._download_and_upload("Alpha", self.src,
                               os.path.join(self.data_root, "Alpha"),
                               self.trash_root,
                               ["cloud1.bin", "cloud2.bin"], 2048, agg)
        self.assertEqual(agg["downloaded"], 0)
        self.assertEqual(sorted(agg["dataless_pending"]),
                         ["cloud1.bin", "cloud2.bin"])
