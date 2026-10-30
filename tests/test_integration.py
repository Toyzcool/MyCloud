import tests._path  # noqa: F401

"""End-to-end integration tests on a REAL local mount (PLAN section 10).

A 512 MiB APFS disk image, created and attached by tests/integration.sh, stands
in for the router NAS. These tests wire the REAL components together -- StateStore,
SyncEngine (real /usr/bin/rsync), TrashManager, Notifier (osascript runner stubbed
so no notifications actually fire), a real MountManager subclass, and a real
Daemon in --once mode -- and exercise the failure scenarios from PLAN section 10
against genuine filesystem behavior (CJK filenames, rename-into-trash, detach).

Self-skips with a clear message when MC_FAKE_NAS is unset, so a plain
`unittest discover` (without the disk image) stays green.

Only probe_tcp445 / session_ok / is_mounted / mount are overridden on the mount
manager; sentinel_ok and force_unmount stay REAL (see FakeMount).
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_FAKE_NAS = os.environ.get("MC_FAKE_NAS")
_FAKE_DMG = os.environ.get("MC_FAKE_DMG")
_HDIUTIL = "/usr/bin/hdiutil"
_SENTINEL_UUID = "11111111-2222-3333-4444-555555555555"
_CJK_NAME = "自检 测试.txt"  # "自检 测试.txt": CJK + space

if _FAKE_NAS:
    from mycloud import config as config_mod
    from mycloud import notify as notify_mod
    from mycloud import paths
    from mycloud import util
    from mycloud.daemon import (
        Daemon, F_DELETIONS_HELD, F_UP_TO_DATE,
    )
    from mycloud.engine import SyncEngine
    from mycloud.mount import MountManager
    from mycloud.state import StateStore
    from mycloud.trash import TrashManager
    from mycloud import trash as trash_mod

    class FakeMount(MountManager):
        """Real MountManager with the network/mount layers neutralized.

        probe_tcp445 / session_ok always pass (no router present); is_mounted
        falls back to os.path.ismount (the stand-in is APFS, not smbfs); mount()
        reflects the current image state instead of ever calling mount_smbfs
        against the real NAS. sentinel_ok and force_unmount stay REAL.
        """

        def probe_tcp445(self, timeout=3.0):
            return True

        def session_ok(self, timeout=10):
            return True

        def is_mounted(self, timeout=10):
            return os.path.ismount(str(self.mountpoint))

        def mount(self):
            # The image is attached/detached by the test harness, never by
            # mount_smbfs -- we must not touch the real NAS.
            return self.is_mounted()


def _hdiutil_detach(mountpoint):
    # type: (str) -> None
    subprocess.run([_HDIUTIL, "detach", str(mountpoint), "-quiet"],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)


def _hdiutil_detach_force(mountpoint):
    # type: (str) -> None
    subprocess.run([_HDIUTIL, "detach", str(mountpoint), "-force", "-quiet"],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)


def _hdiutil_attach(dmg, mountpoint):
    # type: (str, str) -> None
    if not os.path.isdir(str(mountpoint)):
        os.makedirs(str(mountpoint))
    subprocess.run(
        [_HDIUTIL, "attach", str(dmg), "-mountpoint", str(mountpoint),
         "-nobrowse", "-quiet"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)


def _write(path, data):
    # type: (str, object) -> None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8"}
    with open(path, mode, **kwargs) as handle:
        handle.write(data)


def _find_under(root, basename):
    # type: (str, str) -> object
    for dirpath, _dirs, files in os.walk(root):
        if basename in files:
            return os.path.join(dirpath, basename)
    return None


@unittest.skipUnless(
    _FAKE_NAS,
    "MC_FAKE_NAS is unset: run via tests/integration.sh to create/attach the "
    "disk-image stand-in for the NAS (plain unittest discover skips this).")
class IntegrationBase(unittest.TestCase):

    def setUp(self):
        self.mnt = _FAKE_NAS
        # A detach test may have left the image unmounted; reattach it so tests
        # are order-independent.
        if not os.path.ismount(self.mnt):
            if not _FAKE_DMG:
                self.skipTest("image not mounted and MC_FAKE_DMG unset")
            _hdiutil_attach(_FAKE_DMG, self.mnt)
            self.assertTrue(os.path.ismount(self.mnt), "failed to (re)attach image")

        # Fresh mirror/trash namespace on the image for every test.
        self.mycloud_root = os.path.join(self.mnt, "MyCloud")
        shutil.rmtree(self.mycloud_root, ignore_errors=True)
        self.data_root = os.path.join(self.mycloud_root, "data")
        self.trash_root = os.path.join(self.mycloud_root, ".trash")
        os.makedirs(self.data_root)
        os.makedirs(self.trash_root)

        # Sentinel on the image so the REAL sentinel_ok() guard passes.
        _write(os.path.join(self.mnt, paths.SENTINEL_REL), _SENTINEL_UUID)

        # The 512 MiB stand-in image is far below the production free-space
        # thresholds (stop < 1 GiB / warn < 5 GiB), which would abort every
        # sync. Scale them down so the small image still exercises the real
        # free_space() logic; restore afterwards.
        self._orig_stop = trash_mod._STOP_BELOW
        self._orig_warn = trash_mod._WARN_BELOW
        trash_mod._STOP_BELOW = 1 * 2 ** 20
        trash_mod._WARN_BELOW = 2 * 2 ** 20

        def _restore_thresholds():
            trash_mod._STOP_BELOW = self._orig_stop
            trash_mod._WARN_BELOW = self._orig_warn
        self.addCleanup(_restore_thresholds)

        # Local (not on the image) source + runtime directories.
        self.tmp = tempfile.mkdtemp(prefix="mycloud-int-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, "Docs")
        os.makedirs(self.src)
        self.runtime = os.path.join(self.tmp, "runtime")
        os.makedirs(self.runtime)
        self.excludes = os.path.join(self.runtime, "excludes.txt")

        self.cfg = config_mod.Config()
        # Serial uploads keep batch-order-dependent tests deterministic;
        # concurrency-specific tests raise this themselves.
        self.cfg.upload_concurrency = 1
        self.cfg.host = "192.168.5.1"
        self.cfg.share = "SHARE"
        self.cfg.username = "admin"
        self.cfg.sentinel_uuid = _SENTINEL_UUID
        self.cfg.watch_mode = "scan"
        self.cfg.folders = [config_mod.FolderSpec(Path(self.src), "Docs")]
        self.cfg.excludes = []
        config_mod.write_excludes(self.cfg, self.excludes)

        self.notify_calls = []

        def _stub_runner(cmd, timeout):
            # Stand in for util.run so osascript/open never actually execute.
            self.notify_calls.append(list(cmd))
            return util.RunResult(0, "", "", False)

        self.state = StateStore(self.runtime, logger=None)
        self.proc_registry = set()
        self.engine = SyncEngine(paths.RSYNC, self.excludes, None,
                                 proc_registry=self.proc_registry)
        self.mount = FakeMount(self.cfg.host, self.cfg.share, self.cfg.username,
                               Path(self.mnt), _SENTINEL_UUID, logger=None)
        self.trash_mgr = TrashManager(self.trash_root, self.cfg.retention_days,
                                      self.cfg.trash_max_gb, logger=None)
        self.notifier = notify_mod.Notifier(self.state, runner=_stub_runner,
                                            logger=None)

    def _daemon(self, once=True):
        return Daemon(self.cfg, self.state, self.mount, self.engine,
                      self.trash_mgr, self.notifier, None, once=once)

    def _mirror(self, rel):
        return os.path.join(self.data_root, "Docs", rel)

    def _notify_classes(self):
        # The stub runner only sees osascript argv, so read categories from state.
        return set((self.state.get().get("notify_ts") or {}).keys())


class TestInitialMirror(IntegrationBase):
    def test_initial_mirror_including_cjk(self):
        payload = "你好 world \U0001f680".encode("utf-8")  # 你好 world 🚀
        _write(os.path.join(self.src, "plain.txt"), "hello")
        _write(os.path.join(self.src, "nested", "deep.txt"), "deep")
        _write(os.path.join(self.src, _CJK_NAME), payload)

        rc = self._daemon().run()
        self.assertEqual(rc, 0)

        self.assertTrue(os.path.exists(self._mirror("plain.txt")))
        self.assertTrue(os.path.exists(self._mirror("nested/deep.txt")))
        cjk_mirror = self._mirror(_CJK_NAME)
        self.assertTrue(os.path.exists(cjk_mirror), "CJK filename not mirrored")
        with open(cjk_mirror, "rb") as handle:
            self.assertEqual(handle.read(), payload, "CJK content not byte-identical")

        # Heartbeat written onto the image.
        self.assertTrue(os.path.exists(
            os.path.join(self.mnt, paths.HEARTBEAT_REL)))


class TestModifyVersioning(IntegrationBase):
    def test_overwrite_moves_old_version_to_trash(self):
        doc = os.path.join(self.src, "doc.txt")
        _write(doc, "original-v1")
        # Fillers keep the mirror populated (no bearing here, just realism).
        for i in range(5):
            _write(os.path.join(self.src, "f%02d.txt" % i), "x")

        self.assertEqual(self._daemon().run(), 0)
        with open(self._mirror("doc.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "original-v1")

        # Overwrite with a differently-sized body so rsync re-transfers.
        _write(doc, "changed-version-two-longer")
        self.assertEqual(self._daemon().run(), 0)

        with open(self._mirror("doc.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "changed-version-two-longer")
        old = _find_under(self.trash_root, "doc.txt")
        self.assertIsNotNone(old, "old version not archived under .trash")
        with open(old, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "original-v1")


class TestSmallDeletion(IntegrationBase):
    def test_small_deletion_propagates_to_trash(self):
        for i in range(10):
            _write(os.path.join(self.src, "keep%02d.txt" % i), "k")
        _write(os.path.join(self.src, "gone.txt"), "delete-me")

        self.assertEqual(self._daemon().run(), 0)
        self.assertTrue(os.path.exists(self._mirror("gone.txt")))

        os.remove(os.path.join(self.src, "gone.txt"))
        self._daemon().run()

        self.assertFalse(os.path.exists(self._mirror("gone.txt")),
                         "deletion did not propagate")
        trashed = _find_under(self.trash_root, "gone.txt")
        self.assertIsNotNone(trashed, "deleted file not moved into .trash")
        self.assertEqual(
            self.state.get()["folders"]["Docs"]["state"], F_UP_TO_DATE)


class TestMassDeletionHeld(IntegrationBase):
    def test_mass_deletion_held_then_approved(self):
        self.cfg.max_delete_files = 2
        self.cfg.max_delete_pct = 90  # isolate the count breaker
        victims = ["a.txt", "b.txt", "c.txt", "d.txt"]
        for name in victims:
            _write(os.path.join(self.src, name), "data-" + name)
        for name in ("keep1.txt", "keep2.txt"):
            _write(os.path.join(self.src, name), "keep")

        self.assertEqual(self._daemon().run(), 0)
        for name in victims:
            self.assertTrue(os.path.exists(self._mirror(name)))

        # Delete all four -> exceeds max_delete_files=2 -> HELD.
        for name in victims:
            os.remove(os.path.join(self.src, name))
        self._daemon().run()

        for name in victims:
            self.assertTrue(os.path.exists(self._mirror(name)),
                            "mirror file wrongly deleted while HELD")
        fstate = self.state.get()["folders"]["Docs"]
        self.assertEqual(fstate["state"], F_DELETIONS_HELD)
        self.assertEqual(len(fstate["held_deletions"]), 4)
        self.assertIn("deletions_held", self._notify_classes())

        # Approve and re-run -> executed, token consumed.
        _write(os.path.join(self.runtime, "approval.token"), "ok")
        self._daemon().run()
        for name in victims:
            self.assertFalse(os.path.exists(self._mirror(name)),
                             "approved deletion did not execute")
        self.assertFalse(
            os.path.exists(os.path.join(self.runtime, "approval.token")),
            "approval token not consumed")
        self.assertIsNotNone(_find_under(self.trash_root, "a.txt"))


class TestPruneStamps(IntegrationBase):
    def test_old_stamps_pruned_malformed_untouched(self):
        old_stamp = "2020-01-01T000000"       # far past retention_days
        malformed_a = "not-a-stamp"
        malformed_b = "2020-13-99T999999"     # right shape-ish, invalid date
        for name in (old_stamp, malformed_a, malformed_b):
            _write(os.path.join(self.trash_root, name, "junk.txt"), "junk")

        result = self.trash_mgr.prune(guard_cb=self.mount.guard_ok,
                                      now_utc=datetime.now(timezone.utc))

        self.assertIn(old_stamp, result.removed_stamps)
        self.assertFalse(os.path.exists(os.path.join(self.trash_root, old_stamp)),
                         "aged stamp not pruned")
        self.assertTrue(os.path.exists(os.path.join(self.trash_root, malformed_a)),
                        "malformed name wrongly pruned")
        self.assertTrue(os.path.exists(os.path.join(self.trash_root, malformed_b)),
                        "malformed name wrongly pruned")


class TestDetachDisaster(IntegrationBase):
    def test_detach_guards_fail_then_recover(self):
        if not _FAKE_DMG:
            self.skipTest("MC_FAKE_DMG unset: cannot detach/reattach the image")

        _write(os.path.join(self.src, "safe.txt"), "precious")
        for i in range(3):
            _write(os.path.join(self.src, "g%02d.txt" % i), "g")
        self.assertEqual(self._daemon().run(), 0)
        self.assertTrue(os.path.exists(self._mirror("safe.txt")))

        # --- pull the plug: detach the image ---
        _hdiutil_detach(self.mnt)
        if os.path.ismount(self.mnt):
            _hdiutil_detach_force(self.mnt)
        self.assertFalse(os.path.ismount(self.mnt))
        self.assertFalse(self.mount.guard_ok(), "guard passed with no mount")

        # A pass with the destination gone must fail, writing nothing locally.
        rc = self._daemon().run()
        self.assertNotEqual(rc, 0, "daemon reported success with no destination")

        # Disaster-mode assertion: the bare mount point (the permanent installed
        # directory, recreated here after hdiutil removed it on detach) must end
        # up EMPTY and locked to 0500 so any stray write hits EACCES.
        if not os.path.isdir(self.mnt):
            os.makedirs(self.mnt)
        self.mount.force_unmount()
        self.mount.ensure_bare_dir_safe()
        self.assertEqual(os.listdir(self.mnt), [],
                         "local data was written into the dead mount point")
        import stat as _stat
        mode = _stat.S_IMODE(os.stat(self.mnt).st_mode)
        self.assertEqual(mode, 0o500, "bare mount point not locked to 0500")

        # --- power restored: reattach and confirm recovery ---
        _hdiutil_attach(_FAKE_DMG, self.mnt)
        self.assertTrue(os.path.ismount(self.mnt))
        # Data persisted on the image across the outage.
        self.assertTrue(os.path.exists(self._mirror("safe.txt")))
        # A fresh change now flows through again.
        _write(os.path.join(self.src, "after.txt"), "recovered")
        self.assertEqual(self._daemon().run(), 0)
        self.assertTrue(os.path.exists(self._mirror("after.txt")),
                        "sync did not recover after reattach")


if __name__ == "__main__":
    unittest.main()
