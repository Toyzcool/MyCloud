import tests._path  # noqa: F401

import json
import logging
import os
import tempfile
import unittest
from unittest import mock

from mycloud import notify
from mycloud import paths
from mycloud.state import StateStore
from mycloud.util import RunResult


class _Folder(object):
    def __init__(self, name, src):
        self.name = name
        self.src = src


class _RecLogger(object):
    """Captures WARNING messages (Notifier._log_warning passes a single string)."""

    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args):
        self.warnings.append((msg % args) if args else msg)


def _ok_result():
    return RunResult(0, "", "", False)


class SetupLoggingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_creates_logdir_and_handlers(self):
        logdir = os.path.join(self.dir, "nested", "logs")
        logger = notify.setup_logging(logdir, name="mycloud_test1")
        self.assertTrue(os.path.isdir(logdir))
        import sys
        expected = 2 if sys.stderr.isatty() else 1   # stderr mirror only when interactive
        self.assertEqual(len(logger.handlers), expected)
        self.assertTrue(os.path.exists(os.path.join(logdir, "mycloud.log")))

    def test_idempotent_no_duplicate_handlers(self):
        logdir = os.path.join(self.dir, "logs2")
        a = notify.setup_logging(logdir, name="mycloud_test2")
        count = len(a.handlers)
        b = notify.setup_logging(logdir, name="mycloud_test2")
        self.assertIs(a, b)
        self.assertEqual(len(b.handlers), count)
        import sys
        self.assertEqual(count, 2 if sys.stderr.isatty() else 1)


class NotifierRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = StateStore(self.dir)
        self.calls = []
        self.clock = {"t": 1000.0}

    def _now(self):
        return self.clock["t"]

    def _runner(self, cmd, timeout):
        self.calls.append((cmd, timeout))
        return _ok_result()

    def _make(self):
        return notify.Notifier(self.store, now_fn=self._now, runner=self._runner)

    def test_first_sends_second_within_window_skips(self):
        n = self._make()
        self.assertTrue(n.notify("err", "T", "B", min_hours=6.0))
        self.assertEqual(len(self.calls), 1)
        # 5 hours later: still within 6h window -> skip.
        self.clock["t"] += 5 * 3600
        self.assertFalse(n.notify("err", "T", "B", min_hours=6.0))
        self.assertEqual(len(self.calls), 1)

    def test_force_overrides_window(self):
        n = self._make()
        n.notify("err", "T", "B", min_hours=6.0)
        self.clock["t"] += 60
        self.assertTrue(n.notify("err", "T", "B", min_hours=6.0, force=True))
        self.assertEqual(len(self.calls), 2)

    def test_after_window_sends_again(self):
        n = self._make()
        n.notify("err", "T", "B", min_hours=6.0)
        self.clock["t"] += 7 * 3600
        self.assertTrue(n.notify("err", "T", "B", min_hours=6.0))
        self.assertEqual(len(self.calls), 2)

    def test_per_category_independence(self):
        n = self._make()
        self.assertTrue(n.notify("tcc", "T", "B"))
        self.assertTrue(n.notify("offline", "T", "B"))
        self.assertEqual(len(self.calls), 2)
        # Both categories now rate-limited.
        self.assertFalse(n.notify("tcc", "T", "B"))
        self.assertFalse(n.notify("offline", "T", "B"))
        self.assertEqual(len(self.calls), 2)


class NotifierArgvTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = StateStore(self.dir)
        self.calls = []

    def _runner(self, cmd, timeout):
        self.calls.append((cmd, timeout))
        return _ok_result()

    def test_osascript_passes_title_body_as_run_args(self):
        # Regression (real hardware 2026-07-25): title/body must be osascript
        # run-ARGUMENTS, not interpolated into the AppleScript source. The old
        # json.dumps() escaped CJK as \uXXXX, which AppleScript cannot parse --
        # every Chinese notification failed with "syntax error: expected \" but
        # found unknown token".
        n = notify.Notifier(self.store, now_fn=lambda: 0.0, runner=self._runner)
        title = 'my "title"「本地网络」'
        body = "探测失败：这几乎可以肯定是权限拦截\n换行"
        n.notify("err", title, body)
        cmd, timeout = self.calls[0]
        self.assertEqual(cmd[0], paths.OSASCRIPT)
        self.assertEqual(cmd[1], "-e")
        self.assertEqual(timeout, 10)
        script = cmd[2]
        self.assertIn("on run argv", script)
        self.assertIn("display notification (item 1 of argv)", script)
        self.assertIn("with title (item 2 of argv)", script)
        # Text is delivered verbatim as run-args (item 1 = body, item 2 = title).
        self.assertEqual(cmd[3], body)
        self.assertEqual(cmd[4], title)
        # The raw CJK text must NOT leak into the AppleScript source (that is
        # exactly what broke parsing before).
        self.assertNotIn("探测", script)
        self.assertNotIn("本地网络", script)

    def test_failure_still_records_and_never_raises(self):
        def failing_runner(cmd, timeout):
            self.calls.append((cmd, timeout))
            return RunResult(1, "", "boom", False)

        n = notify.Notifier(self.store, now_fn=lambda: 5000.0, runner=failing_runner)
        # Returns True (attempted) and does not raise.
        self.assertTrue(n.notify("err", "T", "B"))
        # Attempt time recorded -> next call within window is rate-limited.
        n2 = notify.Notifier(self.store, now_fn=lambda: 5001.0, runner=failing_runner)
        self.assertFalse(n2.notify("err", "T", "B"))

    def test_runner_exception_swallowed(self):
        def boom_runner(cmd, timeout):
            raise OSError("no osascript")

        n = notify.Notifier(self.store, now_fn=lambda: 1.0, runner=boom_runner)
        self.assertTrue(n.notify("err", "T", "B"))
        # Still recorded.
        self.assertIn("err", self.store.get()["notify_ts"])

    def test_failure_logs_stderr_and_full_content(self):
        # osascript display-notification is refused in the launchd context
        # (observed rc=1). On failure notify() must capture the stderr snippet
        # AND log the full intended title+body at WARNING so the information
        # still reaches the user via the log even when the toast cannot display.
        logger = _RecLogger()

        def failing(cmd, timeout):
            return RunResult(1, "", "SIMULATED-OSASCRIPT-STDERR-SNIPPET", False)

        n = notify.Notifier(self.store, now_fn=lambda: 1.0, runner=failing,
                            logger=logger)
        self.assertTrue(n.notify("prefetch_blocked", "MyCloud 通知标题", "云端文件正文说明"))
        joined = " ".join(logger.warnings)
        self.assertIn("rc=1", joined)
        self.assertIn("SIMULATED-OSASCRIPT-STDERR-SNIPPET", joined)  # stderr captured
        self.assertIn("MyCloud 通知标题", joined)                     # full title logged
        self.assertIn("云端文件正文说明", joined)                      # full body logged

    def test_timeout_logs_full_content(self):
        logger = _RecLogger()

        def timing_out(cmd, timeout):
            return RunResult(None, "", "", True)

        n = notify.Notifier(self.store, now_fn=lambda: 1.0, runner=timing_out,
                            logger=logger)
        n.notify("cls", "Title", "Body")
        joined = " ".join(logger.warnings)
        self.assertIn("timed out", joined)
        self.assertIn("Title", joined)
        self.assertIn("Body", joined)

    def test_success_logs_nothing(self):
        logger = _RecLogger()
        n = notify.Notifier(self.store, now_fn=lambda: 1.0,
                            runner=lambda c, t: RunResult(0, "", "", False),
                            logger=logger)
        n.notify("cls", "Title", "Body")
        self.assertEqual(logger.warnings, [])   # no fallback warning on success


class TccSelftestTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = StateStore(self.dir)
        self.open_calls = []
        self.notify_calls = []

    def _runner(self, cmd, timeout):
        self.open_calls.append((cmd, timeout))
        return _ok_result()

    def _notifier(self):
        n = notify.Notifier(self.store, now_fn=lambda: 0.0, runner=self._runner)
        orig = n.notify

        def spy(cls, title, body, min_hours=6.0, force=False):
            self.notify_calls.append(cls)
            return orig(cls, title, body, min_hours=min_hours, force=force)

        n.notify = spy
        return n

    def test_permission_error_denied_and_settings_opened_once(self):
        logger = logging.getLogger("tcc_test")
        logger.addHandler(logging.NullHandler())
        folders = [_Folder("Documents", "/x/Documents")]
        notifier = self._notifier()
        settings_opened = []

        with mock.patch.object(notify.os, "scandir", side_effect=PermissionError):
            acc1, den1 = notify.tcc_selftest(folders, notifier, logger, settings_opened)
            acc2, den2 = notify.tcc_selftest(folders, notifier, logger, settings_opened)

        self.assertEqual(acc1, [])
        self.assertEqual([f.name for f in den1], ["Documents"])
        self.assertEqual([f.name for f in den2], ["Documents"])
        # open (settings deep link) invoked exactly once across two calls.
        open_invocations = [c for c in self.open_calls if c[0][0] == paths.OPEN]
        self.assertEqual(len(open_invocations), 1)
        self.assertEqual(open_invocations[0][0][0], paths.OPEN)

    def test_file_not_found_denied_no_settings(self):
        logger = logging.getLogger("tcc_test2")
        logger.addHandler(logging.NullHandler())
        folders = [_Folder("Gone", "/x/Gone")]
        notifier = self._notifier()
        settings_opened = []

        with mock.patch.object(notify.os, "scandir", side_effect=FileNotFoundError):
            acc, den = notify.tcc_selftest(folders, notifier, logger, settings_opened)

        self.assertEqual(acc, [])
        self.assertEqual([f.name for f in den], ["Gone"])
        self.assertEqual(settings_opened, [])
        open_invocations = [c for c in self.open_calls if c[0][0] == paths.OPEN]
        self.assertEqual(open_invocations, [])

    def test_accessible_folder(self):
        logger = logging.getLogger("tcc_test3")
        logger.addHandler(logging.NullHandler())
        src = tempfile.mkdtemp()
        with open(os.path.join(src, "f.txt"), "w") as h:
            h.write("hi")
        folders = [_Folder("Real", src)]
        notifier = self._notifier()
        acc, den = notify.tcc_selftest(folders, notifier, logger, [])
        self.assertEqual([f.name for f in acc], ["Real"])
        self.assertEqual(den, [])
        self.assertEqual(self.notify_calls, [])

    def test_mixed_folders(self):
        logger = logging.getLogger("tcc_test4")
        logger.addHandler(logging.NullHandler())
        good = tempfile.mkdtemp()
        folders = [_Folder("Bad", "/x/Bad"), _Folder("Good", good)]
        notifier = self._notifier()
        settings_opened = []

        real_scandir = notify.os.scandir

        def fake_scandir(path):
            if path == "/x/Bad":
                raise PermissionError()
            return real_scandir(path)

        with mock.patch.object(notify.os, "scandir", side_effect=fake_scandir):
            acc, den = notify.tcc_selftest(folders, notifier, logger, settings_opened)

        self.assertEqual([f.name for f in den], ["Bad"])
        self.assertEqual([f.name for f in acc], ["Good"])


if __name__ == "__main__":
    unittest.main()


class LaunchdStderrTests(unittest.TestCase):
    """Under launchd stderr IS launchd.err.log, which nothing rotates: every
    record used to be written twice, once into that file."""

    def test_no_stderr_mirror_when_not_interactive(self):
        import io as _io
        import logging
        import sys
        from unittest import mock
        logdir = tempfile.mkdtemp()
        fake = _io.StringIO()   # not a tty
        with mock.patch.object(sys, "stderr", fake):
            logger = notify.setup_logging(logdir, name="mycloud.test.noscream")
            logger.warning("hello")
        self.assertEqual(fake.getvalue(), "")
        self.assertFalse(any(isinstance(h, logging.StreamHandler)
                             and getattr(h, "stream", None) is fake
                             for h in logger.handlers))

