import tests._path  # noqa: F401

"""Daemon-level integration tests for the v2 pipeline (PLAN section 14, M2-M4).

Real rsync + real manifest against a local fake NAS. The mount guard is stubbed
healthy; iCloud predicates are injected."""

import json
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
from mycloud import manifest as manifest_mod
from mycloud import paths
from mycloud import util
from mycloud.util import RunResult
from mycloud.daemon import (
    Daemon, F_DELETIONS_HELD, F_DEGRADED, F_UP_TO_DATE, SyncFailure)
from mycloud.engine import SyncEngine, SyncResult
from mycloud.manifest import Manifest
from mycloud.state import StateStore
from mycloud.trash import TrashManager

_LOG = logging.getLogger("mycloud.test_v2")
_LOG.addHandler(logging.NullHandler())


class StubMount(object):
    def __init__(self, mountpoint):
        self.mountpoint = Path(mountpoint)

    def guard_ok(self, timeout=10):
        return True

    def is_mounted(self, timeout=10):
        return True

    def sentinel_ok(self, timeout=10):
        return True

    def session_ok(self, timeout=10):
        return True

    def mount(self):
        return True

    def force_unmount(self):
        pass

    def ensure_bare_dir_safe(self):
        pass


class StubNotifier(object):
    def __init__(self):
        self.calls = []

    def runner(self, cmd, timeout):
        return None

    def notify(self, cls, title, body, min_hours=6.0, force=False):
        self.calls.append(cls)
        return True


class V2Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mycloud-v2-")
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
        self.cfg.max_delete_files = 200
        config_mod.write_excludes(self.cfg, self.excludes)

        self.state = StateStore(self.runtime, logger=_LOG)
        self.proc_registry = set()
        self.engine = SyncEngine(paths.RSYNC, self.excludes, _LOG,
                                 proc_registry=self.proc_registry)
        self.mount = StubMount(self.fakenas)
        self.trash_root = os.path.join(self.fakenas, paths.TRASH_REL)
        self.trash_mgr = TrashManager(self.trash_root, 30, 50, _LOG)
        self.notifier = StubNotifier()
        self.data_root = os.path.join(self.fakenas, paths.DATA_REL)
        self.manifest = Manifest(os.path.join(self.runtime, "manifest.db"))
        self.addCleanup(self.manifest.close)

    def _daemon(self, **kw):
        return Daemon(self.cfg, self.state, self.mount, self.engine,
                      self.trash_mgr, self.notifier, _LOG,
                      manifest=self.manifest, **kw)

    def _mk(self, rel, content="x"):
        path = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as h:
            h.write(content)
        return path

    def _mirror(self, rel):
        return os.path.join(self.data_root, "Alpha", rel)

    def _mirror_files(self):
        root = os.path.join(self.data_root, "Alpha")
        out = []
        for dp, _d, fs in os.walk(root):
            for fn in fs:
                out.append(os.path.relpath(os.path.join(dp, fn), root))
        return sorted(out)


class TestBatching(V2Base):
    def test_501_files_split_into_two_batches(self):
        for i in range(501):
            self._mk("f%03d.txt" % i, "c%d" % i)
        d = self._daemon()

        sizes = []
        orig = d.engine.phase1_batch

        def _spy(src, dst, backup, relpaths, timeout=None, on_progress=None,
                 **kw):
            sizes.append(len(relpaths))
            return orig(src, dst, backup, relpaths, timeout=timeout,
                        on_progress=on_progress)

        d.engine.phase1_batch = _spy
        self.assertTrue(d.full_pass(None))
        self.assertEqual(sizes, [500, 1])
        self.assertEqual(len(self._mirror_files()), 501)
        self.assertEqual(self.manifest.counts("Alpha")["files"], 501)

    def test_stat_batches_split_on_2gb_boundary(self):
        # Sparse files: logical size counts toward the 2GB batch cap, but no real
        # disk is used. Two 1.5GB files must land in separate batches.
        big = 1500 * 1024 * 1024
        for name in ("a.bin", "b.bin"):
            with open(os.path.join(self.src, name), "wb") as h:
                h.truncate(big)
        d = self._daemon()
        batches = list(d._stat_batches(self.src, ["a.bin", "b.bin"]))
        self.assertEqual(len(batches), 2)
        self.assertEqual([len(b) for b, _bytes in batches], [1, 1])


class TestResume(V2Base):
    def test_crash_after_first_batch_resumes_remainder(self):
        for i in range(501):
            self._mk("f%03d.txt" % i, "c%d" % i)
        d = self._daemon()

        orig = d.engine.phase1_batch
        state = {"n": 0}

        def _crash_on_second(src, dst, backup, relpaths, timeout=None,
                             on_progress=None, **kw):
            state["n"] += 1
            if state["n"] == 1:
                return orig(src, dst, backup, relpaths, timeout=timeout,
                            on_progress=on_progress)
            # Simulate a kill on the 2nd batch (no commit for it).
            return SyncResult("failure", -15, 0, 0, [])

        d.engine.phase1_batch = _crash_on_second
        with self.assertRaises(SyncFailure):
            d.full_pass(None)
        # Batch 1 (500) was committed; batch 2 was not.
        self.assertEqual(self.manifest.counts("Alpha")["files"], 500)

        # Restore the real batch method (the engine instance is shared).
        self.engine.phase1_batch = orig

        # Second run: only the 1 remaining file uploads.
        d2 = self._daemon()
        uploaded = []

        def _spy(src, dst, backup, relpaths, timeout=None, on_progress=None,
                 **kw):
            uploaded.extend(relpaths)
            return orig(src, dst, backup, relpaths, timeout=timeout,
                        on_progress=on_progress)

        d2.engine.phase1_batch = _spy
        self.assertTrue(d2.full_pass(None))
        self.assertEqual(len(uploaded), 1)
        self.assertEqual(self.manifest.counts("Alpha")["files"], 501)
        self.assertEqual(len(self._mirror_files()), 501)


class TestManifestDeletions(V2Base):
    def test_over_threshold_holds_then_approves(self):
        self.cfg.max_delete_files = 2
        for i in range(5):
            self._mk("f%d.txt" % i)
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        self.assertEqual(self.manifest.counts("Alpha")["files"], 5)

        # Delete 4 (> max_delete_files=2): the breaker must HOLD, not trash.
        for i in range(4):
            os.remove(os.path.join(self.src, "f%d.txt" % i))
        d.full_pass(None)
        entry = self.state.get()["folders"]["Alpha"]
        self.assertEqual(entry["state"], F_DELETIONS_HELD)
        self.assertEqual(len(self._mirror_files()), 5)          # nothing trashed
        self.assertEqual(self.manifest.counts("Alpha")["files"], 5)  # rows intact
        self.assertEqual(len(entry.get("held_deletions", [])), 4)

        # Approve: write the token the daemon consumes, then re-run.
        util.atomic_write_text(
            os.path.join(self.runtime, "approval.token"),
            d._now().isoformat() + "\n")
        d.full_pass(None)
        self.assertEqual(sorted(self._mirror_files()), ["f4.txt"])
        self.assertEqual(self.manifest.counts("Alpha")["files"], 1)
        # The 4 deletions were archived to the trash, never destroyed.
        trashed = []
        for dp, _dd, fs in os.walk(self.trash_root):
            trashed.extend(fs)
        self.assertEqual(len(trashed), 4)

    def test_normal_deletion_commits_manifest(self):
        self.cfg.max_delete_pct = 100  # avoid the pct breaker on a tiny mirror
        self._mk("a.txt"); self._mk("b.txt")
        d = self._daemon()
        d.full_pass(None)
        os.remove(os.path.join(self.src, "a.txt"))
        d.full_pass(None)
        self.assertEqual(self._mirror_files(), ["b.txt"])
        self.assertEqual(set(self.manifest.rows("Alpha")), {"b.txt"})


class TestRenamePropagation(V2Base):
    def test_local_rename_is_nas_rename_no_upload_no_trash(self):
        self._mk("orig.txt", "rename me, i am content")
        d = self._daemon()
        d.full_pass(None)
        self.assertEqual(self._mirror_files(), ["orig.txt"])

        os.rename(os.path.join(self.src, "orig.txt"),
                  os.path.join(self.src, "moved.txt"))
        calls = []
        orig = d.engine.phase1_batch

        def _spy(src, dst, backup, relpaths, timeout=None, on_progress=None,
                 **kw):
            calls.append(list(relpaths))
            return orig(src, dst, backup, relpaths, timeout=timeout,
                        on_progress=on_progress)

        d.engine.phase1_batch = _spy
        d.full_pass(None)
        self.assertEqual(self._mirror_files(), ["moved.txt"])  # renamed on NAS
        self.assertEqual(calls, [])                            # no re-upload
        self.assertEqual(set(self.manifest.rows("Alpha")), {"moved.txt"})
        # A rename must not create a trash/backup copy (no wasted re-archive).
        trashed = []
        for _dp, _dd, fs in os.walk(self.trash_root):
            trashed.extend(fs)
        self.assertEqual(trashed, [])

    def test_rename_fallback_when_mirror_rename_blocked(self):
        # If the mirror rename fails, fall back to upload(new)+delete(old).
        self.cfg.max_delete_pct = 100  # avoid the pct breaker on a tiny mirror
        self._mk("orig.txt", "content for fallback path")
        d = self._daemon()
        d.full_pass(None)
        os.rename(os.path.join(self.src, "orig.txt"),
                  os.path.join(self.src, "moved.txt"))

        real_rename = os.rename
        mirror_dir = os.path.join(self.data_root, "Alpha")

        def _blocked_rename(a, b):
            # Block only the in-place mirror rename (dest under the mirror);
            # allow the trash-archival rename (dest under the trash root).
            if str(b).startswith(mirror_dir):
                raise OSError("simulated rename failure")
            return real_rename(a, b)

        os.rename = _blocked_rename
        try:
            d.full_pass(None)
        finally:
            os.rename = real_rename
        # Fallback still converges: moved.txt present, orig.txt gone.
        self.assertEqual(self._mirror_files(), ["moved.txt"])
        self.assertEqual(set(self.manifest.rows("Alpha")), {"moved.txt"})


class TestDeepVerify(V2Base):
    def _seed(self, n=3):
        for i in range(n):
            self._mk("f%d.txt" % i, "content-%d" % i)
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        return d

    def test_missing_on_nas_marked_and_reuploaded(self):
        d = self._seed()
        os.remove(self._mirror("f0.txt"))          # NAS-side loss
        summary = d.deep_verify()
        self.assertEqual(summary["missing_uploaded"], 1)
        self.assertFalse(summary["running"])
        self.assertTrue(summary["finished_at"])
        self.assertEqual(self.manifest.counts("Alpha")["pending"], 1)
        # The follow-up pass restores it from the (still-present) source.
        d.full_pass(None)
        self.assertTrue(os.path.exists(self._mirror("f0.txt")))
        self.assertEqual(self.manifest.counts("Alpha")["pending"], 0)

    def test_drift_reuploaded(self):
        d = self._seed()
        with open(self._mirror("f1.txt"), "w") as h:   # tamper NAS copy
            h.write("bit-rot: different size and content")
        summary = d.deep_verify()
        self.assertEqual(summary["drift_reuploaded"], 1)
        self.assertGreaterEqual(self.manifest.counts("Alpha")["pending"], 1)

    def test_extra_on_nas_routed_to_trash(self):
        self.cfg.max_delete_pct = 100   # avoid the pct breaker on a tiny mirror
        d = self._seed()
        # A file on the NAS with no source and no manifest row.
        extra = self._mirror("ghost.txt")
        with open(extra, "w") as h:
            h.write("not from here")
        summary = d.deep_verify()
        self.assertEqual(summary["extra_found"], 1)
        self.assertFalse(os.path.exists(extra))        # removed from mirror
        trashed = []
        for _dp, _dd, fs in os.walk(self.trash_root):
            trashed.extend(fs)
        self.assertIn("ghost.txt", trashed)            # archived, not destroyed

    def test_extra_with_live_source_is_left_alone(self):
        d = self._seed()
        # Mirror has a file whose SOURCE still exists but manifest row is missing
        # (drift): it must NOT be trashed -- the next pass re-uploads it.
        self._mk("resurrected.txt", "i exist locally")
        with open(self._mirror("resurrected.txt"), "w") as h:
            h.write("i exist locally")
        summary = d.deep_verify()
        self.assertEqual(summary["extra_found"], 0)
        self.assertTrue(os.path.exists(self._mirror("resurrected.txt")))

    def test_verify_summary_written_to_state(self):
        d = self._seed()
        d.deep_verify()
        verify = self.state.get()["verify"]
        self.assertFalse(verify["running"])
        self.assertIsNotNone(verify["finished_at"])
        self.assertIn("missing_uploaded", verify)
        self.assertEqual(
            self.manifest.get_meta("last_deep_verify_at"), verify["finished_at"])


class TestDownloadPolicy(V2Base):
    def _stateful(self):
        """Shared 'materialized' set: dataless until read_fn downloads it."""
        materialized = set()

        def dl_check(path, st):
            return path.endswith(".key") and path not in materialized

        def is_dl(path):
            return path.endswith(".key") and path not in materialized

        def read_fn(path):
            materialized.add(path)

        return dl_check, is_dl, read_fn, materialized

    def test_download_materializes_uploads_and_evicts(self):
        self.cfg.icloud_policy = "download"
        self.cfg.icloud_evict_after_backup = True
        self._mk("normal.txt"); self._mk("cloud.key", "cloud payload")
        dl_check, is_dl, read_fn, _mat = self._stateful()
        evicted = []

        def run_cmd(argv, timeout):
            if len(argv) >= 2 and argv[1] == "help":
                return RunResult(0, "evict [<path>]\n", "", False)
            if len(argv) >= 2 and argv[1] == "evict":
                evicted.append(argv[2])
                return RunResult(0, "", "", False)
            return RunResult(0, "", "", False)

        d = self._daemon(dataless_check=dl_check, is_dataless_fn=is_dl,
                         read_fn=read_fn, run_cmd=run_cmd)
        self.assertTrue(d.full_pass(None))
        self.assertEqual(self._mirror_files(), ["cloud.key", "normal.txt"])
        self.assertEqual(evicted, [os.path.join(self.src, "cloud.key")])
        entry = self.state.get()["folders"]["Alpha"]
        self.assertEqual(entry["icloud_skipped"]["count"], 0)   # all downloaded

    def test_skip_policy_surfaces_and_excludes(self):
        self.cfg.icloud_policy = "skip"
        self._mk("normal.txt"); self._mk("cloud.key", "stays in cloud")

        def dl_check(path, st):
            return path.endswith(".key")

        d = self._daemon(dataless_check=dl_check,
                         is_dataless_fn=lambda p: p.endswith(".key"))
        d.full_pass(None)
        self.assertEqual(self._mirror_files(), ["normal.txt"])  # .key excluded
        isk = self.state.get()["folders"]["Alpha"]["icloud_skipped"]
        self.assertEqual(isk["count"], 1)
        self.assertIn("cloud.key", isk["samples"])
        self.assertGreater(isk["bytes"], 0)

    def test_disk_guard_refuses_download(self):
        self.cfg.icloud_policy = "download"
        self._mk("cloud.key", "would download")
        dl_check, is_dl, read_fn, _mat = self._stateful()
        d = self._daemon(dataless_check=dl_check, is_dataless_fn=is_dl,
                         read_fn=read_fn)

        orig_guard = icloud.disk_guard_ok
        icloud.disk_guard_ok = lambda *a, **k: False
        self.addCleanup(setattr, icloud, "disk_guard_ok", orig_guard)
        d.full_pass(None)
        # Download refused: file not on the NAS, folder degraded, user notified,
        # and it is surfaced as pending-download.
        self.assertNotIn("cloud.key", self._mirror_files())
        self.assertIn("icloud_disk", self.notifier.calls)
        isk = self.state.get()["folders"]["Alpha"]["icloud_skipped"]
        self.assertEqual(isk["count"], 1)

    def test_evict_absent_leaves_downloaded(self):
        self.cfg.icloud_policy = "download"
        self.cfg.icloud_evict_after_backup = True
        self._mk("cloud.key", "payload")
        dl_check, is_dl, read_fn, _mat = self._stateful()

        def run_cmd(argv, timeout):   # brctl with NO evict subcommand
            return RunResult(0, "diagnose\nlog\nstatus\n", "", False)

        d = self._daemon(dataless_check=dl_check, is_dataless_fn=is_dl,
                         read_fn=read_fn, run_cmd=run_cmd)
        self.assertTrue(d.full_pass(None))
        self.assertIn("cloud.key", self._mirror_files())   # backed up anyway
        entry = self.state.get()["folders"]["Alpha"]
        self.assertEqual(entry.get("files_copied", 0), 1)


class _BatchSpy(object):
    """Thread-safe recorder wrapping a real (or fake) phase1_batch."""

    def __init__(self, inner):
        self.inner = inner
        self.lock = threading.Lock()
        self.calls = []  # type: list

    def __call__(self, src, dst, backup, relpaths, timeout=None,
                 on_progress=None, inplace=False, no_times=False, **kw):
        with self.lock:
            self.calls.append({
                "relpaths": list(relpaths),
                "inplace": inplace,
                "no_times": no_times,
            })
        return self.inner(src, dst, backup, relpaths, timeout=timeout,
                          on_progress=on_progress, inplace=inplace,
                          no_times=no_times, **kw)


class TestP0InplaceRouting(V2Base):
    """PLAN 15 P0: new files ride --inplace; changed files keep -b."""

    def _spying_daemon(self):
        d = self._daemon()
        d._inplace_capable = True   # the capability probe passed (see
        # TestCapabilityFailClosed.test_probe_gates_inplace_capability)
        spy = _BatchSpy(self.engine.phase1_batch)
        d.engine.phase1_batch = spy
        return d, spy

    def test_first_backup_rides_inplace_updates_ride_backup(self):
        self._mk("a.txt", "a1")
        self._mk("b.txt", "b1")
        d, spy = self._spying_daemon()
        self.assertTrue(d.full_pass(None))
        self.assertEqual(len(spy.calls), 1)
        self.assertTrue(spy.calls[0]["inplace"])      # all new -> fast path
        self.assertTrue(spy.calls[0]["no_times"])     # default on
        self.assertEqual(self._mirror_files(), ["a.txt", "b.txt"])

        # Change one file and add one: the changed file must go through the
        # -b versioning batch, the new file through the inplace batch.
        self._mk("a.txt", "a2-changed-longer")
        self._mk("c.txt", "new")
        spy.calls[:] = []
        self.assertTrue(d.full_pass(None))
        by_inplace = {c["inplace"]: c["relpaths"] for c in spy.calls}
        self.assertEqual(by_inplace[False], ["a.txt"])
        self.assertEqual(by_inplace[True], ["c.txt"])
        # The overwritten old version was preserved by the -b batch.
        self.assertIsNotNone(_find_trash(self.trash_root, "a.txt"))

    def test_inplace_needs_capability_probe(self):
        self._mk("a.txt")
        d = self._daemon()          # _inplace_capable stays False
        spy = _BatchSpy(self.engine.phase1_batch)
        d.engine.phase1_batch = spy
        self.assertTrue(d.full_pass(None))
        self.assertFalse(any(c["inplace"] for c in spy.calls))

    def test_inplace_config_off_forces_backup_path(self):
        self.cfg.rsync_inplace = False
        self._mk("a.txt")
        d, spy = self._spying_daemon()
        self.assertTrue(d.full_pass(None))
        self.assertFalse(any(c["inplace"] for c in spy.calls))

    def test_no_times_config_passthrough(self):
        self.cfg.rsync_no_times = False
        self._mk("a.txt")
        d, spy = self._spying_daemon()
        self.assertTrue(d.full_pass(None))
        self.assertFalse(any(c["no_times"] for c in spy.calls))


class TestP0ParallelUploads(V2Base):
    def _shrink_batches(self, cap):
        import mycloud.daemon as daemon_mod
        old_cap = daemon_mod._BATCH_MAX_FILES
        daemon_mod._BATCH_MAX_FILES = cap
        self.addCleanup(setattr, daemon_mod, "_BATCH_MAX_FILES", old_cap)

    def test_new_file_batches_run_concurrently_and_commit_everything(self):
        self.cfg.upload_concurrency = 3
        for i in range(8):
            self._mk("f%d.txt" % i, "c%d" % i)
        d = self._daemon()
        d._inplace_capable = True   # new-file batches are the parallel path

        stats = {"inflight": 0, "peak": 0}
        lock = threading.Lock()
        orig = self.engine.phase1_batch

        def _tracking(src, dst, backup, relpaths, timeout=None,
                      on_progress=None, **kw):
            with lock:
                stats["inflight"] += 1
                stats["peak"] = max(stats["peak"], stats["inflight"])
            try:
                time.sleep(0.15)  # hold the slot so overlap is observable
                return orig(src, dst, backup, relpaths, timeout=timeout,
                            on_progress=on_progress, **kw)
            finally:
                with lock:
                    stats["inflight"] -= 1

        d.engine.phase1_batch = _tracking
        self._shrink_batches(2)   # 8 files -> 4 batches of 2

        self.assertTrue(d.full_pass(None))
        self.assertGreaterEqual(stats["peak"], 2)   # true overlap happened
        self.assertEqual(len(self._mirror_files()), 8)
        self.assertEqual(self.manifest.counts("Alpha")["files"], 8)
        # Progress accounting survived the concurrency: all batches landed.
        entry = self.state.get()["folders"]["Alpha"]
        self.assertEqual(entry.get("files_copied", 0), 8)

    def test_changed_file_batches_stay_serial(self):
        # Concurrent -b batches race on shared backup-dir mkdirs (openrsync
        # mk_backup_dir is not EEXIST-tolerant, verified 2026-07-21), so
        # changed-file batches must never overlap even with a big pool.
        self.cfg.upload_concurrency = 4
        for i in range(4):
            self._mk("f%d.txt" % i, "v1-%d" % i)
        d = self._daemon()
        d._inplace_capable = True
        self.assertTrue(d.full_pass(None))
        for i in range(4):
            self._mk("f%d.txt" % i, "v2-%d-longer" % i)   # all changed now

        stats = {"inflight": 0, "peak": 0}
        lock = threading.Lock()
        orig = self.engine.phase1_batch

        def _tracking(src, dst, backup, relpaths, timeout=None,
                      on_progress=None, **kw):
            self.assertFalse(kw.get("inplace", False))
            with lock:
                stats["inflight"] += 1
                stats["peak"] = max(stats["peak"], stats["inflight"])
            try:
                time.sleep(0.1)
                return orig(src, dst, backup, relpaths, timeout=timeout,
                            on_progress=on_progress, **kw)
            finally:
                with lock:
                    stats["inflight"] -= 1

        d.engine.phase1_batch = _tracking
        self._shrink_batches(1)   # 4 changed files -> 4 -b batches
        self.assertTrue(d.full_pass(None))
        self.assertEqual(stats["peak"], 1)   # strictly serial
        # The overwritten old versions were all preserved.
        for i in range(4):
            self.assertIsNotNone(_find_trash(self.trash_root, "f%d.txt" % i))

    def test_batch_failure_stops_dispatch_and_raises(self):
        self.cfg.upload_concurrency = 2
        for i in range(6):
            self._mk("f%d.txt" % i, "c%d" % i)
        d = self._daemon()
        d._inplace_capable = True   # exercise the PARALLEL dispatch loop

        calls = []
        lock = threading.Lock()

        def _always_fail(src, dst, backup, relpaths, timeout=None,
                         on_progress=None, **kw):
            with lock:
                calls.append(list(relpaths))
            return SyncResult("failure", 20, 0, 0, [])

        d.engine.phase1_batch = _always_fail
        self._shrink_batches(1)

        with self.assertRaises(SyncFailure):
            d.full_pass(None)
        # Dispatch stopped early: nowhere near all 6 single-file batches ran
        # (at most the in-flight window when the first failure landed).
        self.assertLess(len(calls), 6)
        self.assertEqual(self.manifest.counts("Alpha")["files"], 0)


class TestP0BuiltinExcludes(V2Base):
    def test_v2_pipeline_honors_builtin_excludes(self):
        # Regression for the 2026-07-21 bug: plan_folder used to receive ONLY
        # the user patterns, so .DS_Store/AppleDouble/node_modules litter was
        # backed up by the batch pipeline (which passes no --exclude-from).
        self._mk("real.txt", "keep me")
        self._mk(".DS_Store", "finder litter")
        self._mk("._real.txt", "appledouble sidecar")
        self._mk("node_modules/dep/index.js", "junk")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        self.assertEqual(self._mirror_files(), ["real.txt"])
        self.assertEqual(
            sorted(self.manifest.rows("Alpha")), ["real.txt"])


class TestP0Verify(V2Base):
    def test_verify_ignores_junk_on_nas(self):
        # Finder browsing the share litters mirrors with .DS_Store/._*; the
        # weekly verify must not treat that litter as "extra" (it would be
        # archived into the trash every week, forever).
        self._mk("real.txt", "keep")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        mirror_root = os.path.join(self.data_root, "Alpha")
        with open(os.path.join(mirror_root, ".DS_Store"), "w") as h:
            h.write("litter")
        with open(os.path.join(mirror_root, "._real.txt"), "w") as h:
            h.write("sidecar")
        summary = d.deep_verify()
        self.assertEqual(summary["extra_found"], 0)
        self.assertTrue(
            os.path.exists(os.path.join(mirror_root, ".DS_Store")))

    def test_verify_size_only_under_no_times(self):
        # With rsync_no_times=true (default) the NAS mtime is the write time,
        # so only a size mismatch may count as drift.
        self._mk("a.txt", "stable content")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        mirror = self._mirror("a.txt")
        old = time.time() - 86400.0
        os.utime(mirror, (old, old))            # mtime drifts, size intact
        summary = d.deep_verify()
        self.assertEqual(summary["drift_reuploaded"], 0)

        # Size drift is still caught in the same mode.
        with open(mirror, "a", encoding="utf-8") as h:
            h.write("corruption")
        summary = d.deep_verify()
        self.assertEqual(summary["drift_reuploaded"], 1)

    def test_verify_compares_mtime_when_times_preserved(self):
        self.cfg.rsync_no_times = False
        self._mk("a.txt", "stable content")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        mirror = self._mirror("a.txt")
        old = time.time() - 86400.0
        os.utime(mirror, (old, old))
        summary = d.deep_verify()
        self.assertEqual(summary["drift_reuploaded"], 1)


class _DyingMount(StubMount):
    """A mount whose is_mounted() flips to False after `alive_calls` calls.

    Models the real failure: the SMB mount dies WHILE the verify walks it, so
    os.walk quietly returns a truncated tree from that point on.
    """

    def __init__(self, mountpoint, alive_calls=1):
        StubMount.__init__(self, mountpoint)
        self.alive_calls = alive_calls
        self.mounted_calls = 0

    def is_mounted(self, timeout=10):
        self.mounted_calls += 1
        return self.mounted_calls <= self.alive_calls


class TestP1VerifyRobustness(V2Base):
    """T-C1 (PLAN 16.2, 2026-07-30): the deep-verify NAS walk aborts -- with
    ZERO side effects -- when the mount dies or the daemon is stopping, instead
    of marking every unseen row pending from a truncated walk."""

    _STALE_STAMP = "2026-01-01T00:00:00+00:00"

    def _tighten_guard(self, every_dirs=1, every_s=0.0):
        """Check the guard on EVERY walked directory (production: 256 / 5s)."""
        import mycloud.daemon as daemon_mod
        old_dirs = daemon_mod._VERIFY_GUARD_EVERY_DIRS
        old_secs = daemon_mod._VERIFY_GUARD_EVERY_S
        daemon_mod._VERIFY_GUARD_EVERY_DIRS = every_dirs
        daemon_mod._VERIFY_GUARD_EVERY_S = every_s
        self.addCleanup(setattr, daemon_mod,
                        "_VERIFY_GUARD_EVERY_DIRS", old_dirs)
        self.addCleanup(setattr, daemon_mod, "_VERIFY_GUARD_EVERY_S", old_secs)

    def _seed(self, n=3):
        """Committed rows + a real mirror (nested, so the walk yields >1 dir)."""
        for i in range(n):
            self._mk("sub/f%d.txt" % i, "content-%d" % i)
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        self.assertEqual(self.manifest.counts("Alpha")["pending"], 0)
        self.manifest.set_meta("last_deep_verify_at", self._STALE_STAMP)
        return d

    def _lose_mirror(self, n=3):
        """Delete the NAS copies: an UNGUARDED verify would mark all n pending."""
        for i in range(n):
            os.remove(self._mirror("sub/f%d.txt" % i))

    def _assert_no_side_effects(self, summary):
        self.assertTrue(summary["aborted"])
        self.assertEqual(summary["missing_uploaded"], 0)
        self.assertEqual(summary["drift_reuploaded"], 0)
        self.assertEqual(summary["extra_found"], 0)
        # No row was marked pending -> no phantom re-upload storm.
        self.assertEqual(self.manifest.counts("Alpha")["pending"], 0)
        # The cadence must retry: the completion stamp stays untouched.
        self.assertEqual(self.manifest.get_meta("last_deep_verify_at"),
                         self._STALE_STAMP)

    def test_mount_death_mid_walk_aborts_whole_verify(self):
        self._tighten_guard()
        d = self._seed()
        self._lose_mirror()
        d.mount = _DyingMount(self.fakenas, alive_calls=1)
        summary = d.deep_verify()
        self._assert_no_side_effects(summary)
        self.assertTrue(d.mount.mounted_calls >= 2)   # aborted mid-walk
        # state.json carries the abort, and it does NOT claim a fresh verify.
        state = self.state.get()
        self.assertTrue(state["verify"]["aborted"])
        self.assertIsNone(state.get("last_deep_verify_at"))
        # The journal records the abort under its own event name.
        events = []
        with open(self.state.journal_path, "r", encoding="utf-8") as handle:
            for line in handle:
                events.append(json.loads(line).get("event"))
        self.assertIn("deep_verify_aborted", events)
        self.assertNotIn("deep_verify", events)

    def test_stop_request_aborts_whole_verify(self):
        self._tighten_guard()
        d = self._seed()
        self._lose_mirror()
        d._stop.set()
        summary = d.deep_verify()
        self._assert_no_side_effects(summary)

    def test_abort_does_not_continue_to_later_folders(self):
        # A dead mount must abort the RUN, not just the folder: continuing would
        # walk the next folder's dead tree and mark all of ITS rows pending.
        second = os.path.join(self.tmp, "src", "Beta")
        os.makedirs(second)
        with open(os.path.join(second, "b.txt"), "w", encoding="utf-8") as h:
            h.write("beta")
        self.cfg.folders.append(config_mod.FolderSpec(Path(second), "Beta"))
        self._tighten_guard()
        d = self._seed()
        self._lose_mirror()
        d.mount = _DyingMount(self.fakenas, alive_calls=1)
        summary = d.deep_verify()
        self.assertTrue(summary["aborted"])
        self.assertEqual(self.manifest.counts("Alpha")["pending"], 0)
        self.assertEqual(self.manifest.counts("Beta")["pending"], 0)

    def test_clean_verify_is_not_aborted_and_still_marks_missing(self):
        # Guard armed on every directory, mount healthy: existing behavior holds.
        self._tighten_guard()
        d = self._seed()
        os.remove(self._mirror("sub/f0.txt"))
        summary = d.deep_verify()
        self.assertFalse(summary["aborted"])
        self.assertEqual(summary["missing_uploaded"], 1)
        self.assertEqual(self.manifest.counts("Alpha")["pending"], 1)
        self.assertEqual(self.manifest.get_meta("last_deep_verify_at"),
                         summary["finished_at"])

    def test_heartbeat_restamps_progress_during_deep_verify(self):
        # A multi-hour verify used to leave state.json untouched, so the GUI
        # reported "守护进程无响应" for its entire duration.
        d = self._daemon()
        d._progress_hb_interval = 0.02
        captured = []
        orig_update = self.state.update

        def _spy(**fields):
            if "progress" in fields:
                captured.append(dict(fields["progress"]))
            return orig_update(**fields)

        self.state.update = _spy
        d._verifying.set()
        d._progress_last = {"active": True, "phase": "verifying",
                            "folder": "Alpha"}
        d._start_progress_heartbeat()
        try:
            time.sleep(0.2)
        finally:
            d._stop.set()
            d._join_progress_heartbeat()
        stamps = [p for p in captured if p.get("phase") == "verifying"]
        self.assertGreaterEqual(len(stamps), 2)


def _find_trash(trash_root, basename):
    for dirpath, _dirs, files in os.walk(trash_root):
        if basename in files:
            return os.path.join(dirpath, basename)
    return None


class TestInitialEstimate(V2Base):
    """_estimate_initial must not re-walk huge trees for a cosmetic total, and
    must publish progress when it does walk (2026-07-28: on a 292k-file iCloud
    source the silent double scan cost 10+ extra minutes and looked like a hang).
    """

    def test_estimate_skipped_once_manifest_has_rows(self):
        self._mk("a.txt", "a")
        d = self._daemon()
        # First run: manifest empty -> the estimate walks and records a total.
        walks = []
        orig = self.manifest.plan_folder

        def _spy(folder, src, excludes, dl_check, subtrees=None):
            walks.append(folder)
            return orig(folder, src, excludes, dl_check, subtrees=subtrees)

        self.manifest.plan_folder = _spy
        d._estimate_initial()
        self.assertEqual(walks, ["Alpha"])
        self.assertIsInstance(self.state.get().get("initial_sync"), dict)

        # Commit rows, then re-run: the walk must be skipped entirely.
        self.manifest.commit_uploaded("Alpha", [("a.txt", 1, 1, 1)])
        walks[:] = []
        d._estimate_initial()
        self.assertEqual(walks, [])

    def test_estimate_publishes_progress_on_first_run(self):
        self._mk("a.txt", "a")
        d = self._daemon()
        captured = []
        orig_update = self.state.update

        def _spy(**fields):
            if "progress" in fields:
                captured.append(dict(fields["progress"]))
            return orig_update(**fields)

        self.state.update = _spy
        d._estimate_initial()
        # Progress was published while scanning, then cleared.
        self.assertTrue(any(p.get("active") and p.get("folder") == "Alpha"
                            for p in captured),
                        "estimate published no progress: %r" % (captured,))
        self.assertFalse(captured[-1].get("active"))

    def test_estimate_stops_on_shutdown(self):
        self._mk("a.txt", "a")
        d = self._daemon()
        d._stop.set()
        walks = []
        self.manifest.plan_folder = lambda *a, **k: walks.append(1)
        d._estimate_initial()
        self.assertEqual(walks, [])


class TestMirrorCountFromManifest(V2Base):
    """mirror_count must come from the manifest, never an os.walk over SMB.

    That walk ran for every folder on every pass; over a 292k-file mirror on the
    real router it took 10+ minutes with no progress written, which is what made
    a healthy daemon look hung (SIGINFO stack dump, 2026-07-28).
    """

    def test_uses_manifest_and_never_walks_nas(self):
        self.manifest.commit_uploaded(
            "Alpha", [("f%d.txt" % i, 1, 1, i) for i in range(7)])
        d = self._daemon()
        walked = []
        real_walk = os.walk

        def _spy_walk(top, *a, **k):
            walked.append(top)
            return real_walk(top, *a, **k)

        os.walk = _spy_walk
        try:
            n = d._mirror_count("Alpha", os.path.join(self.data_root, "Alpha"))
        finally:
            os.walk = real_walk
        self.assertEqual(n, 7)
        self.assertEqual(walked, [], "walked the NAS despite manifest rows")

    def test_display_path_never_walks_even_with_empty_manifest(self):
        # A mirror can hold files the manifest never tracked (71443 on the real
        # NAS, left by a source whose local contents moved away). Display-only
        # callers must NOT pay for that walk.
        mirror = os.path.join(self.data_root, "Alpha")
        os.makedirs(mirror, exist_ok=True)
        with open(os.path.join(mirror, "untracked.txt"), "w") as h:
            h.write("x")
        d = self._daemon()
        walked = []
        real_walk = os.walk
        os.walk = lambda top, *a, **k: (walked.append(top), real_walk(top, *a, **k))[1]
        try:
            self.assertEqual(d._mirror_count("Alpha", mirror), 0)
        finally:
            os.walk = real_walk
        self.assertEqual(walked, [])

    def test_breaker_path_walks_when_manifest_empty(self):
        mirror = os.path.join(self.data_root, "Alpha")
        os.makedirs(os.path.join(mirror, "sub"), exist_ok=True)
        for rel in ("a.txt", "sub/b.txt"):
            with open(os.path.join(mirror, rel), "w") as h:
                h.write("x")
        d = self._daemon()
        self.assertEqual(d._mirror_count("Alpha", mirror, allow_walk=True), 2)

    def test_fallback_walk_is_capped(self):
        import mycloud.daemon as daemon_mod
        old_cap = daemon_mod._MIRROR_WALK_CAP
        daemon_mod._MIRROR_WALK_CAP = 3
        self.addCleanup(setattr, daemon_mod, "_MIRROR_WALK_CAP", old_cap)
        # The cap is checked once per DIRECTORY (os.walk yields a whole
        # directory's file list at a time), so spread files across dirs.
        mirror = os.path.join(self.data_root, "Alpha")
        for d_i in range(5):
            sub = os.path.join(mirror, "d%d" % d_i)
            os.makedirs(sub, exist_ok=True)
            for i in range(2):
                with open(os.path.join(sub, "f%d.txt" % i), "w") as h:
                    h.write("x")
        d = self._daemon()
        n = d._mirror_count("Alpha", mirror, allow_walk=True)
        self.assertGreaterEqual(n, 3)
        self.assertLess(n, 10)          # stopped at the cap, did not finish

    def test_zero_when_no_rows_and_no_root(self):
        d = self._daemon()
        self.assertEqual(d._mirror_count("Alpha"), 0)

    def test_pass_records_manifest_backed_mirror_count(self):
        for i in range(3):
            self._mk("f%d.txt" % i, "c")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        entry = self.state.get()["folders"]["Alpha"]
        self.assertEqual(entry["mirror_count"],
                         self.manifest.counts("Alpha")["files"])


class TestWatchdogStartup(V2Base):
    def test_watchdog_started_once_and_before_initial_pass(self):
        # The initial pass is the longest and most exposed; it used to run with no
        # watchdog because the thread only started in _main_loop (2026-07-28).
        d = self._daemon()
        self.assertIsNone(d._watchdog_thread)
        d._start_watchdog()
        first = d._watchdog_thread
        self.assertIsNotNone(first)
        self.assertTrue(first.is_alive())
        self.assertTrue(first.daemon)
        # Idempotent: _main_loop calling it again must not spawn a second thread.
        d._start_watchdog()
        self.assertIs(d._watchdog_thread, first)
        d._stop.set()
        first.join(timeout=5)


class TestP1VerifiedCommit(V2Base):
    """PLAN 16.2 T-A1: a partial batch commits only what the NAS confirms.

    The 2026-07-30 real-machine audit found ~1.3% of manifest rows (~4,200 files,
    real photos and work documents included) pointed at files that were never on
    the NAS: partial (rc=23/24) batches committed everything rsync's stderr parse
    failed to name, and that parse both misses failures and yields paths in a
    shape that never matches the batch relpaths.
    """

    def setUp(self):
        V2Base.setUp(self)
        self.real_batch = self.engine.phase1_batch

    def _lossy_engine(self, d, lost, rc=23):
        """phase1_batch that silently drops `lost` and reports partial.

        Reproduces the field trap exactly: the file never reaches the NAS, and
        failed_paths comes back absolute (rsync's own shape), so any parse-based
        commit filter waves the dropped file straight into the manifest.
        """
        def _lossy(src, dst, backup, relpaths, timeout=None, on_progress=None,
                   **kw):
            kept = [r for r in relpaths if r not in lost]
            copied, nbytes = 0, 0
            if kept:
                res = self.real_batch(src, dst, backup, kept, timeout=timeout,
                                      on_progress=on_progress, **kw)
                copied, nbytes = res.files_copied, res.bytes_copied
            return SyncResult("partial", rc, copied, nbytes,
                              ["/absolute/garbage/path"])

        d.engine.phase1_batch = _lossy

    def test_partial_commits_only_verified_regression_2026_07_30(self):
        self._mk("good1.txt", "landed")
        self._mk("good2.txt", "landed too")
        self._mk("lost.txt", "never reaches the NAS")
        d = self._daemon()
        self._lossy_engine(d, {"lost.txt"})

        self.assertFalse(d.full_pass(None))          # partial => degraded
        self.assertEqual(self._mirror_files(), ["good1.txt", "good2.txt"])
        # The lie: lost.txt used to be committed here because the garbage
        # failed_paths entry did not match its relpath.
        self.assertEqual(set(self.manifest.rows("Alpha")),
                         {"good1.txt", "good2.txt"})
        self.assertEqual(
            self.state.get()["folders"]["Alpha"]["state"], F_DEGRADED)

    def test_rc24_vanished_file_is_not_committed_either(self):
        # rc=24 (source file vanished mid-transfer) took the same commit-anyway
        # path and recorded never-transferred files as synced.
        self._mk("kept.txt", "landed")
        self._mk("gone.txt", "vanishes")
        d = self._daemon()
        self._lossy_engine(d, {"gone.txt"}, rc=24)
        self.assertFalse(d.full_pass(None))
        self.assertEqual(set(self.manifest.rows("Alpha")), {"kept.txt"})

    def test_unverified_file_is_reuploaded_next_pass(self):
        self._mk("good.txt", "landed")
        self._mk("lost.txt", "arrives late")
        d = self._daemon()
        self._lossy_engine(d, {"lost.txt"})
        d.full_pass(None)
        self.assertNotIn("lost.txt", set(self.manifest.rows("Alpha")))

        # No manifest row means the next plan re-uploads it; nothing else needed.
        d.engine.phase1_batch = self.real_batch
        self.assertTrue(d.full_pass(None))
        self.assertEqual(self._mirror_files(), ["good.txt", "lost.txt"])
        self.assertEqual(set(self.manifest.rows("Alpha")),
                         {"good.txt", "lost.txt"})

    def test_three_consecutive_failures_disclose_the_file(self):
        self._mk("good.txt", "landed")
        self._mk("lost.txt", "chronic failure")
        d = self._daemon()
        self._lossy_engine(d, {"lost.txt"})
        for _ in range(2):
            d.full_pass(None)
            self.assertEqual(
                self.state.get()["folders"]["Alpha"].get("failed_files", []), [],
                "a file must not be named after one or two misses")
        d.full_pass(None)
        self.assertEqual(
            self.state.get()["folders"]["Alpha"]["failed_files"], ["lost.txt"])

    def test_verified_upload_clears_the_failure_counter(self):
        self._mk("lost.txt", "flaky")
        d = self._daemon()
        self._lossy_engine(d, {"lost.txt"})
        d.full_pass(None)
        d.full_pass(None)
        self.assertEqual(d._upload_retry_counts[("Alpha", "lost.txt")], 2)

        d.engine.phase1_batch = self.real_batch
        self.assertTrue(d.full_pass(None))
        self.assertEqual(d._upload_retry_counts, {})
        self.assertEqual(
            self.state.get()["folders"]["Alpha"]["failed_files"], [])


class TestP1VerifyBatchOnNas(V2Base):
    """PLAN 16.2 T-A1 unit: lstat + size, fail-safe on anything else."""

    def _mirror_root(self):
        root = os.path.join(self.data_root, "Alpha")
        os.makedirs(root, exist_ok=True)
        return root

    def _put(self, root, rel, content):
        with open(os.path.join(root, rel), "w", encoding="utf-8") as h:
            h.write(content)

    def test_present_with_matching_size_passes(self):
        d = self._daemon()
        root = self._mirror_root()
        self._put(root, "a.txt", "12345")
        self.assertEqual(
            d._verify_batch_on_nas(root, [("a.txt", 5, 0, 0)]), {"a.txt"})

    def test_size_mismatch_fails(self):
        d = self._daemon()
        root = self._mirror_root()
        self._put(root, "a.txt", "123")          # truncated transfer
        self.assertEqual(
            d._verify_batch_on_nas(root, [("a.txt", 5, 0, 0)]), set())

    def test_absent_file_fails(self):
        d = self._daemon()
        root = self._mirror_root()
        self._put(root, "here.txt", "12345")
        self.assertEqual(
            d._verify_batch_on_nas(
                root, [("here.txt", 5, 0, 0), ("gone.txt", 5, 0, 0)]),
            {"here.txt"})

    def test_lstat_error_fails(self):
        # A wedged mount raises rather than lying; that must never read as "ok".
        d = self._daemon()
        root = self._mirror_root()
        self._put(root, "a.txt", "12345")
        real_lstat = os.lstat

        def _boom(path, *a, **kw):
            if str(path).endswith("a.txt"):
                raise OSError("simulated NAS I/O error")
            return real_lstat(path, *a, **kw)

        os.lstat = _boom
        self.addCleanup(setattr, os, "lstat", real_lstat)
        self.assertEqual(
            d._verify_batch_on_nas(root, [("a.txt", 5, 0, 0)]), set())


class TestP1BatchRetryOnRc20(V2Base):
    """PLAN 16.2 T-A2: rsync's own I/O timeout retries once in place.

    Classified by the engine (field review 2026-09-25): exit 30 on rsync 2.6.9,
    exit 20 + "poll: timeout" on openrsync -> kind "io_timeout". A bare exit 20
    is a SIGTERM -- our watchdog -- and must NOT be retried into the dead mount
    (the old rule retried exactly that, and never retried 2.6.9's real timeout)."""

    def setUp(self):
        V2Base.setUp(self)
        self.real_batch = self.engine.phase1_batch
        import mycloud.daemon as daemon_mod
        old = daemon_mod._BATCH_RETRY_SLEEP
        daemon_mod._BATCH_RETRY_SLEEP = 0.0
        self.addCleanup(setattr, daemon_mod, "_BATCH_RETRY_SLEEP", old)

    def test_rc20_retry_succeeds_and_commits(self):
        self._mk("a.txt", "payload")
        d = self._daemon()
        calls = []

        def _flaky(src, dst, backup, relpaths, timeout=None, on_progress=None,
                   **kw):
            calls.append(list(relpaths))
            if len(calls) == 1:
                return SyncResult("failure", 30, 0, 0, [], "io_timeout")
            return self.real_batch(src, dst, backup, relpaths, timeout=timeout,
                                   on_progress=on_progress, **kw)

        d.engine.phase1_batch = _flaky
        self.assertTrue(d.full_pass(None))       # no SyncFailure, no recovery
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])     # identical arguments
        self.assertEqual(self._mirror_files(), ["a.txt"])
        self.assertEqual(set(self.manifest.rows("Alpha")), {"a.txt"})

    def test_rc20_twice_raises_sync_failure(self):
        self._mk("a.txt", "payload")
        d = self._daemon()
        calls = []

        def _always_20(src, dst, backup, relpaths, timeout=None,
                       on_progress=None, **kw):
            calls.append(list(relpaths))
            return SyncResult("failure", 20, 0, 0, [], "io_timeout")

        d.engine.phase1_batch = _always_20
        with self.assertRaises(SyncFailure):
            d.full_pass(None)
        self.assertEqual(len(calls), 2)          # exactly one retry
        self.assertEqual(self.manifest.counts("Alpha")["files"], 0)

    def test_signal_death_is_not_retried(self):
        # rc < 0 is our own watchdog killpg: recovery is already on its way, so
        # retrying would only fight it (pre-existing behaviour, kept).
        self._mk("a.txt", "payload")
        d = self._daemon()
        calls = []

        def _killed(src, dst, backup, relpaths, timeout=None, on_progress=None,
                    **kw):
            calls.append(list(relpaths))
            return SyncResult("failure", -15, 0, 0, [])

        d.engine.phase1_batch = _killed
        with self.assertRaises(SyncFailure):
            d.full_pass(None)
        self.assertEqual(len(calls), 1)

    def test_watchdog_sigterm_exit_20_is_not_retried(self):
        self._mk("a.txt", "payload")
        d = self._daemon()
        calls = []

        def _sigterm(src, dst, backup, relpaths, timeout=None, on_progress=None,
                     **kw):
            calls.append(list(relpaths))
            return SyncResult("failure", 20, 0, 0, [], "killed")

        d.engine.phase1_batch = _sigterm
        with self.assertRaises(SyncFailure):
            d.full_pass(None)
        self.assertEqual(len(calls), 1)

    def test_io_timeout_is_not_retried_once_recovery_is_requested(self):
        self._mk("a.txt", "payload")
        d = self._daemon()
        calls = []

        def _timeout(src, dst, backup, relpaths, timeout=None, on_progress=None,
                     **kw):
            calls.append(list(relpaths))
            d._recovery_needed.set()          # the watchdog saw the mount go
            return SyncResult("failure", 30, 0, 0, [], "io_timeout")

        d.engine.phase1_batch = _timeout
        with self.assertRaises(SyncFailure):
            d.full_pass(None)
        self.assertEqual(len(calls), 1)

    def test_other_failure_rc_is_not_retried(self):
        self._mk("a.txt", "payload")
        d = self._daemon()
        calls = []

        def _rc12(src, dst, backup, relpaths, timeout=None, on_progress=None,
                  **kw):
            calls.append(list(relpaths))
            return SyncResult("failure", 12, 0, 0, [])

        d.engine.phase1_batch = _rc12
        with self.assertRaises(SyncFailure):
            d.full_pass(None)
        self.assertEqual(len(calls), 1)

    def test_retry_does_not_double_count_progress(self):
        # The retried rsync re-emits its itemize lines from zero, so the first
        # attempt's in-flight credit must be rolled back or the displayed file
        # count runs past the batch size.
        self._mk("a.txt", "one")
        self._mk("b.txt", "two")
        d = self._daemon()
        calls = []

        def _flaky(src, dst, backup, relpaths, timeout=None, on_progress=None,
                   **kw):
            calls.append(list(relpaths))
            for i, rel in enumerate(relpaths, start=1):
                on_progress(i, rel)
            if len(calls) == 1:
                return SyncResult("failure", 30, 0, 0, [], "io_timeout")
            return SyncResult("ok", 0, len(relpaths), 0, [])

        d.engine.phase1_batch = _flaky
        shown = []
        d._on_progress = lambda folder, n, cur, ph="transferring": shown.append(n)
        agg = {"copied": 0, "failed": [], "degraded": False, "downloaded": 0,
               "evicted": 0, "files_done": 0, "dataless_pending": []}
        d._upload_relpaths(
            "Alpha", self.src, os.path.join(self.data_root, "Alpha"),
            os.path.join(self.trash_root, "stamp", "Alpha"),
            ["a.txt", "b.txt"], agg)
        self.assertEqual(len(calls), 2)
        self.assertEqual(max(shown), 2)          # never counted a file twice
        self.assertEqual(agg["files_done"], 2)


class TestP1WalkErrorSuppression(V2Base):
    """PLAN 16.2 T-B1: a pass whose local walk hit errors must not delete.

    An unreadable directory makes its whole subtree absent from the plan walk,
    and every manifest row under it then reads as 'vanished' -- indistinguishable
    from a real deletion. On 2026-07-30 an iCloud fileproviderd hiccup did that
    to 1000 live files (the circuit breaker held those, but a sub-threshold set
    would have gone to the trash for real). Uploads keep flowing; deletions and
    renames wait for a walk that can be trusted.
    """

    def _break_scandir_for(self, target):
        """os.scandir raises PermissionError for target only; real elsewhere."""
        real = os.scandir
        wanted = os.path.realpath(target)

        def _fake(path="."):
            if os.path.realpath(os.fspath(path)) == wanted:
                raise PermissionError(13, "Permission denied", str(path))
            return real(path)

        manifest_mod.os.scandir = _fake
        self.addCleanup(self._restore_scandir, real)
        return real

    @staticmethod
    def _restore_scandir(real):
        manifest_mod.os.scandir = real

    def _journal(self):
        path = self.state.journal_path
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as h:
            return [json.loads(line) for line in h if line.strip()]

    def test_deletions_suppressed_uploads_continue_then_recover(self):
        self.cfg.max_delete_pct = 100   # isolate the guard from the pct breaker
        self._mk("victim.txt"); self._mk("stay.txt")
        self._mk("flaky/one.txt"); self._mk("flaky/two.txt")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        self.assertEqual(len(self._mirror_files()), 4)

        # Round 2: 'flaky/' turns unreadable AND victim.txt is really deleted.
        os.remove(os.path.join(self.src, "victim.txt"))
        self._mk("fresh.txt", "written during the flaky pass")
        real = self._break_scandir_for(os.path.join(self.src, "flaky"))
        self.assertTrue(d.full_pass(None))

        # Nothing was trashed: not the phantom flaky/* rows, and not even the
        # genuinely deleted victim.txt -- this walk cannot tell them apart.
        rows = self.manifest.rows("Alpha")
        self.assertIn("victim.txt", self._mirror_files())
        self.assertIn("victim.txt", rows)
        self.assertIn("flaky/one.txt", rows)
        self.assertIn("flaky/two.txt", rows)
        # Uploads are untouched by the guard.
        self.assertIn("fresh.txt", self._mirror_files())
        self.assertIn("fresh.txt", rows)

        events = [e for e in self._journal()
                  if e.get("event") == "deletions_suppressed"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["folder"], "Alpha")
        self.assertGreaterEqual(events[0]["walk_errors"], 1)
        self.assertEqual(events[0]["deletions"], 3)

        # Round 3: the walk is clean again, so the real deletion propagates and
        # the phantom ones evaporate on their own.
        self._restore_scandir(real)
        self.assertTrue(d.full_pass(None))
        self.assertNotIn("victim.txt", self._mirror_files())
        rows = self.manifest.rows("Alpha")
        self.assertNotIn("victim.txt", rows)
        self.assertEqual(sorted(rows),
                         ["flaky/one.txt", "flaky/two.txt", "fresh.txt",
                          "stay.txt"])

    def test_renames_are_suppressed_and_replayed_next_pass(self):
        # A rename pairs a vanished old path with an appeared new one, so it is
        # poisoned by the same bad walk -- and deferring it is free: the pair is
        # re-detected intact on the next clean pass.
        self.cfg.max_delete_pct = 100
        self._mk("orig.txt", "rename me, i am content")
        self._mk("flaky/one.txt")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))

        os.rename(os.path.join(self.src, "orig.txt"),
                  os.path.join(self.src, "moved.txt"))
        real = self._break_scandir_for(os.path.join(self.src, "flaky"))
        self.assertTrue(d.full_pass(None))
        self.assertEqual(self._mirror_files(), ["flaky/one.txt", "orig.txt"])
        self.assertIn("orig.txt", self.manifest.rows("Alpha"))

        self._restore_scandir(real)
        self.assertTrue(d.full_pass(None))
        self.assertEqual(self._mirror_files(), ["flaky/one.txt", "moved.txt"])
        self.assertEqual(sorted(self.manifest.rows("Alpha")),
                         ["flaky/one.txt", "moved.txt"])

    def test_clean_pass_journals_nothing_and_deletes_normally(self):
        self.cfg.max_delete_pct = 100
        self._mk("a.txt"); self._mk("b.txt")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        os.remove(os.path.join(self.src, "a.txt"))
        self.assertTrue(d.full_pass(None))
        self.assertEqual(self._mirror_files(), ["b.txt"])
        self.assertEqual(
            [e for e in self._journal()
             if e.get("event") == "deletions_suppressed"], [])


class TestP1ExcludeMigration(V2Base):
    """PLAN 16.2 T-B2: rows for newly excluded paths are forgotten, not deleted.

    Growing BUILTIN_EXCLUDES (developer litter, 2026-07-30) hides already-tracked
    files from the local walk. Without the forget pass at start/reload every one
    of those rows becomes a 'vanished' path, i.e. a mass fake deletion.
    """

    def _seed_litter_rows(self):
        self.manifest.commit_uploaded("Alpha", [
            (".venv/lib/python3.9/site-packages/x.py", 10, 111, 9001),
            ("proj/__pycache__/mod.cpython-39.pyc", 10, 222, 9002),
            ("proj/stale.pyc", 10, 333, 9003),
        ])

    def test_without_the_migration_those_rows_are_deletions(self):
        # Control for the test below -- this IS the storm the migration exists
        # to prevent. If this ever stops holding, the migration is moot.
        self._mk("real.txt", "user data")
        self._seed_litter_rows()
        d = self._daemon()
        plan = self.manifest.plan_folder(
            "Alpha", self.src, d._effective_excludes(), d.dataless_check)
        self.assertEqual(len(plan.deletions), 3)
        self.assertEqual(plan.walk_errors, 0)

    def test_startup_forget_then_pass_deletes_nothing(self):
        self._mk("real.txt", "user data")
        self._seed_litter_rows()
        d = self._daemon()
        d._forget_excluded_rows()           # what run()/reload does at startup
        self.assertEqual(sorted(self.manifest.rows("Alpha")), [])

        self.assertTrue(d.full_pass(None))
        self.assertEqual(self._mirror_files(), ["real.txt"])
        self.assertEqual(sorted(self.manifest.rows("Alpha")), ["real.txt"])
        entry = self.state.get()["folders"]["Alpha"]
        self.assertEqual(entry["state"], F_UP_TO_DATE)
        self.assertNotEqual(entry["state"], F_DELETIONS_HELD)
        self.assertEqual(int(entry.get("files_trashed", 0) or 0), 0)
        self.assertEqual(entry.get("held_deletions", []), [])
        trashed = []
        for _dp, _dd, files in os.walk(self.trash_root):
            trashed.extend(files)
        self.assertEqual(trashed, [])

    def test_user_exclude_patterns_are_honored_and_it_is_idempotent(self):
        self.cfg.excludes = ["*.photoslibrary"]
        self.manifest.commit_uploaded("Alpha", [
            ("Pictures/My.photoslibrary/db", 10, 111, 9101),
            ("keep.txt", 10, 222, 9102),
        ])
        d = self._daemon()
        d._forget_excluded_rows()
        self.assertEqual(sorted(self.manifest.rows("Alpha")), ["keep.txt"])
        d._forget_excluded_rows()
        self.assertEqual(sorted(self.manifest.rows("Alpha")), ["keep.txt"])


if __name__ == "__main__":
    unittest.main()


class TestP1StDevRebootTolerance(V2Base):
    """st_dev is not a stable identity: APFS/FileProvider volumes get a fresh
    device minor on reboot (2026-08-01: all three real sources flipped
    16777231 -> 16777234 and every folder wedged in sourceMissing). A changed
    device id with a readable, NON-EMPTY source must sync normally; only an
    empty/unreadable source keeps the conservative skip (PLAN 6.4's actual
    threat: an unmounted external disk's empty stub directory)."""

    def test_dev_drift_with_healthy_source_syncs_and_adopts(self):
        self._mk("a.txt", "content")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))          # records the real st_dev
        real_dev = self.state.get()["folders"]["Alpha"]["st_dev"]
        # Simulate the reboot: the recorded device id no longer matches.
        self.state.folder_update("Alpha", st_dev=real_dev + 3)
        self._mk("b.txt", "new file after reboot")
        d2 = self._daemon()
        self.assertTrue(d2.full_pass(None))
        entry = self.state.get()["folders"]["Alpha"]
        self.assertEqual(entry["state"], F_UP_TO_DATE)      # not sourceMissing
        self.assertEqual(entry["st_dev"], real_dev)         # new id adopted
        self.assertIn("b.txt", self._mirror_files())        # and it synced
        self.assertNotIn("source_device", self.notifier.calls)

    def test_dev_drift_with_empty_source_still_skips(self):
        self._mk("a.txt", "content")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        real_dev = self.state.get()["folders"]["Alpha"]["st_dev"]
        os.remove(os.path.join(self.src, "a.txt"))          # source now EMPTY
        self.state.folder_update("Alpha", st_dev=real_dev + 3)
        d2 = self._daemon()
        d2.full_pass(None)
        entry = self.state.get()["folders"]["Alpha"]
        self.assertEqual(entry["state"], "sourceMissing")
        self.assertIn("source_device", self.notifier.calls)
        self.assertIn("a.txt", self._mirror_files())        # mirror untouched


class TestP1DanglingSymlinkPoisonPill(V2Base):
    """openrsync stat()s every --files-from entry and silently DROPS the batch
    remainder after the first failure (verified 2026-08-01: [good, broken-link,
    good2] transfers only `good`). On the real machine 61 dangling venv/ links
    wedged 911 innocent files in a permanent retry loop. Dangling links must
    never reach rsync; they are disclosed via failed_files instead."""

    def test_batcher_skips_dangling_link_and_reports_it(self):
        self._mk("aaa.txt", "good1")
        os.symlink("/nonexistent/target",
                   os.path.join(self.src, "broken_link"))
        self._mk("zzz.txt", "good2")
        d = self._daemon()
        seen = []
        batches = list(d._stat_batches(
            self.src, ["aaa.txt", "broken_link", "zzz.txt"],
            on_broken=seen.append))
        rels = [rel for batch, _b in batches for (rel, _s, _m, _i) in batch]
        self.assertEqual(rels, ["aaa.txt", "zzz.txt"])   # pill excluded
        self.assertEqual(seen, ["broken_link"])          # and reported

    def test_valid_symlink_still_ships(self):
        self._mk("target.txt", "real content")
        os.symlink(os.path.join(self.src, "target.txt"),
                   os.path.join(self.src, "good_link"))
        d = self._daemon()
        seen = []
        batches = list(d._stat_batches(
            self.src, ["good_link", "target.txt"], on_broken=seen.append))
        rels = [rel for batch, _b in batches for (rel, _s, _m, _i) in batch]
        self.assertIn("good_link", rels)
        self.assertEqual(seen, [])

    def test_full_pass_unwedges_files_behind_the_pill(self):
        # End-to-end regression for the 972-file wedge: files sorting AFTER a
        # dangling link must transfer and commit; the pill lands in
        # failed_files; the pass keeps pending from wedging forever.
        self._mk("aaa.txt", "before the pill")
        os.symlink("/nonexistent/target",
                   os.path.join(self.src, "mmm_broken"))
        self._mk("zzz.txt", "after the pill")
        d = self._daemon()
        self.assertTrue(d.full_pass(None) is not None)
        files = self._mirror_files()
        self.assertIn("aaa.txt", files)
        self.assertIn("zzz.txt", files)                  # the previously-wedged tail
        self.assertNotIn("mmm_broken", files)
        rows = self.manifest.rows("Alpha")
        self.assertIn("zzz.txt", rows)                   # committed, not pending
        self.assertFalse(rows["zzz.txt"]["pending"])
        entry = self.state.get()["folders"]["Alpha"]
        self.assertIn("mmm_broken", entry.get("failed_files") or [])
