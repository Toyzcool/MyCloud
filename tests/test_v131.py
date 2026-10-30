import tests._path  # noqa: F401

"""Regressions for 1.3.1: the gateway's two-month upload failure (2026-08-02 ..
10-02) and the gaps it exposed.

On the macOS 12 gateway every upload ran `rsync ... --contimeout=15 ...`;
Apple's rsync 2.6.9 has no such option, exited 1 ("unknown option") before
transferring anything, and the daemon answered each failure with an
unmount/remount (「离线」) and an immediate re-plan -- for two months, while the
capability probe and the doctor (their own argv) stayed green. Pinned here:

- every write-mode option exists in rsync 2.6.9 AND openrsync;
- "unknown option" is its own failure kind ("usage"), recognised from stderr;
- the real upload argv is self-tested at startup and in the doctor;
- a failure a remount cannot fix keeps the mount, says why, and backs off;
- a retried -b batch never overwrites the old versions the first try saved;
- files that keep failing (named in failed_files) no longer fail every pass;
- macOS 12's ".X.icloud" placeholders are cloud-only files, never deletions;
- a router refusing the share is named as such, not sent to the EEXIST path;
- once -N was rejected, the nsmb.conf credential is used directly;
- the wake path logs, touches state and lets the network settle;
- verify says "queued for re-upload", not "re-uploaded".

The review of 1.3.1 itself added: a failed pass never leaves 同步中 behind; a
hung destination and a watchdog kill remount; only a pass that could have
exercised a failure clears it (and only its own message); sleep counts toward
the backoff; recovery time is not pass time; file names never read as usage
errors; only server-side mount errors are "refused"; and the placeholder
holes -- old ".X.icloud" rows, evicted packages, evicted file sources, moved
evicted files, downloads of placeholders, a placeholder next to its directory.
"""

import io
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import mycloudctl
from mycloud import daemon as daemon_mod
from mycloud import engine as engine_mod
from mycloud import manifest as manifest_mod
from mycloud import mount
from mycloud import paths
from mycloud import util
from mycloud.config import FolderSpec as config_mod_FolderSpec
from mycloud.config import write_excludes as config_mod_write_excludes
from mycloud.daemon import SyncFailure
from mycloud.engine import SyncEngine, SyncResult

from tests.test_mount import _FreshMountpoint, _RecLogger, _RunRecorder, _fail, _ok
from tests.test_v2_daemon import V2Base

_GATEWAY_STDERR = (
    "rsync: --contimeout=15: unknown option\n"
    "rsync error: syntax or usage error (code 1) at /AppleInternal/Library/"
    "BuildRoots/bc10cbcd-3cda-11ee-b8ac-16228a05f5d2/Library/Caches/"
    "com.apple.xbs/Sources/rsync/rsync/main.c(1337) [client=2.6.9]\n")


class _Log(object):
    def __init__(self):
        self.lines = []

    def _f(self, level):
        def _w(msg, *args):
            self.lines.append((level, (msg % args) if args else msg))
        return _w

    def __getattr__(self, name):
        return self._f(name)


class PortableArgvTests(unittest.TestCase):
    def _capture(self, call):
        seen = []

        def _fake_run(argv, timeout, **kw):
            seen.append(list(argv))
            return 0, "", "", False

        eng = SyncEngine(paths.RSYNC, os.devnull, _Log())
        with mock.patch.object(eng, "_run", side_effect=_fake_run), \
                mock.patch.object(eng, "_ensure_dir", return_value=None):
            call(eng)
        return seen

    def test_every_builder_uses_only_portable_options(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        argvs = []
        for inplace in (True, False):
            for no_times in (True, False):
                argvs += self._capture(lambda e: e.phase1_batch(
                    tmp, tmp, tmp, ["a"], inplace=inplace, no_times=no_times))
        argvs += self._capture(lambda e: e.phase1(tmp, tmp, tmp))
        argvs += self._capture(lambda e: e.phase1_file(os.path.join(tmp, "f"), tmp, tmp))
        self.assertEqual(len(argvs), 6)
        for argv in argvs:
            engine_mod.assert_portable_argv(argv)      # raises on any bad flag
            self.assertNotIn("--contimeout=15", argv)

    def test_the_gateway_killer_is_rejected(self):
        for bad in ("--contimeout=15", "--partial-dir=x", "--info=progress2",
                    "-z", "--delete"):
            with self.assertRaises(AssertionError, msg=bad):
                engine_mod.assert_portable_argv(["rsync", "-rlD", bad, "a/", "b/"])


class FailureKindTests(unittest.TestCase):
    def test_unknown_option_is_usage_not_error(self):
        self.assertEqual(engine_mod._failure_kind(1, _GATEWAY_STDERR, False), "usage")
        self.assertEqual(engine_mod._failure_kind(
            1, "rsync: unrecognized option `--foo'\nusage: rsync [-a]", False), "usage")

    def test_openrsync_runtime_rc1_stays_error(self):
        # openrsync exits 1 for ordinary runtime failures too: never "usage"
        # from the exit code alone.
        self.assertEqual(engine_mod._failure_kind(
            1, "rsync: unexpected end of file", False), "error")
        self.assertEqual(engine_mod._failure_kind(30, "io timeout after 180 seconds", False),
                         "io_timeout")

    def test_stderr_summary_keeps_the_first_line(self):
        long_err = _GATEWAY_STDERR + "x" * 600
        summary = engine_mod.stderr_summary(long_err)
        self.assertIn("unknown option", summary)
        self.assertLessEqual(len(summary), 410)


class BatchSelftestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mycloud-selftest-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_passes_with_this_machines_rsync(self):
        eng = SyncEngine(paths.RSYNC, os.devnull, _Log())
        self.assertIsNone(eng.batch_selftest(os.path.join(self.tmp, "w")))

    def test_fails_like_the_gateway(self):
        fake = os.path.join(self.tmp, "rsync269")
        err_file = os.path.join(self.tmp, "stderr.txt")
        with open(err_file, "w") as h:
            h.write(_GATEWAY_STDERR)
        with open(fake, "w") as h:
            h.write("#!/bin/sh\ncat >/dev/null\ncat '%s' >&2\nexit 1\n" % err_file)
        os.chmod(fake, 0o755)
        log = _Log()
        problem = SyncEngine(fake, os.devnull, log).batch_selftest(
            os.path.join(self.tmp, "w"))
        self.assertIsNotNone(problem)
        self.assertIn("kind=usage", problem)
        self.assertTrue(any("unknown option" in m for _l, m in log.lines))


class _FailingBatchEngine(object):
    """Wraps a real engine; phase1_batch returns a scripted failure."""

    def __init__(self, real, kind, rc=1):
        self._real = real
        self.kind = kind
        self.rc = rc
        self.calls = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def phase1_batch(self, src, dst, backup_dir, rels, **kw):
        self.calls.append(backup_dir)
        return SyncResult("failure", self.rc, 0, 0, [], self.kind)


class PassFailureRoutingTests(V2Base):
    def _daemon_with(self, kind):
        self._mk("a.txt", "a")
        d = self._daemon()
        d.engine = _FailingBatchEngine(self.engine, kind)
        self.recoveries = []
        d.recovery = lambda: self.recoveries.append(1)
        return d

    def test_usage_failure_keeps_the_mount_explains_and_backs_off(self):
        d = self._daemon_with("usage")
        d._safe_full_pass(None)
        self.assertEqual(self.recoveries, [], "an rsync usage error must not remount")
        self.assertIn("rsync 不接受", self.state.get().get("last_error") or "")
        self.assertTrue(d._backing_off())
        first = d._pass_backoff_until - time.monotonic()
        d._safe_full_pass(None)
        second = d._pass_backoff_until - time.monotonic()
        self.assertGreater(second, first + 100, "the backoff must grow")

    def test_error_with_healthy_guard_does_not_remount(self):
        d = self._daemon_with("error")
        d._safe_full_pass(None)
        self.assertEqual(self.recoveries, [])
        self.assertIn("NAS 挂载正常", self.state.get().get("last_error") or "")

    def test_error_with_failing_guard_recovers(self):
        d = self._daemon_with("error")
        calls = {"n": 0}

        def _guard(child_timeout=10.0):
            calls["n"] += 1
            return calls["n"] == 1      # healthy before the pass, dead after

        d._guard_check = _guard
        d._safe_full_pass(None)
        self.assertEqual(self.recoveries, [1])

    def test_transport_failure_still_recovers(self):
        d = self._daemon_with("killed")
        d._safe_full_pass(None)
        self.assertEqual(self.recoveries, [1])

    def test_a_good_pass_clears_the_error_and_the_backoff(self):
        d = self._daemon_with("usage")
        d._safe_full_pass(None)
        self.assertTrue(d._backing_off())
        d.engine = self.engine                     # fixed (e.g. backend updated)
        d._safe_full_pass(None)
        self.assertFalse(d._backing_off())
        self.assertIsNone(self.state.get().get("last_error"))
        self.assertTrue(os.path.exists(self._mirror("a.txt")))

    def test_scheduled_passes_wait_out_the_backoff(self):
        d = self._daemon_with("usage")
        d._safe_full_pass(None)
        ran = []
        d._safe_full_pass = lambda scope: ran.append(scope)
        far = time.monotonic() - 10 * 3600
        d._main_loop_tick(time.monotonic(), time.time(), far, time.monotonic(),
                          time.monotonic(), set(), None)
        self.assertEqual(ran, [], "tier-2 pass ran while backing off")


class RetryBackupDirTests(V2Base):
    def test_retry_gets_its_own_stamp_dir(self):
        d = self._daemon()
        first = os.path.join(self.trash_root, "2026-10-02T120000", "Alpha")
        retry = d._retry_backup_dir(first, "Alpha")
        self.assertNotEqual(retry, first)
        stamp = os.path.basename(os.path.dirname(retry))
        self.assertIsNotNone(util.parse_stamp(stamp), "retry dir must stay prunable")
        self.assertEqual(os.path.basename(retry), "Alpha")

    def test_io_timeout_retry_of_a_changed_file_batch_uses_a_new_backup_dir(self):
        self._mk("a.txt", "v1")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        self._mk("a.txt", "v2-changed")
        spy = _FailingBatchEngine(self.engine, "io_timeout", rc=30)
        d.engine = spy
        with mock.patch.object(daemon_mod, "_BATCH_RETRY_SLEEP", 0.0):
            with self.assertRaises(SyncFailure):
                d.full_pass(None)
        self.assertEqual(len(spy.calls), 2)
        self.assertNotEqual(spy.calls[0], spy.calls[1])


class ChronicFailureTests(V2Base):
    def test_only_chronic_failures_do_not_fail_the_pass(self):
        self._mk("ok.txt", "fine")
        self._mk("bad.txt", "never lands")
        d = self._daemon()
        d._upload_retry_counts[("Alpha", "bad.txt")] = daemon_mod._UPLOAD_FAIL_DISCLOSE - 1
        real = d._verify_batch_on_nas

        def _partial(*a, **k):
            return SyncResult("partial", 23, 1, 0, ["bad.txt"], None)

        def _verified(dst_root, batch, should_abort=None):
            return set(r for r in real(dst_root, batch) if r != "bad.txt")

        orig = self.engine.phase1_batch

        def _batch(src, dst, bak, rels, **kw):
            orig(src, dst, bak, [r for r in rels if r != "bad.txt"], **kw)
            return _partial()

        d.engine = mock.Mock(wraps=self.engine)
        d.engine.phase1_batch = _batch
        d._verify_batch_on_nas = _verified
        self.assertTrue(d.full_pass(None), "a known chronic failure failed the pass")
        entry = self.state.get()["folders"]["Alpha"]
        self.assertIn("bad.txt", entry["failed_files"])
        self.assertEqual(entry["state"], "upToDate")


class IcloudStubTests(V2Base):
    def _stub(self, rel_target):
        head, tail = os.path.split(os.path.join(self.src, rel_target))
        os.makedirs(head, exist_ok=True)
        with open(os.path.join(head, "." + tail + ".icloud"), "wb") as h:
            h.write(b"bplist00" + b"\0" * 100)

    def test_evicted_file_is_cloud_only_not_deleted(self):
        self._mk("docs/report.pdf", "real content")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        # macOS 12 evicts it: the file goes, a placeholder appears.
        os.remove(os.path.join(self.src, "docs/report.pdf"))
        self._stub("docs/report.pdf")
        plan = self.manifest.plan_folder("Alpha", self.src, [], lambda p, s: False)
        self.assertEqual(plan.deletions, [])
        self.assertIn("docs/report.pdf", plan.dataless)
        self.assertNotIn("docs/.report.pdf.icloud", plan.uploads)
        self.assertTrue(d.full_pass(None))
        self.assertTrue(os.path.exists(self._mirror("docs/report.pdf")),
                        "the NAS copy of an evicted file must stay")
        self.assertFalse(os.path.exists(self._mirror("docs/.report.pdf.icloud")))

    def test_real_file_wins_over_a_leftover_stub(self):
        self._mk("x.txt", "here")
        self._stub("x.txt")
        plan = self.manifest.plan_folder("Alpha", self.src, [], lambda p, s: False)
        self.assertIn("x.txt", plan.uploads)
        self.assertNotIn("x.txt", plan.dataless)

    def test_big_dot_icloud_file_is_a_real_file(self):
        path = os.path.join(self.src, ".notes.icloud")
        with open(path, "wb") as h:
            h.write(b"x" * 10000)
        plan = self.manifest.plan_folder("Alpha", self.src, [], lambda p, s: False)
        self.assertIn(".notes.icloud", plan.uploads)

    def test_verify_does_not_call_an_evicted_file_extra(self):
        self.assertTrue(daemon_mod._source_has(self.src, "missing.txt") is False)
        self._stub("gone.txt")
        self.assertTrue(daemon_mod._source_has(self.src, "gone.txt"))


class MountClassificationTests(_FreshMountpoint, unittest.TestCase):
    _REFUSED = ("mount_smbfs: mount error: /Users/x/Library/Application Support/"
                "MyCloud/mnt: Unknown error: -1073741275")

    def _mgr(self, log=None):
        return mount.MountManager("192.168.5.1", "SHARE", "admin", Path("/mnt"),
                                  "the-uuid", log or _RecLogger())

    def test_classification(self):
        k = mount._mount_error_kind
        self.assertEqual(k(64, "mount_smbfs: ... File exists"), "eexist")
        self.assertEqual(k(77, "Authentication error"), "auth")
        self.assertEqual(k(64, self._REFUSED), "refused")
        self.assertEqual(mount._ntstatus(self._REFUSED), "0xC0000225")

    def test_router_refusing_the_share_is_named_not_eexist(self):
        m = self._mgr()
        rec = _RunRecorder([_fail(rc=77, err="Authentication error"),
                            _fail(rc=64, err=self._REFUSED)])
        with mock.patch.object(util, "run", side_effect=rec), \
                mock.patch.object(mount, "read_nsmb_password", return_value="pw"), \
                mock.patch.object(mount.MountManager, "_resolve_eexist") as ee:
            self.assertFalse(m.mount())
        ee.assert_not_called()
        self.assertIn("拒绝连接共享「SHARE」", m.last_mount_error)
        self.assertIn("0xC0000225", m.last_mount_error)

    def test_saved_credential_is_used_directly_after_n_was_rejected(self):
        m = self._mgr()
        rec = _RunRecorder([_fail(rc=77, err="Authentication error"), _ok(), _ok()])
        with mock.patch.object(util, "run", side_effect=rec), \
                mock.patch.object(mount, "read_nsmb_password", return_value="pw"):
            self.assertTrue(m.mount())
            self.assertTrue(m.mount())          # e.g. the next recovery
        self.assertEqual(len(rec.calls), 3)
        self.assertIn("-N", rec.calls[0][0])
        self.assertNotIn("-N", rec.calls[2][0], "-N retried after it was rejected")


class WakeTests(V2Base):
    def test_guard_after_wake_lets_the_network_settle(self):
        d = self._daemon()
        answers = iter([False, False, True])
        d._guard_check = lambda child_timeout=10.0: next(answers)
        with mock.patch.object(daemon_mod, "_WAKE_GUARD_WAIT", 0.01):
            self.assertTrue(d._guard_check_after_wake())

    def test_guard_after_wake_gives_up(self):
        d = self._daemon()
        d._guard_check = lambda child_timeout=10.0: False
        with mock.patch.object(daemon_mod, "_WAKE_GUARD_WAIT", 0.01):
            self.assertFalse(d._guard_check_after_wake())

    def test_wake_is_logged_and_touches_state(self):
        log = _Log()
        d = self._daemon()
        d.logger = log
        touched = []
        d._idle_touch = lambda: touched.append(1)
        d._guard_check_after_wake = lambda: True
        d._safe_full_pass = lambda scope: None
        now_m, now_w = time.monotonic(), time.time()
        d._main_loop_tick(now_m, now_w - 3600, now_m, now_m, now_m, set(), None)
        self.assertTrue(touched)
        self.assertTrue(any("system wake detected" in m for _l, m in log.lines))


class StartupSelftestTests(V2Base):
    def test_a_failing_selftest_reaches_the_app(self):
        d = self._daemon()
        d.engine = mock.Mock(wraps=self.engine)
        d.engine.batch_selftest = lambda tmp: "rsync rejected MyCloud's upload command"
        d._upload_selftest()
        self.assertIn("rsync 不接受", self.state.get().get("last_error") or "")

    def test_a_mock_engine_without_a_real_answer_is_ignored(self):
        d = self._daemon()
        d.engine = mock.Mock()                 # batch_selftest returns a Mock
        d._upload_selftest()
        self.assertIsNone(self.state.get().get("last_error"))


class DoctorAndVerifyWordingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mycloud-v131-ctl-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _rows(self, selftest):
        from tests.test_ctl import _base_ctl
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, upload_selftest=selftest)
        ctl.cmd_doctor(as_json=True)
        import json
        return {r["id"]: r for r in json.loads(out.getvalue())}

    def test_doctor_reports_a_failing_upload_command(self):
        rows = self._rows(lambda d: "rsync rejected MyCloud's upload command (kind=usage)")
        self.assertFalse(rows["rsync_upload"]["ok"])
        self.assertIn("更新后端", rows["rsync_upload"]["hint"])

    def test_doctor_passes_a_working_upload_command(self):
        self.assertTrue(self._rows(lambda d: None)["rsync_upload"]["ok"])

    def test_verify_says_queued_not_reuploaded(self):
        with open(os.path.join(os.path.dirname(mycloudctl.__file__), "mycloudctl.py"),
                  encoding="utf-8") as h:
            src = h.read()
        self.assertNotIn("missing re-uploaded", src)
        self.assertIn("queued for re-upload", src)


# --- 1.3.1 review fixes ------------------------------------------------------


def _plist_stub(path, size=None, name=None):
    """A macOS <= 13 placeholder: a small binary plist (real size optional)."""
    import plistlib
    data = {"NSURLNameKey": name or os.path.basename(path)}
    if size is not None:
        data["NSURLFileSizeKey"] = size
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as h:
        h.write(plistlib.dumps(data, fmt=plistlib.FMT_BINARY))


class _ScriptedBatchEngine(_FailingBatchEngine):
    """phase1_batch answers from a script of SyncResults, then the real engine."""

    def __init__(self, real, script):
        _FailingBatchEngine.__init__(self, real, None)
        self.script = list(script)

    def phase1_batch(self, src, dst, backup_dir, rels, **kw):
        self.calls.append(backup_dir)
        if self.script:
            return self.script.pop(0)
        return self._real.phase1_batch(src, dst, backup_dir, rels, **kw)


class _Clock(object):
    """daemon_mod.time stand-in with a movable monotonic clock."""

    def __init__(self, start=1000.0):
        self.mono = start

    def monotonic(self):
        return self.mono

    def __getattr__(self, name):
        return getattr(time, name)


class ReviewRoutingTests(V2Base):
    def _daemon_with(self, engine_factory):
        self._mk("a.txt", "a")
        d = self._daemon()
        d.engine = engine_factory(self.engine)
        self.recoveries = []
        d.recovery = lambda: self.recoveries.append(1)
        return d

    def test_a_failed_pass_never_leaves_syncing_behind(self):
        for kind in ("usage", "error"):
            d = self._daemon_with(lambda e: _FailingBatchEngine(e, kind))
            d._safe_full_pass(None)
            data = self.state.get()
            self.assertNotEqual(data.get("state"), "SYNCING", kind)
            states = dict((n, f.get("state"))
                          for n, f in data.get("folders", {}).items())
            self.assertNotIn("syncing", states.values(), (kind, states))

    def test_mkdir_timeout_is_a_transport_failure(self):
        eng = SyncEngine(paths.RSYNC, os.devnull, _Log())
        timed_out = util.RunResult(None, "", "", True)
        with mock.patch.object(util, "run", return_value=timed_out):
            res = eng.phase1_batch(self.src, os.path.join(self.tmp, "dst"),
                                   os.path.join(self.tmp, "bak"), ["a.txt"])
        self.assertEqual(res.status, "failure")
        self.assertIn(res.kind, daemon_mod._TRANSPORT_KINDS)
        failed = util.RunResult(1, "", "Not a directory", False)
        with mock.patch.object(util, "run", return_value=failed):
            res = eng.phase1_batch(self.src, os.path.join(self.tmp, "dst"),
                                   os.path.join(self.tmp, "bak"), ["a.txt"])
        self.assertEqual(res.kind, "error", "a layout problem is not transport")

    def test_io_timeout_then_any_retry_failure_still_remounts(self):
        script = [SyncResult("failure", 30, 0, 0, [], "io_timeout"),
                  SyncResult("failure", None, 0, 0, [], "error")]
        d = self._daemon_with(lambda e: _ScriptedBatchEngine(e, script))
        with mock.patch.object(daemon_mod, "_BATCH_RETRY_SLEEP", 0.0):
            d._safe_full_pass(None)
        self.assertEqual(self.recoveries, [1])
        self.assertFalse(d._backing_off())

    def test_io_timeout_then_usage_is_usage(self):
        script = [SyncResult("failure", 30, 0, 0, [], "io_timeout"),
                  SyncResult("failure", 1, 0, 0, [], "usage")]
        d = self._daemon_with(lambda e: _ScriptedBatchEngine(e, script))
        with mock.patch.object(daemon_mod, "_BATCH_RETRY_SLEEP", 0.0):
            d._safe_full_pass(None)
        self.assertEqual(self.recoveries, [])
        self.assertTrue(d._backing_off())

    def test_a_watchdog_kill_is_transport_whatever_the_exit_code(self):
        d = self._daemon()
        d._kill_reason = "guard"
        d._kill_reason_mono = time.monotonic()
        exc = d._phase1_failure("phase1_batch", "Alpha", 1, "error")
        self.assertEqual(exc.kind, "killed")
        self.assertIn("killed by watchdog", str(exc))
        self.assertEqual(d._phase1_failure("phase1_batch", "Alpha", 1, "error").kind,
                         "error", "the reason is consumed once")

    def test_a_scoped_pass_with_no_uploads_proves_nothing(self):
        self._mk("sub/old.txt", "o")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        self._mk("a.txt", "changed")
        real = d.engine
        d.engine = _FailingBatchEngine(real, "usage")
        d.recovery = lambda: None
        d._safe_full_pass(None)
        msg = self.state.get().get("last_error")
        self.assertIn("rsync 不接受", msg or "")
        first = d._pass_backoff_until - time.monotonic()
        os.remove(os.path.join(self.src, "sub", "old.txt"))   # deletion-only churn
        d._pass_backoff_until = 0.0
        d._safe_full_pass({self.src: ["sub"]})
        self.assertEqual(self.state.get().get("last_error"), msg)
        self.assertEqual(d._pass_fail_streak, 1)
        d._safe_full_pass(None)
        self.assertGreater(d._pass_backoff_until - time.monotonic(), first + 100,
                           "the backoff must keep escalating")

    def test_a_scoped_pass_with_no_uploads_keeps_a_transient_error_too(self):
        self._mk("sub/old.txt", "o")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        self._mk("a.txt", "changed")
        d.engine = _FailingBatchEngine(self.engine, "error")
        d.recovery = lambda: None
        d._safe_full_pass(None)
        msg = self.state.get().get("last_error")
        self.assertIn("NAS 挂载正常", msg or "")
        os.remove(os.path.join(self.src, "sub", "old.txt"))
        d._pass_backoff_until = 0.0
        d._safe_full_pass({self.src: ["sub"]})
        self.assertEqual(self.state.get().get("last_error"), msg)
        self.assertEqual(d._pass_fail_streak, 1)

    def test_a_message_written_after_the_failure_is_not_wiped(self):
        d = self._daemon_with(lambda e: _FailingBatchEngine(e, "error"))
        d._safe_full_pass(None)
        self.state.update(last_error="配置文件有误")      # someone else's message
        d.engine = self.engine
        d._safe_full_pass(None)
        self.assertEqual(d._pass_fail_streak, 0)
        self.assertEqual(self.state.get().get("last_error"), "配置文件有误")

    def test_a_good_pass_clears_the_error_even_with_a_missing_folder(self):
        d = self._daemon_with(lambda e: _FailingBatchEngine(e, "error"))
        d._safe_full_pass(None)
        self.assertEqual(d._pass_fail_streak, 1)
        self.cfg.folders.append(config_mod_FolderSpec(
            Path(os.path.join(self.tmp, "src", "Ext")), "Ext"))
        d.engine = self.engine
        self._mk("b.txt", "b")
        d._safe_full_pass(None)
        self.assertFalse(d._backing_off())
        self.assertEqual(d._pass_fail_streak, 0)
        self.assertIsNone(self.state.get().get("last_error"))
        self.assertEqual(self.state.get()["folders"]["Ext"]["state"], "sourceMissing")

    def test_an_unrelated_message_is_not_wiped(self):
        d = self._daemon()
        self.state.update(last_error="配置文件有误")
        self._mk("a.txt", "a")
        d._safe_full_pass(None)
        self.assertEqual(self.state.get().get("last_error"), "配置文件有误")

    def test_sleep_counts_toward_the_backoff(self):
        d = self._daemon()
        d._pass_backoff_until = time.monotonic() + 3000
        ran = []
        d._guard_check_after_wake = lambda: True
        d._safe_full_pass = lambda scope: ran.append(scope)
        now_m, now_w = time.monotonic(), time.time()
        d._main_loop_tick(now_m, now_w - 8 * 3600, now_m, now_m, now_m, set(), None)
        self.assertEqual(ran, [None], "the wake pass waited out a backoff slept through")

    def test_a_short_nap_only_shortens_the_backoff(self):
        d = self._daemon()
        d._pass_backoff_until = time.monotonic() + 3000
        ran = []
        d._guard_check_after_wake = lambda: True
        d._safe_full_pass = lambda scope: ran.append(scope)
        now_m, now_w = time.monotonic(), time.time()
        d._main_loop_tick(now_m, now_w - 600, now_m, now_m, now_m, set(), None)
        self.assertEqual(ran, [])
        self.assertTrue(d._backing_off())

    def test_recovery_time_is_not_pass_time(self):
        clock = _Clock()
        d = self._daemon_with(lambda e: _FailingBatchEngine(e, "killed"))

        def _long_recovery():
            clock.mono += 6 * 3600
        d.recovery = _long_recovery
        with mock.patch.object(daemon_mod, "time", clock):
            d._safe_full_pass(None)
        self.assertLess(d._last_full_duration, 60)

    def test_startup_selftest_message_survives_a_pass_with_nothing_to_upload(self):
        self._mk("a.txt", "a")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        d2 = self._daemon()
        d2.engine = mock.Mock(wraps=self.engine)
        d2.engine.batch_selftest = lambda tmp: "rsync rejected (kind=usage)"
        d2._upload_selftest()
        msg = self.state.get().get("last_error")
        self.assertIn("rsync 不接受", msg or "")
        self.assertTrue(d2.full_pass(None))
        self.assertEqual(self.state.get().get("last_error"), msg)
        d2.engine = self.engine
        self._mk("b.txt", "b")              # a real upload proves the argv works
        self.assertTrue(d2.full_pass(None))
        self.assertIsNone(self.state.get().get("last_error"))


class UsageRegexTests(unittest.TestCase):
    def test_file_names_never_read_as_usage(self):
        k = engine_mod._failure_kind
        self.assertEqual(k(1, "rsync(1): error: invalid option notes.bin: write: "
                              "No space left on device\nrsync(1): error: unexpected "
                              "end of file", False), "error")
        self.assertEqual(k(11, 'rsync: write failed on "/m/unknown option list.txt": '
                               "No space left on device (28)\nrsync error: error in "
                               "file IO (code 11) at main.c(1)", False), "error")
        self.assertEqual(k(30, 'rsync: send_files failed to open "/s/illegal option '
                               'memo.txt": Permission denied (13)\nio timeout after '
                               "180 seconds -- exiting", False), "io_timeout")

    def test_real_usage_errors_still_are(self):
        k = engine_mod._failure_kind
        self.assertEqual(k(1, _GATEWAY_STDERR, False), "usage")
        self.assertEqual(k(1, "rsync: unrecognized option `--foo'\nusage: rsync [-a]",
                           False), "usage")
        self.assertEqual(k(1, "rsync: invalid option -- Z\nusage: rsync [-a]", False),
                         "usage")
        self.assertEqual(k(12, _GATEWAY_STDERR, False), "error", "usage needs exit 1")


class MountRefusedNarrowTests(_FreshMountpoint, unittest.TestCase):
    def test_only_server_side_causes_are_refused(self):
        k = mount._mount_error_kind
        self.assertEqual(k(64, "mount_smbfs: mount error: /x/mnt: Operation not "
                               "permitted"), "other")
        self.assertEqual(k(64, "mount_smbfs: mount error: /x/mnt: Socket is not "
                               "connected"), "other")
        self.assertEqual(k(64, "mount_smbfs: mount error: /x/mnt: No such file or "
                               "directory"), "refused")
        self.assertEqual(k(64, "mount_smbfs: mount error: /x/mnt: Unknown error: "
                               "-1073741275"), "refused")

    def test_named_message_keeps_the_raw_text(self):
        m = mount.MountManager("192.168.5.1", "SHARE", "admin", Path("/mnt"),
                               "the-uuid", _RecLogger())
        m._share_refused("mount_smbfs: mount error: /x/mnt: No such file or directory")
        self.assertIn("No such file or directory", m.last_mount_error)


class PlaceholderReviewTests(V2Base):
    def _plan(self):
        return self.manifest.plan_folder("Alpha", self.src, [], lambda p, s: False)

    def test_old_placeholder_rows_are_forgotten_not_held(self):
        rows = []
        for i in range(250):
            stub = os.path.join(self.src, "d", ".f%03d.pdf.icloud" % i)
            _plist_stub(stub, size=5000)
            st = os.lstat(stub)
            mirror = self._mirror(os.path.join("d", ".f%03d.pdf.icloud" % i))
            os.makedirs(os.path.dirname(mirror), exist_ok=True)
            shutil.copy(stub, mirror)
            rows.append((os.path.relpath(stub, self.src), st.st_size,
                         st.st_mtime_ns, st.st_ino))
        self.manifest.commit_uploaded("Alpha", rows)          # what <= 1.3.0 did
        self.cfg.max_delete_pct = 20
        plan = self._plan()
        self.assertEqual(plan.deletions, [])
        self.assertEqual(len(plan.forget), 250)
        d = self._daemon()
        for _ in range(2):
            d.full_pass(None)
            entry = self.state.get()["folders"]["Alpha"]
            self.assertNotEqual(entry["state"], "deletionsHeld")
            self.assertFalse(entry.get("held_deletions"))
        self.assertEqual(self.manifest.counts("Alpha")["files"], 0)
        self.assertTrue(os.path.exists(self._mirror("d/.f000.pdf.icloud")),
                        "no NAS action for a forgotten row")

    def test_evicted_package_keeps_its_files(self):
        for i in range(50):
            self._mk("other/f%02d.txt" % i, "x")
        self._mk("Notes.rtfd/TXT.rtf", "text")
        self._mk("Notes.rtfd/img.png", "png")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        shutil.rmtree(os.path.join(self.src, "Notes.rtfd"))
        _plist_stub(os.path.join(self.src, ".Notes.rtfd.icloud"))
        plan = self._plan()
        self.assertEqual(plan.deletions, [])
        self.assertNotIn("Notes.rtfd", plan.dataless)
        self.assertTrue(d.full_pass(None))
        self.assertTrue(os.path.exists(self._mirror("Notes.rtfd/TXT.rtf")))
        self.assertTrue(os.path.exists(self._mirror("Notes.rtfd/img.png")))
        self.assertTrue(daemon_mod._source_has(self.src, "Notes.rtfd/TXT.rtf"))
        res = self.engine.phase2_execute(
            self.src, os.path.join(self.data_root, "Alpha"),
            os.path.join(self.trash_root, "x", "Alpha"), ["Notes.rtfd/TXT.rtf"])
        self.assertEqual(res.moved, 0)
        self.assertTrue(os.path.exists(self._mirror("Notes.rtfd/TXT.rtf")))

    def _file_source(self, content="budget"):
        parent = os.path.join(self.tmp, "src", "Docs")
        os.makedirs(parent)
        path = os.path.join(parent, "budget.numbers")
        with open(path, "w") as h:
            h.write(content)
        self.cfg.folders = [config_mod_FolderSpec(Path(path), "budget.numbers",
                                                   is_file=True)]
        return path

    def test_evicted_file_source_is_not_archived(self):
        path = self._file_source()
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        mirror = os.path.join(self.data_root, "budget.numbers")
        self.assertTrue(os.path.exists(mirror))
        os.remove(path)
        _plist_stub(manifest_mod.placeholder_path(path), size=os.path.getsize(mirror))
        self.assertTrue(d.full_pass(None))
        self.assertTrue(os.path.exists(mirror), "the only backup went to the trash")
        self.assertFalse(os.path.exists(self.trash_root)
                         and any(files for _d, _s, files in os.walk(self.trash_root)))
        entry = self.state.get()["folders"]["budget.numbers"]
        self.assertEqual(entry["icloud_skipped"]["count"], 0, "same size: backed up")

    def test_evicted_file_source_never_reaches_archival(self):
        path = self._file_source()
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        os.remove(path)
        _plist_stub(manifest_mod.placeholder_path(path), size=6)
        calls = []
        d._phase2_file = lambda *a: calls.append(a) or (0, False)
        self.assertTrue(d.full_pass(None))
        self.assertEqual(calls, [])

    def test_phase2_file_refuses_a_cloud_only_source(self):
        path = self._file_source()
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        os.remove(path)
        _plist_stub(manifest_mod.placeholder_path(path))
        calls = []
        d._apply_deletions = lambda *a, **k: calls.append(a) or (1, False, [])
        self.assertEqual(d._phase2_file("budget.numbers", path,
                                        os.path.join(self.trash_root, "x"), {}),
                         (0, False))
        self.assertEqual(calls, [])

    def test_evicted_file_source_with_a_changed_size_is_surfaced(self):
        path = self._file_source()
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        os.remove(path)
        _plist_stub(manifest_mod.placeholder_path(path), size=99999)
        self.assertTrue(d.full_pass(None))
        entry = self.state.get()["folders"]["budget.numbers"]
        self.assertEqual(entry["icloud_skipped"]["count"], 1)

    def test_a_big_dot_icloud_file_is_no_placeholder_for_a_file_source(self):
        path = self._file_source()
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        os.remove(path)
        with open(manifest_mod.placeholder_path(path), "wb") as h:
            h.write(b"x" * (manifest_mod._ICLOUD_STUB_MAX + 1))
        d.full_pass(None)
        self.assertFalse(os.path.exists(os.path.join(self.data_root, "budget.numbers")))

    def test_moving_an_evicted_file_renames_its_nas_copy(self):
        self._mk("a/report.pdf", "r" * 5000)
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        os.remove(os.path.join(self.src, "a", "report.pdf"))
        _plist_stub(os.path.join(self.src, "b", ".report.pdf.icloud"), size=5000)
        plan = self._plan()
        self.assertEqual(plan.renames, [("a/report.pdf", "b/report.pdf")])
        self.assertEqual(plan.deletions, [])
        self.assertTrue(d.full_pass(None))
        self.assertTrue(os.path.exists(self._mirror("b/report.pdf")))
        self.assertFalse(os.path.exists(self._mirror("a/report.pdf")))
        plan = self._plan()
        self.assertEqual((plan.deletions, plan.renames, plan.dataless), ([], [], []))

    def test_an_unpaired_evicted_move_defers_the_deletion(self):
        self._mk("a/report.pdf", "r" * 5000)
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        os.remove(os.path.join(self.src, "a", "report.pdf"))
        _plist_stub(os.path.join(self.src, "b", ".report.pdf.icloud"))   # no size
        plan = self._plan()
        self.assertEqual(plan.deletions, [])
        self.assertEqual(plan.deferred, ["a/report.pdf"])
        self.assertTrue(d.full_pass(None))
        self.assertTrue(os.path.exists(self._mirror("a/report.pdf")))

    def test_evicted_after_backup_is_unchanged_not_cloud_only(self):
        self._mk("r.pdf", "r" * 777)
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        os.remove(os.path.join(self.src, "r.pdf"))
        _plist_stub(os.path.join(self.src, ".r.pdf.icloud"), size=777)
        plan = self._plan()
        self.assertEqual(plan.dataless, [])
        self.assertEqual(plan.totals["unchanged_files"], 1)
        _plist_stub(os.path.join(self.src, ".r.pdf.icloud"), size=778)
        self.assertEqual(self._plan().dataless, ["r.pdf"], "changed in the cloud")

    def test_download_stage_keeps_placeholder_entries(self):
        _plist_stub(os.path.join(self.src, ".new.pdf.icloud"), size=4321)
        d = self._daemon()
        batches = list(d._stat_batches_raw(self.src, ["new.pdf"]))
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0][0][0][:2], ("new.pdf", 4321))

    def test_placeholder_next_to_its_directory_is_ignored(self):
        self._mk("Pkg.rtfd/TXT.rtf", "t")
        _plist_stub(os.path.join(self.src, ".Pkg.rtfd.icloud"))
        plan = self._plan()
        self.assertNotIn("Pkg.rtfd", plan.dataless + plan.uploads)
        self.assertIn("Pkg.rtfd/TXT.rtf", plan.uploads)
        d = self._daemon()
        self.assertEqual(list(d._stat_batches(self.src, ["Pkg.rtfd"])), [])
        self.assertEqual(list(d._stat_batches_raw(self.src, ["Pkg.rtfd"])), [])

    def test_degenerate_names_are_ordinary_files(self):
        for name in ("...icloud", "....icloud"):
            with open(os.path.join(self.src, name), "wb") as h:
                h.write(b"x")
        plan = self._plan()
        self.assertEqual(sorted(plan.uploads), ["....icloud", "...icloud"])
        self.assertEqual(plan.dataless, [])


# --- second review round ----------------------------------------------------


class SecondRoundRoutingTests(V2Base):
    def _fail_once(self, kind="error"):
        self._mk("a.txt", "a")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        self._mk("a.txt", "changed")
        d.engine = _FailingBatchEngine(self.engine, kind)
        d.recovery = lambda: None
        d._safe_full_pass(None)
        self.assertEqual(d._pass_fail_streak, 1)
        return d

    def test_recovery_pass_rechecks_the_guard_before_backing_off(self):
        self._mk("a.txt", "a")
        d = self._daemon()
        d.engine = _FailingBatchEngine(self.engine, "error")
        d._try_online_gated = lambda: True
        d._guard_check = lambda child_timeout=10.0: False   # died again
        d.recovery()
        self.assertTrue(d._recovery_needed.is_set())
        self.assertFalse(d._backing_off())
        self.assertNotIn("NAS 挂载正常", self.state.get().get("last_error") or "")

    def test_an_unchanged_file_source_proves_nothing(self):
        home = os.path.join(self.tmp, "src", "Home")
        os.makedirs(home)
        note = os.path.join(home, "note.txt")
        with open(note, "w") as h:
            h.write("n")
        self.cfg.folders.append(config_mod_FolderSpec(Path(note), "note.txt",
                                                      is_file=True))
        d = self._fail_once()
        msg = self.state.get().get("last_error")
        d._pass_backoff_until = 0.0
        d._safe_full_pass({note: ["."]})         # rsync copies nothing
        self.assertEqual(self.state.get().get("last_error"), msg)
        self.assertEqual(d._pass_fail_streak, 1)

    def test_a_file_source_never_clears_a_rejected_upload_argv(self):
        home = os.path.join(self.tmp, "src", "Home")
        os.makedirs(home)
        note = os.path.join(home, "note.txt")
        with open(note, "w") as h:
            h.write("n")
        self.cfg.folders.append(config_mod_FolderSpec(Path(note), "note.txt",
                                                      is_file=True))
        self._mk("a.txt", "a")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        with open(note, "w") as h:
            h.write("changed")                  # phase1_file really copies
        d.engine = mock.Mock(wraps=self.engine)
        d.engine.batch_selftest = lambda tmp: "--inplace rejected (kind=usage)"
        d._upload_selftest()
        msg = self.state.get().get("last_error")
        self.assertTrue(d.full_pass(None))
        self.assertEqual(self.state.get().get("last_error"), msg,
                         "phase1_file lacks the batch options; it proves nothing")

    def test_a_scoped_upload_elsewhere_does_not_clear_a_failure(self):
        d = self._fail_once()
        msg = self.state.get().get("last_error")
        d.engine = self.engine
        self._mk("docs/new.txt", "fresh")
        d._pass_backoff_until = 0.0
        d._safe_full_pass({self.src: ["docs"]})
        self.assertTrue(os.path.exists(self._mirror("docs/new.txt")))
        self.assertEqual(self.state.get().get("last_error"), msg)
        self.assertEqual(d._pass_fail_streak, 1)
        d._safe_full_pass(None)                 # the full pass proves it
        self.assertIsNone(self.state.get().get("last_error"))

    def test_scoped_passes_wait_out_any_backoff(self):
        for kind in ("error", "usage"):
            d = self._fail_once(kind)
            self.assertTrue(d._backing_off())
            ran = []
            d._safe_full_pass = lambda scope: ran.append(scope)
            d._build_scope = lambda pending: {self.src: ["."]}
            now_m = time.monotonic()
            d._main_loop_tick(now_m, time.time(), now_m, now_m, now_m,
                              {self.src}, now_m - 3600)
            self.assertEqual(ran, [], kind)

    def test_a_huge_failure_streak_never_overflows(self):
        d = self._fail_once()
        d._pass_fail_streak = 5000
        d._pass_backoff_until = 0.0
        d._safe_full_pass(None)
        left = d._pass_backoff_until - time.monotonic()
        self.assertTrue(3500 < left <= 3600, left)

    def test_startup_reconnect_answers_a_recovery_request(self):
        d = self._daemon()
        answers = iter([False, True])
        d._guard_check = lambda child_timeout=10.0: next(answers)
        d.recovery = lambda: d._recovery_needed.set()   # its pass asked again
        self.assertTrue(d.ensure_online())
        self.assertFalse(d._recovery_needed.is_set())

    def test_interrupted_pass_keeps_the_watchdog_guard_reason(self):
        d = self._daemon()
        self.state.update(guard={"layer": "tcp445_local_network", "detail": "x"})
        self.state.folder_update("Alpha", state="syncing")
        d._end_interrupted_pass()
        data = self.state.get()
        self.assertEqual(data["state"], "MOUNTED_IDLE")
        self.assertEqual((data.get("guard") or {}).get("layer"), "tcp445_local_network")
        self.assertEqual(data["folders"]["Alpha"]["state"], "degraded")

    def test_vanished_and_rejected_downloads_never_trip_the_latch(self):
        d = self._daemon()
        gone = [os.path.join(self.src, "gone%d.pdf" % i) for i in range(8)]
        stop = threading.Event()
        mat, failed, blocked = d._materialize_probed(gone, stop)
        self.assertEqual((mat, failed, blocked), ([], [], False))
        for i in range(8):
            _plist_stub(os.path.join(self.src, ".rej%d.pdf.icloud" % i), size=10)
        rej = [os.path.join(self.src, "rej%d.pdf" % i) for i in range(8)]
        d.run_cmd = lambda argv, timeout: util.RunResult(1, "", "no such item", False)
        mat, failed, blocked = d._materialize_probed(rej, stop)
        self.assertFalse(blocked, "a refused placeholder is not the launchd restriction")

    def test_vanished_entries_do_not_dilute_a_real_block(self):
        d = self._daemon(read_fn=lambda p: None)
        d._materialize_timeout = 0.05
        stuck = []
        for i in range(2):
            path = os.path.join(self.src, "stuck%d.pdf" % i)
            _plist_stub(manifest_mod.placeholder_path(path), size=10)
            stuck.append(path)
        gone = [os.path.join(self.src, "gone%d.pdf" % i) for i in range(6)]
        mat, failed, blocked = d._materialize_probed(stuck + gone, threading.Event())
        self.assertTrue(blocked, "every file that could be tried timed out")


class SecondRoundPlaceholderTests(V2Base):
    def _plan(self):
        return self.manifest.plan_folder("Alpha", self.src, [], lambda p, s: False)

    def _backed_up_package(self, rel="Docs/Notes.rtfd"):
        for i in range(50):
            self._mk("other/f%02d.txt" % i, "x")
        self._mk(rel + "/TXT.rtf", "text")
        self._mk(rel + "/img.png", "png")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        shutil.rmtree(os.path.join(self.src, rel))
        _plist_stub(manifest_mod.placeholder_path(os.path.join(self.src, rel)))
        return d

    def _trash_files(self):
        return [f for _d, _s, fs in os.walk(self.trash_root) for f in fs] \
            if os.path.isdir(self.trash_root) else []

    def test_moving_an_evicted_package_renames_its_files(self):
        d = self._backed_up_package()
        os.makedirs(os.path.join(self.src, "Moved"))
        os.rename(os.path.join(self.src, "Docs", ".Notes.rtfd.icloud"),
                  os.path.join(self.src, "Moved", ".Notes.rtfd.icloud"))
        plan = self._plan()
        self.assertEqual(plan.deletions, [])
        self.assertEqual(sorted(plan.renames), [
            ("Docs/Notes.rtfd/TXT.rtf", "Moved/Notes.rtfd/TXT.rtf"),
            ("Docs/Notes.rtfd/img.png", "Moved/Notes.rtfd/img.png")])
        self.assertNotIn("Moved/Notes.rtfd", plan.dataless)
        self.assertTrue(d.full_pass(None))
        self.assertTrue(os.path.exists(self._mirror("Moved/Notes.rtfd/TXT.rtf")))
        self.assertEqual(self._trash_files(), [])
        plan = self._plan()
        self.assertEqual((plan.deletions, plan.renames, plan.dataless), ([], [], []))

    def test_renaming_the_folder_above_an_evicted_package(self):
        d = self._backed_up_package()
        os.rename(os.path.join(self.src, "Docs"), os.path.join(self.src, "Docs2"))
        self.assertEqual(self._plan().deletions, [])
        self.assertTrue(d.full_pass(None))
        self.assertTrue(os.path.exists(self._mirror("Docs2/Notes.rtfd/img.png")))
        self.assertEqual(self._trash_files(), [])

    def test_an_ambiguous_package_move_is_deferred(self):
        d = self._backed_up_package()
        for where in ("A", "B"):
            _plist_stub(os.path.join(self.src, where, ".Notes.rtfd.icloud"))
        os.remove(os.path.join(self.src, "Docs", ".Notes.rtfd.icloud"))
        plan = self._plan()
        self.assertEqual(plan.deletions, [])
        self.assertEqual(sorted(plan.deferred),
                         ["Docs/Notes.rtfd/TXT.rtf", "Docs/Notes.rtfd/img.png"])
        d.full_pass(None)
        self.assertEqual(self._trash_files(), [])
        entry = self.state.get()["folders"]["Alpha"]
        self.assertEqual(entry["deferred_deletions"]["count"], 2)

    def test_a_package_renamed_in_place_is_deferred(self):
        self._backed_up_package()
        os.rename(os.path.join(self.src, "Docs", ".Notes.rtfd.icloud"),
                  os.path.join(self.src, "Docs", ".Notes 2.rtfd.icloud"))
        plan = self._plan()
        self.assertEqual(plan.deletions, [])
        self.assertEqual(len(plan.deferred), 2)

    def test_a_package_with_a_file_missing_on_the_nas_is_not_backed_up(self):
        self._backed_up_package()
        self.manifest.mark_pending("Alpha", ["Docs/Notes.rtfd/img.png"])
        plan = self._plan()
        self.assertIn("Docs/Notes.rtfd", plan.dataless)
        self.assertEqual(plan.deletions, [])

    def test_a_failing_file_inside_an_evicted_package_stays_listed(self):
        d = self._backed_up_package()
        self.state.folder_update("Alpha", failed_files=["Docs/Notes.rtfd/big.mov"])
        self.assertTrue(d.full_pass(None))
        entry = self.state.get()["folders"]["Alpha"]
        self.assertIn("Docs/Notes.rtfd/big.mov", entry.get("failed_files") or [])
        self.assertIn("Docs/Notes.rtfd", entry["icloud_skipped"]["samples"])

    def test_download_policy_uploads_and_evicts_a_package(self):
        self.cfg.icloud_policy = "download"
        pkg = os.path.join(self.src, "New.rtfd")
        _plist_stub(manifest_mod.placeholder_path(pkg))
        evicted = []

        def read_fn(path):
            os.remove(manifest_mod.placeholder_path(path))
            os.makedirs(path)
            for name in ("TXT.rtf", "img.png"):
                with open(os.path.join(path, name), "w") as h:
                    h.write(name)

        def run_cmd(argv, timeout):
            if argv[1:2] == ["help"]:
                return util.RunResult(0, "evict <path>\n", "", False)
            if argv[1:2] == ["evict"]:
                evicted.append(argv[2])
                shutil.rmtree(argv[2])
                _plist_stub(manifest_mod.placeholder_path(argv[2]))
            return util.RunResult(0, "", "", False)

        d = self._daemon(read_fn=read_fn, run_cmd=run_cmd)
        self.assertTrue(d.full_pass(None))
        self.assertTrue(os.path.exists(self._mirror("New.rtfd/TXT.rtf")))
        self.assertTrue(os.path.exists(self._mirror("New.rtfd/img.png")))
        self.assertEqual(evicted, [pkg])
        self.assertEqual(self.state.get()["folders"]["Alpha"]["icloud_skipped"]["count"], 0)
        plan = self._plan()
        self.assertEqual((plan.deletions, plan.dataless), ([], []))

    def test_finder_replace_with_an_evicted_file_defers_the_old_copy(self):
        self._mk("a/report.pdf", "r" * 5000)
        self._mk("b/report.pdf", "s" * 3000)
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        os.remove(os.path.join(self.src, "a", "report.pdf"))
        os.remove(os.path.join(self.src, "b", "report.pdf"))
        _plist_stub(os.path.join(self.src, "b", ".report.pdf.icloud"), size=5000)
        plan = self._plan()
        self.assertEqual(plan.deletions, [])
        self.assertIn("a/report.pdf", plan.deferred)
        d.full_pass(None)
        self.assertTrue(os.path.exists(self._mirror("a/report.pdf")))
        self.assertEqual(self._trash_files(), [])

    def test_a_pending_row_is_not_paired_but_its_arrival_is_fetched(self):
        self._mk("a/report.pdf", "r" * 5000)
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        os.remove(self._mirror("a/report.pdf"))
        self.manifest.mark_pending("Alpha", ["a/report.pdf"])
        os.remove(os.path.join(self.src, "a", "report.pdf"))
        _plist_stub(os.path.join(self.src, "b", ".report.pdf.icloud"), size=5000)
        plan = self._plan()
        self.assertEqual(plan.renames, [])
        self.assertIn("b/report.pdf", plan.dataless)
        self.assertEqual(plan.deletions, [])

    def test_unrelated_deletions_are_not_deferred(self):
        _plist_stub(os.path.join(self.src, "Inbox", ".Scan.pdf.icloud"), size=88888)
        self._mk("Receipts/Scan.pdf", "s" * 1234)       # same name, other size
        self._mk("misc/blob.bin", "b" * 4321)
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        _plist_stub(os.path.join(self.src, "Inbox", ".Scan.pdf.icloud"), size=4321)
        os.remove(os.path.join(self.src, "Receipts", "Scan.pdf"))
        _plist_stub(os.path.join(self.src, "Inbox", ".Scan.pdf.icloud"), size=88888)
        os.remove(os.path.join(self.src, "misc", "blob.bin"))
        plan = self._plan()
        self.assertEqual(plan.deferred, [])
        self.assertEqual(sorted(plan.deletions), ["Receipts/Scan.pdf", "misc/blob.bin"])

    def test_a_gone_mirror_finishes_the_deletion(self):
        for i in range(10):                     # stay under the breaker's 20 %
            self._mk("keep%d.txt" % i, "k")
        self._mk("x.txt", "x")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        os.remove(self._mirror("x.txt"))
        os.remove(os.path.join(self.src, "x.txt"))
        self.assertTrue(d.full_pass(None))
        self.assertEqual(self._plan().deletions, [], "the row was never dropped")

    def test_a_scoped_pass_never_deletes_in_a_placeholder_folder(self):
        for i in range(10):
            self._mk("keep%d.txt" % i, "k")
        self._mk("a/report.pdf", "r" * 5000)
        self._mk("a/other.txt", "o")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        os.remove(os.path.join(self.src, "a", "report.pdf"))
        _plist_stub(os.path.join(self.src, "b", ".report.pdf.icloud"))   # no size
        self.assertTrue(d.full_pass(None))                # whole pass: deferred
        self._mk("a/new.txt", "n")                         # then a/ gets dirty
        d.full_pass({self.src: ["a"]})
        self.assertTrue(os.path.exists(self._mirror("a/report.pdf")),
                        "a scoped pass trashed a deferred deletion")
        self.assertTrue(os.path.exists(self._mirror("a/new.txt")))
        # A real deletion in a placeholder folder still happens -- whole pass.
        os.remove(os.path.join(self.src, "a", "other.txt"))
        d.full_pass({self.src: ["a"]})
        self.assertTrue(os.path.exists(self._mirror("a/other.txt")))
        d.full_pass(None)
        self.assertFalse(os.path.exists(self._mirror("a/other.txt")))

    def test_finder_replace_of_an_evicted_package_defers(self):
        for i in range(50):
            self._mk("other/f%02d.txt" % i, "x")
        self._mk("Docs/Notes.rtfd/TXT.rtf", "moved one")
        self._mk("Archive/Notes.rtfd/TXT.rtf", "old one")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        shutil.rmtree(os.path.join(self.src, "Docs", "Notes.rtfd"))
        shutil.rmtree(os.path.join(self.src, "Archive", "Notes.rtfd"))
        _plist_stub(os.path.join(self.src, "Archive", ".Notes.rtfd.icloud"))
        plan = self._plan()
        self.assertEqual(plan.deletions, [])
        self.assertIn("Docs/Notes.rtfd/TXT.rtf", plan.deferred)
        self.assertEqual(plan.renames, [], "never renamed onto a backed-up package")

    def test_finder_keep_both_move_defers(self):
        self._mk("a/report.pdf", "r" * 5000)
        self._mk("b/report.pdf", "s" * 3000)
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        os.remove(os.path.join(self.src, "a", "report.pdf"))
        _plist_stub(os.path.join(self.src, "b", ".report 2.pdf.icloud"), size=5000)
        plan = self._plan()
        self.assertEqual(plan.deletions, [])
        self.assertEqual(plan.deferred, ["a/report.pdf"])

    def test_a_pending_package_never_absorbs_another(self):
        self._backed_up_package("A/Notes.rtfd")
        self.manifest.mark_pending("Alpha", ["A/Notes.rtfd/img.png"])
        self._mk("B/Notes.rtfd/TXT.rtf", "another")
        d = self._daemon()
        d.full_pass(None)
        shutil.rmtree(os.path.join(self.src, "B", "Notes.rtfd"))
        plan = self._plan()
        self.assertEqual(plan.renames, [])
        self.assertEqual(plan.deletions, [])

    def test_a_package_move_leaves_a_pending_file_behind(self):
        self._backed_up_package()
        self.manifest.mark_pending("Alpha", ["Docs/Notes.rtfd/img.png"])
        os.makedirs(os.path.join(self.src, "Moved"))
        os.rename(os.path.join(self.src, "Docs", ".Notes.rtfd.icloud"),
                  os.path.join(self.src, "Moved", ".Notes.rtfd.icloud"))
        plan = self._plan()
        self.assertEqual(plan.renames,
                         [("Docs/Notes.rtfd/TXT.rtf", "Moved/Notes.rtfd/TXT.rtf")])
        self.assertIn("Docs/Notes.rtfd/img.png", plan.deferred)
        self.assertEqual(plan.deletions, [])

    def test_an_excluded_package_is_never_fetched_for_a_stale_failure(self):
        self.cfg.excludes = ["Notes.rtfd"]
        config_mod_write_excludes(self.cfg, self.excludes)
        _plist_stub(os.path.join(self.src, "Docs", ".Notes.rtfd.icloud"))
        self._mk("Docs/keep.txt", "k")
        d = self._daemon()
        self.state.folder_update("Alpha", failed_files=["Docs/Notes.rtfd/big.mov"])
        d.full_pass(None)
        entry = self.state.get()["folders"]["Alpha"]
        self.assertNotIn("Docs/Notes.rtfd", entry["icloud_skipped"]["samples"])

    def test_a_rename_never_overwrites_a_nas_file(self):
        self._mk("old.txt", "content")
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        stray = self._mirror("new.txt")
        with open(stray, "w") as h:
            h.write("something already on the NAS")
        renamed, fb_up, fb_del = d._execute_renames(
            "Alpha", self.src, os.path.join(self.data_root, "Alpha"),
            [("old.txt", "new.txt")])
        self.assertEqual((renamed, fb_up, fb_del), (0, ["new.txt"], ["old.txt"]))
        with open(stray) as h:
            self.assertEqual(h.read(), "something already on the NAS")

    def test_rename_keeps_pending(self):
        self.manifest.commit_uploaded("Alpha", [("x.txt", 1, 1, 7)])
        self.manifest.mark_pending("Alpha", ["x.txt"])
        self.manifest.commit_renamed("Alpha", [("x.txt", "y.txt")])
        self.assertEqual(self.manifest.rows("Alpha")["y.txt"]["pending"], 1)

    def test_package_files_include_directory_symlinks(self):
        self._mk("P.rtfd/a.txt", "a")
        os.symlink("elsewhere", os.path.join(self.src, "P.rtfd", "link"))
        os.makedirs(os.path.join(self.src, "real"))
        os.symlink(os.path.join(self.src, "real"), os.path.join(self.src, "P.rtfd", "dirlink"))
        d = self._daemon()
        self.assertEqual(d._package_files("Alpha", self.src, "P.rtfd"),
                         ["P.rtfd/a.txt", "P.rtfd/dirlink", "P.rtfd/link"])

    def test_a_package_the_plan_would_select_again_is_not_evicted(self):
        d = self._daemon()
        self.assertFalse(d._package_backed_up("Alpha", "New.rtfd", set()),
                         "no rows: the next plan selects it again")
        self.manifest.commit_uploaded("Alpha", [("New.rtfd/a", 1, 1, 1)])
        self.assertTrue(d._package_backed_up("Alpha", "New.rtfd", set()))
        self.manifest.mark_pending("Alpha", ["New.rtfd/a"])
        self.assertFalse(d._package_backed_up("Alpha", "New.rtfd", set()))

    def test_plan_bytes_use_the_real_size(self):
        for i in range(3):
            _plist_stub(os.path.join(self.src, ".m%d.mov.icloud" % i), size=3000)
        self.assertEqual(self._plan().totals["dataless_bytes"], 9000)

    def test_backed_up_file_source_is_not_downloaded_again(self):
        parent = os.path.join(self.tmp, "src", "Docs")
        os.makedirs(parent)
        path = os.path.join(parent, "budget.numbers")
        with open(path, "w") as h:
            h.write("budget")
        self.cfg.folders = [config_mod_FolderSpec(Path(path), "budget.numbers",
                                                   is_file=True)]
        self.cfg.icloud_policy = "download"
        d = self._daemon()
        self.assertTrue(d.full_pass(None))
        os.remove(path)
        _plist_stub(manifest_mod.placeholder_path(path), size=6)
        calls = []
        d._materialize_one = lambda p: calls.append(p) or False
        self.assertTrue(d.full_pass(None))
        self.assertEqual(calls, [])
        self.assertEqual(self.state.get()["folders"]["budget.numbers"]
                         ["icloud_skipped"]["count"], 0)


# --- dangling symlinks (2026-10-04) ------------------------------------------

_FAKE_RSYNC_269 = """#!/usr/bin/python3
# rsync 2.6.9's batch semantics, as far as MyCloud uses them: every
# --files-from entry is lstat()ed, a symlink is copied AS A LINK (its target
# need not exist), -b moves the replaced entry into --backup-dir.
import os, shutil, sys
args = sys.argv[1:]
pos = [a for a in args if not a.startswith("-")]
src, dst = pos[-2].rstrip("/"), pos[-1].rstrip("/")
bak = next((a.split("=", 1)[1] for a in args if a.startswith("--backup-dir=")), None)
for raw in sys.stdin.buffer.read().split(b"\\0"):
    if not raw:
        continue
    rel = os.fsdecode(raw)
    rel = rel[2:] if rel.startswith("./") else rel
    s, d = os.path.join(src, rel), os.path.join(dst, rel)
    os.lstat(s)
    os.makedirs(os.path.dirname(d), exist_ok=True)
    if os.path.lexists(d):
        if bak:
            b = os.path.join(bak, rel)
            os.makedirs(os.path.dirname(b), exist_ok=True)
            os.rename(d, b)
        else:
            os.remove(d)
    if os.path.islink(s):
        os.symlink(os.readlink(s), d)
    else:
        shutil.copyfile(s, d)
    print(">f+++++++ " + rel)
"""


def _fake_rsync_269(tmp):
    path = os.path.join(tmp, "rsync269")
    with open(path, "w") as h:
        h.write(_FAKE_RSYNC_269)
    os.chmod(path, 0o755)
    return path


def _local_rsync_is_openrsync():
    out = util.run([paths.RSYNC, "--version"], 10)
    return "openrsync" in ((out.out or "") + (out.err or ""))


class DanglingLinkSelftestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mycloud-dangling-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_rsync_269_semantics_pass(self):
        eng = SyncEngine(_fake_rsync_269(self.tmp), os.devnull, _Log())
        self.assertTrue(eng.dangling_link_selftest(os.path.join(self.tmp, "w")))

    def test_an_rsync_that_quietly_skips_links_fails(self):
        path = os.path.join(self.tmp, "rsync-skips-links")
        with open(path, "w") as h:
            h.write(_FAKE_RSYNC_269.replace(
                "    if os.path.islink(s):\n        os.symlink(os.readlink(s), d)\n",
                "    if os.path.islink(s):\n        continue\n"))
        os.chmod(path, 0o755)
        eng = SyncEngine(path, os.devnull, _Log())
        self.assertFalse(eng.dangling_link_selftest(os.path.join(self.tmp, "w")))

    def test_this_machines_rsync_is_judged_by_what_it_does(self):
        eng = SyncEngine(paths.RSYNC, os.devnull, _Log())
        got = eng.dangling_link_selftest(os.path.join(self.tmp, "w"))
        # openrsync drops the link AND the rest of the batch (exit 23).
        self.assertEqual(got, not _local_rsync_is_openrsync())

    @unittest.skipUnless(os.environ.get("MYCLOUD_RSYNC269"),
                         "set MYCLOUD_RSYNC269 to a real rsync 2.6.9 binary")
    def test_real_rsync_269(self):
        eng = SyncEngine(os.environ["MYCLOUD_RSYNC269"], os.devnull, _Log())
        self.assertTrue(eng.dangling_link_selftest(os.path.join(self.tmp, "w")))


class DanglingLinkUploadTests(V2Base):
    def _link(self, rel="venv", target="/Users/someone-else/project/venv"):
        path = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)
        return path

    def test_backed_up_as_a_link_where_rsync_can(self):
        self._mk("a.txt", "a")
        self._mk("z.txt", "z")
        self._link()
        d = self._daemon()
        d.engine = SyncEngine(_fake_rsync_269(self.tmp), self.excludes, _Log(),
                              proc_registry=self.proc_registry)
        self.state.folder_update("Alpha", failed_files=["venv"])   # listed before
        d._probe_dangling_links()
        self.assertTrue(d._dangling_links_ok)
        self.assertTrue(d.full_pass(None))
        mirror = self._mirror("venv")
        self.assertTrue(os.path.islink(mirror))
        self.assertEqual(os.readlink(mirror), "/Users/someone-else/project/venv")
        self.assertTrue(os.path.exists(self._mirror("z.txt")))
        entry = self.state.get()["folders"]["Alpha"]
        self.assertNotIn("venv", entry.get("failed_files") or [])
        plan = self.manifest.plan_folder("Alpha", self.src, [], lambda p, s: False)
        self.assertEqual(plan.uploads, [], "the link's row was not committed")

    @unittest.skipUnless(os.environ.get("MYCLOUD_RSYNC269"),
                         "set MYCLOUD_RSYNC269 to a real rsync 2.6.9 binary")
    def test_real_rsync_269_end_to_end(self):
        self._mk("a.txt", "a")
        self._mk("z.txt", "z")
        self._link()
        d = self._daemon()
        d.engine = SyncEngine(os.environ["MYCLOUD_RSYNC269"], self.excludes, _Log(),
                              proc_registry=self.proc_registry)
        d._probe_dangling_links()
        self.assertTrue(d._dangling_links_ok)
        self.assertTrue(d.full_pass(None))
        self.assertEqual(os.readlink(self._mirror("venv")),
                         "/Users/someone-else/project/venv")
        self.assertTrue(os.path.exists(self._mirror("z.txt")))
        os.remove(os.path.join(self.src, "venv"))
        self._link(target="/Users/someone-else/other")       # retargeted
        self.assertTrue(d.full_pass(None))
        self.assertEqual(os.readlink(self._mirror("venv")), "/Users/someone-else/other")

    def test_kept_out_where_rsync_cannot(self):
        self._mk("a.txt", "a")
        self._link()
        d = self._daemon()
        d._probe_dangling_links()
        if not _local_rsync_is_openrsync():
            self.skipTest("this machine's rsync copies dangling links")
        self.assertFalse(d._dangling_links_ok)
        d.full_pass(None)
        self.assertFalse(os.path.lexists(self._mirror("venv")))
        self.assertIn("venv", self.state.get()["folders"]["Alpha"]["failed_files"])
        self.assertTrue(os.path.exists(self._mirror("a.txt")))

    def test_probed_at_startup(self):
        d = self._daemon()
        calls = []
        d._probe_dangling_links = lambda: calls.append(1)
        d._capability_check()
        self.assertEqual(calls, [1])

    def test_an_engine_without_a_real_answer_keeps_them_out(self):
        d = self._daemon()
        d.engine = mock.Mock()                 # returns a Mock, not True
        d._probe_dangling_links()
        self.assertFalse(d._dangling_links_ok)


if __name__ == "__main__":
    unittest.main()
