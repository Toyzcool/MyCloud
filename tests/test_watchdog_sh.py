import tests._path  # noqa: F401

import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "install", "watchdog.sh")


class WatchdogScriptTests(unittest.TestCase):
    """install/watchdog.sh must tell "not running" from "running but not
    syncing": offline/recovery loops refresh state.json every 30s, so file age
    alone never noticed a daemon that could not sync (2026-09-25 review)."""

    def setUp(self):
        self.app = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.app, "runtime"))
        self.state = os.path.join(self.app, "runtime", "state.json")
        with open(_SCRIPT, encoding="utf-8") as h:
            rendered = h.read().replace("__APP__", self.app)
        self.script = os.path.join(self.app, "watchdog.sh")
        with open(self.script, "w", encoding="utf-8") as h:
            h.write(rendered)

    def _run(self):
        env = dict(os.environ, WATCHDOG_DRY_RUN="1")
        out = subprocess.run(["/bin/sh", self.script], env=env, timeout=30,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        return out.decode("utf-8")

    def _write(self, last_ok=None, mtime_age=0):
        doc = {"state": "OFFLINE", "last_sync_ok_at": last_ok}
        with open(self.state, "w", encoding="utf-8") as h:
            json.dump(doc, h, indent=2)
        if mtime_age:
            t = time.time() - mtime_age
            os.utime(self.state, (t, t))

    def _iso(self, ago):
        return (datetime.now(timezone.utc) - ago).isoformat()

    def test_running_but_not_synced_for_days_is_reported(self):
        self._write(last_ok=self._iso(timedelta(days=5)))
        self.assertIn("没有成功同步", self._run())

    def test_recent_sync_is_quiet(self):
        self._write(last_ok=self._iso(timedelta(hours=1)))
        self.assertEqual(self._run(), "")

    def test_never_synced_only_checks_liveness(self):
        self._write(last_ok=None)
        self.assertEqual(self._run(), "")

    def test_stale_state_file_means_not_running(self):
        self._write(last_ok=self._iso(timedelta(hours=1)), mtime_age=3 * 86400)
        self.assertIn("没有运行", self._run())

    def test_missing_state_file_means_not_running(self):
        self.assertIn("没有运行", self._run())
