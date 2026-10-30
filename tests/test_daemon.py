import tests._path  # noqa: F401

import io
import json
import logging
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

import mycloudctl
from mycloud import config as config_mod
from mycloud import paths
from mycloud.daemon import (
    Daemon, F_SOURCE_MISSING, F_DELETIONS_HELD, F_DEGRADED, SyncFailure)
from mycloud.engine import SyncEngine, SyncResult
from mycloud.state import StateStore
from mycloud.trash import TrashManager

_LOG = logging.getLogger("mycloud.test")
_LOG.addHandler(logging.NullHandler())


class StubMount(object):
    """Mount that is always healthy; mountpoint is a local temp directory."""

    def __init__(self, mountpoint):
        self.mountpoint = Path(mountpoint)
        self.guard_calls = 0

    def guard_ok(self, timeout=10):
        self.guard_calls += 1
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
    """Records notify() calls; never touches osascript."""

    def __init__(self):
        self.calls = []

    def runner(self, cmd, timeout):
        return None

    def notify(self, cls, title, body, min_hours=6.0, force=False):
        self.calls.append(cls)
        return True

    def classes(self):
        return [c for c in self.calls]


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _find_under(root, basename):
    for dirpath, _dirs, files in os.walk(root):
        if basename in files:
            return os.path.join(dirpath, basename)
    return None


class DaemonTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mycloud-daemon-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.src_a = os.path.join(self.tmp, "src", "Alpha")
        self.src_b = os.path.join(self.tmp, "src", "Beta")
        os.makedirs(self.src_a)
        os.makedirs(self.src_b)

        self.fakenas = os.path.join(self.tmp, "fakenas")
        os.makedirs(self.fakenas)
        self.runtime = os.path.join(self.tmp, "runtime")
        os.makedirs(self.runtime)

        self.excludes = os.path.join(self.runtime, "excludes.txt")
        self.cfg = config_mod.Config()
        # Serial uploads keep batch-order-dependent tests deterministic;
        # concurrency-specific tests raise this themselves.
        self.cfg.upload_concurrency = 1
        self.cfg.folders = [
            config_mod.FolderSpec(Path(self.src_a), "Alpha"),
            config_mod.FolderSpec(Path(self.src_b), "Beta"),
        ]
        self.cfg.excludes = []
        config_mod.write_excludes(self.cfg, self.excludes)

        self.state = StateStore(self.runtime, logger=_LOG)
        self.proc_registry = set()
        self.engine = SyncEngine(paths.RSYNC, self.excludes, _LOG,
                                 proc_registry=self.proc_registry)
        self.mount = StubMount(self.fakenas)
        self.trash_root = os.path.join(self.fakenas, paths.TRASH_REL)
        self.trash_mgr = TrashManager(self.trash_root,
                                      self.cfg.retention_days,
                                      self.cfg.trash_max_gb, _LOG)
        self.notifier = StubNotifier()
        self.data_root = os.path.join(self.fakenas, paths.DATA_REL)

    def _daemon(self, once=False):
        return Daemon(self.cfg, self.state, self.mount, self.engine,
                      self.trash_mgr, self.notifier, _LOG, once=once)

    def _mirror(self, name, rel):
        return os.path.join(self.data_root, name, rel)


class TestMirroring(DaemonTestBase):
    def test_once_initial_pass_mirrors_two_folders(self):
        _write(os.path.join(self.src_a, "one.txt"), "hello")
        _write(os.path.join(self.src_b, "nested", "two.txt"), "world")

        rc = self._daemon(once=True).run()
        self.assertEqual(rc, 0)

        self.assertTrue(os.path.exists(self._mirror("Alpha", "one.txt")))
        self.assertTrue(os.path.exists(self._mirror("Beta", "nested/two.txt")))
        with open(self._mirror("Alpha", "one.txt"), encoding="utf-8") as h:
            self.assertEqual(h.read(), "hello")

    def test_second_run_propagates_change_and_delete(self):
        keep = os.path.join(self.src_a, "keep.txt")
        gone = os.path.join(self.src_a, "gone.txt")
        _write(keep, "v1")
        _write(gone, "delete-me")
        # Filler so a single deletion stays under the 20% circuit breaker.
        for i in range(20):
            _write(os.path.join(self.src_a, "filler%02d.txt" % i), "x")
        _write(os.path.join(self.src_b, "b.txt"), "b")

        self.assertEqual(self._daemon(once=True).run(), 0)
        self.assertTrue(os.path.exists(self._mirror("Alpha", "gone.txt")))

        # Modify keep, delete gone.
        _write(keep, "v2-changed")
        os.remove(gone)

        self.assertEqual(self._daemon(once=True).run(), 0)

        with open(self._mirror("Alpha", "keep.txt"), encoding="utf-8") as h:
            self.assertEqual(h.read(), "v2-changed")
        self.assertFalse(os.path.exists(self._mirror("Alpha", "gone.txt")))
        trashed = _find_under(self.trash_root, "gone.txt")
        self.assertIsNotNone(trashed)


class TestCircuitBreaker(DaemonTestBase):
    def test_large_deletion_held_then_approved(self):
        self.cfg.max_delete_files = 2
        files = ["a.txt", "b.txt", "c.txt"]
        for f in files:
            _write(os.path.join(self.src_a, f), "data-" + f)
        # Survivors keep the source non-empty (isolates the breaker from the
        # source-empty guard).
        for f in ["keep1.txt", "keep2.txt"]:
            _write(os.path.join(self.src_a, f), "keep")
        _write(os.path.join(self.src_b, "b.txt"), "b")

        d = self._daemon()
        d.full_pass(None)
        for f in files:
            self.assertTrue(os.path.exists(self._mirror("Alpha", f)))

        # Delete all three; exceeds max_delete_files=2.
        for f in files:
            os.remove(os.path.join(self.src_a, f))

        d.full_pass(None)
        # Nothing removed from the mirror; deletion is held.
        for f in files:
            self.assertTrue(os.path.exists(self._mirror("Alpha", f)))
        fstate = self.state.get()["folders"]["Alpha"]
        self.assertEqual(fstate["state"], F_DELETIONS_HELD)
        self.assertEqual(len(fstate["held_deletions"]), 3)
        self.assertIn("deletions_held", self.notifier.calls)

        # Approve and re-run: deletions execute, token consumed.
        approval = os.path.join(self.runtime, "approval.token")
        _write(approval, "ok")
        d.full_pass(None)
        for f in files:
            self.assertFalse(os.path.exists(self._mirror("Alpha", f)))
        self.assertFalse(os.path.exists(approval))
        self.assertIsNotNone(_find_under(self.trash_root, "a.txt"))


class TestHeldSchemaRoundTrip(DaemonTestBase):
    """The state.json the daemon writes on a hold must be readable by the CLI.

    Guards the daemon/CLI schema contract end-to-end (the two suites otherwise
    fabricate their own held-state shapes), so mycloudctl status shows the HELD
    banner and approve-deletions can actually release the hold.
    """

    def test_daemon_hold_is_visible_and_approvable_by_ctl(self):
        self.cfg.max_delete_files = 2
        victims = ["a.txt", "b.txt", "c.txt"]
        for f in victims:
            _write(os.path.join(self.src_a, f), "data-" + f)
        _write(os.path.join(self.src_a, "keep.txt"), "keep")
        _write(os.path.join(self.src_b, "b.txt"), "b")

        d = self._daemon()
        d.full_pass(None)
        for f in victims:
            os.remove(os.path.join(self.src_a, f))
        d.full_pass(None)  # writes the held state to state.json

        # A real Ctl pointed at the SAME runtime dir the daemon just wrote.
        def _ctl(out, answer="yes"):
            return mycloudctl.Ctl(
                runtime_dir=self.runtime, logdir=self.runtime,
                config_path=os.path.join(self.runtime, "config.ini"),
                out=out, kill=lambda pid, sig: None,
                input_fn=lambda prompt="": answer)

        # status: the HELD banner and the count are visible.
        out_status = io.StringIO()
        _ctl(out_status).cmd_status(as_json=False)
        self.assertIn("DELETIONS HELD", out_status.getvalue())

        # approve-deletions: finds the held paths and writes the token.
        out_appr = io.StringIO()
        rc = _ctl(out_appr).cmd_approve_deletions()
        self.assertEqual(rc, 0)
        self.assertIn("a.txt", out_appr.getvalue())
        token = os.path.join(self.runtime, "approval.token")
        self.assertTrue(os.path.exists(token))

        # The daemon honors that token on the next pass and releases the hold.
        d.full_pass(None)
        for f in victims:
            self.assertFalse(os.path.exists(self._mirror("Alpha", f)))
        self.assertFalse(os.path.exists(token))


class TestCapabilityFailClosed(DaemonTestBase):
    def test_fingerprint_mismatch_disables_deletion(self):
        for f in ["a.txt", "b.txt", "c.txt"]:
            _write(os.path.join(self.src_a, f), "x")
        _write(os.path.join(self.src_b, "b.txt"), "b")

        d = self._daemon()
        d.full_pass(None)

        # Pre-seed a fingerprint whose KNOWN keys carry flipped values (a real
        # behavior change) so the probe fails closed.
        d._capability_check()  # records the true fingerprint
        true_map = json.loads(self.state.get()["capability_fingerprint"])
        stale = dict(true_map)
        stale["delete_suppressed_with_backup"] = (
            not stale["delete_suppressed_with_backup"])
        self.state.update(capability_fingerprint=json.dumps(
            stale, sort_keys=True))
        d._capability_check()
        self.assertTrue(d.deletion_disabled)
        self.assertIn("capability", self.notifier.calls)

        # Delete a source file; deletion must NOT propagate.
        os.remove(os.path.join(self.src_a, "a.txt"))
        d.full_pass(None)
        self.assertTrue(os.path.exists(self._mirror("Alpha", "a.txt")))

    def test_new_probe_key_is_recorded_without_freezing(self):
        # An upgrade that ADDS a probe (inplace_updates_ok, 2026-07-21) must not
        # read as a behavior change on an already-deployed machine.
        d = self._daemon()
        d._capability_check()  # records the current (full) fingerprint
        full = json.loads(self.state.get()["capability_fingerprint"])
        legacy = {k: v for k, v in full.items() if k != "inplace_updates_ok"}
        self.assertNotEqual(legacy, full)
        self.state.update(capability_fingerprint=json.dumps(
            legacy, sort_keys=True), deletion_disabled=False)
        d.deletion_disabled = False
        d._capability_check()
        self.assertFalse(d.deletion_disabled)
        self.assertNotIn("capability", self.notifier.calls)
        # The stored fingerprint was widened to include the new key.
        widened = json.loads(self.state.get()["capability_fingerprint"])
        self.assertIn("inplace_updates_ok", widened)

    def test_unreadable_fingerprint_rerecords_without_freezing(self):
        d = self._daemon()
        self.state.update(capability_fingerprint="STALE-FINGERPRINT")
        d._capability_check()
        self.assertFalse(d.deletion_disabled)
        self.assertNotIn("capability", self.notifier.calls)
        json.loads(self.state.get()["capability_fingerprint"])  # valid again

    def test_probe_gates_inplace_capability(self):
        d = self._daemon()
        self.assertFalse(d._inplace_capable)  # conservative before the probe
        d._capability_check()
        # The real openrsync on this machine passes the inplace probe.
        self.assertTrue(d._inplace_capable)
        self.assertTrue(self.state.get().get("inplace_capable"))


class TestSourceGuards(DaemonTestBase):
    def test_missing_source_skips_folder_untouched(self):
        _write(os.path.join(self.src_a, "keep.txt"), "a")
        _write(os.path.join(self.src_b, "b.txt"), "b")

        d = self._daemon()
        d.full_pass(None)
        self.assertTrue(os.path.exists(self._mirror("Alpha", "keep.txt")))

        # Remove the entire source directory for Alpha.
        shutil.rmtree(self.src_a)
        d.full_pass(None)

        # Mirror untouched; folder flagged sourceMissing.
        self.assertTrue(os.path.exists(self._mirror("Alpha", "keep.txt")))
        self.assertEqual(self.state.get()["folders"]["Alpha"]["state"], F_SOURCE_MISSING)


class TestOrphans(DaemonTestBase):
    def test_orphan_dir_marked_not_touched(self):
        _write(os.path.join(self.src_a, "a.txt"), "a")
        _write(os.path.join(self.src_b, "b.txt"), "b")

        d = self._daemon()
        d.full_pass(None)

        # A leftover mirror from a folder no longer configured.
        orphan = os.path.join(self.data_root, "OldFolder")
        _write(os.path.join(orphan, "leftover.txt"), "keepme")

        d.full_pass(None)
        self.assertTrue(self.state.get()["folders"]["OldFolder"]["orphaned"])
        self.assertTrue(os.path.exists(os.path.join(orphan, "leftover.txt")))


class TestFileSource(DaemonTestBase):
    def test_once_mode_file_mirror_then_delete_archives(self):
        file_src = os.path.join(self.tmp, "src", "hello.txt")
        _write(file_src, "hi")
        self.cfg.folders.append(
            config_mod.FolderSpec(Path(file_src), "hello.txt", is_file=True))
        _write(os.path.join(self.src_a, "a.txt"), "a")
        _write(os.path.join(self.src_b, "b.txt"), "b")

        # First pass: the file mirror appears directly under data_root.
        self.assertEqual(self._daemon(once=True).run(), 0)
        mirror = os.path.join(self.data_root, "hello.txt")
        self.assertTrue(os.path.exists(mirror))
        with open(mirror, encoding="utf-8") as h:
            self.assertEqual(h.read(), "hi")

        # Delete the source file: next pass archives the mirror via the breaker.
        os.remove(file_src)
        self.assertEqual(self._daemon(once=True).run(), 0)
        self.assertFalse(os.path.exists(mirror))
        self.assertIsNotNone(_find_under(self.trash_root, "hello.txt"))

    def test_file_overwrite_versions_into_trash(self):
        file_src = os.path.join(self.tmp, "src", "note.txt")
        _write(file_src, "v1")
        self.cfg.folders = [
            config_mod.FolderSpec(Path(file_src), "note.txt", is_file=True)]
        d = self._daemon()
        d.full_pass(None)
        mirror = os.path.join(self.data_root, "note.txt")
        with open(mirror, encoding="utf-8") as h:
            self.assertEqual(h.read(), "v1")

        _write(file_src, "v2-changed")
        d.full_pass(None)
        with open(mirror, encoding="utf-8") as h:
            self.assertEqual(h.read(), "v2-changed")
        # The overwritten old version was parked in the trash by rsync's -b.
        old = _find_under(self.trash_root, "note.txt")
        self.assertIsNotNone(old)
        with open(old, encoding="utf-8") as h:
            self.assertEqual(h.read(), "v1")


class TestDestRel(DaemonTestBase):
    def test_custom_dest_rel_creates_layout_under_dest(self):
        self.cfg.dest_rel = "Custom/Base"
        _write(os.path.join(self.src_a, "a.txt"), "a")
        _write(os.path.join(self.src_b, "b.txt"), "b")

        d = self._daemon()
        d.full_pass(None)

        base = os.path.join(self.fakenas, "Custom", "Base")
        self.assertTrue(os.path.isdir(os.path.join(base, "data")))
        self.assertTrue(os.path.isdir(os.path.join(base, ".trash")))
        # The mirror lands under the custom data root, not the default one.
        self.assertTrue(os.path.exists(os.path.join(base, "data", "Alpha", "a.txt")))
        self.assertFalse(os.path.isdir(os.path.join(self.fakenas, "MyCloud", "data", "Alpha")))

    def test_dest_change_notifies_and_persists(self):
        _write(os.path.join(self.src_a, "a.txt"), "a")
        _write(os.path.join(self.src_b, "b.txt"), "b")
        # A previously-stored dest_rel that differs from the configured one.
        self.state.update(dest_rel="OldPlace")

        d = self._daemon()  # cfg.dest_rel defaults to "MyCloud"
        d.full_pass(None)

        self.assertIn("dest_changed", self.notifier.calls)
        self.assertEqual(self.state.get()["dest_rel"], "MyCloud")

    def test_first_run_persists_dest_without_notify(self):
        _write(os.path.join(self.src_a, "a.txt"), "a")
        _write(os.path.join(self.src_b, "b.txt"), "b")
        d = self._daemon()
        d.full_pass(None)
        self.assertEqual(self.state.get()["dest_rel"], "MyCloud")
        self.assertNotIn("dest_changed", self.notifier.calls)


class TestProgress(DaemonTestBase):
    def test_progress_written_active_then_inactive(self):
        for i in range(6):
            _write(os.path.join(self.src_a, "f%02d.txt" % i), "x" * (i + 1))
        _write(os.path.join(self.src_b, "b.txt"), "b")

        d = self._daemon()
        d._progress_min_interval = 0.0  # disable throttle: capture every write

        captured = []
        orig_update = self.state.update

        def _spy(**fields):
            if "progress" in fields:
                captured.append(dict(fields["progress"]))
            return orig_update(**fields)

        self.state.update = _spy
        d.full_pass(None)

        active = [p for p in captured if p.get("active")]
        self.assertTrue(active, "expected at least one active progress write")
        first = active[0]
        self.assertIn(first["folder"], ("Alpha", "Beta"))
        self.assertIsInstance(first["files_done"], int)
        self.assertIn("current", first)
        self.assertIn("started_at", first)
        # The pass ends by marking progress inactive.
        self.assertFalse(captured[-1]["active"])


class TestDestLayoutError(DaemonTestBase):
    def test_oserror_degrades_folder_not_daemon(self):
        # Regression: a bad destination layout (data_root/<name> collides with an
        # existing file) makes rsync's os.makedirs raise NotADirectoryError.
        # full_pass must degrade the folder, never let the OSError escape and
        # crash the daemon.
        _write(os.path.join(self.src_a, "one.txt"), "hi")
        d = self._daemon()

        def _boom(*a, **k):
            raise NotADirectoryError("data_root is a file")

        d.engine.phase1_batch = _boom
        # Must not raise.
        d.full_pass(None)
        folders = self.state.get().get("folders", {})
        self.assertEqual(folders.get("Alpha", {}).get("state"), F_DEGRADED)


class TestLock(DaemonTestBase):
    def test_duplicate_lock_exits_clean(self):
        d1 = self._daemon()
        self.assertTrue(d1._acquire_lock())
        self.addCleanup(d1._release_lock)
        # PID recorded as first token, matching mycloudctl's reader.
        with open(d1.lock_path, encoding="utf-8") as h:
            self.assertEqual(int(h.read().split()[0]), os.getpid())
        d2 = self._daemon()
        self.assertFalse(d2._acquire_lock())


class _GuardStubMount(object):
    """Mount whose four layers are individually configurable, for _guard_check."""

    def __init__(self, mountpoint, ok=False, last_failure="tcp445",
                 mounted=True, sentinel=True, session=True):
        self.mountpoint = Path(mountpoint)
        self._ok = ok
        self.last_failure = last_failure
        self._mounted = mounted
        self._sentinel = sentinel
        self._session = session

    def guard_ok(self, timeout=10):
        return self._ok

    def is_mounted(self, timeout=10):
        return self._mounted

    def sentinel_ok(self, timeout=10):
        return self._sentinel

    def session_ok(self, timeout=10):
        return self._session

    def mount(self):
        return True

    def force_unmount(self):
        pass

    def ensure_bare_dir_safe(self):
        pass


class _RecLogger(object):
    """Records (level, message) tuples with %-args applied."""

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


class TestGuardCheck(DaemonTestBase):
    """F3: failing-layer logging, the Local-Network heuristic, and soft-pass."""

    def _daemon_with(self, mount, logger=None):
        return Daemon(self.cfg, self.state, mount, self.engine,
                      self.trash_mgr, self.notifier, logger or _LOG)

    def test_guard_ok_passes_through_silently(self):
        d = self._daemon_with(_GuardStubMount(self.fakenas, ok=True))
        self.assertTrue(d._guard_check())
        self.assertEqual(self.notifier.calls, [])

    def test_tcp445_while_session_alive_soft_passes(self):
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="tcp445",
                              mounted=True, sentinel=True, session=True)
        log = _RecLogger()
        d = self._daemon_with(mnt, logger=log)
        # tcp445 is the only failing layer and the session is provably alive:
        # soft-pass so syncing continues under the Local-Network denial.
        self.assertTrue(d._guard_check())
        # Log-only, and at INFO: nothing is wrong (the soft-pass IS the bypass)
        # and the user cannot act anyway -- macOS lists no Local Network switch
        # for a bare launchd python3 (2026-07-31 user report). Deliberately no
        # notification and no pointer at System Settings.
        self.assertNotIn("local_network", self.notifier.calls)
        infos = " ".join(log.msgs("info"))
        self.assertIn("本地网络", infos)
        self.assertIn("无需任何操作", infos)
        self.assertNotIn("系统设置 > 隐私与安全性", infos)

    def test_tcp445_mounted_but_session_dead_does_not_soft_pass(self):
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="tcp445",
                              mounted=True, sentinel=True, session=False)
        d = self._daemon_with(mnt)
        self.assertFalse(d._guard_check())
        # No notification here either: the failure keeps the daemon offline and
        # recovery handles it; a toast asking for an impossible setting is noise.
        self.assertNotIn("local_network", self.notifier.calls)

    def test_tcp445_sentinel_dead_does_not_soft_pass(self):
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="tcp445",
                              mounted=True, sentinel=False, session=True)
        d = self._daemon_with(mnt)
        self.assertFalse(d._guard_check())

    def test_tcp445_not_mounted_is_plain_offline(self):
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="tcp445",
                              mounted=False)
        log = _RecLogger()
        d = self._daemon_with(mnt, logger=log)
        self.assertFalse(d._guard_check())
        # Genuine outage: no Local-Network notification.
        self.assertNotIn("local_network", self.notifier.calls)

    def test_non_tcp445_failure_logs_layer_without_notify(self):
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="sentinel",
                              mounted=True)
        log = _RecLogger()
        d = self._daemon_with(mnt, logger=log)
        self.assertFalse(d._guard_check())
        self.assertNotIn("local_network", self.notifier.calls)
        self.assertIn("sentinel", " ".join(log.msgs("info")))

    def test_failure_logging_is_rate_limited(self):
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="tcp445",
                              mounted=True, sentinel=True, session=True)
        d = self._daemon_with(mnt)
        log = _RecLogger()
        d = self._daemon_with(mnt, logger=log)
        for _ in range(5):
            self.assertTrue(d._guard_check())  # soft-pass each time
        # Five consecutive failures within the 10-minute window log the hint
        # exactly once (rate-limited), and never notify (nothing to act on).
        infos = [m for m in log.msgs("info") if "本地网络" in m]
        self.assertEqual(len(infos), 1)
        self.assertNotIn("local_network", self.notifier.calls)


class TestWatchdogKill(DaemonTestBase):
    def test_kill_sync_procs_terminates_registered(self):
        import subprocess as sp
        proc = sp.Popen(["/bin/sh", "-c", "sleep 30"], start_new_session=True)
        self.proc_registry.add(proc)
        d = self._daemon()
        d._kill_sync_procs()
        self.assertEqual(proc.wait(timeout=10), -15)  # SIGTERM


class TestWatchdogPolicy(DaemonTestBase):
    """A1: the guard watchdog kills the rsync group ONLY when the mount is gone
    (immediately) or the soft-pass-aware guard fails on 3 consecutive checks --
    never on a single transient timeout and never during a Local-Network
    soft-pass. _watchdog_tick is the unit under test (no 60s sleep needed)."""

    def _daemon_with(self, mount):
        return Daemon(self.cfg, self.state, mount, self.engine,
                      self.trash_mgr, self.notifier, _LOG)

    def _armed(self, mount):
        d = self._daemon_with(mount)
        d._syncing.set()
        self.killed = []  # type: list
        # Stub the killer so nothing real is signalled; record the kill reason.
        d._kill_sync_procs = lambda reason=None: self.killed.append(reason)
        return d

    def test_no_kill_on_single_guard_failure(self):
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="session",
                              mounted=True, sentinel=True, session=False)
        d = self._armed(mnt)
        d._watchdog_tick()
        self.assertEqual(self.killed, [])
        self.assertFalse(d._recovery_needed.is_set())
        self.assertEqual(d._watchdog_consecutive, 1)

    def test_kill_on_mount_gone(self):
        # is_mounted False -> kill immediately, no consecutive streak required.
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="is_mounted",
                              mounted=False)
        d = self._armed(mnt)
        d._watchdog_tick()
        self.assertEqual(self.killed, ["is_mounted"])
        self.assertTrue(d._recovery_needed.is_set())

    def test_kill_after_three_consecutive(self):
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="session",
                              mounted=True, sentinel=True, session=False)
        d = self._armed(mnt)
        d._watchdog_tick()
        d._watchdog_tick()
        self.assertEqual(self.killed, [])  # 2 in a row: still no kill
        d._watchdog_tick()  # third consecutive -> kill
        self.assertEqual(self.killed, ["session"])
        self.assertTrue(d._recovery_needed.is_set())

    def test_soft_pass_never_kills(self):
        # tcp445 the only failing layer while the session is provably alive:
        # _guard_check soft-passes, so the streak never accrues.
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="tcp445",
                              mounted=True, sentinel=True, session=True)
        d = self._armed(mnt)
        for _ in range(5):
            d._watchdog_tick()
        self.assertEqual(self.killed, [])
        self.assertEqual(d._watchdog_consecutive, 0)

    def test_consecutive_counter_resets_on_recovery(self):
        # Proves the rule is "3 IN A ROW", not cumulative: a recovery in the
        # middle zeroes the streak.
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="session",
                              mounted=True, sentinel=True, session=False)
        d = self._armed(mnt)
        d._watchdog_tick()
        d._watchdog_tick()          # 2 consecutive failures
        mnt._ok = True              # guard recovers
        d._watchdog_tick()          # resets the streak
        self.assertEqual(d._watchdog_consecutive, 0)
        mnt._ok = False             # fails again
        d._watchdog_tick()
        self.assertEqual(d._watchdog_consecutive, 1)
        self.assertEqual(self.killed, [])  # never 3 in a row -> never killed

    def test_widened_child_timeout_used_while_syncing(self):
        # The mount-table check during a sync must use the raised 30s timeout.
        seen = []

        class _TimeoutSpyMount(_GuardStubMount):
            def is_mounted(self, timeout=10):
                seen.append(timeout)
                return self._mounted

        mnt = _TimeoutSpyMount(self.fakenas, ok=True, mounted=True)
        d = self._armed(mnt)
        d._watchdog_tick()
        self.assertIn(30.0, seen)

    def test_no_kill_while_upload_progressing(self):
        # PLAN 15 P0 (2026-07-26): a guard read starved under parallel-upload
        # load must NOT kill rsync while the mount is present and files are
        # still transferring. Repeated sentinel/session timeouts with recent
        # upload progress => never kill, streak never accrues.
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="session",
                              mounted=True, sentinel=True, session=False)
        d = self._armed(mnt)
        d._last_upload_mono = time.monotonic()   # rsync just transferred a file
        for _ in range(6):
            d._watchdog_tick()
        self.assertEqual(self.killed, [])
        self.assertEqual(d._watchdog_consecutive, 0)
        self.assertFalse(d._recovery_needed.is_set())

    def test_kill_when_upload_progress_goes_stale(self):
        # If transfer progress goes STALE (session wedged, no itemize output)
        # the grace lapses and the normal 3-in-a-row kill path resumes.
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="session",
                              mounted=True, sentinel=True, session=False)
        d = self._armed(mnt)
        d._last_upload_mono = time.monotonic() - 1000.0   # older than the grace
        d._watchdog_tick()
        d._watchdog_tick()
        self.assertEqual(self.killed, [])                 # 2 in a row: no kill yet
        d._watchdog_tick()                                # third -> kill
        self.assertEqual(self.killed, ["session"])
        self.assertTrue(d._recovery_needed.is_set())

    def test_progress_grace_ignored_when_never_uploaded(self):
        # _last_upload_mono == 0 (no transfer yet this run, e.g. still planning)
        # must NOT be treated as "recent progress"; normal kill path applies.
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="session",
                              mounted=True, sentinel=True, session=False)
        d = self._armed(mnt)
        self.assertEqual(d._last_upload_mono, 0.0)
        d._watchdog_tick(); d._watchdog_tick(); d._watchdog_tick()
        self.assertEqual(self.killed, ["session"])


class TestKillReasonAttribution(DaemonTestBase):
    """A3: a phase1 failure caused by our OWN watchdog killpg is reported as
    'killed by watchdog (reason: <layer>)' instead of a bare rc."""

    def test_phase1_failure_msg_names_watchdog_reason_once(self):
        d = self._daemon()
        d._record_kill_reason("is_mounted")
        msg = d._phase1_failure_msg("phase1", "Alpha", -15)
        self.assertIn("killed by watchdog", msg)
        self.assertIn("is_mounted", msg)
        # The reason is consumed exactly once: a later failure with no fresh
        # kill reverts to the plain rc form.
        msg2 = d._phase1_failure_msg("phase1", "Alpha", -15)
        self.assertNotIn("killed by watchdog", msg2)
        self.assertIn("rc=-15", msg2)

    def test_stale_kill_reason_is_ignored(self):
        d = self._daemon()
        d._record_kill_reason("session")
        d._kill_reason_mono -= 120.0  # older than the 60s TTL
        msg = d._phase1_failure_msg("phase1", "Alpha", 20)
        self.assertNotIn("killed by watchdog", msg)
        self.assertIn("rc=20", msg)

    def test_full_pass_syncfailure_carries_watchdog_reason(self):
        _write(os.path.join(self.src_a, "a.txt"), "a")
        _write(os.path.join(self.src_b, "b.txt"), "b")
        d = self._daemon()

        def _killed_phase1(*a, **k):
            d._record_kill_reason("session")           # watchdog just killed us
            return SyncResult("failure", -15, 0, 0, [])

        d.engine.phase1_batch = _killed_phase1
        with self.assertRaises(SyncFailure) as ctx:
            d.full_pass(None)
        self.assertIn("killed by watchdog (reason: session)", str(ctx.exception))


class TestSyncHeartbeat(DaemonTestBase):
    """A2: progress carries phase planning->transferring, the heartbeat re-stamps
    it during a long no-progress plan/batch, and it clears at pass end (PLAN M5:
    the v1 "scanning" phase is replaced by "planning")."""

    def _spy_progress(self, d):
        captured = []  # type: list
        orig = self.state.update

        def _spy(**fields):
            if "progress" in fields:
                captured.append(dict(fields["progress"]))
            return orig(**fields)

        self.state.update = _spy
        return captured

    def test_phase_flips_scanning_to_transferring_then_clears(self):
        for i in range(3):
            _write(os.path.join(self.src_a, "f%d.txt" % i), "x")
        _write(os.path.join(self.src_b, "b.txt"), "b")
        d = self._daemon()
        d._progress_min_interval = 0.0  # capture every on_progress write
        captured = self._spy_progress(d)
        d.full_pass(None)

        phases = [p.get("phase") for p in captured if p.get("active")]
        self.assertIn("planning", phases)      # written at folder start
        self.assertIn("transferring", phases)  # after the first transfer line
        self.assertFalse(captured[-1]["active"])  # cleared at pass end

    def test_activity_fields_published_for_ui(self):
        # The "is it stuck?" signals (2026-07-27): a wall-clock stamp of the last
        # transfer line and a coarse batch counter. files_done advances in jumps
        # (rsync block-buffers itemize output), so without these a healthy slow
        # transfer is indistinguishable from a stall in the UI.
        for i in range(3):
            _write(os.path.join(self.src_a, "f%d.txt" % i), "x")
        self.cfg.folders = [config_mod.FolderSpec(Path(self.src_a), "Alpha")]
        d = self._daemon()
        d._progress_min_interval = 0.0
        captured = self._spy_progress(d)
        d.full_pass(None)

        transferring = [p for p in captured if p.get("phase") == "transferring"]
        self.assertTrue(transferring, "no transferring snapshot captured")
        last = transferring[-1]
        # Batch counters present and sane.
        self.assertGreaterEqual(last.get("batches_done"), 1)
        self.assertGreaterEqual(last.get("batches_total"), 1)
        # A parseable ISO stamp of the most recent transfer.
        stamp = last.get("last_transfer_at")
        self.assertIsInstance(stamp, str)
        datetime.fromisoformat(stamp)
        # Byte counters drive the UI progress bar, so they must be populated.
        self.assertGreater(last.get("bytes_done"), 0)

    def test_batch_counters_reset_between_folders(self):
        # A new folder must not inherit the previous folder's "batch 7/7".
        _write(os.path.join(self.src_a, "a.txt"), "a")
        _write(os.path.join(self.src_b, "b.txt"), "b")
        d = self._daemon()
        d._batches_done = 7
        d._batches_total = 7
        d._begin_folder_progress("Beta")
        self.assertEqual(d._batches_done, 0)
        self.assertIsNone(d._batches_total)

    def test_heartbeat_restamps_during_slow_scan(self):
        _write(os.path.join(self.src_a, "a.txt"), "a")
        self.cfg.folders = [config_mod.FolderSpec(Path(self.src_a), "Alpha")]
        d = self._daemon()
        d._progress_hb_interval = 0.02   # fast heartbeat for the test
        d._progress_min_interval = 0.0

        def _slow_phase1(src, dst, backup, rels, timeout=None, on_progress=None,
                         **kw):
            time.sleep(0.2)              # planning/batch: no transfer lines yet
            if on_progress is not None:
                on_progress(1, "a.txt")  # flip to transferring
            time.sleep(0.05)
            return SyncResult("ok", 0, 1, 1, [])

        d.engine.phase1_batch = _slow_phase1
        captured = self._spy_progress(d)

        d._start_progress_heartbeat()
        try:
            d.full_pass(None)
        finally:
            d._stop.set()
            d._join_progress_heartbeat()

        planning = [p for p in captured
                    if p.get("active") and p.get("phase") == "planning"]
        transferring = [p for p in captured if p.get("phase") == "transferring"]
        # The heartbeat re-stamped the planning snapshot several times during the
        # 0.2s no-progress window (>=2 is a robust floor for a 0.02s interval).
        self.assertGreaterEqual(len(planning), 2)
        self.assertTrue(transferring)
        self.assertFalse(captured[-1]["active"])

    def test_idle_touch_refreshes_updated_at(self):
        d = self._daemon()
        self.state.update(state="MOUNTED_IDLE")
        before = self.state.get().get("updated_at")
        time.sleep(0.01)
        d._idle_touch()
        after = self.state.get().get("updated_at")
        self.assertIsNotNone(after)
        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()


class TestTransferStallKill(DaemonTestBase):
    """A healthy guard does not prove the transfer moves: an rsync child can sit
    in uninterruptible SMB I/O while the sentinel reads fine, and every
    guard-based rule then resets the streak so nothing intervenes (real hardware,
    2026-07-28: 0% CPU, state UN, 14 minutes, daemon waiting forever)."""

    class _FakeProc(object):
        def __init__(self, alive=True):
            self.pid = 4242
            self._alive = alive

        def poll(self):
            return None if self._alive else 0

    def _shrink_threshold(self, value=0.05):
        # time.monotonic() is process-relative on macOS (a fresh process reads
        # ~0.1s), so a "very old" timestamp cannot be faked by subtraction --
        # shrink the threshold instead.
        import mycloud.daemon as daemon_mod
        old = daemon_mod._TRANSFER_STALL_KILL
        daemon_mod._TRANSFER_STALL_KILL = value
        self.addCleanup(setattr, daemon_mod, "_TRANSFER_STALL_KILL", old)

    def _armed(self, alive=True, uploaded=True):
        # liveness is asked of the process GROUP (the client may be reaped while
        # its stuck child lives on), so model the group, not poll()
        import mycloud.daemon as daemon_mod
        orig = daemon_mod._group_alive
        daemon_mod._group_alive = lambda pgid: alive
        self.addCleanup(setattr, daemon_mod, "_group_alive", orig)
        mnt = _GuardStubMount(self.fakenas, ok=True, mounted=True)
        d = Daemon(self.cfg, self.state, mnt, self.engine,
                   self.trash_mgr, self.notifier, _LOG)
        d._syncing.set()
        self.killed = []
        d._kill_sync_procs = lambda reason=None: self.killed.append(reason)
        self.engine.proc_registry = set([self._FakeProc(alive)])
        if uploaded:
            d._last_upload_mono = time.monotonic()
        return d

    def test_kills_when_uploads_stall_despite_healthy_guard(self):
        self._shrink_threshold()
        d = self._armed()
        time.sleep(0.1)                      # exceed the shrunk threshold
        d._watchdog_tick()
        self.assertEqual(self.killed, ["transfer_stall"])
        self.assertTrue(d._recovery_needed.is_set())

    def test_no_kill_while_progress_is_recent(self):
        self._shrink_threshold(value=60.0)   # progress is well within budget
        d = self._armed()
        d._watchdog_tick()
        self.assertEqual(self.killed, [])
        self.assertFalse(d._recovery_needed.is_set())

    def test_no_kill_without_a_live_rsync(self):
        # Long planning phase: no children yet, so staleness must not kill.
        self._shrink_threshold()
        d = self._armed(alive=False)
        time.sleep(0.1)
        d._watchdog_tick()
        self.assertEqual(self.killed, [])

    def test_no_kill_before_any_upload_ever_happened(self):
        self._shrink_threshold()
        d = self._armed(uploaded=False)      # _last_upload_mono stays 0.0
        self.assertEqual(d._last_upload_mono, 0.0)
        time.sleep(0.1)
        d._watchdog_tick()
        self.assertEqual(self.killed, [])

    def test_upload_start_rebaselines_the_stall_clock(self):
        # Planning may run for many minutes; that time must not be charged to
        # this pass's first batch.
        d = self._armed(uploaded=False)
        d.engine.phase1_batch = lambda *a, **k: SyncResult("ok", 0, 1, 1, [])
        _write(os.path.join(self.src_a, "a.txt"), "a")
        agg = {"copied": 0, "failed": [], "degraded": False,
               "downloaded": 0, "evicted": 0, "files_done": 0,
               "dataless_pending": []}
        d._upload_relpaths("Alpha", self.src_a,
                           os.path.join(self.data_root, "Alpha"),
                           os.path.join(self.trash_root, "s", "Alpha"),
                           ["a.txt"], agg)
        self.assertGreater(d._last_upload_mono, 0.0)
        self.assertLess(time.monotonic() - d._last_upload_mono, 60)


class TestLifecycleResilience(DaemonTestBase):
    """Field review 2026-09-25 ("守护进程时常失败导致无法同步"): the daemon must
    never run a pass into a dead mount, never leave "a pass is running" set
    after the pass is gone, and not bury the log under a per-minute repeat of
    the same watchdog verdict."""

    def test_scheduled_pass_recovers_instead_of_syncing_into_a_dead_mount(self):
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="sentinel",
                              mounted=True)
        d = Daemon(self.cfg, self.state, mnt, self.engine,
                   self.trash_mgr, self.notifier, _LOG)
        calls = []
        d.full_pass = lambda scope=None: calls.append(("pass", scope))
        d.recovery = lambda: calls.append(("recovery",))
        d._safe_full_pass(None)
        self.assertEqual(calls, [("recovery",)])

    def test_scheduled_pass_runs_when_the_guard_is_green(self):
        mnt = _GuardStubMount(self.fakenas, ok=True)
        d = Daemon(self.cfg, self.state, mnt, self.engine,
                   self.trash_mgr, self.notifier, _LOG)
        calls = []
        d.full_pass = lambda scope=None: calls.append(("pass", scope))
        d.recovery = lambda: calls.append(("recovery",))
        d._safe_full_pass({"x": ["."]})
        self.assertEqual(calls, [("pass", {"x": ["."]})])

    def test_pass_setup_failure_does_not_leave_syncing_set(self):
        mnt = _GuardStubMount(self.fakenas, ok=True)
        d = Daemon(self.cfg, self.state, mnt, self.engine,
                   self.trash_mgr, self.notifier, _LOG)

        def boom():
            raise RuntimeError("layout failed")
        d._ensure_dest_layout = boom
        with self.assertRaises(RuntimeError):
            d.full_pass(None)
        self.assertFalse(d._syncing.is_set())

    def test_watchdog_reports_a_mount_loss_once_per_episode(self):
        mnt = _GuardStubMount(self.fakenas, ok=False, mounted=False)
        log = _RecLogger()
        d = Daemon(self.cfg, self.state, mnt, self.engine,
                   self.trash_mgr, self.notifier, log)
        d._syncing.set()
        d._kill_sync_procs = lambda reason=None: None
        for _ in range(5):
            d._watchdog_tick()
        gone = [m for (lvl, m) in log.records if "mount is gone" in m]
        self.assertEqual(len(gone), 1)
        self.assertTrue(d._recovery_needed.is_set())
        # the mount comes back, then drops again: a NEW episode is reported
        mnt._mounted = True
        mnt._ok = True
        d._watchdog_tick()
        mnt._mounted = False
        d._watchdog_tick()
        gone = [m for (lvl, m) in log.records if "mount is gone" in m]
        self.assertEqual(len(gone), 2)

    def test_guard_failure_survives_an_unwritable_state_store(self):
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="sentinel",
                              mounted=True)
        d = Daemon(self.cfg, self.state, mnt, self.engine,
                   self.trash_mgr, self.notifier, _LOG)

        def broken(**_fields):
            raise OSError(28, "No space left on device")
        d.state.update = broken
        self.assertFalse(d._guard_check())   # returns, never raises

    def test_recovery_keeps_retrying_a_mount_that_will_not_release(self):
        from mycloud.mount import MountError
        mnt = _GuardStubMount(self.fakenas, ok=True)
        refusals = {"left": 2}

        def stubborn():
            if refusals["left"] > 0:
                refusals["left"] -= 1
                raise MountError("mountpoint is still mounted")
        mnt.ensure_bare_dir_safe = stubborn
        log = _RecLogger()
        d = Daemon(self.cfg, self.state, mnt, self.engine,
                   self.trash_mgr, self.notifier, log)
        waits = []
        d._offline_wait = lambda s: waits.append(s)
        d._try_online_gated = lambda: True
        passes = []
        d.full_pass = lambda scope=None: passes.append(scope)
        d.recovery()
        self.assertEqual(refusals["left"], 0)          # retried past both refusals
        self.assertEqual(len(waits), 2)                # backed off between them
        self.assertEqual(passes, [None])               # then remounted and synced
        warned = [m for (lvl, m) in log.records if "will not release" in m]
        self.assertEqual(len(warned), 1)               # warned once, not per retry

    def test_recovery_reports_offline_while_the_mount_is_stuck(self):
        from mycloud.mount import MountError
        mnt = _GuardStubMount(self.fakenas, ok=True)
        seen = []

        def stubborn():
            seen.append(self.state.get().get("state"))
            d._stop.set()                              # end after one refusal
            raise MountError("mountpoint is still mounted")
        mnt.ensure_bare_dir_safe = stubborn
        d = Daemon(self.cfg, self.state, mnt, self.engine,
                   self.trash_mgr, self.notifier, _LOG)
        d._offline_wait = lambda s: None
        d.recovery()
        self.assertEqual(seen, ["OFFLINE"])            # GUI sees the truth

    def test_main_loop_survives_an_unexpected_tick_error(self):
        import mycloud.daemon as daemon_mod
        for name, value in (("_TICK_SECONDS", 0.01), ("_LOOP_ERROR_BACKOFF", 0.01)):
            old = getattr(daemon_mod, name)
            setattr(daemon_mod, name, value)
            self.addCleanup(setattr, daemon_mod, name, old)
        mnt = _GuardStubMount(self.fakenas, ok=True)
        log = _RecLogger()
        d = Daemon(self.cfg, self.state, mnt, self.engine,
                   self.trash_mgr, self.notifier, log)
        d._start_watcher = lambda: None
        d._start_watchdog = lambda: None
        ticks = {"n": 0}

        def tick(*bookkeeping):
            ticks["n"] += 1
            if ticks["n"] == 1:
                raise KeyError("boom")        # a bug on some periodic path
            if ticks["n"] >= 3:
                d._stop.set()
            return bookkeeping
        d._main_loop_tick = tick
        d._main_loop()                        # must return, not raise
        self.assertGreaterEqual(ticks["n"], 3)   # kept ticking after the error

    def test_watchdog_thread_survives_a_failing_tick(self):
        import mycloud.daemon as daemon_mod
        old = daemon_mod._WATCHDOG_INTERVAL
        daemon_mod._WATCHDOG_INTERVAL = 0.01
        self.addCleanup(setattr, daemon_mod, "_WATCHDOG_INTERVAL", old)
        mnt = _GuardStubMount(self.fakenas, ok=True)
        d = Daemon(self.cfg, self.state, mnt, self.engine,
                   self.trash_mgr, self.notifier, _RecLogger())
        d._syncing.set()
        ticks = {"n": 0}

        def tick():
            ticks["n"] += 1
            if ticks["n"] >= 3:
                d._stop.set()
            raise RuntimeError("tick bug")
        d._watchdog_tick = tick
        d._watchdog_loop()                   # returns only via _stop
        self.assertGreaterEqual(ticks["n"], 3)

    def test_kill_reaches_the_group_after_the_client_was_reaped(self):
        # rsync's own --timeout ends the client; its forked child (stuck in SMB
        # I/O in the field) lives on in the same group. getpgid(client) fails
        # once the client is reaped, so the old kill found nothing to kill.
        import subprocess
        import mycloud.daemon as daemon_mod
        proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & exit 0"],
                                start_new_session=True)
        proc.wait()                                   # client reaped
        self.assertTrue(daemon_mod._group_alive(proc.pid))   # child still in group
        mnt = _GuardStubMount(self.fakenas, ok=True)
        d = Daemon(self.cfg, self.state, mnt, self.engine,
                   self.trash_mgr, self.notifier, _LOG)
        self.engine.proc_registry = set([proc])
        d._kill_sync_procs(reason="test")
        deadline = time.monotonic() + 5
        while daemon_mod._group_alive(proc.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(daemon_mod._group_alive(proc.pid))

    def test_watchdog_unreadable_mount_table_is_not_a_mount_loss(self):
        mnt = _GuardStubMount(self.fakenas, ok=True)
        mnt.mount_state = lambda timeout=10: None      # table unreadable
        d = Daemon(self.cfg, self.state, mnt, self.engine,
                   self.trash_mgr, self.notifier, _RecLogger())
        d._syncing.set()
        killed = []
        d._kill_sync_procs = lambda reason=None: killed.append(reason)
        d._watchdog_tick()
        self.assertEqual(killed, [])
        self.assertFalse(d._recovery_needed.is_set())

    def test_offline_attempt_drops_a_dead_session_on_an_existing_mount(self):
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="session")
        mnt.probe_reachable = lambda: True
        mnt.last_mount_note = "already_mounted"
        mnt.last_failure_detail = "statshares timed out after 10s"
        unmounted = []
        mnt.force_unmount = lambda: unmounted.append(1) or True
        d = Daemon(self.cfg, self.state, mnt, self.engine,
                   self.trash_mgr, self.notifier, _LOG)
        self.assertFalse(d._try_online_gated())
        self.assertEqual(unmounted, [1])

    def test_offline_attempt_keeps_the_mount_for_a_permission_problem(self):
        mnt = _GuardStubMount(self.fakenas, ok=False, last_failure="sentinel")
        mnt.probe_reachable = lambda: True
        mnt.last_mount_note = "already_mounted"
        mnt.last_failure_detail = "cat rc=1: Operation not permitted"
        unmounted = []
        mnt.force_unmount = lambda: unmounted.append(1) or True
        d = Daemon(self.cfg, self.state, mnt, self.engine,
                   self.trash_mgr, self.notifier, _LOG)
        self.assertFalse(d._try_online_gated())
        self.assertEqual(unmounted, [])             # remounting cannot fix TCC

    def test_mount_failure_reason_reaches_the_state_document(self):
        mnt = _GuardStubMount(self.fakenas, ok=False)
        mnt.probe_reachable = lambda: True
        mnt.mount = lambda: False
        mnt.last_mount_error = "the share is already mounted at /Volumes/SHARE ..."
        d = Daemon(self.cfg, self.state, mnt, self.engine,
                   self.trash_mgr, self.notifier, _LOG)
        self.assertFalse(d._try_online_gated())
        guard = self.state.get().get("guard")
        self.assertEqual(guard["layer"], "mount")
        self.assertIn("/Volumes/SHARE", guard["detail"])

    def test_progress_redraws_feed_the_stall_detector(self):
        mnt = _GuardStubMount(self.fakenas, ok=True)
        d = Daemon(self.cfg, self.state, mnt, self.engine,
                   self.trash_mgr, self.notifier, _LOG)
        d._last_upload_mono = 0.0
        d._on_transfer_alive()
        self.assertGreater(d._last_upload_mono, 0.0)
