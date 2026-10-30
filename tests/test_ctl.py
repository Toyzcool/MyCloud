import tests._path  # noqa: F401

import fcntl
import io
import json
import shutil
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import mycloudctl
from mycloud import config as config_mod
from mycloud import paths
from mycloud.trash import TrashManager
from mycloud.util import RunResult

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CTL_PY = os.path.join(_REPO, "src", "mycloudctl.py")


FIXED_NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


def _now():
    return FIXED_NOW


def _ok(out=""):
    return RunResult(0, out, "", False)


def _fail(rc=1, out="", err=""):
    return RunResult(rc, out, err, False)


class _Recorder(object):
    """A run_cmd stub that records calls and returns canned results by argv[0]."""

    def __init__(self, results=None, default=None):
        self.calls = []
        self.inputs = []   # stdin text per call (None when nothing was piped)
        self.results = results or {}
        self.default = default if default is not None else _ok()

    def __call__(self, argv, timeout, input_text=None):
        self.calls.append(list(argv))
        self.inputs.append(input_text)
        key = argv[0]
        result = self.results.get(key, self.default)
        if callable(result):
            return result(argv)
        return result


def _write_state(runtime_dir, doc):
    with open(os.path.join(runtime_dir, "state.json"), "w", encoding="utf-8") as h:
        json.dump(doc, h)


def _hold_daemon_lock(testcase, runtime_dir, pid):
    """Simulate a LIVE daemon: write the PID to daemon.lock AND hold an
    exclusive flock on it (the ctl's liveness check probes the flock, since a
    stale unlocked PID could be a recycled, unrelated process). The fd is kept
    open for the test's duration and closed on cleanup."""
    path = os.path.join(runtime_dir, "daemon.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.ftruncate(fd, 0)
    os.write(fd, ("%d\n" % pid).encode("ascii"))
    os.fsync(fd)
    testcase.addCleanup(os.close, fd)
    return fd


def _base_ctl(runtime_dir, **kwargs):
    defaults = dict(
        runtime_dir=runtime_dir,
        logdir=runtime_dir,
        config_path=os.path.join(runtime_dir, "config.ini"),
        mnt=os.path.join(runtime_dir, "mnt"),
        agent_plist=os.path.join(runtime_dir, "agent.plist"),
        uid=501,
        now_fn=_now,
        out=io.StringIO(),
        # Fully stubbed seams: nothing touches the real system by default.
        run_cmd=_Recorder(default=_fail()),
        probe_tcp=lambda host, *a, **k: False,
        kill=lambda pid, sig: None,
        cap_probe=lambda tmp, rsync: {
            "backup_moves_overwritten": True,
            "delete_suppressed_with_backup": True,
            "deleting_line_format_ok": True,
        },
        execv=lambda path, argv: None,
        input_fn=lambda prompt="": "no",
        sleep_fn=lambda s: None,
        upload_selftest=lambda tmp_dir: None,
    )
    defaults.update(kwargs)
    return mycloudctl.Ctl(**defaults)


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "mnt"), exist_ok=True)

    def _fixture_state(self):
        last_ok = (FIXED_NOW - timedelta(hours=2)).isoformat()
        updated = (FIXED_NOW - timedelta(minutes=5)).isoformat()
        return {
            "state": "MOUNTED_IDLE",
            "updated_at": updated,
            "last_sync_ok_at": last_ok,
            "deletion_disabled": False,
            "watcher_degraded": True,
            "folders": {
                "Documents": {
                    "state": "upToDate",
                    "last_ok": last_ok,
                    "files_copied": 12,
                    "files_trashed": 3,
                    "held_deletions": 0,
                    "failed_files": ["huge.iso", "bad:name.txt"],
                    "orphaned": False,
                },
                "Desktop": {
                    "state": "deletionsHeld",
                    "last_ok": last_ok,
                    "files_copied": 4,
                    "files_trashed": 0,
                    "held_deletions": 250,
                    "held_paths": ["a/b.txt", "c.doc"],
                    "failed_files": [],
                    "orphaned": True,
                },
            },
        }

    def test_status_json_derived_fields(self):
        _write_state(self.dir, self._fixture_state())
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out)
        rc = ctl.cmd_status(as_json=True)
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertIn("derived", payload)
        d = payload["derived"]
        # last sync ok was 2h ago.
        self.assertAlmostEqual(d["last_sync_ok_age_seconds"], 7200, delta=2)
        self.assertTrue(d["deletions_held"])
        self.assertEqual(d["total_held_deletions"], 250)
        self.assertEqual(d["total_failed_files"], 2)
        self.assertEqual(d["orphaned_folders"], ["Desktop"])
        self.assertTrue(d["watcher_degraded"])
        self.assertEqual(
            d["permanently_failing_files"]["Documents"],
            ["huge.iso", "bad:name.txt"])
        self.assertEqual(d["folders"]["Documents"]["failed_files_count"], 2)
        self.assertEqual(d["folders"]["Desktop"]["held_deletions"], 250)
        # Raw state document is preserved alongside derived.
        self.assertEqual(payload["state"], "MOUNTED_IDLE")

    def test_status_totals_exclude_removed_sources(self):
        # Companion to the doctor fix (2026-07-28): headline totals must describe
        # the folders the daemon still syncs. A removed source keeps its counters
        # frozen in state.folders forever, so counting it inflates the numbers --
        # but the folder must STILL be listed (and flagged orphaned) so the user
        # can see it exists and clean it up.
        live = os.path.join(self.dir, "LiveSource")
        os.makedirs(live, exist_ok=True)
        cfg_path = os.path.join(self.dir, "config.ini")
        _write_config(cfg_path, folders=[live])
        _write_state(self.dir, {
            "state": "MOUNTED_IDLE",
            "updated_at": FIXED_NOW.isoformat(),
            "folders": {
                "LiveSource": {"failed_files": ["real-problem.txt"],
                               "held_deletions": 5, "held_paths": ["x.txt"]},
                "GhostRemovedSource": {"orphaned": True,
                                       "failed_files": ["stale1", "stale2"],
                                       "held_deletions": 900,
                                       "held_paths": ["old.txt"]},
            }})
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, config_path=cfg_path)
        self.assertEqual(ctl.cmd_status(as_json=True), 0)
        d = json.loads(out.getvalue())["derived"]
        self.assertEqual(d["total_failed_files"], 1)      # not 3
        self.assertEqual(d["total_held_deletions"], 5)    # not 905
        self.assertNotIn("GhostRemovedSource", d["permanently_failing_files"])
        # Still visible, still flagged as orphaned.
        self.assertIn("GhostRemovedSource", d["folders"])
        self.assertIn("GhostRemovedSource", d["orphaned_folders"])
        self.assertFalse(d["folders"]["GhostRemovedSource"]["configured"])
        self.assertTrue(d["folders"]["LiveSource"]["configured"])

    def test_status_human_readable_names_failing_files(self):
        _write_state(self.dir, self._fixture_state())
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out)
        rc = ctl.cmd_status(as_json=False)
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("DELETIONS HELD", text)
        self.assertIn("huge.iso", text)
        self.assertIn("bad:name.txt", text)
        self.assertIn("DEGRADED", text)
        self.assertIn("Documents", text)
        self.assertIn("Desktop", text)

    def test_status_json_passes_progress_and_watcher_verbatim(self):
        state = self._fixture_state()
        state["progress"] = {
            "active": True, "folder": "Documents", "files_done": 7,
            "total_files": 42, "current": "a/b.txt",
            "started_at": FIXED_NOW.isoformat(),
        }
        _write_state(self.dir, state)
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out)
        ctl.cmd_status(as_json=True)
        payload = json.loads(out.getvalue())
        # progress is passed through verbatim (not reshaped).
        self.assertEqual(payload["progress"], state["progress"])
        self.assertTrue(payload["watcher_degraded"])

    def test_status_missing_state_is_default(self):
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out)
        rc = ctl.cmd_status(as_json=True)
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["state"], "OFFLINE")
        self.assertIsNone(payload["derived"]["last_sync_ok_age_seconds"])
        self.assertFalse(payload["derived"]["deletions_held"])

    def test_status_shows_prefetch_line_when_active(self):
        state = {
            "state": "OFFLINE",
            "updated_at": (FIXED_NOW - timedelta(minutes=1)).isoformat(),
            "prefetch": {"active": True, "done": 100, "total": 500},
            "prefetch_skipped": 3,
            "progress": {
                "active": True, "phase": "downloading", "folder": "Documents",
                "files_done": 100, "total_files": 500,
                "bytes_done": 5 * 2 ** 30, "bytes_total": 45 * 2 ** 30,
                "current": None, "started_at": FIXED_NOW.isoformat(),
            },
        }
        _write_state(self.dir, state)
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out)
        ctl.cmd_status(as_json=False)
        text = out.getvalue()
        self.assertIn("prefetch", text)
        self.assertIn("downloading", text)
        self.assertIn("100/500", text)
        self.assertIn("GiB", text)          # byte progress from the live snapshot
        self.assertIn("skipped", text)      # 3 skipped files surfaced

    def test_status_no_prefetch_line_when_absent(self):
        _write_state(self.dir, {"state": "MOUNTED_IDLE",
                                "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out)
        ctl.cmd_status(as_json=False)
        self.assertNotIn("prefetch", out.getvalue())

    def test_status_json_passes_prefetch_blocked_through(self):
        state = {"state": "MOUNTED_IDLE", "updated_at": FIXED_NOW.isoformat(),
                 "prefetch": {"active": False, "done": 0, "total": 200,
                              "blocked": True}}
        _write_state(self.dir, state)
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out)
        ctl.cmd_status(as_json=True)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["prefetch"]["blocked"])   # passed through verbatim

    def test_status_human_shows_blocked_note(self):
        state = {"state": "OFFLINE", "updated_at": FIXED_NOW.isoformat(),
                 "prefetch": {"active": False, "done": 0, "total": 200,
                              "blocked": True}}
        _write_state(self.dir, state)
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out)
        ctl.cmd_status(as_json=False)
        self.assertIn("BLOCKED by macOS", out.getvalue())


class SyncNowReloadTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_sync_now_no_lock_file(self):
        out = io.StringIO()
        killed = []
        ctl = _base_ctl(self.dir, out=out, kill=lambda p, s: killed.append((p, s)))
        rc = ctl.cmd_sync_now()
        self.assertEqual(rc, 1)
        self.assertIn("does not appear to be running", out.getvalue())
        self.assertEqual(killed, [])

    def test_sync_now_signals_pid(self):
        _hold_daemon_lock(self, self.dir, 4321)
        out = io.StringIO()
        killed = []
        ctl = _base_ctl(self.dir, out=out, kill=lambda p, s: killed.append((p, s)))
        rc = ctl.cmd_sync_now()
        self.assertEqual(rc, 0)
        self.assertEqual(killed, [(4321, signal.SIGUSR1)])

    def test_sync_now_dead_pid(self):
        with open(os.path.join(self.dir, "daemon.lock"), "w") as h:
            h.write("999999\n")

        def _raise(pid, sig):
            raise ProcessLookupError()

        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, kill=_raise)
        rc = ctl.cmd_sync_now()
        self.assertEqual(rc, 1)
        self.assertIn("not running", out.getvalue())

    def test_sync_now_reports_already_syncing_with_byte_progress(self):
        # 2026-07-27 user report: "clicking sync-now does nothing". SIGUSR1 is
        # only read BETWEEN passes, so during a long pass nothing observable
        # happens. The command must SAY that, and show live progress -- byte
        # percentage first, plus the last-transfer stamp that distinguishes
        # healthy-but-slow from stalled.
        _hold_daemon_lock(self, self.dir, 4321)
        _write_state(self.dir, {
            "state": "SYNCING",
            "updated_at": FIXED_NOW.isoformat(),
            "progress": {
                "active": True, "folder": "com~apple~CloudDocs",
                "phase": "transferring",
                "files_done": 44490, "total_files": 87919,
                "bytes_done": 21 * (1 << 30), "bytes_total": 42 * (1 << 30),
                "batches_done": 2, "batches_total": 12,
                "last_transfer_at": (FIXED_NOW - timedelta(seconds=8)).isoformat(),
            }})
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, kill=lambda p, s: None)
        rc = ctl.cmd_sync_now()
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("已在同步中", text)
        self.assertIn("50%", text)                 # 21/42 GiB by BYTES
        self.assertIn("21.0/42.0 GiB", text)
        self.assertIn("44490/87919", text)         # file count as secondary info
        self.assertIn("第 3/12 批", text)
        self.assertIn("8 秒前", text)              # last-transfer freshness
        self.assertIn("本轮结束后", text)          # the real queuing semantics

    def test_sync_now_reports_offline_queued(self):
        _hold_daemon_lock(self, self.dir, 4321)
        _write_state(self.dir, {"state": "OFFLINE",
                                "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, kill=lambda p, s: None)
        self.assertEqual(ctl.cmd_sync_now(), 0)
        text = out.getvalue()
        self.assertIn("不可达", text)
        self.assertIn("重新挂载", text)

    def test_sync_now_idle_says_triggered(self):
        _hold_daemon_lock(self, self.dir, 4321)
        _write_state(self.dir, {"state": "MOUNTED_IDLE",
                                "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, kill=lambda p, s: None)
        self.assertEqual(ctl.cmd_sync_now(), 0)
        self.assertIn("已触发立即同步", out.getvalue())

    def test_reload_sends_sigusr2(self):
        _hold_daemon_lock(self, self.dir, 222)
        out = io.StringIO()
        killed = []
        ctl = _base_ctl(self.dir, out=out, kill=lambda p, s: killed.append((p, s)))
        rc = ctl.cmd_reload()
        self.assertEqual(rc, 0)
        self.assertEqual(killed, [(222, signal.SIGUSR2)])


class ApproveDeletionsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _held_state(self):
        return {
            "state": "DELETIONS_HELD",
            "folders": {
                "Documents": {
                    "held_deletions": 2,
                    "held_paths": ["notes/old.txt", "こんにちは.md"],
                },
            },
        }

    def test_approve_writes_token_on_yes(self):
        _write_state(self.dir, self._held_state())
        _hold_daemon_lock(self, self.dir, 77)
        out = io.StringIO()
        prompts = []
        killed = []
        ctl = _base_ctl(
            self.dir, out=out,
            input_fn=lambda prompt="": prompts.append(prompt) or "yes",
            kill=lambda p, s: killed.append((p, s)))
        rc = ctl.cmd_approve_deletions()
        self.assertEqual(rc, 0)
        token = os.path.join(self.dir, "approval.token")
        self.assertTrue(os.path.exists(token))
        text = out.getvalue()
        self.assertIn("notes/old.txt", text)
        self.assertIn("こんにちは.md", text)
        self.assertEqual(killed, [(77, signal.SIGUSR1)])
        self.assertTrue(prompts)  # input() was actually consulted

    def test_approve_refuses_when_nothing_held(self):
        _write_state(self.dir, {"state": "MOUNTED_IDLE", "folders": {}})
        out = io.StringIO()
        called = []
        ctl = _base_ctl(
            self.dir, out=out,
            input_fn=lambda prompt="": called.append(True) or "yes")
        rc = ctl.cmd_approve_deletions()
        self.assertEqual(rc, 1)
        self.assertIn("Nothing is held", out.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.dir, "approval.token")))
        self.assertEqual(called, [])  # never prompted

    def test_approve_aborts_on_non_yes(self):
        _write_state(self.dir, self._held_state())
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, input_fn=lambda prompt="": "nope")
        rc = ctl.cmd_approve_deletions()
        self.assertEqual(rc, 1)
        self.assertIn("Aborted", out.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.dir, "approval.token")))


class PauseResumeTests(unittest.TestCase):
    """pause/resume are IDEMPOTENT (2026-07-31 user report: clicking 恢复 while
    the daemon was already running failed with "bootstrap rc=5" and did
    nothing). launchctl print <target> rc==0 means "agent loaded"; the daemon
    process being ALIVE is a separate check (the flock on daemon.lock)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    @staticmethod
    def _launchctl(print_rc=0, bootout_rc=0, bootstrap_rc=0, kick_rc=0):
        """A run_cmd router keyed on the launchctl subcommand."""
        def _r(argv):
            if argv[0] != paths.LAUNCHCTL:
                return _ok()
            sub = argv[1]
            rc = {"print": print_rc, "bootout": bootout_rc,
                  "bootstrap": bootstrap_rc, "kickstart": kick_rc}.get(sub, 0)
            return _ok() if rc == 0 else _fail(rc=rc)
        return _r

    def _subcalls(self, rec):
        return [c[1] for c in rec.calls if c[0] == paths.LAUNCHCTL]

    # -- pause ---------------------------------------------------------------

    def test_pause_boots_out_when_loaded(self):
        rec = _Recorder(default=self._launchctl(print_rc=0))
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_pause()
        self.assertEqual(rc, 0)
        self.assertEqual(self._subcalls(rec), ["print", "bootout"])
        self.assertIn("已暂停", out.getvalue())

    def test_pause_when_already_paused_is_a_clean_noop(self):
        rec = _Recorder(default=self._launchctl(print_rc=3))  # not loaded
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_pause()
        self.assertEqual(rc, 0)
        self.assertEqual(self._subcalls(rec), ["print"])  # never bootout
        self.assertIn("已处于暂停状态", out.getvalue())

    def test_pause_reports_bootout_failure(self):
        rec = _Recorder(default=self._launchctl(print_rc=0, bootout_rc=9))
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_pause()
        self.assertEqual(rc, 1)
        self.assertIn("暂停失败", out.getvalue())

    # -- resume --------------------------------------------------------------

    def test_resume_while_running_is_a_clean_noop(self):
        # THE reported bug: loaded agent + alive daemon used to hit
        # "bootstrap rc=5" and exit 1.
        _hold_daemon_lock(self, self.dir, 4321)   # daemon alive (flock held)
        rec = _Recorder(default=self._launchctl(print_rc=0))
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_resume()
        self.assertEqual(rc, 0)
        self.assertEqual(self._subcalls(rec), ["print"])  # no bootstrap/kick
        self.assertIn("已在运行", out.getvalue())

    def test_resume_loaded_but_dead_kickstarts(self):
        # Loaded agent, dead daemon (no flock): kickstart restarts it.
        rec = _Recorder(default=self._launchctl(print_rc=0))
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_resume()
        self.assertEqual(rc, 0)
        self.assertEqual(self._subcalls(rec), ["print", "kickstart"])
        self.assertIn("重新启动", out.getvalue())

    def test_resume_not_loaded_bootstraps_then_kickstarts(self):
        rec = _Recorder(default=self._launchctl(print_rc=3))  # not loaded
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_resume()
        self.assertEqual(rc, 0)
        self.assertEqual(self._subcalls(rec),
                         ["print", "bootstrap", "kickstart"])
        self.assertIn("已恢复", out.getvalue())

    def test_resume_bootstrap_ealready_race_confirms_then_kickstarts(self):
        # print said "not loaded", bootstrap raced to EALREADY (rc=5), and a
        # second print CONFIRMS the agent is loaded -> proceed (kickstart).
        prints = {"n": 0}
        base = self._launchctl(print_rc=0, bootstrap_rc=5)

        def router(argv):
            if argv[0] == paths.LAUNCHCTL and argv[1] == "print":
                prints["n"] += 1
                return _fail(rc=3) if prints["n"] == 1 else _ok()
            return base(argv)
        rec = _Recorder(default=router)
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_resume()
        self.assertEqual(rc, 0)
        self.assertEqual(self._subcalls(rec),
                         ["print", "bootstrap", "print", "kickstart"])

    def test_resume_rc5_while_nothing_gets_loaded_is_a_failure(self):
        # rc=5 because the previous instance is still being torn down, and it
        # never ends: claiming success left NOTHING running (2026-09-25).
        rec = _Recorder(default=self._launchctl(print_rc=3, bootstrap_rc=5))
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_resume()
        self.assertEqual(rc, 1)
        self.assertGreater(self._subcalls(rec).count("bootstrap"), 1)   # retried

    def test_resume_reports_real_bootstrap_failure(self):
        rec = _Recorder(default=self._launchctl(print_rc=3, bootstrap_rc=9))
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_resume()
        self.assertEqual(rc, 1)
        self.assertIn("恢复失败", out.getvalue())

    # -- 2026-08-01 gateway field report: kickstart -k after bootstrap killed
    # the RunAtLoad-started daemon mid-startup; on a slow first start the
    # kill+restart overran launchctl's 30s budget and resume reported failure
    # for a healthy daemon. Liveness now short-circuits / rescues kickstart.

    def test_resume_not_loaded_skips_kickstart_when_daemon_comes_up(self):
        _hold_daemon_lock(self, self.dir, 4321)  # RunAtLoad brought it up
        rec = _Recorder(default=self._launchctl(print_rc=3))
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_resume()
        self.assertEqual(rc, 0)
        self.assertEqual(self._subcalls(rec), ["print", "bootstrap"])
        self.assertIn("已恢复", out.getvalue())

    def test_resume_kickstart_timeout_with_live_daemon_is_success(self):
        # loaded + dead -> kickstart -k times out, but the daemon is up by
        # the time we re-probe: that is a success, not a failure.
        def router(argv):
            if argv[0] == paths.LAUNCHCTL and argv[1] == "kickstart":
                return RunResult(1, "", "", True)  # timed out
            return self._launchctl(print_rc=0)(argv)
        rec = _Recorder(default=router)
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        probes = iter([False, False, True])  # dead at entry, alive on re-probe
        ctl._daemon_alive = lambda: next(probes, True)
        rc = ctl.cmd_resume()
        self.assertEqual(rc, 0)
        self.assertEqual(self._subcalls(rec), ["print", "kickstart"])
        self.assertIn("存活探测确认", out.getvalue())

    def test_resume_slow_start_rescued_by_probe_after_kickstart_error(self):
        # not loaded -> bootstrap ok, daemon slower than the first probe
        # window, fallback kickstart hits EALREADY (daemon won the race),
        # second probe window sees it alive: success.
        def router(argv):
            if argv[0] == paths.LAUNCHCTL and argv[1] == "kickstart":
                self.assertNotIn("-k", argv)  # fallback must not kill it
                return _fail(rc=5)
            return self._launchctl(print_rc=3)(argv)
        rec = _Recorder(default=router)
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        probes = iter([False] * 25 + [True])
        ctl._daemon_alive = lambda: next(probes, True)
        rc = ctl.cmd_resume()
        self.assertEqual(rc, 0)
        self.assertEqual(self._subcalls(rec), ["print", "bootstrap", "kickstart"])
        self.assertIn("存活探测确认", out.getvalue())


class LogsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_logs_prints_path_and_execs_tail(self):
        out = io.StringIO()
        execs = []
        ctl = _base_ctl(self.dir, out=out, execv=lambda p, a: execs.append((p, a)))
        rc = ctl.cmd_logs(follow=False)
        self.assertEqual(rc, 0)
        self.assertIn(os.path.join(self.dir, "mycloud.log"), out.getvalue())
        self.assertEqual(len(execs), 1)
        self.assertIn("-n", execs[0][1])

    def test_logs_follow_uses_dash_f(self):
        out = io.StringIO()
        execs = []
        ctl = _base_ctl(self.dir, out=out, execv=lambda p, a: execs.append((p, a)))
        ctl.cmd_logs(follow=True)
        self.assertIn("-f", execs[0][1])


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.mnt = os.path.join(self.dir, "mnt")
        os.makedirs(self.mnt, exist_ok=True)
        os.chmod(self.mnt, 0o500)
        self.config_path = os.path.join(self.dir, "config.ini")
        with open(self.config_path, "w", encoding="utf-8") as h:
            h.write(
                "[server]\n"
                "host = 192.168.5.1\n"
                "share = SHARE\n"
                "username = admin\n"
                "sentinel_uuid = test-uuid\n"
                "[backup]\n"
                "folders =\n"
                "    %s\n" % self.dir)

    def _doctor_ctl(self, out, rec, **kw):
        return _base_ctl(
            self.dir, out=out, mnt=self.mnt, config_path=self.config_path,
            run_cmd=rec, **kw)

    def test_doctor_never_touches_real_network_when_stubbed(self):
        # Guard: fail hard if any code path opens a real socket.
        import socket as socket_mod
        real_create = socket_mod.create_connection
        opened = []

        def _boom(*a, **k):
            opened.append(a)
            raise AssertionError("real network access attempted in doctor")

        socket_mod.create_connection = _boom
        try:
            rec = _Recorder(default=_fail())
            probe_called = []
            out = io.StringIO()
            ctl = self._doctor_ctl(
                out, rec,
                probe_tcp=lambda host, *a, **k: probe_called.append(host) or False)
            rc = ctl.cmd_doctor()
        finally:
            socket_mod.create_connection = real_create
        # All-stubbed with failing runners: doctor must report failures, not crash.
        self.assertEqual(rc, 1)
        self.assertEqual(opened, [])
        self.assertEqual(probe_called, ["192.168.5.1"])
        text = out.getvalue()
        # Both probes stubbed dead -> the row reads as a plain NAS-offline
        # failure (never a pointer to the switchless Local Network pane).
        self.assertIn("NAS 不可达", text)

    def test_doctor_all_pass_exit_zero(self):
        # Craft runner results so every subprocess-backed check passes.
        def _results(argv):
            binary = argv[0]
            if binary.endswith("xcode-select"):
                return _ok("/Library/Developer/CommandLineTools")
            if binary.endswith("mount"):
                return _ok("//admin@192.168.5.1/SHARE on %s (smbfs, ...)" % self.mnt)
            if binary.endswith("cat"):
                return _ok("test-uuid\n")
            if binary.endswith("smbutil"):
                return _ok("SHARE  online")
            if binary.endswith("launchctl"):
                return _ok("com.mycloud.agent = { ... }")
            if binary == paths.DF:
                return _ok("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                           "//admin@192.168.5.1/SHARE 900000000 100 800000000 1%% %s\n"
                           % self.mnt)
            if binary == paths.DU:
                return _ok("1024\t%s\n" % argv[-1])
            return _ok()

        rec = _Recorder(default=lambda argv: _results(argv))
        # Fresh state with matching capability fingerprint.
        probe = {
            "backup_moves_overwritten": True,
            "delete_suppressed_with_backup": True,
            "deleting_line_format_ok": True,
        }
        fp = mycloudctl._fingerprint(probe)
        _write_state(self.dir, {
            "state": "MOUNTED_IDLE",
            "updated_at": (FIXED_NOW - timedelta(hours=1)).isoformat(),
            "capability_fingerprint": fp,
            "deletion_disabled": False,
            # Recent deep verify so the deep_verify_age check passes (PLAN M5).
            "last_deep_verify_at": (FIXED_NOW - timedelta(hours=1)).isoformat(),
            "folders": {},
        })
        out = io.StringIO()
        ctl = self._doctor_ctl(
            out, rec,
            probe_tcp=lambda host, *a, **k: True,
            cap_probe=lambda tmp, rsync: probe)
        rc = ctl.cmd_doctor()
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("all checks passed", out.getvalue())

    def test_doctor_fingerprint_change_flagged(self):
        rec = _Recorder(default=_ok())
        _write_state(self.dir, {
            "state": "MOUNTED_IDLE",
            "updated_at": FIXED_NOW.isoformat(),
            "capability_fingerprint": "OLD-DIFFERENT-FINGERPRINT",
            "folders": {},
        })
        out = io.StringIO()
        ctl = self._doctor_ctl(
            out, rec,
            probe_tcp=lambda host, *a, **k: True,
            cap_probe=lambda tmp, rsync: {
                "backup_moves_overwritten": True,
                "delete_suppressed_with_backup": True,
                "deleting_line_format_ok": True,
            })
        rc = ctl.cmd_doctor()
        self.assertEqual(rc, 1)
        self.assertIn("行为发生变化", out.getvalue())

    def test_doctor_unmounted_bare_dir_mode_checked(self):
        os.chmod(self.mnt, 0o755)  # wrong mode
        rec = _Recorder(default=_fail())
        _write_state(self.dir, {"state": "OFFLINE", "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        ctl = self._doctor_ctl(out, rec, probe_tcp=lambda h, *a, **k: False)
        ctl.cmd_doctor()
        os.chmod(self.mnt, 0o500)  # restore for cleanup
        self.assertIn("0500", out.getvalue())

    # -- F4: mount-guarded space check, stale mounts, Local-Network row --------

    def _rows(self, out):
        return {r["id"]: r for r in json.loads(out.getvalue())}

    def _mount_results(self, extra_lines=()):
        our = "//admin@192.168.5.1/SHARE on %s (smbfs, nodev, nosuid)" % self.mnt
        lines = [our] + list(extra_lines)

        def _results(argv):
            if argv[0].endswith("mount"):
                return _ok("\n".join(lines))
            return _fail()
        return _results

    def test_doctor_unmounted_skips_nas_space_never_statvfs_local(self):
        # run_cmd default _fail -> empty mount table -> not mounted.
        rec = _Recorder(default=_fail())
        _write_state(self.dir, {"state": "OFFLINE", "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        ctl = self._doctor_ctl(out, rec, probe_tcp=lambda h, *a, **k: False)
        import unittest.mock as _m
        with _m.patch("os.statvfs",
                      side_effect=AssertionError("statvfs must not run while unmounted")):
            ctl.cmd_doctor(as_json=True)
        rows = self._rows(out)
        self.assertIn("nas_space", rows)  # id preserved
        self.assertFalse(rows["nas_space"]["ok"])
        self.assertIn("共享未挂载，跳过 NAS 空间检查", rows["nas_space"]["hint"])

    def test_doctor_flags_stale_verify_mount(self):
        rec = _Recorder(default=self._mount_results(extra_lines=[
            "//admin@192.168.5.1/SHARE on "
            "/private/tmp/mycloud_verify.Ab3Xz9 (smbfs, nodev, nosuid)"]))
        _write_state(self.dir, {"state": "MOUNTED_IDLE", "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        ctl = self._doctor_ctl(out, rec, probe_tcp=lambda h, *a, **k: True)
        ctl.cmd_doctor(as_json=True)
        rows = self._rows(out)
        self.assertIn("stale_verify_mounts", rows)
        self.assertFalse(rows["stale_verify_mounts"]["ok"])
        self.assertIn("mycloud_verify.Ab3Xz9", rows["stale_verify_mounts"]["detail"])
        self.assertIn("diskutil unmount force", rows["stale_verify_mounts"]["hint"])

    def test_doctor_no_stale_verify_when_clean(self):
        rec = _Recorder(default=self._mount_results())
        _write_state(self.dir, {"state": "MOUNTED_IDLE", "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        ctl = self._doctor_ctl(out, rec, probe_tcp=lambda h, *a, **k: True)
        ctl.cmd_doctor(as_json=True)
        self.assertTrue(self._rows(out)["stale_verify_mounts"]["ok"])

    def test_doctor_local_network_row_offline_but_mounted(self):
        rec = _Recorder(default=self._mount_results())
        _write_state(self.dir, {"state": "OFFLINE", "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        # tcp445 probe fails (the denial symptom), yet the mount table shows us mounted.
        ctl = self._doctor_ctl(out, rec, probe_tcp=lambda h, *a, **k: False)
        ctl.cmd_doctor(as_json=True)
        rows = self._rows(out)
        self.assertIn("local_network", rows)
        self.assertFalse(rows["local_network"]["ok"])
        self.assertIn("本地网络", rows["local_network"]["hint"])

    def test_doctor_local_network_ok_when_not_offline(self):
        rec = _Recorder(default=self._mount_results())
        _write_state(self.dir, {"state": "MOUNTED_IDLE", "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        ctl = self._doctor_ctl(out, rec, probe_tcp=lambda h, *a, **k: True)
        ctl.cmd_doctor(as_json=True)
        self.assertTrue(self._rows(out)["local_network"]["ok"])

    def test_doctor_tcp445_passes_via_brokered_smb_when_socket_blocked(self):
        # Direct socket blocked (probe_tcp=False) but `smbutil view` succeeds:
        # the tcp445 row reports OK via the SMB-subsystem fallback, matching the
        # daemon's probe_reachable (real-hardware fix 2026-07-24).
        def _canned(argv):
            if argv[0].endswith("smbutil"):
                return _ok("Share\nSHARE  Disk\n")
            return _fail()

        rec = _Recorder(default=_canned)
        _write_state(self.dir, {"state": "OFFLINE", "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        ctl = self._doctor_ctl(out, rec, probe_tcp=lambda h, *a, **k: False)
        ctl.cmd_doctor(as_json=True)
        row = self._rows(out)["tcp445"]
        self.assertTrue(row["ok"])
        self.assertIn("SMB", json.dumps(row, ensure_ascii=False))

    def test_doctor_tcp445_fails_when_socket_and_smb_both_blocked(self):
        # Neither the socket nor the brokered SMB probe reaches the NAS -> the
        # tcp445 row FAILS as a plain "NAS offline" (deliberately NOT pointing
        # at the Local Network pane: no switch exists there for launchd python3,
        # and a blocked-but-healthy machine takes the smb-fallback OK row).
        rec = _Recorder(default=_fail())
        _write_state(self.dir, {"state": "OFFLINE", "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        ctl = self._doctor_ctl(out, rec, probe_tcp=lambda h, *a, **k: False)
        ctl.cmd_doctor(as_json=True)
        row = self._rows(out)["tcp445"]
        self.assertFalse(row["ok"])
        self.assertIn("离线", row["hint"])
        self.assertNotIn("启用 python3", row["hint"])

    def test_doctor_icloud_materialize_blocked_row(self):
        # state.prefetch.blocked=true -> the icloud_materialize row FAILS with the
        # launchd-restriction explanation and NO one-click fix (fix_id null).
        rec = _Recorder(default=_fail())
        _write_state(self.dir, {
            "state": "OFFLINE", "updated_at": FIXED_NOW.isoformat(),
            "prefetch": {"active": False, "done": 0, "total": 200, "blocked": True}})
        out = io.StringIO()
        ctl = self._doctor_ctl(out, rec, probe_tcp=lambda h, *a, **k: False)
        ctl.cmd_doctor(as_json=True)
        rows = self._rows(out)
        self.assertIn("icloud_materialize", rows)
        self.assertFalse(rows["icloud_materialize"]["ok"])
        self.assertIsNone(rows["icloud_materialize"]["fix_id"])
        self.assertIn("fileproviderd", rows["icloud_materialize"]["hint"])

    def test_doctor_icloud_materialize_ok_when_not_blocked(self):
        rec = _Recorder(default=self._mount_results())
        _write_state(self.dir, {"state": "MOUNTED_IDLE",
                                "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        ctl = self._doctor_ctl(out, rec, probe_tcp=lambda h, *a, **k: True)
        ctl.cmd_doctor(as_json=True)
        self.assertTrue(self._rows(out)["icloud_materialize"]["ok"])

    def test_doctor_local_network_ok_offline_and_unmounted_prefetch(self):
        # Offline iCloud prefetch (PLAN M4) is OFFLINE with the share NOT mounted
        # -- the prefetch-normal condition. The local_network row REQUIRES a
        # mounted entry, so it must NOT misfire here (empty mount table = not
        # mounted, tcp445 fails because the NAS is genuinely away).
        rec = _Recorder(default=_fail())     # empty mount table -> not mounted
        _write_state(self.dir, {
            "state": "OFFLINE", "updated_at": FIXED_NOW.isoformat(),
            "prefetch": {"active": True, "done": 5, "total": 100}})
        out = io.StringIO()
        ctl = self._doctor_ctl(out, rec, probe_tcp=lambda h, *a, **k: False)
        ctl.cmd_doctor(as_json=True)
        self.assertTrue(self._rows(out)["local_network"]["ok"])


class MainDispatchTests(unittest.TestCase):
    def test_no_command_prints_help(self):
        rc = mycloudctl.main([])
        self.assertEqual(rc, 2)

    def test_parser_has_all_subcommands(self):
        parser = mycloudctl.build_parser()
        # Every documented subcommand must parse.
        for cmd in ["status", "sync-now", "approve-deletions", "reload",
                    "pause", "resume", "logs", "doctor"]:
            args = parser.parse_args([cmd])
            self.assertEqual(args.command, cmd)

    def test_status_json_flag_parses(self):
        parser = mycloudctl.build_parser()
        args = parser.parse_args(["status", "--json"])
        self.assertTrue(args.as_json)

    def test_fix_subcommand_parses(self):
        parser = mycloudctl.build_parser()
        args = parser.parse_args(["fix", "restart_daemon"])
        self.assertEqual(args.command, "fix")
        self.assertEqual(args.fix_id, "restart_daemon")


def _write_config(path, folders=(), extra_backup="", server=True, comment=False):
    lines = []
    if server:
        lines += [
            "[server]",
            "host = 192.168.5.1",
            "share = SHARE",
            "username = admin",
            "sentinel_uuid = uuid-xyz",
        ]
    if comment:
        lines.append("# a user comment")
    lines.append("[backup]")
    lines.append("dest_rel = MyCloud")
    lines.append("folders =")
    for f in folders:
        lines.append("    %s" % f)
    if extra_backup:
        lines.append(extra_backup)
    with open(path, "w", encoding="utf-8") as h:
        h.write("\n".join(lines) + "\n")


class ConfigGetTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = os.path.join(self.dir, "config.ini")
        self.src = tempfile.mkdtemp()

    def test_config_get_full_payload(self):
        _write_config(self.cfg, folders=[self.src],
                      extra_backup="retention_days = 45\nexclude =\n    *.log\n")
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, config_path=self.cfg)
        rc = ctl.cmd_config_get()
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["host"], "192.168.5.1")
        self.assertEqual(payload["share"], "SHARE")
        self.assertEqual(payload["dest_rel"], "MyCloud")
        self.assertEqual(payload["retention_days"], 45)
        self.assertEqual(payload["excludes"], ["*.log"])  # user patterns only
        self.assertEqual(len(payload["sources"]), 1)
        self.assertEqual(payload["sources"][0]["path"], str(mycloudctl._resolve_path(self.src)))
        self.assertFalse(payload["sources"][0]["is_file"])
        self.assertIn("warnings", payload)

    def test_config_get_single_file_source(self):
        f = os.path.join(self.src, "note.txt")
        with open(f, "w") as h:
            h.write("hi")
        _write_config(self.cfg, folders=[f])
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, config_path=self.cfg)
        ctl.cmd_config_get()
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["sources"][0]["is_file"])

    def test_config_get_bad_config_errors(self):
        with open(self.cfg, "w") as h:
            h.write("[backup]\nmax_delete_pct = 500\n")  # out of range
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, config_path=self.cfg)
        rc = ctl.cmd_config_get()
        self.assertEqual(rc, 1)
        self.assertIn("error", json.loads(out.getvalue()))


class ConfigMutationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = os.path.join(self.dir, "config.ini")
        self.a = tempfile.mkdtemp()
        self.b = tempfile.mkdtemp()

    def _ctl(self, out=None, err=None, **kw):
        return _base_ctl(self.dir, out=out or io.StringIO(),
                         err=err or io.StringIO(), config_path=self.cfg, **kw)

    def test_add_source_roundtrips_through_load_config(self):
        _write_config(self.cfg, folders=[self.a])
        out = io.StringIO()
        ctl = self._ctl(out=out)
        rc = ctl.cmd_config_add_source(self.b)
        self.assertEqual(rc, 0)
        # config.ini now loads with both sources.
        cfg = config_mod.load_config(self.cfg)
        paths_set = set(str(f.src) for f in cfg.folders)
        self.assertIn(str(mycloudctl._resolve_path(self.a)), paths_set)
        self.assertIn(str(mycloudctl._resolve_path(self.b)), paths_set)
        payload = json.loads(out.getvalue())
        self.assertEqual(len(payload["sources"]), 2)

    def test_corrupt_config_not_destroyed_on_mutation(self):
        # Regression: a config.ini configparser cannot parse must NOT be silently
        # rewritten down to just the mutated key (which wiped [server] + sources).
        with open(self.cfg, "w", encoding="utf-8") as h:
            h.write(
                "[server]\n"
                "host = 192.168.5.1\n"
                "share = SHARE\n"
                "sentinel_uuid = keep-me\n"
                "no equals sign here\n"  # unparseable line
                "[backup]\n"
                "folders =\n"
                "    %s\n" % self.a)
        before = open(self.cfg, encoding="utf-8").read()
        out = io.StringIO()
        ctl = self._ctl(out=out)
        rc = ctl.cmd_config_add_source(self.b)
        self.assertEqual(rc, 1)
        self.assertIn("error", json.loads(out.getvalue()))
        # config.ini is left EXACTLY as it was (sentinel_uuid still present).
        self.assertEqual(open(self.cfg, encoding="utf-8").read(), before)
        self.assertIn("keep-me", open(self.cfg, encoding="utf-8").read())

    def test_add_duplicate_source_errors(self):
        _write_config(self.cfg, folders=[self.a])
        out = io.StringIO()
        ctl = self._ctl(out=out)
        rc = ctl.cmd_config_add_source(self.a)
        self.assertEqual(rc, 1)
        self.assertIn("error", json.loads(out.getvalue()))

    def test_add_nested_source_rejected(self):
        _write_config(self.cfg, folders=[self.a])
        nested = os.path.join(self.a, "sub")
        os.makedirs(nested)
        out = io.StringIO()
        ctl = self._ctl(out=out)
        rc = ctl.cmd_config_add_source(nested)
        self.assertEqual(rc, 1)
        err = json.loads(out.getvalue())
        self.assertIn("error", err)
        self.assertIn("nested", err["error"])
        # Original config.ini is unchanged (still only self.a).
        cfg = config_mod.load_config(self.cfg)
        self.assertEqual([str(f.src) for f in cfg.folders],
                         [str(mycloudctl._resolve_path(self.a))])

    def test_add_source_signals_daemon_sigusr2(self):
        _write_config(self.cfg, folders=[self.a])
        _hold_daemon_lock(self, self.dir, 4242)
        killed = []
        out = io.StringIO()
        ctl = self._ctl(out=out, kill=lambda p, s: killed.append((p, s)))
        rc = ctl.cmd_config_add_source(self.b)
        self.assertEqual(rc, 0)
        # Liveness is verified via the flock (not os.kill(pid, 0)); only the
        # real SIGUSR2 is sent through self.kill.
        self.assertEqual(killed, [(4242, signal.SIGUSR2)])
        self.assertTrue(json.loads(out.getvalue()).get("reload_signalled"))

    def test_add_source_no_signal_when_daemon_dead(self):
        _write_config(self.cfg, folders=[self.a])
        with open(os.path.join(self.dir, "daemon.lock"), "w") as h:
            h.write("999999\n")

        def _dead(pid, sig):
            raise ProcessLookupError()

        out = io.StringIO()
        ctl = self._ctl(out=out, kill=_dead)
        rc = ctl.cmd_config_add_source(self.b)
        self.assertEqual(rc, 0)
        self.assertNotIn("reload_signalled", json.loads(out.getvalue()))

    def test_no_signal_to_stale_unlocked_pid(self):
        # Regression: a stale (unlocked) PID could be a recycled, unrelated
        # process; the ctl must NOT signal it (SIGUSR2 would kill it).
        _write_config(self.cfg, folders=[self.a])
        with open(os.path.join(self.dir, "daemon.lock"), "w") as h:
            h.write("4242\n")  # PID present but NO flock held
        killed = []
        out = io.StringIO()
        ctl = self._ctl(out=out, kill=lambda p, s: killed.append((p, s)))
        rc = ctl.cmd_config_add_source(self.b)
        self.assertEqual(rc, 0)
        self.assertEqual(killed, [])  # nothing signalled
        self.assertNotIn("reload_signalled", json.loads(out.getvalue()))

    def test_remove_source_notice_and_gone(self):
        _write_config(self.cfg, folders=[self.a, self.b])
        out = io.StringIO()
        ctl = self._ctl(out=out)
        rc = ctl.cmd_config_remove_source(self.a)
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertIn("orphaned", payload["notice"])
        cfg = config_mod.load_config(self.cfg)
        self.assertEqual([str(f.src) for f in cfg.folders],
                         [str(mycloudctl._resolve_path(self.b))])

    def test_remove_unknown_source_errors(self):
        _write_config(self.cfg, folders=[self.a])
        out = io.StringIO()
        ctl = self._ctl(out=out)
        rc = ctl.cmd_config_remove_source(self.b)
        self.assertEqual(rc, 1)
        self.assertIn("error", json.loads(out.getvalue()))

    def test_set_dest_valid(self):
        _write_config(self.cfg, folders=[self.a])
        out = io.StringIO()
        ctl = self._ctl(out=out)
        rc = ctl.cmd_config_set_dest("Backup/MyCloud")
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["dest_rel"], "Backup/MyCloud")
        self.assertIn("orphaned", payload["notice"])
        self.assertEqual(config_mod.load_config(self.cfg).dest_rel, "Backup/MyCloud")

    def test_set_dest_invalid_rejected(self):
        _write_config(self.cfg, folders=[self.a])
        out = io.StringIO()
        ctl = self._ctl(out=out)
        rc = ctl.cmd_config_set_dest("../escape")
        self.assertEqual(rc, 1)
        self.assertIn("error", json.loads(out.getvalue()))
        # unchanged
        self.assertEqual(config_mod.load_config(self.cfg).dest_rel, "MyCloud")

    def test_config_set_numeric(self):
        _write_config(self.cfg, folders=[self.a])
        out = io.StringIO()
        ctl = self._ctl(out=out)
        rc = ctl.cmd_config_set("retention_days", "90")
        self.assertEqual(rc, 0)
        self.assertEqual(config_mod.load_config(self.cfg).retention_days, 90)

    def test_config_set_watch_mode(self):
        _write_config(self.cfg, folders=[self.a])
        out = io.StringIO()
        ctl = self._ctl(out=out)
        rc = ctl.cmd_config_set("watch_mode", "scan")
        self.assertEqual(rc, 0)
        self.assertEqual(config_mod.load_config(self.cfg).watch_mode, "scan")

    def test_config_set_unknown_key_rejected(self):
        _write_config(self.cfg, folders=[self.a])
        out = io.StringIO()
        ctl = self._ctl(out=out)
        rc = ctl.cmd_config_set("host", "evil")
        self.assertEqual(rc, 1)
        self.assertIn("error", json.loads(out.getvalue()))

    def test_config_set_bad_value_rejected(self):
        _write_config(self.cfg, folders=[self.a])
        out = io.StringIO()
        ctl = self._ctl(out=out)
        rc = ctl.cmd_config_set("retention_days", "notanint")
        self.assertEqual(rc, 1)
        self.assertIn("error", json.loads(out.getvalue()))

    def test_comment_dropped_emits_stderr_notice(self):
        _write_config(self.cfg, folders=[self.a], comment=True)
        err = io.StringIO()
        ctl = self._ctl(err=err)
        ctl.cmd_config_add_source(self.b)
        self.assertIn("comments", err.getvalue())
        # stdout stays pure JSON (no notice leaks there).

    def test_password_never_written(self):
        # A stray password key in [server] must not survive a rewrite.
        with open(self.cfg, "w") as h:
            h.write("[server]\nhost = h\nshare = s\nusername = u\n"
                    "sentinel_uuid = x\npassword = SECRET\n[backup]\nfolders =\n")
        ctl = self._ctl()
        ctl.cmd_config_add_source(self.a)
        with open(self.cfg, "r") as h:
            text = h.read()
        self.assertNotIn("SECRET", text)
        self.assertNotIn("password", text)


class DoctorJsonTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.mnt = os.path.join(self.dir, "mnt")
        os.makedirs(self.mnt, exist_ok=True)
        os.chmod(self.mnt, 0o500)
        self.cfg = os.path.join(self.dir, "config.ini")
        with open(self.cfg, "w", encoding="utf-8") as h:
            h.write("[server]\nhost = 192.168.5.1\nshare = SHARE\n"
                    "username = admin\nsentinel_uuid = test-uuid\n[backup]\nfolders =\n")

    def test_doctor_json_is_pure_array(self):
        rec = _Recorder(default=_fail())
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, mnt=self.mnt, config_path=self.cfg,
                        run_cmd=rec, probe_tcp=lambda h, *a, **k: False)
        rc = ctl.cmd_doctor(as_json=True)
        self.assertEqual(rc, 1)  # stubbed failures
        results = json.loads(out.getvalue())
        self.assertIsInstance(results, list)
        ids = set()
        for r in results:
            for key in ("id", "name", "ok", "detail", "hint"):
                self.assertIn(key, r)
            self.assertIsInstance(r["ok"], bool)
            ids.add(r["id"])
        # Spot-check that the full check set is represented.
        for expected in ("python_clt", "config_loads", "tcp445", "mount_present",
                         "sentinel", "smb_session", "bare_dir_mode",
                         "rsync_capability", "nas_space", "launchagent",
                         "state_fresh"):
            self.assertIn(expected, ids)

    def test_doctor_json_no_prose(self):
        # The entire stdout must parse as JSON (no human lines mixed in).
        rec = _Recorder(default=_fail())
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, mnt=self.mnt, config_path=self.cfg,
                        run_cmd=rec, probe_tcp=lambda h, *a, **k: False)
        ctl.cmd_doctor(as_json=True)
        json.loads(out.getvalue())  # raises if any prose leaked

    def test_doctor_human_still_works(self):
        rec = _Recorder(default=_fail())
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, mnt=self.mnt, config_path=self.cfg,
                        run_cmd=rec, probe_tcp=lambda h, *a, **k: False)
        rc = ctl.cmd_doctor(as_json=False)
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL]", out.getvalue())
        self.assertIn("NAS 不可达", out.getvalue())


class _FakeMount(object):
    def __init__(self, mounted=False, reachable=True, mount_ok=True, sentinel=True):
        self._mounted = mounted
        self._reachable = reachable
        self._mount_ok = mount_ok
        self._sentinel = sentinel
        self.mount_called = False

    def is_mounted(self):
        return self._mounted

    def probe_tcp445(self, timeout=3.0):
        return self._reachable

    def probe_reachable(self, timeout=3.0):
        # Mirrors the real MountManager: cmd_mount / the mount fix now gate on
        # reachability (socket OR brokered SMB), so the fake exposes it too.
        return self._reachable

    def mount(self):
        self.mount_called = True
        if self._mount_ok:
            self._mounted = True
        return self._mount_ok

    def sentinel_ok(self):
        return self._sentinel


class MountCommandTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.mnt = os.path.join(self.dir, "mnt")
        os.makedirs(self.mnt, exist_ok=True)
        self.cfg = os.path.join(self.dir, "config.ini")
        _write_config(self.cfg, folders=[])

    def _ctl(self, fake, out=None):
        return _base_ctl(self.dir, out=out or io.StringIO(), mnt=self.mnt,
                         config_path=self.cfg, mount_factory=lambda cfg: fake)

    def test_already_mounted(self):
        out = io.StringIO()
        ctl = self._ctl(_FakeMount(mounted=True), out=out)
        rc = ctl.cmd_mount()
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["mounted"])
        self.assertEqual(payload["mountpoint"], self.mnt)

    def test_unreachable_nas_friendly_error(self):
        out = io.StringIO()
        fake = _FakeMount(mounted=False, reachable=False)
        ctl = self._ctl(fake, out=out)
        rc = ctl.cmd_mount()
        self.assertEqual(rc, 1)
        self.assertIn("error", json.loads(out.getvalue()))
        self.assertFalse(fake.mount_called)  # never tried to mount

    def test_mount_success(self):
        out = io.StringIO()
        fake = _FakeMount(mounted=False, reachable=True, mount_ok=True, sentinel=True)
        ctl = self._ctl(fake, out=out)
        rc = ctl.cmd_mount()
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())["mounted"])
        self.assertTrue(fake.mount_called)

    def test_mount_sentinel_mismatch_errors(self):
        out = io.StringIO()
        fake = _FakeMount(mounted=False, reachable=True, mount_ok=True, sentinel=False)
        ctl = self._ctl(fake, out=out)
        rc = ctl.cmd_mount()
        self.assertEqual(rc, 1)
        self.assertIn("error", json.loads(out.getvalue()))

    def test_mount_no_server_config(self):
        with open(self.cfg, "w") as h:
            h.write("[backup]\nfolders =\n")  # no server
        out = io.StringIO()
        ctl = self._ctl(_FakeMount(), out=out)
        rc = ctl.cmd_mount()
        self.assertEqual(rc, 1)
        self.assertIn("error", json.loads(out.getvalue()))


class ApproveYesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_yes_flag_skips_prompt(self):
        _write_state(self.dir, {
            "state": "DELETIONS_HELD",
            "folders": {"Documents": {"held_deletions": ["old.txt"]}},
        })
        prompts = []
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out,
                        input_fn=lambda prompt="": prompts.append(prompt) or "no")
        rc = ctl.cmd_approve_deletions(assume_yes=True)
        self.assertEqual(rc, 0)
        self.assertEqual(prompts, [])  # never prompted
        self.assertTrue(os.path.exists(os.path.join(self.dir, "approval.token")))
        # --yes is the GUI path: it emits the JSON envelope, not human text.
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["approved"], 1)
        self.assertEqual(payload["folders"], {"Documents": ["old.txt"]})
        self.assertIn("signalled", payload)

    def test_yes_flag_error_json_when_nothing_held(self):
        _write_state(self.dir, {"state": "MOUNTED_IDLE", "folders": {}})
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out)
        rc = ctl.cmd_approve_deletions(assume_yes=True)
        self.assertEqual(rc, 1)
        self.assertIn("error", json.loads(out.getvalue()))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "approval.token")))


class SubprocessContractTests(unittest.TestCase):
    """End-to-end: the real script emits pure JSON under MYCLOUD_APP override."""

    def setUp(self):
        self.app = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.app, "runtime"), exist_ok=True)
        self.src = tempfile.mkdtemp()
        _write_config(os.path.join(self.app, "config.ini"), folders=[self.src])

    def _run(self, *args):
        env = dict(os.environ)
        env["MYCLOUD_APP"] = self.app
        return subprocess.run(
            [sys.executable, _CTL_PY] + list(args),
            capture_output=True, text=True, timeout=60, env=env)

    def test_config_get_pure_json_exit_zero(self):
        res = self._run("config-get")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)  # must be pure JSON
        self.assertEqual(payload["share"], "SHARE")

    def test_add_source_mutates_config_ini(self):
        other = tempfile.mkdtemp()
        res = self._run("config-add-source", other)
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(len(payload["sources"]), 2)
        cfg = config_mod.load_config(os.path.join(self.app, "config.ini"))
        self.assertIn(str(mycloudctl._resolve_path(other)),
                      set(str(f.src) for f in cfg.folders))

    def test_nesting_rejection_is_json_error(self):
        nested = os.path.join(self.src, "child")
        os.makedirs(nested)
        res = self._run("config-add-source", nested)
        self.assertEqual(res.returncode, 1)
        self.assertIn("error", json.loads(res.stdout))


class _PruneMount(object):
    """Minimal mount for prune_trash_now: only is_mounted + guard_ok are used."""

    def __init__(self, mounted=True, guard=True):
        self._mounted = mounted
        self._guard = guard

    def is_mounted(self, timeout=10):
        return self._mounted

    def guard_ok(self, timeout=10):
        return self._guard


class FixTests(unittest.TestCase):
    """A4: the `fix <id>` subcommand -- every registered id, both branches of
    rerun_capability_probe, and the {"ok","detail","followup"} envelope."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.mnt = os.path.join(self.dir, "mnt")
        os.makedirs(self.mnt, exist_ok=True)
        self.cfg = os.path.join(self.dir, "config.ini")
        _write_config(self.cfg, folders=[])

    def _payload(self, ctl_fn):
        out = io.StringIO()
        return out, ctl_fn(out)

    def _assert_envelope(self, payload):
        for key in ("ok", "detail", "followup"):
            self.assertIn(key, payload)
        self.assertIsInstance(payload["ok"], bool)
        self.assertIn(payload["followup"], ("reopen_doctor", None))
        self.assertIsInstance(payload["detail"], str)

    # -- unknown id --------------------------------------------------------

    def test_fix_unknown_id_errors(self):
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out)
        rc = ctl.cmd_fix("no_such_fix")
        self.assertEqual(rc, 1)
        payload = json.loads(out.getvalue())
        self._assert_envelope(payload)
        self.assertFalse(payload["ok"])
        self.assertIn("未知", payload["detail"])

    # -- restart_daemon ----------------------------------------------------

    def test_fix_restart_daemon_kickstarts(self):
        rec = _Recorder(default=_ok())
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_fix("restart_daemon")
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self._assert_envelope(payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["followup"], "reopen_doctor")
        self.assertIn("kickstart", rec.calls[0])
        self.assertIn("-k", rec.calls[0])
        self.assertIn("gui/501/com.mycloud.agent", rec.calls[0])

    def test_fix_restart_daemon_failure(self):
        rec = _Recorder(default=_fail(rc=3))
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_fix("restart_daemon")
        self.assertEqual(rc, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["followup"])

    # -- cleanup_stale_mounts ----------------------------------------------

    def test_fix_cleanup_umounts_stale(self):
        verify = "/private/tmp/mycloud_verify.Ab3Xz9"
        table = "//admin@192.168.5.1/SHARE on %s (smbfs, nodev, nosuid)" % verify
        rec = _Recorder(results={paths.MOUNT: _ok(table), paths.UMOUNT: _ok()},
                        default=_fail())
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_fix("cleanup_stale_mounts")
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self._assert_envelope(payload)
        self.assertTrue(payload["ok"])
        self.assertTrue(any(c[0] == paths.UMOUNT and verify in c for c in rec.calls))
        # umount succeeded, so no forced diskutil escalation.
        self.assertFalse(any(c[0] == paths.DISKUTIL for c in rec.calls))

    def test_fix_cleanup_nothing_to_do(self):
        table = "//admin@192.168.5.1/SHARE on /Users/x/mnt (smbfs)"
        rec = _Recorder(results={paths.MOUNT: _ok(table)}, default=_fail())
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_fix("cleanup_stale_mounts")
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertFalse(any(c[0] == paths.UMOUNT for c in rec.calls))

    def test_fix_cleanup_escalates_then_reports_stuck(self):
        verify = "/private/tmp/mycloud_verify.Stuck"
        table = "//admin@192.168.5.1/SHARE on %s (smbfs)" % verify
        rec = _Recorder(
            results={paths.MOUNT: _ok(table), paths.UMOUNT: _fail(rc=1),
                     paths.DISKUTIL: _fail(rc=1)},
            default=_fail())
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_fix("cleanup_stale_mounts")
        self.assertEqual(rc, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        # It escalated umount -> diskutil unmount force before giving up.
        self.assertTrue(any(c[0] == paths.DISKUTIL and "force" in c for c in rec.calls))

    # -- mount_share -------------------------------------------------------

    def test_fix_mount_share_already_mounted(self):
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, mnt=self.mnt, config_path=self.cfg,
                        mount_factory=lambda cfg: _FakeMount(mounted=True))
        rc = ctl.cmd_fix("mount_share")
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self._assert_envelope(payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["followup"], "reopen_doctor")

    def test_fix_mount_share_success(self):
        fake = _FakeMount(mounted=False, reachable=True, mount_ok=True)
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, mnt=self.mnt, config_path=self.cfg,
                        mount_factory=lambda cfg: fake)
        rc = ctl.cmd_fix("mount_share")
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertTrue(fake.mount_called)

    def test_fix_mount_share_unreachable(self):
        fake = _FakeMount(mounted=False, reachable=False)
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, mnt=self.mnt, config_path=self.cfg,
                        mount_factory=lambda cfg: fake)
        rc = ctl.cmd_fix("mount_share")
        self.assertEqual(rc, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["followup"])
        self.assertFalse(fake.mount_called)

    # -- open_fda_settings -------------------------------------------------

    def test_fix_open_fda_settings(self):
        rec = _Recorder(default=_ok())
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_fix("open_fda_settings")
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["followup"])
        self.assertTrue(any(c[0] == paths.OPEN and "Privacy_AllFiles" in c[1]
                            for c in rec.calls))

    def test_fix_open_fda_settings_failure(self):
        rec = _Recorder(default=_fail())
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, run_cmd=rec)
        rc = ctl.cmd_fix("open_fda_settings")
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out.getvalue())["ok"])

    # -- rerun_capability_probe (both branches) ----------------------------

    def _probe(self):
        return {
            "backup_moves_overwritten": True,
            "delete_suppressed_with_backup": True,
            "deleting_line_format_ok": True,
        }

    def test_fix_rerun_capability_match_clears_disabled(self):
        probe = self._probe()
        fp = mycloudctl._fingerprint(probe)
        _write_state(self.dir, {"capability_fingerprint": fp,
                                "deletion_disabled": True, "folders": {}})
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, cap_probe=lambda tmp, rsync: probe)
        rc = ctl.cmd_fix("rerun_capability_probe")
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["followup"], "reopen_doctor")
        # deletion propagation is re-enabled in state.
        self.assertFalse(ctl.store.get()["deletion_disabled"])

    def test_fix_rerun_capability_mismatch_keeps_disabled(self):
        probe = self._probe()
        _write_state(self.dir, {"capability_fingerprint": "OLD-DIFFERENT",
                                "deletion_disabled": True, "folders": {}})
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, cap_probe=lambda tmp, rsync: probe)
        rc = ctl.cmd_fix("rerun_capability_probe")
        self.assertEqual(rc, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["followup"])
        # The mismatch must NOT clear the fail-closed flag.
        self.assertTrue(ctl.store.get()["deletion_disabled"])

    # -- prune_trash_now ---------------------------------------------------

    def _seed_trash(self):
        trash_dir = os.path.join(self.dir, "trashfix")
        aged = os.path.join(trash_dir, "2000-01-01T000000")
        os.makedirs(aged)
        with open(os.path.join(aged, "old.bin"), "wb") as h:
            h.write(b"x" * 4096)
        return trash_dir, aged

    def test_fix_prune_runs_when_mounted_and_guard_ok(self):
        trash_dir, aged = self._seed_trash()
        out = io.StringIO()
        ctl = _base_ctl(
            self.dir, out=out, mnt=self.mnt, config_path=self.cfg,
            mount_factory=lambda cfg: _PruneMount(mounted=True, guard=True),
            trash_factory=lambda root, ret, cap: TrashManager(
                trash_dir, ret, cap, None))
        rc = ctl.cmd_fix("prune_trash_now")
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["followup"], "reopen_doctor")
        self.assertIn("MiB", payload["detail"])
        # The aged batch was actually pruned (rmtree, confined to trash.py).
        self.assertFalse(os.path.exists(aged))

    def test_fix_prune_refuses_when_guard_fails(self):
        trash_dir, aged = self._seed_trash()

        def _boom_factory(*a, **k):
            raise AssertionError("trash_factory must not run when guard fails")

        out = io.StringIO()
        ctl = _base_ctl(
            self.dir, out=out, mnt=self.mnt, config_path=self.cfg,
            mount_factory=lambda cfg: _PruneMount(mounted=True, guard=False),
            trash_factory=_boom_factory)
        rc = ctl.cmd_fix("prune_trash_now")
        self.assertEqual(rc, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["followup"])
        self.assertTrue(os.path.exists(aged))  # untouched

    def test_fix_prune_refuses_when_unmounted(self):
        out = io.StringIO()
        ctl = _base_ctl(
            self.dir, out=out, mnt=self.mnt, config_path=self.cfg,
            mount_factory=lambda cfg: _PruneMount(mounted=False))
        rc = ctl.cmd_fix("prune_trash_now")
        self.assertEqual(rc, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("未挂载", payload["detail"])

    # -- sync_now ----------------------------------------------------------

    def test_fix_sync_now_signals_daemon(self):
        _hold_daemon_lock(self, self.dir, 88)
        killed = []
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out,
                        kill=lambda p, s: killed.append((p, s)))
        rc = ctl.cmd_fix("sync_now")
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(killed, [(88, signal.SIGUSR1)])

    def test_fix_sync_now_no_daemon(self):
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out)
        rc = ctl.cmd_fix("sync_now")
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out.getvalue())["ok"])


class DoctorFixMappingTests(unittest.TestCase):
    """A4: doctor --json rows carry the pinned fix_id (+ label) on failing rows
    that have a remedy, and NONE on rows deliberately left un-fixable (sentinel)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.mnt = os.path.join(self.dir, "mnt")
        os.makedirs(self.mnt, exist_ok=True)
        os.chmod(self.mnt, 0o500)
        self.cfg = os.path.join(self.dir, "config.ini")
        with open(self.cfg, "w", encoding="utf-8") as h:
            h.write(
                "[server]\n"
                "host = 192.168.5.1\n"
                "share = SHARE\n"
                "username = admin\n"
                "sentinel_uuid = test-uuid\n"
                "[backup]\n"
                "folders =\n"
                "    %s\n" % self.dir)

    def _rows(self, out):
        return {r["id"]: r for r in json.loads(out.getvalue())}

    def _ctl(self, out, rec, **kw):
        return _base_ctl(self.dir, out=out, mnt=self.mnt, config_path=self.cfg,
                         run_cmd=rec, **kw)

    def test_mount_session_sentinel_state_capability_mapping(self):
        rec = _Recorder(default=_fail())  # nothing mounted / no session / no cat
        _write_state(self.dir, {
            "state": "MOUNTED_IDLE",
            "updated_at": (FIXED_NOW - timedelta(hours=100)).isoformat(),
            "capability_fingerprint": "OLD-DIFFERENT",
            "folders": {}})
        out = io.StringIO()
        ctl = self._ctl(out, rec, probe_tcp=lambda h, *a, **k: False,
                        cap_probe=lambda tmp, rsync: {
                            "backup_moves_overwritten": True,
                            "delete_suppressed_with_backup": True,
                            "deleting_line_format_ok": True})
        ctl.cmd_doctor(as_json=True)
        rows = self._rows(out)
        self.assertEqual(rows["mount_present"]["fix_id"], "mount_share")
        self.assertEqual(rows["mount_present"]["fix_label"], "挂载共享")
        self.assertEqual(rows["smb_session"]["fix_id"], "mount_share")
        # sentinel mismatch while (nominally) present: deliberately NO fix.
        self.assertIsNone(rows["sentinel"]["fix_id"])
        self.assertIsNone(rows["sentinel"]["fix_label"])
        self.assertEqual(rows["state_fresh"]["fix_id"], "restart_daemon")
        self.assertEqual(rows["rsync_capability"]["fix_id"], "rerun_capability_probe")
        # An OK row never carries a fix.
        self.assertIsNone(rows["python_clt"]["fix_id"])

    def test_stale_verify_maps_to_cleanup(self):
        our = "//admin@192.168.5.1/SHARE on %s (smbfs)" % self.mnt
        verify = "//admin@192.168.5.1/SHARE on /private/tmp/mycloud_verify.Q (smbfs)"
        rec = _Recorder(results={paths.MOUNT: _ok(our + "\n" + verify)},
                        default=_fail())
        _write_state(self.dir, {"state": "MOUNTED_IDLE",
                                "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        ctl = self._ctl(out, rec, probe_tcp=lambda h, *a, **k: True)
        ctl.cmd_doctor(as_json=True)
        rows = self._rows(out)
        self.assertFalse(rows["stale_verify_mounts"]["ok"])
        self.assertEqual(rows["stale_verify_mounts"]["fix_id"],
                         "cleanup_stale_mounts")

    def test_local_network_maps_to_open_settings(self):
        our = "//admin@192.168.5.1/SHARE on %s (smbfs)" % self.mnt
        rec = _Recorder(results={paths.MOUNT: _ok(our)}, default=_fail())
        _write_state(self.dir, {"state": "OFFLINE",
                                "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        ctl = self._ctl(out, rec, probe_tcp=lambda h, *a, **k: False)
        ctl.cmd_doctor(as_json=True)
        rows = self._rows(out)
        self.assertFalse(rows["local_network"]["ok"])
        # No one-click fix on purpose: macOS never lists a bare launchd python3
        # in the Local Network pane, so "open the pane" sent users hunting for
        # a switch that does not exist (2026-07-31 user report).
        self.assertIsNone(rows["local_network"]["fix_id"])
        self.assertIn("无法", rows["local_network"]["hint"])

    def test_fda_permission_maps_to_open_fda(self):
        rec = _Recorder(default=_fail())
        _write_state(self.dir, {"state": "MOUNTED_IDLE",
                                "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        ctl = self._ctl(out, rec, probe_tcp=lambda h, *a, **k: False)
        import unittest.mock as _m
        real_scandir = os.scandir
        target_real = os.path.realpath(self.dir)

        def _fake_scandir(path, *a, **k):
            if os.path.realpath(str(path)) == target_real:
                raise PermissionError("EPERM")
            return real_scandir(path, *a, **k)

        with _m.patch("os.scandir", side_effect=_fake_scandir):
            ctl.cmd_doctor(as_json=True)
        rows = self._rows(out)
        fda = [r for k, r in rows.items() if k.startswith("fda_scandir:")]
        self.assertTrue(fda)
        self.assertFalse(fda[0]["ok"])
        self.assertEqual(fda[0]["fix_id"], "open_fda_settings")

    def test_nas_space_low_maps_to_prune(self):
        our = "//admin@192.168.5.1/SHARE on %s (smbfs)" % self.mnt
        df = ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
              "//admin@192.168.5.1/SHARE 1000 999 1 100%% %s\n" % self.mnt)
        rec = _Recorder(results={paths.MOUNT: _ok(our), paths.DF: _ok(df),
                                 paths.DU: _ok("0\t/x\n")}, default=_fail())
        _write_state(self.dir, {"state": "MOUNTED_IDLE",
                                "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        ctl = self._ctl(out, rec, probe_tcp=lambda h, *a, **k: True)
        ctl.cmd_doctor(as_json=True)      # 1 KiB free -> below the 1 GiB stop threshold
        rows = self._rows(out)
        self.assertFalse(rows["nas_space"]["ok"])
        self.assertEqual(rows["nas_space"]["fix_id"], "prune_trash_now")

    def test_unmounted_nas_space_has_no_fix(self):
        # When unmounted the space check is skipped, not a prune candidate.
        rec = _Recorder(default=_fail())
        _write_state(self.dir, {"state": "OFFLINE",
                                "updated_at": FIXED_NOW.isoformat()})
        out = io.StringIO()
        ctl = self._ctl(out, rec, probe_tcp=lambda h, *a, **k: False)
        ctl.cmd_doctor(as_json=True)
        rows = self._rows(out)
        self.assertFalse(rows["nas_space"]["ok"])
        self.assertIsNone(rows["nas_space"]["fix_id"])

    def test_every_row_has_fix_keys(self):
        rec = _Recorder(default=_fail())
        out = io.StringIO()
        ctl = self._ctl(out, rec, probe_tcp=lambda h, *a, **k: False)
        ctl.cmd_doctor(as_json=True)
        for row in json.loads(out.getvalue()):
            self.assertIn("fix_id", row)
            self.assertIn("fix_label", row)
            # A fix is offered only on failing rows.
            if row["fix_id"] is not None:
                self.assertFalse(row["ok"])
                self.assertIn(row["fix_id"], mycloudctl._FIX_LABELS)
                self.assertEqual(row["fix_label"],
                                 mycloudctl._FIX_LABELS[row["fix_id"]])


class VerifyTests(unittest.TestCase):
    """`mycloudctl verify` request + poll, and the run_deep_verify fix (PLAN M3)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "mnt"), exist_ok=True)

    def test_verify_requests_and_polls_result(self):
        _hold_daemon_lock(self, self.dir, 909)
        # Simulate the daemon: when signalled, write a fresh verify summary.
        killed = []

        def _kill(pid, sig):
            killed.append((pid, sig))
            _write_state(self.dir, {"verify": {
                "running": False,
                "started_at": FIXED_NOW.isoformat(),
                "finished_at": FIXED_NOW.isoformat(),
                "missing_uploaded": 2, "extra_found": 1,
                "drift_reuploaded": 0, "errors": []}})

        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, kill=_kill,
                        now_fn=lambda: FIXED_NOW - timedelta(seconds=1))
        ctl.verify_poll_interval = 0
        ctl.sleep_fn = lambda s: None
        rc = ctl.cmd_verify(as_json=True)
        self.assertEqual(rc, 0)
        # The verify.request marker was written and SIGUSR1 sent.
        self.assertTrue(os.path.exists(os.path.join(self.dir, "verify.request")))
        self.assertEqual(killed, [(909, signal.SIGUSR1)])
        result = json.loads(out.getvalue())
        self.assertEqual(result["missing_uploaded"], 2)
        self.assertEqual(result["extra_found"], 1)

    def test_verify_daemon_not_running(self):
        # No live daemon: the request is written but polling is skipped.
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out)
        ctl.verify_poll_interval = 0
        ctl.sleep_fn = lambda s: None
        rc = ctl.cmd_verify(as_json=True)
        self.assertEqual(rc, 1)
        result = json.loads(out.getvalue())
        self.assertTrue(result["requested"])
        self.assertFalse(result["finished"])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "verify.request")))

    def test_verify_times_out_while_running(self):
        _hold_daemon_lock(self, self.dir, 910)
        # Daemon "starts" the verify but never finishes within the poll window.
        def _kill(pid, sig):
            _write_state(self.dir, {"verify": {
                "running": True, "started_at": FIXED_NOW.isoformat(),
                "finished_at": None}})

        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, kill=_kill, now_fn=lambda: FIXED_NOW)
        ctl.verify_timeout = 0.0            # give up immediately
        ctl.verify_poll_interval = 0
        ctl.sleep_fn = lambda s: None
        rc = ctl.cmd_verify(as_json=True)
        self.assertEqual(rc, 1)
        result = json.loads(out.getvalue())
        self.assertTrue(result["running"])
        self.assertFalse(result["finished"])

    def test_fix_run_deep_verify_returns_immediately(self):
        _hold_daemon_lock(self, self.dir, 911)
        killed = []
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out,
                        kill=lambda p, s: killed.append((p, s)))
        rc = ctl.cmd_fix("run_deep_verify")
        self.assertEqual(rc, 0)
        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertIsNone(result["followup"])          # returns at once (M3)
        self.assertEqual(killed, [(911, signal.SIGUSR1)])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "verify.request")))

    def test_run_deep_verify_registered_in_fix_labels(self):
        self.assertEqual(mycloudctl._FIX_LABELS["run_deep_verify"], "立即校验备份")


class DoctorV2RowTests(unittest.TestCase):
    """The two v2 doctor rows: icloud_dataless and deep_verify_age (PLAN M5)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.mnt = os.path.join(self.dir, "mnt")
        os.makedirs(self.mnt, exist_ok=True)
        self.config_path = os.path.join(self.dir, "config.ini")
        with open(self.config_path, "w", encoding="utf-8") as h:
            h.write("[server]\nhost = 127.0.0.1\nshare = SHARE\n"
                    "[backup]\nfolders =\n    %s\n"
                    "icloud_policy = skip\n" % self.dir)

    def _rows(self, state):
        _write_state(self.dir, state)
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, mnt=self.mnt,
                        config_path=self.config_path,
                        probe_tcp=lambda h, *a, **k: False)
        ctl.cmd_doctor(as_json=True)
        return {r["id"]: r for r in json.loads(out.getvalue())}

    def _configured_name(self):
        """The folder name config.ini's single source resolves to (its basename)."""
        return os.path.basename(os.path.realpath(self.dir))

    def test_icloud_dataless_fails_when_skipped_under_skip_policy(self):
        rows = self._rows({
            "state": "MOUNTED_IDLE", "updated_at": FIXED_NOW.isoformat(),
            "folders": {self._configured_name(): {
                "icloud_skipped": {"count": 7, "bytes": 1, "samples": []}}},
        })
        self.assertFalse(rows["icloud_dataless"]["ok"])
        self.assertIsNone(rows["icloud_dataless"]["fix_id"])   # a policy choice
        self.assertIn("download", rows["icloud_dataless"]["hint"])
        self.assertIn("7", rows["icloud_dataless"]["name"])

    def test_icloud_dataless_ok_when_nothing_skipped(self):
        rows = self._rows({
            "state": "MOUNTED_IDLE", "updated_at": FIXED_NOW.isoformat(),
            "folders": {self._configured_name(): {
                "icloud_skipped": {"count": 0, "bytes": 0, "samples": []}}},
        })
        self.assertTrue(rows["icloud_dataless"]["ok"])

    def test_icloud_dataless_ignores_removed_sources(self):
        # Regression (2026-07-28): state.folders keeps an entry per folder ever
        # synced, frozen at removal time. A source the user removed was still
        # contributing 315306 stale dataless files, so doctor reported 316275
        # when the truth was 969. Only configured sources may count.
        rows = self._rows({
            "state": "MOUNTED_IDLE", "updated_at": FIXED_NOW.isoformat(),
            "folders": {
                self._configured_name(): {
                    "icloud_skipped": {"count": 0, "bytes": 0, "samples": []}},
                "GhostSourceRemovedLongAgo": {
                    "orphaned": True,
                    "icloud_skipped": {"count": 315306, "bytes": 1, "samples": []}},
            },
        })
        self.assertTrue(rows["icloud_dataless"]["ok"])
        self.assertNotIn("315306", rows["icloud_dataless"]["name"])

    def test_deep_verify_age_fails_when_never_run(self):
        rows = self._rows({"state": "MOUNTED_IDLE",
                           "updated_at": FIXED_NOW.isoformat()})
        self.assertFalse(rows["deep_verify_age"]["ok"])
        self.assertEqual(rows["deep_verify_age"]["fix_id"], "run_deep_verify")

    def test_deep_verify_age_fails_when_stale(self):
        old = (FIXED_NOW - timedelta(days=9)).isoformat()
        rows = self._rows({"state": "MOUNTED_IDLE",
                           "updated_at": FIXED_NOW.isoformat(),
                           "last_deep_verify_at": old})
        self.assertFalse(rows["deep_verify_age"]["ok"])
        self.assertEqual(rows["deep_verify_age"]["fix_id"], "run_deep_verify")

    def test_deep_verify_age_ok_when_recent(self):
        recent = (FIXED_NOW - timedelta(days=1)).isoformat()
        rows = self._rows({"state": "MOUNTED_IDLE",
                           "updated_at": FIXED_NOW.isoformat(),
                           "last_deep_verify_at": recent})
        self.assertTrue(rows["deep_verify_age"]["ok"])


class MaterializeTests(unittest.TestCase):
    """`mycloudctl materialize` (PLAN 16.2 T-C2, 2026-07-30): user-context
    download of cloud-only iCloud files, since a launchd daemon cannot trigger
    one. Fully stubbed -- the dataless predicate, the downloader and the
    free-space probe are injected, so no real iCloud file is ever touched."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = os.path.join(self.dir, "config.ini")
        self.src = tempfile.mkdtemp()
        self.calls = []          # batches handed to the fake materializer

    def _mk(self, rel, size=4):
        path = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"x" * size)
        return os.path.realpath(path)

    def _fake_materialize(self, fail=()):
        """Record each batch; report every path materialized except `fail`."""
        failing = set(fail)

        def _materialize(abspaths, is_dataless_fn=None, stop_event=None,
                         run_cmd=None, **kwargs):
            self.calls.append(list(abspaths))
            ok = [p for p in abspaths if os.path.realpath(p) not in failing]
            bad = [p for p in abspaths if os.path.realpath(p) in failing]
            return ok, bad

        return _materialize

    def _ctl(self, out, dataless=(), free=10 ** 12, materialize_fn=None):
        _write_config(self.cfg, folders=[self.src])
        cloud = set(os.path.realpath(p) for p in dataless)

        def _is_dataless(path, st=None):
            return os.path.realpath(path) in cloud

        return _base_ctl(
            self.dir, out=out, config_path=self.cfg,
            dataless_check=_is_dataless,
            materialize_fn=materialize_fn or self._fake_materialize(),
            free_bytes_fn=lambda path: free)

    def test_no_dataless_files_is_a_clean_noop(self):
        self._mk("local.txt")
        out = io.StringIO()
        ctl = self._ctl(out)                     # nothing is dataless
        rc = ctl.cmd_materialize()
        self.assertEqual(rc, 0)
        self.assertIn("没有需要下载的云端文件", out.getvalue())
        self.assertEqual(self.calls, [])

    def test_dry_run_only_reports(self):
        cloud = [self._mk("a.bin", 8), self._mk("sub/b.bin", 8)]
        out = io.StringIO()
        ctl = self._ctl(out, dataless=cloud)
        rc = ctl.cmd_materialize(dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn("发现 2 个仅云端文件", out.getvalue())
        self.assertEqual(self.calls, [])         # nothing downloaded

    def test_materializes_every_dataless_file(self):
        cloud = [self._mk("a.bin"), self._mk("b.bin"), self._mk("sub/c.bin")]
        out = io.StringIO()
        ctl = self._ctl(out, dataless=cloud)
        rc = ctl.cmd_materialize()
        self.assertEqual(rc, 0)
        handed = [os.path.realpath(p) for batch in self.calls for p in batch]
        self.assertEqual(sorted(handed), sorted(cloud))
        text = out.getvalue()
        self.assertIn("已下载 3/3", text)
        self.assertIn("成功 3 个，失败 0 个", text)
        self.assertIn("sync-now", text)          # next-step hint

    def test_finds_macos12_placeholders(self):
        # macOS <= 13: no dataless files, only hidden ".X.icloud" placeholders;
        # the GUI's 立即下载到本机 found nothing to download there.
        import plistlib
        os.makedirs(os.path.join(self.src, "sub"))
        with open(os.path.join(self.src, "sub", ".r.pdf.icloud"), "wb") as h:
            h.write(plistlib.dumps({"NSURLFileSizeKey": 1234},
                                   fmt=plistlib.FMT_BINARY))
        self._mk("sub/.done.txt.icloud")          # its file is already here
        self._mk("sub/done.txt")
        out = io.StringIO()
        ctl = self._ctl(out)
        rc = ctl.cmd_materialize()
        self.assertEqual(rc, 0)
        handed = [os.path.realpath(p) for batch in self.calls for p in batch]
        self.assertEqual(handed, [os.path.join(os.path.realpath(self.src),
                                               "sub", "r.pdf")])
        self.assertIn("发现 1 个仅云端文件", out.getvalue())

    def test_limit_truncates_deterministically(self):
        # Truncation is by sorted relpath, so the same --limit picks the same
        # files on every run.
        self._mk("a.bin"); self._mk("b.bin"); self._mk("c.bin")
        cloud = [os.path.realpath(os.path.join(self.src, n))
                 for n in ("a.bin", "b.bin", "c.bin")]
        out = io.StringIO()
        ctl = self._ctl(out, dataless=cloud)
        rc = ctl.cmd_materialize(limit=2)
        self.assertEqual(rc, 0)
        handed = [os.path.realpath(p) for batch in self.calls for p in batch]
        self.assertEqual(handed, cloud[:2])
        self.assertIn("按 --limit 只处理前 2 个", out.getvalue())

    def test_disk_guard_refuses_when_free_space_is_short(self):
        cloud = [self._mk("big.bin", 1000)]
        out = io.StringIO()
        # Free space below 2x the pending download.
        ctl = self._ctl(out, dataless=cloud, free=1500)
        rc = ctl.cmd_materialize()
        self.assertEqual(rc, 1)
        self.assertIn("磁盘可用空间不足", out.getvalue())
        self.assertEqual(self.calls, [])         # refused before downloading

    def test_partial_failure_exits_nonzero_and_lists_samples(self):
        good = self._mk("ok.bin")
        bad = self._mk("stuck.bin")
        out = io.StringIO()
        ctl = self._ctl(out, dataless=[good, bad],
                        materialize_fn=self._fake_materialize(fail=[bad]))
        rc = ctl.cmd_materialize()
        self.assertEqual(rc, 1)
        text = out.getvalue()
        self.assertIn("成功 1 个，失败 1 个", text)
        self.assertIn("stuck.bin", text)

    def test_materialize_subcommand_parses(self):
        parser = mycloudctl.build_parser()
        args = parser.parse_args(["materialize", "--limit", "5", "--dry-run"])
        self.assertEqual(args.command, "materialize")
        self.assertEqual(args.limit, 5)
        self.assertTrue(args.dry_run)
        plain = parser.parse_args(["materialize"])
        self.assertIsNone(plain.limit)
        self.assertFalse(plain.dry_run)


if __name__ == "__main__":
    unittest.main()


class DoctorRsyncImplementationToleranceTests(unittest.TestCase):
    """The capability row must tolerate the REAL samba rsync 2.6.9 (macOS <=14):
    delete_suppressed_with_backup=False there is an implementation difference,
    not a defect -- write-mode argv never carries --delete, so suppression never
    matters. Requiring all() probes True falsely failed every pre-openrsync
    gateway (Monterey deployment, 2026-08-01)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.mnt = os.path.join(self.dir, "mnt")
        os.makedirs(self.mnt, exist_ok=True)
        os.chmod(self.mnt, 0o500)
        self.config_path = os.path.join(self.dir, "config.ini")
        with open(self.config_path, "w", encoding="utf-8") as h:
            h.write("[server]\nhost = 127.0.0.1\nshare = SHARE\n"
                    "[backup]\nfolders =\n    %s\n" % self.dir)

    _MONTEREY = {"backup_moves_overwritten": True,
                 "delete_suppressed_with_backup": False,
                 "deleting_line_format_ok": True,
                 "inplace_updates_ok": True}

    def _rows(self, out):
        rows = {}
        for r in json.loads(out.getvalue()):
            rows[r["id"]] = r
        return rows

    def _doctor(self, probe, state_doc):
        _write_state(self.dir, state_doc)
        out = io.StringIO()
        ctl = _base_ctl(self.dir, out=out, mnt=self.mnt,
                        config_path=self.config_path,
                        run_cmd=_Recorder(default=_fail()),
                        probe_tcp=lambda h, *a, **k: False,
                        cap_probe=lambda tmp, rsync: dict(probe))
        ctl.cmd_doctor(as_json=True)
        return self._rows(out)

    def test_real_rsync_269_first_run_passes_with_note(self):
        # No stored fingerprint (doctor runs from install.sh before the daemon
        # ever started): suppression=False must NOT fail the row.
        rows = self._doctor(self._MONTEREY,
                            {"state": "OFFLINE",
                             "updated_at": FIXED_NOW.isoformat()})
        row = rows["rsync_capability"]
        self.assertTrue(row["ok"])
        self.assertIn("2.6.9", row["detail"])
        self.assertIn("安全性不受影响", row["detail"])

    def test_missing_critical_probe_still_fails(self):
        bad = dict(self._MONTEREY, deleting_line_format_ok=False)
        rows = self._doctor(bad, {"state": "OFFLINE",
                                  "updated_at": FIXED_NOW.isoformat()})
        self.assertFalse(rows["rsync_capability"]["ok"])
        self.assertIn("关键行为", rows["rsync_capability"]["hint"])

    def test_stored_key_change_still_fails(self):
        stored = json.dumps(dict(self._MONTEREY,
                                 delete_suppressed_with_backup=True),
                            sort_keys=True)
        rows = self._doctor(self._MONTEREY,
                            {"state": "OFFLINE",
                             "updated_at": FIXED_NOW.isoformat(),
                             "capability_fingerprint": stored})
        row = rows["rsync_capability"]
        self.assertFalse(row["ok"])
        self.assertIn("delete_suppressed_with_backup", row["hint"])

    def test_new_probe_key_is_not_a_change(self):
        # Stored fingerprint predates inplace_updates_ok: a NEW key must not
        # read as a behavior change (matches the daemon's semantics).
        legacy = {k: v for k, v in self._MONTEREY.items()
                  if k != "inplace_updates_ok"}
        rows = self._doctor(self._MONTEREY,
                            {"state": "OFFLINE",
                             "updated_at": FIXED_NOW.isoformat(),
                             "capability_fingerprint":
                                 json.dumps(legacy, sort_keys=True)})
        self.assertTrue(rows["rsync_capability"]["ok"])


class _FakeVerifyMount(object):
    """verify_mount_factory stub for server-set: records the ctor args, and on
    mount() optionally plants an existing sentinel in the temp mountpoint."""

    def __init__(self, mount_ok=True, existing_sentinel=None, unreadable=False,
                 listed=True):
        self.mount_ok = mount_ok
        self.existing_sentinel = existing_sentinel
        self.unreadable = unreadable      # plant the sentinel, then chmod 000
        self.listed = listed              # is_mounted() verdict after mount
        self.calls = []
        self.mountpoint = None

    def factory(self, host, share, username, mountpoint, sentinel_uuid, guest,
                password=None):
        self.calls.append(dict(host=host, share=share, username=username,
                               mountpoint=mountpoint, sentinel_uuid=sentinel_uuid,
                               guest=guest, password=password))
        self.mountpoint = mountpoint
        return self

    def mount(self):
        if self.mount_ok and self.existing_sentinel:
            sp = os.path.join(self.mountpoint, ".mycloud_sentinel")
            with open(sp, "w") as h:
                h.write(self.existing_sentinel + "\n")
            if self.unreadable:
                os.chmod(sp, 0o000)
        return self.mount_ok

    def is_mounted(self):
        return self.mount_ok and self.listed


class ServerSetTests(unittest.TestCase):
    """server-set: the GUI's NAS connection editor (设置 → 云盘连接)."""

    PASSWORD = 'p@ss "quoted" \\slash'

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = os.path.join(self.dir, "config.ini")
        self.nsmb = os.path.join(self.dir, "nsmb.conf")
        self.mnt = os.path.join(self.dir, "mnt")
        os.makedirs(self.mnt)
        self.src = os.path.join(self.dir, "Docs")
        os.makedirs(self.src)
        _write_config(self.cfg, folders=[self.src])
        self.fake = _FakeVerifyMount(existing_sentinel="uuid-xyz")

    def tearDown(self):
        if self.fake.mountpoint:
            shutil.rmtree(self.fake.mountpoint, ignore_errors=True)

    def _ctl(self, rec, fake=None):
        return _base_ctl(self.dir, out=io.StringIO(), mnt=self.mnt,
                         config_path=self.cfg, run_cmd=rec, nsmb_conf=self.nsmb,
                         verify_mount_factory=(fake or self.fake).factory)

    @staticmethod
    def _rec(print_rc=3, smb_out=""):
        # Stateful launchd model, PER LABEL: bootout unloads, bootstrap loads
        # (label = plist basename), print reports the label's state -- so a
        # pause→resume cycle, or the installer loading the watchdog before the
        # agent, takes the same branches it takes on a real machine.
        state = {"com.mycloud.agent": print_rc == 0}

        def router(argv):
            if argv[0] == paths.SMBUTIL:
                return _ok(smb_out)
            if argv[0] != paths.LAUNCHCTL:
                return _ok()
            sub = argv[1]
            if sub == "print":
                label = argv[2].rsplit("/", 1)[-1]
                return _ok() if state.get(label, False) else _fail(rc=3)
            if sub == "bootout":
                state[argv[2].rsplit("/", 1)[-1]] = False
            elif sub == "bootstrap":
                state[os.path.basename(argv[3]).replace(".plist", "")] = True
            return _ok()
        return _Recorder(default=router)

    def _payload(self, ctl):
        return json.loads(ctl.out.getvalue())

    # -- user mode -----------------------------------------------------------

    def test_user_mode_password_travels_only_on_stdin(self):
        rec = self._rec()
        ctl = self._ctl(rec)
        rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "user",
                                username="admin", password=self.PASSWORD)
        self.assertEqual(rc, 0, ctl.out.getvalue())
        # THE invariant: no argv of any spawned process carries the password.
        for argv in rec.calls:
            for arg in argv:
                self.assertNotIn(self.PASSWORD, arg)
                self.assertNotIn("quoted", arg)
        sec = [i for i, a in enumerate(rec.calls) if a[0] == paths.SECURITY]
        self.assertEqual(len(sec), 1)
        self.assertEqual(rec.calls[sec[0]], [paths.SECURITY, "-i"])
        stdin = rec.inputs[sec[0]]
        self.assertIn("add-internet-password -U", stdin)
        self.assertIn('-a "admin"', stdin)
        self.assertIn('-s "192.168.5.1"', stdin)
        # backslash and double quote are escaped for security -i's tokenizer
        self.assertIn('-w "p@ss \\"quoted\\" \\\\slash"', stdin)
        # nsmb.conf fallback: exact section, 0600
        with open(self.nsmb) as h:
            text = h.read()
        self.assertIn("[192.168.5.1]\nusername=admin\npassword=%s\n" % self.PASSWORD, text)
        self.assertEqual(os.stat(self.nsmb).st_mode & 0o777, 0o600)
        cfg = config_mod.load_config(self.cfg)
        self.assertEqual((cfg.auth_mode, cfg.username, cfg.sentinel_uuid),
                         ("user", "admin", "uuid-xyz"))
        payload = self._payload(ctl)
        self.assertEqual(payload["auth_mode"], "user")
        self.assertIn("钥匙串", payload["notice"])
        self.assertNotIn(self.PASSWORD, ctl.out.getvalue())

    def test_user_mode_requires_username_and_nonempty_password(self):
        for kwargs in (dict(username="", password="x"),
                       dict(username="admin", password=""),
                       dict(username="admin", password="a\nb")):
            ctl = self._ctl(self._rec())
            rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "user", **kwargs)
            self.assertEqual(rc, 1)
            self.assertIn("error", self._payload(ctl))

    def test_rejects_bad_host_share_mode(self):
        for args in (("bad host", "SHARE", "user"), ("", "SHARE", "user"),
                     ("192.168.5.1", "a/b", "user"), ("192.168.5.1", "", "user"),
                     ("192.168.5.1", "SHARE", "magic")):
            ctl = self._ctl(self._rec())
            rc = ctl.cmd_server_set(*args, username="admin", password="x")
            self.assertEqual(rc, 1, args)
            self.assertIn("error", self._payload(ctl))
        self.assertEqual(self.fake.calls, [])  # never reached verification

    # -- anonymous mode ------------------------------------------------------

    def test_anonymous_mode_mounts_as_guest_and_strips_nsmb_password(self):
        with open(self.nsmb, "w") as h:
            h.write("[default]\nstreams=yes\n[192.168.5.1]\nusername=admin\n"
                    "password=old\n[other.host]\nusername=u\npassword=p\n")
        rec = self._rec()
        ctl = self._ctl(rec)
        rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "anonymous")
        self.assertEqual(rc, 0, ctl.out.getvalue())
        self.assertEqual(self.fake.calls[0]["username"], "guest")
        self.assertTrue(self.fake.calls[0]["guest"])
        with open(self.nsmb) as h:
            text = h.read()
        self.assertNotIn("password=old", text)
        self.assertNotIn("[192.168.5.1]", text)
        self.assertIn("[default]\nstreams=yes\n", text)       # other sections kept
        self.assertIn("[other.host]\nusername=u\npassword=p\n", text)
        self.assertFalse(any(a[0] == paths.SECURITY for a in rec.calls))
        cfg = config_mod.load_config(self.cfg)
        self.assertEqual((cfg.auth_mode, cfg.username, cfg.mount_username),
                         ("anonymous", "guest", "guest"))

    # -- target change / sentinel --------------------------------------------

    def test_new_share_initialises_layout_and_retires_manifest(self):
        runtime_manifest = os.path.join(self.dir, "manifest.db")
        with open(runtime_manifest, "w") as h:
            h.write("rows")
        with open(runtime_manifest + "-wal", "w") as h:
            h.write("")
        fake = _FakeVerifyMount(existing_sentinel=None)   # fresh share
        ctl = self._ctl(self._rec(), fake=fake)
        rc = ctl.cmd_server_set("192.168.5.9", "NEWSHARE", "user",
                                username="admin", password="pw")
        self.assertEqual(rc, 0, ctl.out.getvalue())
        # sentinel written to the share + adopted into config
        with open(os.path.join(fake.mountpoint, ".mycloud_sentinel")) as h:
            written = h.read().strip()
        cfg = config_mod.load_config(self.cfg)
        self.assertEqual(cfg.sentinel_uuid, written)
        self.assertNotEqual(written, "uuid-xyz")
        self.assertTrue(os.path.isdir(os.path.join(fake.mountpoint, "MyCloud", "data")))
        self.assertTrue(os.path.isdir(os.path.join(fake.mountpoint, "MyCloud", ".trash")))
        self.assertEqual((cfg.host, cfg.share), ("192.168.5.9", "NEWSHARE"))
        # the old manifest describes the OLD share: retired, not deleted
        self.assertFalse(os.path.exists(runtime_manifest))
        baks = [f for f in os.listdir(self.dir) if f.startswith("manifest.db.bak-")]
        self.assertEqual(len(baks), 2)   # db + -wal
        self.assertIn("重新全量备份", self._payload(ctl)["notice"])
        self.assertEqual(fake.calls[0]["sentinel_uuid"], "")   # not carried over

    def test_same_target_keeps_manifest_and_sentinel(self):
        runtime_manifest = os.path.join(self.dir, "manifest.db")
        with open(runtime_manifest, "w") as h:
            h.write("rows")
        ctl = self._ctl(self._rec())
        rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "user",
                                username="admin", password="pw")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(runtime_manifest))
        self.assertEqual(self.fake.calls[0]["sentinel_uuid"], "uuid-xyz")
        self.assertEqual(config_mod.load_config(self.cfg).sentinel_uuid, "uuid-xyz")

    def test_mount_failure_reports_error_with_share_listing_and_keeps_config(self):
        fake = _FakeVerifyMount(mount_ok=False)
        ctl = self._ctl(self._rec(smb_out="SHARE   Disk\nMEDIA   Disk\n"), fake=fake)
        rc = ctl.cmd_server_set("192.168.5.1", "WRONG", "user",
                                username="admin", password="pw")
        self.assertEqual(rc, 1)
        err = self._payload(ctl)["error"]
        self.assertIn("无法挂载", err)
        self.assertIn("MEDIA", err)
        self.assertEqual(config_mod.load_config(self.cfg).share, "SHARE")  # untouched
        # verification failed -> NOTHING was persisted (review #3)
        self.assertFalse(os.path.exists(self.nsmb))
        self.assertFalse(any(a[0] == paths.SECURITY for a in ctl.run_cmd.calls))

    def test_no_verify_skips_mount_and_keeps_sentinel(self):
        ctl = self._ctl(self._rec())
        rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "anonymous", verify=False)
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(self._payload(ctl)["verified"])

    # -- daemon restart ------------------------------------------------------

    def test_verification_with_running_agent_stops_it_first(self):
        # Field review 2026-09-25: even with the same target and no new
        # password, the daemon's live mount of the share makes the verification
        # mount collide ("File exists" -> a false 无法挂载) or ride its session.
        rec = self._rec(print_rc=0)   # agent loaded
        ctl = self._ctl(rec)
        rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "user", username="admin")
        self.assertEqual(rc, 0, ctl.out.getvalue())
        subs = [a[1] for a in rec.calls if a[0] == paths.LAUNCHCTL]
        self.assertIn("bootout", subs)
        self.assertIn("bootstrap", subs)
        self.assertLess(subs.index("bootout"), subs.index("bootstrap"))
        self.assertIn("已重启", self._payload(ctl)["notice"])

    def test_same_target_without_verification_just_kickstarts(self):
        rec = self._rec(print_rc=0)
        ctl = self._ctl(rec)
        rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "user", username="admin",
                                verify=False)
        self.assertEqual(rc, 0, ctl.out.getvalue())
        subs = [a[1] for a in rec.calls if a[0] == paths.LAUNCHCTL]
        self.assertIn("kickstart", subs)
        self.assertNotIn("bootout", subs)

    def test_target_change_with_running_agent_pauses_then_resumes(self):
        rec = self._rec(print_rc=0)
        fake = _FakeVerifyMount(existing_sentinel=None)
        ctl = self._ctl(rec, fake=fake)
        rc = ctl.cmd_server_set("192.168.5.9", "SHARE", "user",
                                username="admin", password="pw")
        self.assertEqual(rc, 0, ctl.out.getvalue())
        subs = [a[1] for a in rec.calls if a[0] == paths.LAUNCHCTL]
        self.assertIn("bootout", subs)
        self.assertIn("bootstrap", subs)
        self.assertLess(subs.index("bootout"), subs.index("bootstrap"))

    # -- supervised-review hardening (2026-09-12) ---------------------------

    def test_verification_failure_persists_nothing(self):
        fake = _FakeVerifyMount(mount_ok=False)
        rec = self._rec()
        ctl = self._ctl(rec, fake=fake)
        rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "user",
                                username="admin", password="wrong")
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.nsmb))
        self.assertFalse(any(a[0] == paths.SECURITY for a in rec.calls))
        self.assertEqual(fake.calls[0]["password"], "wrong")   # verified with the candidate

    def test_new_password_with_running_daemon_stops_before_verifying(self):
        rec = self._rec(print_rc=0)
        ctl = self._ctl(rec)
        rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "user",
                                username="admin", password="pw")
        self.assertEqual(rc, 0, ctl.out.getvalue())
        kinds = [(a[0], a[1] if len(a) > 1 else "") for a in rec.calls]
        i_bootout = kinds.index((paths.LAUNCHCTL, "bootout"))
        i_sec = next(i for i, k in enumerate(kinds) if k[0] == paths.SECURITY)
        self.assertLess(i_bootout, i_sec)   # stopped -> verified -> stored
        self.assertIn((paths.LAUNCHCTL, "bootstrap"), kinds)

    def test_target_change_unmounts_old_share_from_daemon_mountpoint(self):
        real_mnt = os.path.realpath(self.mnt)
        table = {"listed": True}
        base = self._rec(print_rc=0)

        def router(argv):
            if argv[0] == paths.MOUNT:
                return _ok("//admin@192.168.5.1/SHARE on %s (smbfs, nobrowse)\n" % real_mnt
                           if table["listed"] else "")
            if argv[0] == paths.UMOUNT and argv[1] == real_mnt:
                table["listed"] = False
                return _ok()
            return base.default(argv)
        rec = _Recorder(default=router)
        fake = _FakeVerifyMount(existing_sentinel=None)
        ctl = self._ctl(rec, fake=fake)
        rc = ctl.cmd_server_set("192.168.5.1", "NEWSHARE", "user",
                                username="admin", password="pw")
        self.assertEqual(rc, 0, ctl.out.getvalue())
        self.assertIn([paths.UMOUNT, real_mnt], rec.calls)
        self.assertEqual(config_mod.load_config(self.cfg).share, "NEWSHARE")
        shutil.rmtree(fake.mountpoint, ignore_errors=True)

    def test_unreleasable_old_mount_aborts_and_resumes(self):
        real_mnt = os.path.realpath(self.mnt)
        base = self._rec(print_rc=0)

        def router(argv):
            if argv[0] == paths.MOUNT:
                return _ok("//admin@192.168.5.1/SHARE on %s (smbfs)\n" % real_mnt)
            if argv[0] in (paths.UMOUNT, paths.DISKUTIL):
                return _fail(rc=1)
            return base.default(argv)
        rec = _Recorder(default=router)
        ctl = self._ctl(rec, fake=_FakeVerifyMount(existing_sentinel=None))
        rc = ctl.cmd_server_set("192.168.5.1", "NEWSHARE", "user",
                                username="admin", password="pw")
        self.assertEqual(rc, 1)
        self.assertIn("仍挂载", self._payload(ctl)["error"])
        self.assertEqual(config_mod.load_config(self.cfg).share, "SHARE")   # unchanged
        subs = [a[1] for a in rec.calls if a[0] == paths.LAUNCHCTL]
        self.assertIn("bootstrap", subs)   # daemon resumed despite the abort

    def test_foreign_sentinel_requires_explicit_adopt(self):
        fake = _FakeVerifyMount(existing_sentinel="SOMEONE-ELSES-UUID")
        ctl = self._ctl(self._rec(), fake=fake)
        rc = ctl.cmd_server_set("192.168.5.9", "OTHER", "user",
                                username="admin", password="pw")
        self.assertEqual(rc, 1)
        payload = self._payload(ctl)
        self.assertEqual(payload["needs_confirm"], "adopt")
        self.assertEqual(payload["found_sentinel"], "SOMEONE-ELSES-UUID")
        self.assertEqual(config_mod.load_config(self.cfg).share, "SHARE")
        self.assertFalse(os.path.exists(self.nsmb))            # nothing persisted
        shutil.rmtree(fake.mountpoint, ignore_errors=True)
        # explicit adoption succeeds and records the foreign sentinel
        fake2 = _FakeVerifyMount(existing_sentinel="SOMEONE-ELSES-UUID")
        ctl2 = self._ctl(self._rec(), fake=fake2)
        rc = ctl2.cmd_server_set("192.168.5.9", "OTHER", "user",
                                 username="admin", password="pw", adopt=True)
        self.assertEqual(rc, 0, ctl2.out.getvalue())
        self.assertEqual(config_mod.load_config(self.cfg).sentinel_uuid,
                         "SOMEONE-ELSES-UUID")
        shutil.rmtree(fake2.mountpoint, ignore_errors=True)

    def test_unreadable_sentinel_is_an_error_not_a_fresh_share(self):
        fake = _FakeVerifyMount(existing_sentinel="uuid-xyz", unreadable=True)
        ctl = self._ctl(self._rec(), fake=fake)
        rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "user",
                                username="admin", password="pw")
        self.assertEqual(rc, 1)
        self.assertIn("无法读取", self._payload(ctl)["error"])
        sp = os.path.join(fake.mountpoint, ".mycloud_sentinel")
        os.chmod(sp, 0o600)
        with open(sp) as h:
            self.assertEqual(h.read().strip(), "uuid-xyz")   # never truncated

    def test_guard_mismatch_after_mount_is_rejected(self):
        # mount() succeeded but the mount-table line does not match the guard's
        # rules: saving would leave the daemon unable to confirm its own mount.
        fake = _FakeVerifyMount(existing_sentinel="uuid-xyz", listed=False)
        ctl = self._ctl(self._rec(), fake=fake)
        rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "anonymous")
        self.assertEqual(rc, 1)
        self.assertIn("匹配规则", self._payload(ctl)["error"])

    def test_daemon_not_stopping_aborts_without_touching_manifest(self):
        runtime_manifest = os.path.join(self.dir, "manifest.db")
        with open(runtime_manifest, "w") as h:
            h.write("rows")
        # bootout "succeeds" but the daemon keeps running: it still holds the
        # flock (a live process) and launchd still lists the agent (print rc 0)
        _hold_daemon_lock(self, self.dir, 4321)
        rec = _Recorder(default=lambda argv: _ok())
        ctl = self._ctl(rec, fake=_FakeVerifyMount(existing_sentinel=None))
        rc = ctl.cmd_server_set("192.168.5.9", "NEWSHARE", "user",
                                username="admin", password="pw")
        self.assertEqual(rc, 1)
        self.assertIn("未能在限时内停止", self._payload(ctl)["error"])
        self.assertTrue(os.path.exists(runtime_manifest))
        self.assertEqual(config_mod.load_config(self.cfg).share, "SHARE")
        self.assertFalse(os.path.exists(self.nsmb))

    def test_username_and_password_edge_validation(self):
        for user in ("a b", "user@dom", "a/b", "a:b", 'q"x'):
            ctl = self._ctl(self._rec())
            rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "user",
                                    username=user, password="pw")
            self.assertEqual(rc, 1, user)
        ctl = self._ctl(self._rec())
        rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "user",
                                username="admin", password=" pw ")
        self.assertEqual(rc, 1)
        self.assertIn("首尾", self._payload(ctl)["error"])
        self.assertEqual(self.fake.calls, [])

    def test_target_comparison_ignores_case_and_slashes(self):
        runtime_manifest = os.path.join(self.dir, "manifest.db")
        with open(runtime_manifest, "w") as h:
            h.write("rows")
        ctl = self._ctl(self._rec())
        rc = ctl.cmd_server_set("192.168.5.1", "share/", "user", username="admin")
        self.assertEqual(rc, 0, ctl.out.getvalue())
        self.assertTrue(os.path.exists(runtime_manifest))        # not a target change
        self.assertEqual(self.fake.calls[0]["sentinel_uuid"], "uuid-xyz")

    def test_vanished_sentinel_on_same_target_retires_manifest(self):
        runtime_manifest = os.path.join(self.dir, "manifest.db")
        with open(runtime_manifest, "w") as h:
            h.write("rows")
        fake = _FakeVerifyMount(existing_sentinel=None)   # share rebuilt: no sentinel
        ctl = self._ctl(self._rec(), fake=fake)
        rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "user", username="admin")
        self.assertEqual(rc, 0, ctl.out.getvalue())
        self.assertFalse(os.path.exists(runtime_manifest))
        self.assertIn("哨兵已消失", self._payload(ctl)["notice"])
        self.assertNotEqual(config_mod.load_config(self.cfg).sentinel_uuid, "uuid-xyz")
        shutil.rmtree(fake.mountpoint, ignore_errors=True)

    def test_anonymous_strips_host_user_variant_sections_too(self):
        with open(self.nsmb, "w") as h:
            h.write("[192.168.5.1]\npassword=a\n[192.168.5.1:ADMIN]\npassword=b\n"
                    "[other]\npassword=c\n")
        ctl = self._ctl(self._rec())
        rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "anonymous")
        self.assertEqual(rc, 0, ctl.out.getvalue())
        with open(self.nsmb) as h:
            text = h.read()
        self.assertNotIn("password=a", text)
        self.assertNotIn("password=b", text)
        self.assertIn("[other]\npassword=c\n", text)


class InstallUninstallTests(unittest.TestCase):
    """`install` / `uninstall`: the GUI-driven ports of install.sh /
    uninstall.sh (2026-09-12 -- every operation must be completable from the
    GUI). The backend comes from a fake bundle dir; launchd, security, mount
    and the verification mount are all stubbed."""

    TEMPLATES = ("com.mycloud.agent.plist.template",
                 "com.mycloud.watchdog.plist.template",
                 "config.ini.template", "watchdog.sh")

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.app = os.path.join(self.dir, "app")
        self.runtime = os.path.join(self.app, "runtime")
        os.makedirs(self.runtime)
        self.cfg = os.path.join(self.app, "config.ini")
        self.mnt = os.path.join(self.app, "mnt")
        self.logs = os.path.join(self.dir, "logs")
        self.agents = os.path.join(self.dir, "LaunchAgents")
        self.nsmb = os.path.join(self.dir, "nsmb.conf")
        self.src = os.path.join(self.dir, "Docs")
        os.makedirs(self.src)
        # fake bundle backend
        self.backend = os.path.join(self.dir, "backend")
        os.makedirs(os.path.join(self.backend, "mycloud", "__pycache__"))
        for name in ("mycloudd.py", "mycloudctl.py"):
            with open(os.path.join(self.backend, name), "w") as h:
                h.write("# %s\n" % name)
        with open(os.path.join(self.backend, "mycloud", "__init__.py"), "w") as h:
            h.write("")
        with open(os.path.join(self.backend, "mycloud", "__pycache__", "x.pyc"), "w") as h:
            h.write("junk")
        repo_install = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "install")
        for name in self.TEMPLATES:
            shutil.copy(os.path.join(repo_install, name), os.path.join(self.backend, name))
        self.fake = _FakeVerifyMount(existing_sentinel=None)

    def tearDown(self):
        if self.fake.mountpoint:
            shutil.rmtree(self.fake.mountpoint, ignore_errors=True)

    def _ctl(self, rec=None, fake=None):
        return _base_ctl(self.runtime, out=io.StringIO(), mnt=self.mnt, logdir=self.logs,
                         config_path=self.cfg,
                         agent_plist=os.path.join(self.agents, "com.mycloud.agent.plist"),
                         run_cmd=rec if rec is not None else ServerSetTests._rec(),
                         nsmb_conf=self.nsmb,
                         verify_mount_factory=(fake or self.fake).factory)

    def _payload(self, ctl):
        return json.loads(ctl.out.getvalue())

    def test_fresh_install_lays_out_everything(self):
        rec = ServerSetTests._rec()
        ctl = self._ctl(rec)
        rc = ctl.cmd_install(self.backend, "192.168.5.1", "SHARE", "anonymous",
                             folders=[self.src])
        self.assertEqual(rc, 0, ctl.out.getvalue())
        # backend copied, pycache dropped
        for name in ("mycloudd.py", "mycloudctl.py", "mycloud/__init__.py"):
            self.assertTrue(os.path.isfile(os.path.join(self.app, name)), name)
        self.assertFalse(os.path.exists(os.path.join(self.app, "mycloud", "__pycache__")))
        # bare mountpoint 0500
        self.assertEqual(os.stat(self.mnt).st_mode & 0o777, 0o500)
        # config rendered: server identity + our folder + a sentinel from server-set
        cfg = config_mod.load_config(self.cfg)
        self.assertEqual((cfg.host, cfg.share, cfg.auth_mode, cfg.username),
                         ("192.168.5.1", "SHARE", "anonymous", "guest"))
        self.assertEqual([str(f.src) for f in cfg.folders], [os.path.realpath(self.src)])
        self.assertTrue(cfg.sentinel_uuid)
        # launchd artefacts rendered with the real paths
        agent = open(os.path.join(self.agents, "com.mycloud.agent.plist")).read()
        self.assertIn(os.path.join(self.app, "mycloudd.py"), agent)
        self.assertIn(os.path.join(self.logs, "launchd.err.log"), agent)
        self.assertNotIn("__APP__", agent)
        watchdog = open(os.path.join(self.app, "watchdog.sh")).read()
        self.assertIn(self.app, watchdog)
        self.assertTrue(os.access(os.path.join(self.app, "watchdog.sh"), os.X_OK))
        self.assertTrue(os.access(os.path.join(self.app, "bin", "mycloudctl"), os.X_OK))
        # both agents bootstrapped
        boots = [a[3] for a in rec.calls if a[0] == paths.LAUNCHCTL and a[1] == "bootstrap"]
        self.assertTrue(any(b.endswith("com.mycloud.agent.plist") for b in boots))
        self.assertTrue(any(b.endswith("com.mycloud.watchdog.plist") for b in boots))
        payload = self._payload(ctl)
        self.assertTrue(payload["installed"])
        self.assertIn("守护进程已启动", payload["notice"])

    def test_reinstall_keeps_backup_section(self):
        os.makedirs(self.app, exist_ok=True)
        _write_config(self.cfg, folders=[self.src], extra_backup="retention_days = 77")
        ctl = self._ctl()
        rc = ctl.cmd_install(self.backend, "192.168.5.1", "SHARE", "anonymous",
                             folders=[os.path.join(self.dir, "ignored")])
        self.assertEqual(rc, 0, ctl.out.getvalue())
        cfg = config_mod.load_config(self.cfg)
        self.assertEqual(cfg.retention_days, 77)                   # kept
        self.assertEqual([str(f.src) for f in cfg.folders], [os.path.realpath(self.src)])
        self.assertEqual(cfg.auth_mode, "anonymous")               # server section updated
        self.assertIn("保留现有 config.ini", self._payload(ctl)["notice"])

    def test_install_missing_backend_or_template_errors(self):
        ctl = self._ctl()
        self.assertEqual(ctl.cmd_install(os.path.join(self.dir, "nope"), "h", "s", "anonymous"), 1)
        os.remove(os.path.join(self.backend, "config.ini.template"))
        ctl = self._ctl()
        rc = ctl.cmd_install(self.backend, "192.168.5.1", "SHARE", "anonymous")
        self.assertEqual(rc, 1)
        self.assertIn("error", self._payload(ctl))

    def test_install_passes_adoption_question_through(self):
        fake = _FakeVerifyMount(existing_sentinel="FOREIGN-UUID")
        ctl = self._ctl(fake=fake)
        rc = ctl.cmd_install(self.backend, "192.168.5.1", "SHARE", "anonymous",
                             folders=[self.src])
        self.assertEqual(rc, 1)
        self.assertEqual(self._payload(ctl)["needs_confirm"], "adopt")
        shutil.rmtree(fake.mountpoint, ignore_errors=True)
        fake2 = _FakeVerifyMount(existing_sentinel="FOREIGN-UUID")
        ctl2 = self._ctl(fake=fake2)
        rc = ctl2.cmd_install(self.backend, "192.168.5.1", "SHARE", "anonymous",
                              folders=[self.src], adopt=True)
        self.assertEqual(rc, 0, ctl2.out.getvalue())
        self.assertEqual(config_mod.load_config(self.cfg).sentinel_uuid, "FOREIGN-UUID")
        shutil.rmtree(fake2.mountpoint, ignore_errors=True)

    def test_install_user_mode_password_on_stdin_only(self):
        rec = ServerSetTests._rec()
        ctl = self._ctl(rec)
        rc = ctl.cmd_install(self.backend, "192.168.5.1", "SHARE", "user",
                             username="admin", password="pw123", folders=[self.src])
        self.assertEqual(rc, 0, ctl.out.getvalue())
        for argv in rec.calls:
            self.assertFalse(any("pw123" in a for a in argv))
        self.assertTrue(any(a[0] == paths.SECURITY for a in rec.calls))
        self.assertTrue(os.path.exists(self.nsmb))
        self.assertNotIn("pw123", ctl.out.getvalue())

    def test_uninstall_removes_local_only_with_backup(self):
        ctl = self._ctl()
        self.assertEqual(ctl.cmd_install(self.backend, "192.168.5.1", "SHARE", "anonymous",
                                         folders=[self.src]), 0)
        os.makedirs(self.logs, exist_ok=True)
        with open(os.path.join(self.logs, "mycloud.log"), "w") as h:
            h.write("log")
        ctl2 = self._ctl()
        rc = ctl2.cmd_uninstall()
        self.assertEqual(rc, 0, ctl2.out.getvalue())
        self.assertFalse(os.path.exists(self.app))
        self.assertFalse(os.path.exists(os.path.join(self.agents, "com.mycloud.agent.plist")))
        self.assertFalse(os.path.exists(os.path.join(self.agents, "com.mycloud.watchdog.plist")))
        baks = [d for d in os.listdir(self.dir) if d.startswith("app.bak-")]
        self.assertEqual(len(baks), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, baks[0], "config.ini")))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, baks[0], "logs", "mycloud.log")))
        payload = self._payload(ctl2)
        self.assertTrue(payload["uninstalled"])
        self.assertIn("NAS 上的备份数据未作任何改动", payload["notice"])

    def test_uninstall_refuses_while_share_still_mounted(self):
        ctl = self._ctl()
        self.assertEqual(ctl.cmd_install(self.backend, "192.168.5.1", "SHARE", "anonymous",
                                         folders=[self.src]), 0)
        real_mnt = os.path.realpath(self.mnt)

        def router(argv):   # the share stays mounted no matter what
            if argv[0] == paths.MOUNT:
                return _ok("//admin@192.168.5.1/SHARE on %s (smbfs)\n" % real_mnt)
            if argv[0] in (paths.UMOUNT, paths.DISKUTIL):
                return _fail(rc=1)
            return _ok()
        ctl2 = self._ctl(rec=_Recorder(default=router))
        rc = ctl2.cmd_uninstall()
        self.assertEqual(rc, 1)
        self.assertIn("NAS", self._payload(ctl2)["error"])
        self.assertTrue(os.path.isdir(self.app))   # nothing deleted

    # -- self-review hardening (2026-09-12, install/uninstall failure paths) --

    def test_failed_install_is_retried_with_corrected_folders(self):
        # Attempt 1 fails at verification: config.ini exists but carries no
        # sentinel -- the install never completed. Attempt 2 must re-render it
        # rather than silently keeping the first attempt's sources.
        bad = _FakeVerifyMount(mount_ok=False)
        ctl = self._ctl(fake=bad)
        rc = ctl.cmd_install(self.backend, "192.168.5.1", "WRONG", "anonymous",
                             folders=[self.src])
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.exists(self.cfg))
        other = os.path.join(self.dir, "Other")
        os.makedirs(other)
        ctl2 = self._ctl()
        rc = ctl2.cmd_install(self.backend, "192.168.5.1", "SHARE", "anonymous",
                              folders=[other])
        self.assertEqual(rc, 0, ctl2.out.getvalue())
        cfg = config_mod.load_config(self.cfg)
        self.assertEqual([str(f.src) for f in cfg.folders], [os.path.realpath(other)])
        self.assertEqual(cfg.share, "SHARE")

    def test_aborted_reinstall_resumes_the_daemon_it_stopped(self):
        # A running daemon + a verification that fails: the machine must not be
        # left with a stopped daemon on the old (working) configuration.
        rec = ServerSetTests._rec(print_rc=0)          # agent loaded
        ctl = self._ctl(rec, fake=_FakeVerifyMount(mount_ok=False))
        rc = ctl.cmd_install(self.backend, "192.168.5.1", "SHARE", "anonymous",
                             folders=[self.src])
        self.assertEqual(rc, 1)
        subs = [a[1] for a in rec.calls if a[0] == paths.LAUNCHCTL]
        self.assertIn("bootout", subs)
        self.assertIn("bootstrap", subs)
        self.assertLess(subs.index("bootout"), subs.index("bootstrap"))

    def test_uninstall_resumes_daemon_when_the_mount_cannot_be_released(self):
        ctl = self._ctl()
        self.assertEqual(ctl.cmd_install(self.backend, "192.168.5.1", "SHARE",
                                         "anonymous", folders=[self.src]), 0)
        real_mnt = os.path.realpath(self.mnt)

        def router(argv):
            if argv[0] == paths.MOUNT:
                return _ok("//admin@192.168.5.1/SHARE on %s (smbfs)\n" % real_mnt)
            if argv[0] in (paths.UMOUNT, paths.DISKUTIL):
                return _fail(rc=1)
            return _ok()
        rec = _Recorder(default=router)
        ctl2 = self._ctl(rec=rec)
        rc = ctl2.cmd_uninstall()
        self.assertEqual(rc, 1)
        self.assertIn("备份已恢复运行", self._payload(ctl2)["error"])
        # resume restarts it: kickstart while the agent is still loaded,
        # bootstrap once launchd has unloaded it.
        subs = [a[1] for a in rec.calls if a[0] == paths.LAUNCHCTL]
        self.assertTrue("kickstart" in subs or "bootstrap" in subs, subs)
        self.assertTrue(os.path.isdir(self.app))

    def test_version_reports_backend_and_commands(self):
        ctl = self._ctl()
        self.assertEqual(ctl.cmd_version(), 0)
        payload = self._payload(ctl)
        from mycloud import __version__ as backend_version
        self.assertEqual(payload["backend_version"], backend_version)
        # The GUI checks for the commands it needs before offering them.
        for cmd in ("server-set", "install", "uninstall", "materialize", "version"):
            self.assertIn(cmd, payload["commands"])

    def test_refresh_updates_backend_and_leaves_the_connection_alone(self):
        ctl = self._ctl()
        self.assertEqual(ctl.cmd_install(self.backend, "192.168.5.1", "SHARE",
                                         "anonymous", folders=[self.src]), 0,
                         ctl.out.getvalue())
        before = open(self.cfg, encoding="utf-8").read()
        calls_before = len(self.fake.calls)
        # a newer app ships a changed backend
        with open(os.path.join(self.backend, "mycloudd.py"), "w") as h:
            h.write("# v2\n")
        ctl2 = self._ctl()
        rc = ctl2.cmd_install(self.backend, refresh=True)
        self.assertEqual(rc, 0, ctl2.out.getvalue())
        self.assertEqual(open(os.path.join(self.app, "mycloudd.py")).read(), "# v2\n")
        self.assertEqual(open(self.cfg, encoding="utf-8").read(), before)   # untouched
        self.assertEqual(len(self.fake.calls), calls_before)   # no verification mount
        self.assertIn("仅更新后端文件", self._payload(ctl2)["notice"])

    def test_refresh_refuses_before_a_completed_install(self):
        ctl = self._ctl()
        rc = ctl.cmd_install(self.backend, refresh=True)
        self.assertEqual(rc, 1)
        self.assertIn("尚未完成过安装", self._payload(ctl)["error"])

    def test_install_without_connection_args_is_rejected(self):
        ctl = self._ctl()
        rc = ctl.cmd_install(self.backend, mode="anonymous")
        self.assertEqual(rc, 1)
        self.assertIn("服务器地址", self._payload(ctl)["error"])


class OtherMountWarningTests(unittest.TestCase):
    # Reuse ServerSetTests' fixture WITHOUT inheriting its tests (inheritance
    # would re-run all of them under a second name and inflate the count).
    setUp = ServerSetTests.setUp
    tearDown = ServerSetTests.tearDown
    _ctl = ServerSetTests._ctl
    _payload = ServerSetTests._payload

    """A Finder mount of the same server keeps an authenticated SMB session
    alive, and a verification mount silently rides it -- so a NEW password is
    not actually proven. The notice must name the volume and say how to get a
    real answer (2026-09-12 field report)."""

    FINDER_MOUNT = "//admin@192.168.5.1/SHARE on /Volumes/SHARE (smbfs, nodev, nosuid)"

    def _rec_with_finder_mount(self, extra=""):
        real_mnt = os.path.realpath(self.mnt)
        base = ServerSetTests._rec(print_rc=3)

        def router(argv):
            if argv[0] == paths.MOUNT:
                return _ok(self.FINDER_MOUNT + "\n" + extra)
            return base.default(argv)
        return _Recorder(default=router)

    def test_new_password_warns_with_the_volume_and_the_remedy(self):
        ctl = self._ctl(self._rec_with_finder_mount())
        rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "user",
                                username="admin", password="newpw")
        self.assertEqual(rc, 0, ctl.out.getvalue())
        notice = self._payload(ctl)["notice"]
        self.assertIn("/Volumes/SHARE", notice)
        self.assertIn("不能", notice)          # cannot prove the new password
        self.assertIn("推出", notice)          # eject, then restart
        self.assertIn("rc=77", notice)

    def test_without_a_new_password_the_note_is_informational(self):
        ctl = self._ctl(self._rec_with_finder_mount())
        rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "user", username="admin")
        self.assertEqual(rc, 0, ctl.out.getvalue())
        notice = self._payload(ctl)["notice"]
        self.assertIn("不影响结果", notice)
        self.assertNotIn("⚠️", notice)

    def test_our_own_mountpoint_is_not_reported_as_another_mount(self):
        own = "//admin@192.168.5.1/SHARE on %s (smbfs, nobrowse)" % os.path.realpath(self.mnt)
        real_mnt = os.path.realpath(self.mnt)
        base = ServerSetTests._rec(print_rc=3)
        table = {"listed": True}

        def router(argv):
            if argv[0] == paths.MOUNT:
                return _ok(own if table["listed"] else "")
            if argv[0] == paths.UMOUNT and argv[-1] == real_mnt:
                table["listed"] = False       # the leftover daemon mount is released
                return _ok()
            return base.default(argv)
        ctl = self._ctl(_Recorder(default=router))
        rc = ctl.cmd_server_set("192.168.5.1", "SHARE", "user",
                                username="admin", password="newpw")
        self.assertEqual(rc, 0, ctl.out.getvalue())
        notice = self._payload(ctl)["notice"]
        self.assertNotIn("已在", notice)


class RestartWaitTests(unittest.TestCase):
    """_wait_daemon_online must not accept the state a PREVIOUS daemon run left
    behind: the first poll used to read the old MOUNTED_IDLE and report 已重新
    上线 while the restarted daemon could not connect at all."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _ctl(self):
        return _base_ctl(self.dir, out=io.StringIO())

    def test_stale_online_state_from_the_previous_run_is_ignored(self):
        ctl = self._ctl()
        t0 = datetime.now(timezone.utc)
        ctl.store.update(state="MOUNTED_IDLE",
                         daemon_started_at=(t0 - timedelta(hours=3)).isoformat())
        self.assertIsNone(ctl._wait_daemon_online(timeout_s=0.01, poll_s=0.01, since=t0))

    def test_online_state_from_the_restarted_daemon_counts(self):
        ctl = self._ctl()
        t0 = datetime.now(timezone.utc)
        ctl.store.update(state="SYNCING",
                         daemon_started_at=(t0 + timedelta(seconds=1)).isoformat())
        self.assertEqual(ctl._wait_daemon_online(timeout_s=0.01, poll_s=0.01, since=t0),
                         "SYNCING")

    def test_mark_restarting_replaces_the_old_state_when_no_daemon_runs(self):
        ctl = self._ctl()
        ctl.store.update(state="MOUNTED_IDLE")
        ctl._mark_restarting()
        self.assertEqual(ctl.store.get()["state"], "RESTARTING")


class DoctorSpaceIsMeasuredOutOfProcessTests(unittest.TestCase):
    """doctor's NAS numbers come from timed df/du children -- never an in-process
    statvfs or os.walk of the NAS trash (10+ minutes for 71k files)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_du_timeout_reports_unknown_instead_of_hanging(self):
        timed_out = RunResult(None, "", "", True)
        rec = _Recorder(results={paths.DU: timed_out}, default=_fail())
        ctl = _base_ctl(self.dir, run_cmd=rec)
        self.assertIsNone(ctl._du_bytes("/nas/MyCloud/.trash"))

    def test_df_parses_available_kib(self):
        df = ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
              "//admin@h/SHARE 732238844 125434628 606804216 18% /mnt\n")
        rec = _Recorder(results={paths.DF: _ok(df)}, default=_fail())
        ctl = _base_ctl(self.dir, run_cmd=rec)
        self.assertEqual(ctl._df_available_bytes("/mnt"), 606804216 * 1024)

    def test_missing_trash_is_zero(self):
        rec = _Recorder(results={paths.DU: _fail(rc=1, err="du: /x: No such file or directory")},
                        default=_fail())
        ctl = _base_ctl(self.dir, run_cmd=rec)
        self.assertEqual(ctl._du_bytes("/x"), 0)



class InstallRenderingRobustnessTests(unittest.TestCase):
    """re.sub replacement strings interpret backslashes: user input must go in
    literally (a CORP\\user login raised, a \\2024 folder was rewritten)."""

    TEMPLATE = ("[server]\nhost = 192.168.5.1\nshare = SHARE\nusername = admin\n"
                "[backup]\nfolders =\n    ~/Documents\n    ~/Desktop\nexclude =\n")

    def test_backslashes_and_group_refs_are_kept_literally(self):
        out = mycloudctl.Ctl._render_config_template(
            self.TEMPLATE, "nas\\1", "SH\\g<0>", "CORP\\user", "user",
            ["/Users/x/Docs\\2024"])
        self.assertIn("host = nas\\1\n", out)
        self.assertIn("share = SH\\g<0>\n", out)
        self.assertIn("username = CORP\\user\nauth_mode = user\n", out)
        self.assertIn("    /Users/x/Docs\\2024\n", out)
        self.assertNotIn("~/Desktop", out)


class UninstallInFlightMountTests(InstallUninstallTests):
    """A mount_smbfs the daemon already started runs in its own session and can
    land the share on <APP>/mnt after any check -- uninstall must wait it out,
    and delete nothing if it will not end (2026-09-25 review, reproduced with a
    disk image: the old flow emptied the "share")."""

    def test_in_flight_mount_blocks_the_uninstall(self):
        ctl = self._ctl()
        self.assertEqual(ctl.cmd_install(self.backend, "192.168.5.1", "SHARE",
                                         "anonymous", folders=[self.src]), 0)
        mnt = self.mnt

        def router(argv):
            if argv[0] == "/bin/ps":
                return _ok("/sbin/mount_smbfs -N -o soft,nobrowse //guest@h/SHARE %s\n" % mnt)
            return _ok()
        rec = _Recorder(default=router)
        ctl2 = self._ctl(rec=rec)
        rc = ctl2.cmd_uninstall()
        self.assertEqual(rc, 1)
        self.assertIn("未删除任何文件", self._payload(ctl2)["error"])
        self.assertTrue(os.path.isdir(self.app))
        self.assertTrue(os.path.exists(os.path.join(self.agents, "com.mycloud.agent.plist")))


class AnonymousToUserNeedsPasswordTests(unittest.TestCase):
    setUp = ServerSetTests.setUp
    tearDown = ServerSetTests.tearDown
    _ctl = ServerSetTests._ctl
    _payload = ServerSetTests._payload

    def test_switching_to_user_mode_without_a_stored_password_is_refused(self):
        # config becomes anonymous; nsmb.conf then has no password for the host
        ctl = self._ctl(ServerSetTests._rec())
        ctl.cmd_server_set("192.168.5.1", "SHARE", "anonymous")      # go anonymous
        ctl2 = self._ctl(ServerSetTests._rec())
        rc = ctl2.cmd_server_set("192.168.5.1", "SHARE", "user", username="admin")
        self.assertEqual(rc, 1)
        self.assertIn("需要输入密码", self._payload(ctl2)["error"])

    def test_switching_to_user_mode_with_a_password_is_fine(self):
        ctl = self._ctl(ServerSetTests._rec())
        ctl.cmd_server_set("192.168.5.1", "SHARE", "anonymous")
        ctl2 = self._ctl(ServerSetTests._rec())
        rc = ctl2.cmd_server_set("192.168.5.1", "SHARE", "user", username="admin",
                                 password="pw")
        self.assertEqual(rc, 0, ctl2.out.getvalue())


class RerunCapabilityProbeTests(unittest.TestCase):
    """A probe key added by an upgrade must not block re-enabling deletion
    propagation forever (the old whole-string comparison never matched)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_new_probe_key_does_not_block_re_enabling(self):
        legacy = {"backup_moves_overwritten": True, "delete_suppressed_with_backup": True,
                  "deleting_line_format_ok": True}
        now = dict(legacy, inplace_updates_ok=True)
        ctl = _base_ctl(self.dir, cap_probe=lambda tmp, rsync: dict(now),
                        run_cmd=_Recorder(default=_fail(rc=3)))   # agent not loaded
        ctl.store.update(deletion_disabled=True,
                         capability_fingerprint=json.dumps(legacy, sort_keys=True))
        res = ctl._fix_rerun_capability_probe()
        self.assertTrue(res["ok"], res)
        self.assertFalse(ctl.store.get()["deletion_disabled"])

    def test_a_changed_behavior_still_keeps_deletion_disabled(self):
        legacy = {"backup_moves_overwritten": True, "deleting_line_format_ok": True}
        now = {"backup_moves_overwritten": False, "deleting_line_format_ok": True}
        ctl = _base_ctl(self.dir, cap_probe=lambda tmp, rsync: dict(now),
                        run_cmd=_Recorder(default=_fail(rc=3)))
        ctl.store.update(deletion_disabled=True,
                         capability_fingerprint=json.dumps(legacy, sort_keys=True))
        res = ctl._fix_rerun_capability_probe()
        self.assertFalse(res["ok"])
        self.assertIn("backup_moves_overwritten", res["detail"])
        self.assertTrue(ctl.store.get()["deletion_disabled"])


class PowerSleepCheckTests(unittest.TestCase):
    """An always-on gateway must not idle-sleep on AC power: every sleep kills
    the SMB session and fails the pass in flight (2026-09-25 review)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _ctl(self, batt, cfg):
        rec = _Recorder(default=lambda argv: (
            _ok(batt) if argv[:3] == [paths.PMSET, "-g", "batt"]
            else _ok(cfg) if argv[:2] == [paths.PMSET, "-g"] else _fail()))
        return _base_ctl(self.dir, run_cmd=rec)

    AC = "Now drawing from 'AC Power'\n"
    BATT = "Now drawing from 'Battery Power'\n"

    def test_ac_with_idle_sleep_fails_with_a_fix(self):
        ok, detail, hint = self._ctl(self.AC, " sleep                10\n")._power_sleep_status()
        self.assertFalse(ok)
        self.assertIn("10", detail)
        self.assertIn("pmset -c sleep 0", hint)

    def test_sleep_zero_or_disabled_passes(self):
        self.assertTrue(self._ctl(self.AC, " sleep                0\n")._power_sleep_status()[0])
        self.assertTrue(self._ctl(self.AC, " sleep 1\n SleepDisabled\t\t1\n")._power_sleep_status()[0])

    def test_battery_is_not_a_fault(self):
        self.assertTrue(self._ctl(self.BATT, " sleep 1\n")._power_sleep_status()[0])

    def test_unreadable_settings_are_not_a_fault(self):
        ctl = _base_ctl(self.dir, run_cmd=_Recorder(default=_fail()))
        self.assertTrue(ctl._power_sleep_status()[0])


class HeldTotalTests(unittest.TestCase):
    """The held list is capped at 1000 per folder; approvals release them ALL,
    so counts shown to the user must use held_total (2026-09-25 review)."""

    def test_held_of_prefers_the_real_total(self):
        count, paths_list = mycloudctl._held_of(
            {"held_deletions": ["a"] * 1000, "held_total": 60000})
        self.assertEqual(count, 60000)
        self.assertEqual(len(paths_list), 1000)

    def test_held_of_without_total_uses_the_list(self):
        self.assertEqual(mycloudctl._held_of({"held_deletions": ["a", "b"]})[0], 2)
