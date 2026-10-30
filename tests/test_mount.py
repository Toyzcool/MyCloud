import tests._path  # noqa: F401

import logging
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from mycloud import mount, util


class _NullLogger(object):
    def error(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


# Real-machine mount-table fixture line (PLAN section 6.2).
FIXTURE_LINE = (
    "//admin@192.168.5.1/SHARE on "
    "/Users/me/Library/Application Support/MyCloud/mnt "
    "(smbfs, nodev, nosuid, mounted by toyz)"
)


def _mgr(mountpoint, host="192.168.5.1", share="SHARE", username="admin",
         sentinel_uuid="the-uuid"):
    return mount.MountManager(
        host, share, username, Path(mountpoint), sentinel_uuid, _NullLogger()
    )


def _ok(out="", rc=0):
    return util.RunResult(rc, out, "", False)


def _timeout(out=""):
    return util.RunResult(None, out, "", True)


class IsMountedParserTests(unittest.TestCase):
    MNT = "/Users/me/Library/Application Support/MyCloud/mnt"

    def test_matches_fixture_line(self):
        m = _mgr(self.MNT)
        with mock.patch.object(util, "run", return_value=_ok(FIXTURE_LINE)):
            self.assertTrue(m.is_mounted())

    def test_matches_among_other_lines(self):
        m = _mgr(self.MNT)
        out = "/dev/disk1 on / (apfs)\n" + FIXTURE_LINE + "\nmap -hosts on /net (autofs)"
        with mock.patch.object(util, "run", return_value=_ok(out)):
            self.assertTrue(m.is_mounted())

    def test_wrong_share_rejected(self):
        m = _mgr(self.MNT, share="OTHER")
        with mock.patch.object(util, "run", return_value=_ok(FIXTURE_LINE)):
            self.assertFalse(m.is_mounted())

    def test_wrong_mountpoint_rejected(self):
        m = _mgr("/some/other/mount")
        with mock.patch.object(util, "run", return_value=_ok(FIXTURE_LINE)):
            self.assertFalse(m.is_mounted())

    def test_wrong_host_rejected(self):
        m = _mgr(self.MNT, host="10.0.0.1")
        with mock.patch.object(util, "run", return_value=_ok(FIXTURE_LINE)):
            self.assertFalse(m.is_mounted())

    def test_wrong_username_rejected(self):
        m = _mgr(self.MNT, username="guest")
        with mock.patch.object(util, "run", return_value=_ok(FIXTURE_LINE)):
            self.assertFalse(m.is_mounted())

    def test_substring_prefix_trap(self):
        # Mountpoint "/a/b" must not match a line whose mountpoint is "/a/bc".
        m = _mgr("/a/b")
        line = "//admin@192.168.5.1/SHARE on /a/bc (smbfs, mounted by toyz)"
        with mock.patch.object(util, "run", return_value=_ok(line)):
            self.assertFalse(m.is_mounted())

    def test_exact_prefix_match(self):
        m = _mgr("/a/b")
        line = "//admin@192.168.5.1/SHARE on /a/b (smbfs, mounted by toyz)"
        with mock.patch.object(util, "run", return_value=_ok(line)):
            self.assertTrue(m.is_mounted())

    def test_empty_output(self):
        m = _mgr(self.MNT)
        with mock.patch.object(util, "run", return_value=_ok("")):
            self.assertFalse(m.is_mounted())

    def test_nonzero_rc_rejected(self):
        m = _mgr(self.MNT)
        with mock.patch.object(util, "run", return_value=_ok(FIXTURE_LINE, rc=1)):
            self.assertFalse(m.is_mounted())

    def test_timeout_rejected(self):
        m = _mgr(self.MNT)
        with mock.patch.object(util, "run", return_value=_timeout(FIXTURE_LINE)):
            self.assertFalse(m.is_mounted())


class SentinelTests(unittest.TestCase):
    def test_matching_uuid(self):
        m = _mgr("/mnt", sentinel_uuid="abc-123")
        with mock.patch.object(util, "run", return_value=_ok("abc-123\n")) as run:
            self.assertTrue(m.sentinel_ok())
        # cat must run as a timed child process against the sentinel path.
        argv, timeout = run.call_args[0][0], run.call_args[0][1]
        self.assertEqual(argv[0], "/bin/cat")
        self.assertTrue(argv[1].endswith(".mycloud_sentinel"))
        self.assertEqual(timeout, 10)

    def test_wrong_uuid(self):
        m = _mgr("/mnt", sentinel_uuid="abc-123")
        with mock.patch.object(util, "run", return_value=_ok("different\n")):
            self.assertFalse(m.sentinel_ok())

    def test_nonzero_rc(self):
        m = _mgr("/mnt", sentinel_uuid="abc-123")
        with mock.patch.object(util, "run", return_value=_ok("abc-123", rc=1)):
            self.assertFalse(m.sentinel_ok())

    def test_timeout_is_false(self):
        m = _mgr("/mnt", sentinel_uuid="abc-123")
        with mock.patch.object(util, "run", return_value=_timeout()):
            self.assertFalse(m.sentinel_ok())


class GuardFailureDetailTests(unittest.TestCase):
    """Failing SMB-read layers must record the MECHANISM in
    last_failure_detail. 2026-08-01 gateway incident: sentinel cat failed for
    80 minutes with TCC's "Operation not permitted", but the discarded stderr
    left the log saying only "failing at layer 'sentinel'" -- undiagnosable."""

    def test_sentinel_cat_error_records_rc_and_stderr(self):
        m = _mgr("/mnt", sentinel_uuid="abc-123")
        denied = util.RunResult(1, "", "cat: ...: Operation not permitted", False)
        with mock.patch.object(util, "run", return_value=denied):
            self.assertFalse(m.sentinel_ok())
        self.assertIn("rc=1", m.last_failure_detail)
        self.assertIn("Operation not permitted", m.last_failure_detail)

    def test_sentinel_uuid_mismatch_records_both_values(self):
        m = _mgr("/mnt", sentinel_uuid="abc-123")
        with mock.patch.object(util, "run", return_value=_ok("other-uuid\n")):
            self.assertFalse(m.sentinel_ok())
        self.assertIn("other-uuid", m.last_failure_detail)
        self.assertIn("abc-123", m.last_failure_detail)

    def test_sentinel_timeout_recorded(self):
        m = _mgr("/mnt", sentinel_uuid="abc-123")
        with mock.patch.object(util, "run", return_value=_timeout()):
            self.assertFalse(m.sentinel_ok())
        self.assertIn("timed out", m.last_failure_detail)

    def test_sentinel_success_clears_stale_detail(self):
        m = _mgr("/mnt", sentinel_uuid="abc-123")
        m.last_failure_detail = "stale"
        with mock.patch.object(util, "run", return_value=_ok("abc-123\n")):
            self.assertTrue(m.sentinel_ok())
        self.assertEqual(m.last_failure_detail, "")

    def test_session_failure_records_detail(self):
        m = _mgr("/mnt", share="SHARE")
        with mock.patch.object(util, "run", return_value=_ok("no shares here")):
            self.assertFalse(m.session_ok())
        self.assertIn("SHARE", m.last_failure_detail)


class SessionTests(unittest.TestCase):
    def test_share_present(self):
        m = _mgr("/mnt", share="SHARE")
        out = "==================\nSHARE\n  SERVER_NAME ...\n"
        with mock.patch.object(util, "run", return_value=_ok(out)):
            self.assertTrue(m.session_ok())

    def test_share_absent(self):
        m = _mgr("/mnt", share="SHARE")
        with mock.patch.object(util, "run", return_value=_ok("no shares here")):
            self.assertFalse(m.session_ok())

    def test_nonzero_rc(self):
        m = _mgr("/mnt", share="SHARE")
        with mock.patch.object(util, "run", return_value=_ok("SHARE", rc=1)):
            self.assertFalse(m.session_ok())

    def test_timeout(self):
        m = _mgr("/mnt", share="SHARE")
        with mock.patch.object(util, "run", return_value=_timeout()):
            self.assertFalse(m.session_ok())


class ReachabilityProbeTests(unittest.TestCase):
    """probe_reachable = direct socket, then a brokered smbutil fallback that
    survives the macOS 26 'Local Network' block (PLAN 6.6; real-hardware fix
    2026-07-24)."""

    def test_socket_ok_skips_brokered(self):
        m = _mgr("/mnt")
        with mock.patch.object(m, "probe_tcp445", return_value=True), \
                mock.patch.object(m, "probe_smb_view") as sv:
            self.assertTrue(m.probe_reachable())
            sv.assert_not_called()

    def test_socket_fail_falls_back_to_brokered(self):
        m = _mgr("/mnt")
        with mock.patch.object(m, "probe_tcp445", return_value=False), \
                mock.patch.object(m, "probe_smb_view", return_value=True) as sv:
            self.assertTrue(m.probe_reachable())
            sv.assert_called_once()

    def test_both_fail_is_unreachable(self):
        m = _mgr("/mnt")
        with mock.patch.object(m, "probe_tcp445", return_value=False), \
                mock.patch.object(m, "probe_smb_view", return_value=False):
            self.assertFalse(m.probe_reachable())

    def test_smb_view_argv_no_password_and_success(self):
        m = _mgr("/mnt")
        captured = {}

        def _fake_run(argv, timeout):
            captured["argv"] = list(argv)
            return util.RunResult(0, "Share\nSHARE  Disk\n", "", False)

        with mock.patch.object(util, "run", _fake_run):
            self.assertTrue(m.probe_smb_view())
        self.assertEqual(captured["argv"][:3], ["/usr/bin/smbutil", "view", "-N"])
        self.assertIn("//admin@192.168.5.1", captured["argv"])

    def test_smb_view_nonzero_rc_is_false(self):
        m = _mgr("/mnt")
        with mock.patch.object(util, "run",
                               return_value=util.RunResult(1, "", "err", False)):
            self.assertFalse(m.probe_smb_view())

    def test_smb_view_timeout_is_false(self):
        m = _mgr("/mnt")
        with mock.patch.object(util, "run",
                               return_value=util.RunResult(None, "", "", True)):
            self.assertFalse(m.probe_smb_view())


class GuardOrderTests(unittest.TestCase):
    def test_reachable_failure_short_circuits(self):
        m = _mgr("/mnt")
        with mock.patch.object(m, "probe_reachable", return_value=False), \
                mock.patch.object(m, "is_mounted") as im:
            self.assertFalse(m.guard_ok())
            self.assertEqual(m.last_failure, "tcp445")
            im.assert_not_called()

    def test_guard_passes_via_brokered_when_socket_blocked(self):
        # The fix's core case: direct socket blocked, but the NAS is reachable
        # via the SMB subsystem AND mounted + sentinel + session all pass.
        m = _mgr("/mnt")
        m.last_failure = "stale"
        with mock.patch.object(m, "probe_tcp445", return_value=False), \
                mock.patch.object(m, "probe_smb_view", return_value=True), \
                mock.patch.object(m, "is_mounted", return_value=True), \
                mock.patch.object(m, "sentinel_ok", return_value=True), \
                mock.patch.object(m, "session_ok", return_value=True):
            self.assertTrue(m.guard_ok())
            self.assertEqual(m.last_failure, "")

    def test_is_mounted_failure(self):
        m = _mgr("/mnt")
        with mock.patch.object(m, "probe_tcp445", return_value=True), \
                mock.patch.object(m, "is_mounted", return_value=False), \
                mock.patch.object(m, "sentinel_ok") as so:
            self.assertFalse(m.guard_ok())
            self.assertEqual(m.last_failure, "is_mounted")
            so.assert_not_called()

    def test_sentinel_failure(self):
        m = _mgr("/mnt")
        with mock.patch.object(m, "probe_tcp445", return_value=True), \
                mock.patch.object(m, "is_mounted", return_value=True), \
                mock.patch.object(m, "sentinel_ok", return_value=False), \
                mock.patch.object(m, "session_ok") as se:
            self.assertFalse(m.guard_ok())
            self.assertEqual(m.last_failure, "sentinel")
            se.assert_not_called()

    def test_session_failure(self):
        m = _mgr("/mnt")
        with mock.patch.object(m, "probe_tcp445", return_value=True), \
                mock.patch.object(m, "is_mounted", return_value=True), \
                mock.patch.object(m, "sentinel_ok", return_value=True), \
                mock.patch.object(m, "session_ok", return_value=False):
            self.assertFalse(m.guard_ok())
            self.assertEqual(m.last_failure, "session")

    def test_all_pass(self):
        m = _mgr("/mnt")
        m.last_failure = "stale"
        with mock.patch.object(m, "probe_tcp445", return_value=True), \
                mock.patch.object(m, "is_mounted", return_value=True), \
                mock.patch.object(m, "sentinel_ok", return_value=True), \
                mock.patch.object(m, "session_ok", return_value=True):
            self.assertTrue(m.guard_ok())
            self.assertEqual(m.last_failure, "")


class MountArgvTests(unittest.TestCase):
    def test_exact_argv_and_no_password(self):
        m = _mgr("/mnt", host="192.168.5.1", share="SHARE", username="admin")
        with mock.patch.object(util, "run", return_value=_ok()) as run:
            self.assertTrue(m.mount())
        argv, timeout = run.call_args[0][0], run.call_args[0][1]
        self.assertEqual(argv, [
            "/sbin/mount_smbfs",
            "-N",
            "-o", "soft,nobrowse,nodev,nosuid",
            "-d", "0700",
            "-f", "0600",
            "//admin@192.168.5.1/SHARE",
            "/mnt",
        ])
        self.assertEqual(timeout, 60)

    def test_mount_failure_rc(self):
        m = _mgr("/mnt")
        with mock.patch.object(util, "run", return_value=_ok(rc=1)):
            self.assertFalse(m.mount())

    def test_mount_timeout(self):
        m = _mgr("/mnt")
        with mock.patch.object(util, "run", return_value=_timeout()):
            self.assertFalse(m.mount())


class ForceUnmountTests(unittest.TestCase):
    """force_unmount() is judged by the mount table alone: it peels every layer
    at our mountpoint (umount -> umount -f -> diskutil force) and reports
    whether anything is left."""

    def _run(self, m, layers, refuse=()):
        """layers: current stack at the mountpoint; each successful unmount pops
        the top. refuse: commands (argv[0], argv[1]) that fail."""
        stack = list(layers)
        calls = []

        def fake_run(cmd, timeout, input_text=None):
            calls.append(list(cmd))
            key = (cmd[0], cmd[1])
            if key in refuse:
                return _fail(rc=16, err="Resource busy")
            if stack:
                stack.pop()
            return _ok()
        with mock.patch.object(util, "run", side_effect=fake_run), \
                mock.patch.object(m, "mounts_at_mountpoint",
                                  side_effect=lambda timeout=10: [(f, "smbfs") for f in stack]):
            ok = m.force_unmount()
        return ok, calls, stack

    def test_nothing_mounted_is_success_without_commands(self):
        ok, calls, _ = self._run(_mgr("/mnt"), [])
        self.assertTrue(ok)
        self.assertEqual(calls, [])

    def test_plain_umount_suffices(self):
        ok, calls, _ = self._run(_mgr("/mnt"), ["//admin@h/SHARE"])
        self.assertTrue(ok)
        self.assertEqual(calls, [["/sbin/umount", "/mnt"]])

    def test_stacked_layers_are_all_peeled(self):
        ok, calls, stack = self._run(_mgr("/mnt"), ["//admin@h/OLD", "//admin@h/SHARE"])
        self.assertTrue(ok)
        self.assertEqual(stack, [])
        self.assertEqual(calls, [["/sbin/umount", "/mnt"], ["/sbin/umount", "/mnt"]])

    def test_busy_layer_escalates_to_forced_and_diskutil(self):
        ok, calls, _ = self._run(_mgr("/mnt"), ["//x@h/FOREIGN"],
                                 refuse={("/sbin/umount", "/mnt"), ("/sbin/umount", "-f")})
        self.assertTrue(ok)
        self.assertIn(["/sbin/umount", "-f", "/mnt"], calls)
        self.assertIn(["/usr/sbin/diskutil", "unmount", "force", "/mnt"], calls)

    def test_reports_failure_when_the_mount_will_not_go(self):
        ok, _calls, stack = self._run(
            _mgr("/mnt"), ["//admin@h/SHARE"],
            refuse={("/sbin/umount", "/mnt"), ("/sbin/umount", "-f"),
                    ("/usr/sbin/diskutil", "unmount")})
        self.assertFalse(ok)
        self.assertEqual(stack, ["//admin@h/SHARE"])


class EnsureBareDirSafeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        os.chmod(self.dir, 0o700)
        for root, dirs, files in os.walk(self.dir, topdown=False):
            for f in files:
                os.unlink(os.path.join(root, f))
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        os.rmdir(self.dir)

    def test_empty_dir_gets_locked_to_0500(self):
        m = _mgr(self.dir)
        with mock.patch.object(m, "mounts_at_mountpoint", return_value=[]):
            m.ensure_bare_dir_safe()
        mode = os.stat(self.dir).st_mode & 0o777
        self.assertEqual(mode, 0o500)

    def test_nonempty_raises_critical(self):
        with open(os.path.join(self.dir, "stray.txt"), "w") as h:
            h.write("local data")
        m = _mgr(self.dir)
        with mock.patch.object(m, "mounts_at_mountpoint", return_value=[]):
            with self.assertRaises(mount.CriticalLocalWriteDetected):
                m.ensure_bare_dir_safe()
        # Must not delete the stray data.
        self.assertTrue(os.path.exists(os.path.join(self.dir, "stray.txt")))

    def test_still_mounted_raises_mounterror(self):
        m = _mgr(self.dir)
        with mock.patch.object(m, "mounts_at_mountpoint",
                               return_value=[("//admin@h/SHARE", "smbfs")]):
            with self.assertRaises(mount.MountError):
                m.ensure_bare_dir_safe()

    def test_unreadable_table_is_a_mount_error_not_a_verdict(self):
        m = _mgr(self.dir)
        with mock.patch.object(m, "mounts_at_mountpoint", return_value=None):
            with self.assertRaises(mount.MountError):
                m.ensure_bare_dir_safe()

    def test_never_stats_the_mountpoint_to_decide(self):
        # a dead SMB root blocks stat() in the kernel; the decision must come
        # from the mount table only
        m = _mgr(self.dir)
        with mock.patch.object(m, "mounts_at_mountpoint", return_value=[]), \
                mock.patch("os.path.ismount", side_effect=AssertionError("stat'd mountpoint")):
            m.ensure_bare_dir_safe()


class _RecLogger(object):
    """Records error/info/warning messages (with %-args already applied)."""

    def __init__(self):
        self.errors = []
        self.infos = []
        self.warnings = []

    def error(self, msg, *args):
        self.errors.append((msg % args) if args else msg)

    def info(self, msg, *args):
        self.infos.append((msg % args) if args else msg)

    def warning(self, msg, *args):
        self.warnings.append((msg % args) if args else msg)


def _fail(rc=1, err=""):
    return util.RunResult(rc, "", err, False)


class _ListHandler(logging.Handler):
    """Captures every emitted LogRecord for redaction assertions."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class _RunRecorder(object):
    """util.run side_effect: returns queued RunResults, recording each argv.

    util.run is called positionally as run(cmd, timeout, input_text=None); we
    keep (argv-copy, timeout) per call so tests can count attempts and inspect
    the exact argv sequence.
    """

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def __call__(self, cmd, timeout, input_text=None):
        self.calls.append((list(cmd), timeout))
        return self._results.pop(0)


class _FreshMountpoint(object):
    """mount() consults the mount table before running mount_smbfs; these tests
    model the "nothing mounted at our mountpoint yet" precondition so util.run
    sees only the mount_smbfs calls they queue."""

    def setUp(self):
        for name, value in (("mount_state", False), ("mounts_at_mountpoint", []),
                            ("same_share_elsewhere", [])):
            patcher = mock.patch.object(mount.MountManager, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)


class MountStderrAndAlreadyMountedTests(_FreshMountpoint, unittest.TestCase):
    """F2: capture mount_smbfs stderr at ERROR, and treat an already-present
    mount ("File exists" / rc 64) at OUR mountpoint as success."""

    def _mgr(self, log):
        return mount.MountManager(
            "192.168.5.1", "SHARE", "admin", Path("/mnt"), "the-uuid", log)

    def test_stderr_captured_at_error_and_truncated_to_200(self):
        log = _RecLogger()
        m = self._mgr(log)
        long_err = "mount error: something specific and diagnostic " + ("x" * 500)
        with mock.patch.object(util, "run", return_value=_fail(rc=1, err=long_err)):
            self.assertFalse(m.mount())
        self.assertTrue(log.errors, "the failure stderr must be logged at ERROR")
        joined = " ".join(log.errors)
        self.assertIn("mount error: something specific and diagnostic", joined)
        # Only the first 200 chars of stderr are kept (no 500-x runaway).
        self.assertNotIn("x" * 250, joined)

    def test_file_exists_but_mounted_returns_true(self):
        log = _RecLogger()
        m = self._mgr(log)
        fail = _fail(rc=64, err="mount_smbfs: /mnt: File exists")
        with mock.patch.object(util, "run", return_value=fail), \
                mock.patch.object(m, "is_mounted", return_value=True):
            self.assertTrue(m.mount())
        # The ERROR (stderr) is still recorded, plus an INFO explaining success.
        self.assertTrue(any("File exists" in e for e in log.errors))
        self.assertTrue(log.infos)

    def test_rc64_without_file_exists_but_mounted_returns_true(self):
        log = _RecLogger()
        m = self._mgr(log)
        fail = _fail(rc=64, err="some other rc64 message")
        with mock.patch.object(util, "run", return_value=fail), \
                mock.patch.object(m, "is_mounted", return_value=True):
            self.assertTrue(m.mount())

    def test_file_exists_but_not_mounted_returns_false(self):
        log = _RecLogger()
        m = self._mgr(log)
        fail = _fail(rc=64, err="File exists")
        with mock.patch.object(util, "run", return_value=fail), \
                mock.patch.object(m, "is_mounted", return_value=False) as im:
            self.assertFalse(m.mount())
        im.assert_called_once()

    def test_generic_failure_does_not_recheck_mount(self):
        # rc != 64 and no "File exists": is_mounted() must NOT be consulted.
        log = _RecLogger()
        m = self._mgr(log)
        with mock.patch.object(util, "run", return_value=_fail(rc=1, err="perm denied")), \
                mock.patch.object(m, "is_mounted") as im:
            self.assertFalse(m.mount())
        im.assert_not_called()


class ProbeTcp445Tests(unittest.TestCase):
    def test_closed_port_returns_false_quickly(self):
        # Find a definitely-closed port on loopback.
        import socket as _socket
        s = _socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        m = _mgr("/mnt", host="127.0.0.1")
        with mock.patch("socket.create_connection") as cc:
            cc.side_effect = OSError("refused")
            self.assertFalse(m.probe_tcp445(timeout=1.0))
            cc.assert_called_once_with(("127.0.0.1", 445), timeout=1.0)

    def test_success_closes_connection(self):
        m = _mgr("/mnt", host="192.168.5.1")
        fake_conn = mock.Mock()
        with mock.patch("socket.create_connection", return_value=fake_conn):
            self.assertTrue(m.probe_tcp445())
        fake_conn.close.assert_called_once()

    def test_real_closed_port(self):
        m = _mgr("/mnt", host="127.0.0.1")
        # Port 1 is not listening; connection refused fast.
        self.assertFalse(m.probe_tcp445(timeout=2.0))


class EmbeddedCredentialFallbackTests(_FreshMountpoint, unittest.TestCase):
    """mount() retries once with a URL-embedded nsmb.conf password when the
    server rejects the -N handshake (rc 77 / 'Authentication error')."""

    def _mgr(self, log=None):
        return mount.MountManager(
            "192.168.5.1", "SHARE", "admin", Path("/mnt"), "the-uuid",
            log or _RecLogger())

    def test_dash_n_success_no_fallback(self):
        m = self._mgr()
        rec = _RunRecorder([_ok()])
        with mock.patch.object(util, "run", side_effect=rec), \
                mock.patch.object(mount, "read_nsmb_password") as rp:
            self.assertTrue(m.mount())
        self.assertEqual(len(rec.calls), 1)
        self.assertIn("-N", rec.calls[0][0])
        rp.assert_not_called()

    def test_rc77_with_password_retries_once_encoded(self):
        log = _RecLogger()
        m = self._mgr(log)
        rec = _RunRecorder([
            _fail(rc=77, err="mount_smbfs: server rejected the connection: "
                             "Authentication error"),
            _ok(),
        ])
        with mock.patch.object(util, "run", side_effect=rec), \
                mock.patch.object(mount, "read_nsmb_password", return_value="S3cr3t!pw"):
            self.assertTrue(m.mount())
        self.assertEqual(len(rec.calls), 2)
        first_argv, second_argv = rec.calls[0][0], rec.calls[1][0]
        self.assertIn("-N", first_argv)
        self.assertNotIn("-N", second_argv)
        self.assertEqual(rec.calls[1][1], 60)  # retry keeps the 60s timeout
        url = second_argv[-2]
        self.assertEqual(
            url,
            "//admin:" + urllib.parse.quote("S3cr3t!pw", safe="") + "@192.168.5.1/SHARE")
        self.assertIn("S3cr3t%21pw", url)  # '!' percent-encoded
        # The -N rejection is expected under launchd (macOS 12's mount_smbfs
        # never reads the Keychain): INFO, not ERROR.
        self.assertEqual(
            log.infos,
            ["mount_smbfs -N rejected (rc=77); trying the nsmb.conf credential",
             "mounted via embedded-credential fallback (-N path rejected by server)"])
        self.assertEqual(log.errors, [])

    def test_auth_error_string_without_rc77_triggers_fallback(self):
        m = self._mgr()
        rec = _RunRecorder([
            _fail(rc=1, err="mount_smbfs: Authentication error"),
            _ok(),
        ])
        with mock.patch.object(util, "run", side_effect=rec), \
                mock.patch.object(mount, "read_nsmb_password", return_value="pw"):
            self.assertTrue(m.mount())
        self.assertEqual(len(rec.calls), 2)
        self.assertNotIn("-N", rec.calls[1][0])

    def test_rc77_no_password_no_retry(self):
        log = _RecLogger()
        m = self._mgr(log)
        rec = _RunRecorder([_fail(rc=77, err="Authentication error")])
        with mock.patch.object(util, "run", side_effect=rec), \
                mock.patch.object(mount, "read_nsmb_password", return_value=None):
            self.assertFalse(m.mount())
        self.assertEqual(len(rec.calls), 1)  # no retry
        self.assertTrue(any("nsmb.conf password" in e for e in log.errors))

    def test_retry_failure_returns_false_both_logged(self):
        log = _RecLogger()
        m = self._mgr(log)
        rec = _RunRecorder([
            _fail(rc=77, err="Authentication error"),
            _fail(rc=1, err="server rejected the connection again"),
        ])
        with mock.patch.object(util, "run", side_effect=rec), \
                mock.patch.object(mount, "read_nsmb_password", return_value="S3cr3t!pw"):
            self.assertFalse(m.mount())
        self.assertEqual(len(rec.calls), 2)
        joined = " ".join(log.errors)
        self.assertIn("embedded-credential fallback failed rc=1", joined)  # retry
        self.assertEqual(log.infos,                                       # -N attempt
                         ["mount_smbfs -N rejected (rc=77); trying the nsmb.conf credential"])

    def test_retry_timeout_returns_false(self):
        log = _RecLogger()
        m = self._mgr(log)
        rec = _RunRecorder([
            _fail(rc=77, err="Authentication error"),
            _timeout(),
        ])
        with mock.patch.object(util, "run", side_effect=rec), \
                mock.patch.object(mount, "read_nsmb_password", return_value="S3cr3t!pw"):
            self.assertFalse(m.mount())
        self.assertEqual(len(rec.calls), 2)
        self.assertTrue(any("fallback timed out" in e for e in log.errors))

    def test_rc64_file_exists_does_not_trigger_fallback(self):
        m = self._mgr()
        rec = _RunRecorder([_fail(rc=64, err="mount_smbfs: /mnt: File exists")])
        with mock.patch.object(util, "run", side_effect=rec), \
                mock.patch.object(m, "is_mounted", return_value=True), \
                mock.patch.object(mount, "read_nsmb_password") as rp:
            self.assertTrue(m.mount())
        self.assertEqual(len(rec.calls), 1)
        rp.assert_not_called()

    def test_rc64_not_mounted_no_fallback(self):
        m = self._mgr()
        rec = _RunRecorder([_fail(rc=64, err="File exists")])
        with mock.patch.object(util, "run", side_effect=rec), \
                mock.patch.object(m, "is_mounted", return_value=False), \
                mock.patch.object(mount, "read_nsmb_password") as rp:
            self.assertFalse(m.mount())
        self.assertEqual(len(rec.calls), 1)
        rp.assert_not_called()


class ReadNsmbPasswordParserTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        for name in os.listdir(self.dir):
            p = os.path.join(self.dir, name)
            os.chmod(p, 0o600)
            os.unlink(p)
        os.rmdir(self.dir)

    def _write(self, text, mode=0o600, name="nsmb.conf"):
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding="utf-8") as h:
            h.write(text)
        os.chmod(p, mode)
        return Path(p)

    def test_missing_file_returns_none(self):
        self.assertIsNone(
            mount.read_nsmb_password("192.168.5.1", Path(self.dir) / "nope.conf"))

    def test_exact_host_section(self):
        p = self._write("[192.168.5.1]\nusername=admin\npassword=S3cr3t!pw\n")
        self.assertEqual(mount.read_nsmb_password("192.168.5.1", p), "S3cr3t!pw")

    def test_host_user_section_form(self):
        p = self._write("[192.168.5.1:ADMIN]\npassword=userpw\n")
        self.assertEqual(mount.read_nsmb_password("192.168.5.1", p), "userpw")

    def test_default_is_weakest_match(self):
        p = self._write("[default]\npassword=defpw\n")
        self.assertEqual(mount.read_nsmb_password("192.168.5.1", p), "defpw")

    def test_exact_beats_prefix_and_default(self):
        p = self._write(
            "[default]\npassword=defpw\n"
            "[192.168.5.1:ADMIN]\npassword=userpw\n"
            "[192.168.5.1]\npassword=exactpw\n")
        self.assertEqual(mount.read_nsmb_password("192.168.5.1", p), "exactpw")

    def test_prefix_beats_default(self):
        p = self._write(
            "[default]\npassword=defpw\n"
            "[192.168.5.1:ADMIN]\npassword=userpw\n")
        self.assertEqual(mount.read_nsmb_password("192.168.5.1", p), "userpw")

    def test_case_insensitive_section(self):
        p = self._write("[MyHost]\npassword=pw\n")
        self.assertEqual(mount.read_nsmb_password("myhost", p), "pw")
        self.assertEqual(mount.read_nsmb_password("MYHOST", p), "pw")

    def test_comments_blanks_and_username_tolerated(self):
        p = self._write(
            "; leading comment\n"
            "# another comment\n"
            "\n"
            "[192.168.5.1]\n"
            "   \n"
            "username=admin\n"
            "  password =  spaced-pw  \n")
        self.assertEqual(mount.read_nsmb_password("192.168.5.1", p), "spaced-pw")

    def test_missing_section_returns_none(self):
        p = self._write("[other.host]\npassword=pw\n")
        self.assertIsNone(mount.read_nsmb_password("192.168.5.1", p))

    def test_missing_password_key_returns_none(self):
        p = self._write("[192.168.5.1]\nusername=admin\n")
        self.assertIsNone(mount.read_nsmb_password("192.168.5.1", p))

    def test_empty_password_value_is_none(self):
        p = self._write("[192.168.5.1]\npassword=\n")
        self.assertIsNone(mount.read_nsmb_password("192.168.5.1", p))

    def test_password_with_hash_not_treated_as_comment(self):
        # '#'/';' are comments only at the START of a line, so a password
        # containing them survives verbatim (quote() encodes it at mount time).
        p = self._write("[192.168.5.1]\npassword=a#b:c@d/e\n")
        self.assertEqual(mount.read_nsmb_password("192.168.5.1", p), "a#b:c@d/e")

    def test_loose_permissions_warn_but_read(self):
        p = self._write("[192.168.5.1]\npassword=Z9distinctpw\n", mode=0o644)
        with self.assertLogs("mycloud.mount", level="WARNING") as cm:
            self.assertEqual(
                mount.read_nsmb_password("192.168.5.1", p), "Z9distinctpw")
        self.assertTrue(any("loose permissions" in line for line in cm.output))
        # The warning names only the path/mode; the password must not leak.
        self.assertFalse(any("Z9distinctpw" in line for line in cm.output))

    def test_mode_0600_no_warning(self):
        p = self._write("[192.168.5.1]\npassword=pw\n", mode=0o600)
        handler = _ListHandler()
        logger = logging.getLogger("mycloud.mount")
        logger.addHandler(handler)
        try:
            self.assertEqual(mount.read_nsmb_password("192.168.5.1", p), "pw")
        finally:
            logger.removeHandler(handler)
        self.assertEqual(
            [r for r in handler.records if r.levelno >= logging.WARNING], [])


class FallbackRedactionInvariantTests(_FreshMountpoint, unittest.TestCase):
    """The plaintext password reaches the subprocess argv ONLY: never a log
    record, never a MountManager attribute, never an exception."""

    PW = "S3cr3t!pw"

    def _run_fallback(self, retry_result):
        handler = _ListHandler()
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        logger = logging.getLogger("mycloud.mount.redactiontest")
        m = mount.MountManager(
            "192.168.5.1", "SHARE", "admin", Path("/mnt"), "the-uuid", logger)
        rec = _RunRecorder([
            _fail(rc=77, err="mount_smbfs: server rejected the connection: "
                             "Authentication error"),
            retry_result,
        ])
        try:
            with mock.patch.object(util, "run", side_effect=rec), \
                    mock.patch.object(mount, "read_nsmb_password", return_value=self.PW):
                result = m.mount()
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        return m, result, rec, handler.records

    def _assert_no_password_in_records(self, records):
        encoded = urllib.parse.quote(self.PW, safe="")
        for rec in records:
            blob = " ".join([str(rec.getMessage()), str(rec.msg), str(rec.args)])
            self.assertNotIn(self.PW, blob)
            self.assertNotIn(encoded, blob)

    def _assert_no_password_in_attrs(self, m):
        encoded = urllib.parse.quote(self.PW, safe="")
        self.assertEqual(m.last_failure, "")
        for value in vars(m).values():
            self.assertNotIn(self.PW, str(value))
            self.assertNotIn(encoded, str(value))

    def test_password_never_logged_on_success(self):
        m, result, rec, records = self._run_fallback(_ok())
        self.assertTrue(result)
        # The fallback genuinely ran: encoded pw is in the retry argv (by design).
        self.assertIn(urllib.parse.quote(self.PW, safe=""), rec.calls[1][0][-2])
        self.assertTrue(records)  # something was logged (the -N failure at least)
        self._assert_no_password_in_records(records)
        self._assert_no_password_in_attrs(m)

    def test_password_never_logged_on_failure(self):
        m, result, rec, records = self._run_fallback(
            _fail(rc=1, err="mount_smbfs: server rejected the connection"))
        self.assertFalse(result)
        self.assertEqual(len(rec.calls), 2)
        self._assert_no_password_in_records(records)
        self._assert_no_password_in_attrs(m)

    def test_quote_encodes_reserved_chars(self):
        for ch, enc in [("@", "%40"), (":", "%3A"), ("/", "%2F"), ("#", "%23")]:
            self.assertEqual(urllib.parse.quote(ch, safe=""), enc)
        pw = "p@s:s/w#d"
        url = "//admin:" + urllib.parse.quote(pw, safe="") + "@192.168.5.1/SHARE"
        # Encoded credential holds no raw reserved chars, so the authority
        # parses unambiguously and the password round-trips.
        userinfo = url[len("//"):].split("@")[0]
        enc_pw = userinfo.split(":", 1)[1]
        for ch in "@:/#":
            self.assertNotIn(ch, enc_pw)
        self.assertEqual(urllib.parse.unquote(enc_pw), pw)


if __name__ == "__main__":
    unittest.main()


class GuestSessionTests(_FreshMountpoint, unittest.TestCase):
    """Anonymous mode (guest=True): an auth rejection must NOT try the nsmb.conf
    embedded-password fallback -- there is no credential to embed -- and the
    log must say the share refuses guest access."""

    def test_guest_rejection_skips_embedded_fallback(self):
        log = _RecLogger()
        m = mount.MountManager("192.168.5.1", "SHARE", "guest", Path("/mnt"),
                               "the-uuid", log, guest=True)
        rejected = _fail(rc=77, err="mount_smbfs: server rejected the connection: Authentication error")
        with mock.patch.object(util, "run", return_value=rejected) as run, \
                mock.patch.object(mount, "read_nsmb_password") as rnp:
            self.assertFalse(m.mount())
        rnp.assert_not_called()
        self.assertEqual(run.call_count, 1)  # only the -N guest attempt
        self.assertTrue(any("guest" in e for e in log.errors))

    def test_guest_defaults_to_false(self):
        self.assertFalse(_mgr("/mnt").guest)


class IsMountedFieldwiseTests(unittest.TestCase):
    """Field-wise mount-table matching (supervised review 2026-09-12): the
    kernel lists a guest mount as //GUEST:@host/share, and SMB names are
    case-insensitive -- the old prefix match failed anonymous mode forever."""

    MNT = "/Users/me/Library/Application Support/MyCloud/mnt"

    def test_guest_line_uppercase_with_colon_matches(self):
        m = _mgr(self.MNT, username="guest")
        line = ("//GUEST:@192.168.5.1/SHARE on %s (smbfs, nodev, nosuid, "
                "mounted by toyz)" % self.MNT)
        with mock.patch.object(util, "run", return_value=_ok(line)):
            self.assertTrue(m.is_mounted())

    def test_host_and_share_case_insensitive(self):
        m = _mgr(self.MNT, host="NAS.local", share="share")
        line = "//admin@nas.local/SHARE on %s (smbfs, mounted by toyz)" % self.MNT
        with mock.patch.object(util, "run", return_value=_ok(line)):
            self.assertTrue(m.is_mounted())

    def test_other_user_still_rejected(self):
        m = _mgr(self.MNT, username="guest")
        with mock.patch.object(util, "run", return_value=_ok(FIXTURE_LINE)):
            self.assertFalse(m.is_mounted())


class CandidatePasswordTests(_FreshMountpoint, unittest.TestCase):
    """server-set verification: with a candidate password the manager must use
    the embedded-credential URL only -- never -N (which could pass via the
    Keychain's OLD password or a reused session) and never nsmb.conf."""

    def test_candidate_password_uses_embedded_url_only(self):
        log = _RecLogger()
        m = mount.MountManager("192.168.5.1", "SHARE", "admin", Path("/mnt"),
                               "the-uuid", log, password="s3cret")
        with mock.patch.object(util, "run", return_value=_ok()) as run, \
                mock.patch.object(mount, "read_nsmb_password") as rnp:
            self.assertTrue(m.mount())
        rnp.assert_not_called()
        self.assertEqual(run.call_count, 1)
        argv = run.call_args[0][0]
        self.assertNotIn("-N", argv)
        self.assertTrue(any("s3cret" in a for a in argv))  # URL to the child only


class MountIdempotenceAndEexistTests(unittest.TestCase):
    """Field review 2026-09-25: mount() must be idempotent (never re-run
    mount_smbfs over our own live mount), clear whatever else occupies the
    mountpoint first, and resolve "File exists" instead of failing forever."""

    def _mgr(self, log=None):
        return mount.MountManager("192.168.5.1", "SHARE", "admin", Path("/mnt"),
                                  "the-uuid", log or _RecLogger())

    def test_already_mounted_runs_no_mount_smbfs(self):
        m = self._mgr()
        with mock.patch.object(m, "mount_state", return_value=True), \
                mock.patch.object(util, "run") as run:
            self.assertTrue(m.mount())
        run.assert_not_called()
        self.assertEqual(m.last_mount_note, "already_mounted")

    def test_occupied_mountpoint_is_cleared_first(self):
        m = self._mgr()
        order = []
        with mock.patch.object(m, "mount_state", return_value=False), \
                mock.patch.object(m, "mounts_at_mountpoint",
                                  return_value=[("//admin@192.168.5.1/OLD", "smbfs")]), \
                mock.patch.object(m, "force_unmount",
                                  side_effect=lambda: order.append("unmount") or True), \
                mock.patch.object(util, "run",
                                  side_effect=lambda c, t, i=None: order.append(c[0]) or _ok()):
            self.assertTrue(m.mount())
        self.assertEqual(order, ["unmount", "/sbin/mount_smbfs"])

    def test_occupied_mountpoint_that_will_not_release_fails_with_a_reason(self):
        m = self._mgr()
        with mock.patch.object(m, "mount_state", return_value=False), \
                mock.patch.object(m, "mounts_at_mountpoint",
                                  return_value=[("//admin@192.168.5.1/OLD", "smbfs")]), \
                mock.patch.object(m, "force_unmount", return_value=False), \
                mock.patch.object(util, "run") as run:
            self.assertFalse(m.mount())
        run.assert_not_called()
        self.assertIn("will not release", m.last_mount_error)

    def test_leftover_verification_mount_is_unmounted_and_retried(self):
        m = self._mgr()
        seq = [_fail(rc=64, err="mount_smbfs: mount error: //admin@192.168.5.1/SHARE: File exists"),
               _ok(),   # umount of the leftover
               _ok()]   # the retried mount_smbfs
        rec = _RunRecorder(seq)
        elsewhere = [["/private/var/folders/x/T/mycloud_verify.ab12"], []]
        with mock.patch.object(m, "mount_state", return_value=False), \
                mock.patch.object(m, "mounts_at_mountpoint", return_value=[]), \
                mock.patch.object(m, "is_mounted", return_value=False), \
                mock.patch.object(m, "same_share_elsewhere",
                                  side_effect=lambda timeout=10: elsewhere.pop(0)), \
                mock.patch.object(util, "run", side_effect=rec):
            self.assertTrue(m.mount())
        self.assertEqual(rec.calls[1][0], ["/sbin/umount", "/private/var/folders/x/T/mycloud_verify.ab12"])
        self.assertEqual(rec.calls[2][0][0], "/sbin/mount_smbfs")

    def test_share_mounted_elsewhere_by_the_user_names_it(self):
        log = _RecLogger()
        m = self._mgr(log)
        with mock.patch.object(m, "mount_state", return_value=False), \
                mock.patch.object(m, "mounts_at_mountpoint", return_value=[]), \
                mock.patch.object(m, "is_mounted", return_value=False), \
                mock.patch.object(m, "same_share_elsewhere", return_value=["/Volumes/SHARE"]), \
                mock.patch.object(util, "run", return_value=_fail(rc=64, err="File exists")):
            self.assertFalse(m.mount())
        self.assertIn("/Volumes/SHARE", m.last_mount_error)
        self.assertTrue(any("/Volumes/SHARE" in e for e in log.errors))

    def test_fallback_file_exists_with_our_share_mounted_is_success(self):
        # the rc=77 -> embedded-credential path used to call EEXIST a failure,
        # leaving a healthy mount OFFLINE until the daemon restarted
        m = self._mgr()
        rec = _RunRecorder([_fail(rc=77, err="Authentication error"),
                            _fail(rc=64, err="File exists")])
        with mock.patch.object(m, "mount_state", return_value=False), \
                mock.patch.object(m, "mounts_at_mountpoint", return_value=[]), \
                mock.patch.object(m, "is_mounted", return_value=True), \
                mock.patch.object(mount, "read_nsmb_password", return_value="pw"), \
                mock.patch.object(util, "run", side_effect=rec):
            self.assertTrue(m.mount())

    def test_url_echoed_by_mount_smbfs_is_redacted_before_logging(self):
        # mount_smbfs prints its URL argument in "mount error: <url>: ..." on
        # macOS 26/27; for the fallback that URL contains the password
        log = _RecLogger()
        m = self._mgr(log)
        echo = "mount_smbfs: mount error: //admin:S3cret%21pw@192.168.5.1/SHARE: Permission denied"
        rec = _RunRecorder([_fail(rc=77, err="Authentication error"),
                            _fail(rc=64, err=echo)])
        with mock.patch.object(m, "mount_state", return_value=False), \
                mock.patch.object(m, "mounts_at_mountpoint", return_value=[]), \
                mock.patch.object(m, "is_mounted", return_value=False), \
                mock.patch.object(m, "same_share_elsewhere", return_value=[]), \
                mock.patch.object(mount, "read_nsmb_password", return_value="S3cret!pw"), \
                mock.patch.object(util, "run", side_effect=rec):
            self.assertFalse(m.mount())
        blob = " ".join(log.errors + log.infos) + " " + m.last_mount_error
        self.assertNotIn("S3cret", blob)
        self.assertIn("//admin:***@192.168.5.1/SHARE", blob)

    def test_share_and_user_are_percent_encoded_in_the_url(self):
        m = mount.MountManager("192.168.5.1", "照片 备份", "DOM;ann", Path("/mnt"),
                               "u", _RecLogger())
        self.assertEqual(m._target(), "//DOM;ann@192.168.5.1/%E7%85%A7%E7%89%87%20%E5%A4%87%E4%BB%BD")


class MountTableTests(unittest.TestCase):
    """The mount table is the only authority on what is mounted where."""

    TABLE = (
        "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
        "map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)\n"
        "//GUEST:@192.168.5.1/SHARE on /Volumes/SHARE (smbfs, nodev, nosuid, mounted by u)\n"
        "//admin@192.168.5.1/%E7%85%A7%E7%89%87 on /Users/u/Library/Application Support/MyCloud/mnt "
        "(smbfs, nodev, nosuid, nobrowse, mounted by u)\n"
    )

    def _mgr(self, share, user="admin", mp="/Users/u/Library/Application Support/MyCloud/mnt"):
        return mount.MountManager("192.168.5.1", share, user, Path(mp), "u", _RecLogger())

    def test_percent_encoded_share_in_from_name_matches(self):
        m = self._mgr("照片")
        with mock.patch.object(util, "run", return_value=_ok(self.TABLE)):
            self.assertIs(m.mount_state(), True)

    def test_unreadable_table_is_unknown_not_gone(self):
        m = self._mgr("照片")
        with mock.patch.object(util, "run", return_value=_timeout()):
            self.assertIsNone(m.mount_state())
            self.assertFalse(m.is_mounted())
            self.assertIsNone(m.mounts_at_mountpoint())

    def test_mounts_at_mountpoint_sees_any_filesystem(self):
        m = self._mgr("OTHER")
        with mock.patch.object(util, "run", return_value=_ok(self.TABLE)):
            self.assertEqual([fs for _f, fs in m.mounts_at_mountpoint()], ["smbfs"])
            self.assertIs(m.mount_state(), False)   # something is there, not ours

    def test_same_share_elsewhere_finds_the_finder_copy(self):
        m = self._mgr("SHARE", user="guest", mp="/Users/u/mnt")
        with mock.patch.object(util, "run", return_value=_ok(self.TABLE)):
            self.assertEqual(m.same_share_elsewhere(), ["/Volumes/SHARE"])

    def test_canonical_parent_spelling_matches(self):
        # the kernel records the canonical path (symlinks resolved); our
        # configured path may go through a symlink
        base = tempfile.mkdtemp()
        real = os.path.join(base, "Real")
        os.makedirs(real)
        link = os.path.join(base, "link")
        os.symlink(real, link)
        m = self._mgr("SHARE", mp=os.path.join(link, "mnt"))
        canon = os.path.join(mount._c_realpath(real) or os.path.realpath(real), "mnt")
        line = "//admin@192.168.5.1/SHARE on %s (smbfs, nobrowse)\n" % canon
        with mock.patch.object(util, "run", return_value=_ok(line)):
            self.assertIs(m.mount_state(), True)


class ProbeAndSessionDetailTests(unittest.TestCase):

    def test_auth_rejection_counts_as_reachable(self):
        m = _mgr("/mnt")
        with mock.patch.object(util, "run",
                               return_value=_fail(rc=77, err="server rejected the connection: Authentication error")):
            self.assertTrue(m.probe_smb_view())

    def test_connection_failure_is_unreachable(self):
        m = _mgr("/mnt")
        with mock.patch.object(util, "run",
                               return_value=_fail(rc=68, err="server connection failed: Operation timed out")):
            self.assertFalse(m.probe_smb_view())

    def test_session_matches_the_share_column_not_any_substring(self):
        m = _mgr("/mnt", share="S")
        out = ("==============\nSHARE    ATTRIBUTE TYPE    VALUE\n==============\n"
               "DATA     SERVER_NAME       192.168.5.1\n")
        with mock.patch.object(util, "run", return_value=_ok(out)):
            self.assertFalse(m.session_ok())   # "S" appears, but no share named S
        m2 = _mgr("/mnt", share="DATA")
        with mock.patch.object(util, "run", return_value=_ok(out)):
            self.assertTrue(m2.session_ok())

    def test_nsmb_prefers_the_configured_users_section(self):
        d = tempfile.mkdtemp()
        conf = os.path.join(d, "nsmb.conf")
        with open(conf, "w") as h:
            h.write("[192.168.5.1:OTHER]\npassword=wrong\n[192.168.5.1:ADMIN]\npassword=right\n")
        os.chmod(conf, 0o600)
        self.assertEqual(mount.read_nsmb_password("192.168.5.1", conf, username="admin"), "right")
