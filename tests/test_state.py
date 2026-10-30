import tests._path  # noqa: F401

import json
import os
import tempfile
import unittest

from mycloud.state import StateStore


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = StateStore(self.dir)

    def test_get_default_when_missing(self):
        data = self.store.get()
        self.assertEqual(data["state"], "OFFLINE")
        self.assertEqual(data["folders"], {})
        self.assertEqual(data["notify_ts"], {})
        self.assertFalse(data["deletion_disabled"])

    def test_update_shallow_merge_and_timestamp(self):
        self.store.update(state="SYNCING", last_error="boom")
        data = self.store.get()
        self.assertEqual(data["state"], "SYNCING")
        self.assertEqual(data["last_error"], "boom")
        self.assertIsNotNone(data["updated_at"])
        # Second update preserves prior fields.
        self.store.update(state="MOUNTED_IDLE")
        data = self.store.get()
        self.assertEqual(data["state"], "MOUNTED_IDLE")
        self.assertEqual(data["last_error"], "boom")

    def test_folder_update(self):
        self.store.folder_update("Documents", state="upToDate", files_copied=5)
        self.store.folder_update("Documents", files_copied=7)
        self.store.folder_update("Desktop", state="syncing")
        data = self.store.get()
        self.assertEqual(data["folders"]["Documents"]["state"], "upToDate")
        self.assertEqual(data["folders"]["Documents"]["files_copied"], 7)
        self.assertEqual(data["folders"]["Desktop"]["state"], "syncing")

    def test_journal_appends_valid_json_lines(self):
        self.store.journal({"event": "sync", "files": 3})
        self.store.journal({"event": "prune", "removed": 1})
        with open(self.store.journal_path, encoding="utf-8") as h:
            lines = [ln for ln in h.read().splitlines() if ln]
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["event"], "sync")
        self.assertIn("ts", first)
        second = json.loads(lines[1])
        self.assertEqual(second["event"], "prune")
        self.assertIn("ts", second)

    def test_corrupt_state_tolerated(self):
        with open(self.store.state_path, "w", encoding="utf-8") as h:
            h.write("{corrupt")
        data = self.store.get()
        self.assertEqual(data["state"], "OFFLINE")
        # An update recovers to a valid document.
        self.store.update(state="DEGRADED")
        self.assertEqual(self.store.get()["state"], "DEGRADED")

    def test_non_object_state_tolerated(self):
        with open(self.store.state_path, "w", encoding="utf-8") as h:
            h.write("[1, 2, 3]")
        data = self.store.get()
        self.assertEqual(data["state"], "OFFLINE")


if __name__ == "__main__":
    unittest.main()


class ConcurrentWriterTests(unittest.TestCase):
    """update()/folder_update() are read-modify-write of the whole document and
    the daemon calls them from several threads (main loop, upload workers,
    progress heartbeat, guard watchdog). Without a lock one writer overwrote
    another's fields -- a folder result or last_sync_ok_at could revert."""

    def test_concurrent_writers_never_lose_a_field(self):
        import threading
        d = tempfile.mkdtemp()
        store = StateStore(d)
        n = 60
        errors = []

        def folder_writer():
            try:
                for i in range(n):
                    store.folder_update("F%d" % i, files_copied=i)
            except Exception as exc:   # pragma: no cover - surfaced below
                errors.append(exc)

        def top_writer():
            try:
                for i in range(n):
                    store.update(**{"k%d" % i: i})
            except Exception as exc:   # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=folder_writer),
                   threading.Thread(target=top_writer),
                   threading.Thread(target=lambda: [store.update(progress={"i": i})
                                                    for i in range(n)])]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        doc = store.get()
        for i in range(n):
            self.assertEqual(doc["folders"]["F%d" % i]["files_copied"], i)
            self.assertEqual(doc["k%d" % i], i)
