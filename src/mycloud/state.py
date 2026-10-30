"""Persistent daemon state store.

Backing files inside the runtime directory:
  state.json      current state document (atomically rewritten)
  journal.ndjson  append-only sync journal, one JSON object per line

Default state schema::

    {
      "state": <str>,                    # top-level state-machine value
      "updated_at": <ISO-8601 UTC str>,  # last time state.json was written
      "last_sync_ok_at": <ISO str|null>,
      "last_error": <str|null>,
      "deletion_disabled": <bool>,       # capability fail-closed flag
      "capability_fingerprint": <str|null>,
      "initial_sync": <bool>,
      "notify_ts": {<category>: <ISO str>},
      "folders": {
        <name>: {
          "state": <str>,
          "last_ok": <ISO str|null>,
          "files_copied": <int>,
          "files_trashed": <int>,
          "held_deletions": [<str>, ...],   # relpaths currently held (mycloudctl
                                            # reads this list; [] when nothing held)
          "failed_files": [<str>, ...],
          "orphaned": <bool>,
          "st_dev": <int|null>
        }
      }
    }
"""

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from . import util


def _utc_now_iso():
    # type: () -> str
    return datetime.now(timezone.utc).isoformat()


def _default_state():
    # type: () -> Dict[str, Any]
    return {
        "state": "OFFLINE",
        "updated_at": None,
        "last_sync_ok_at": None,
        "last_error": None,
        "deletion_disabled": False,
        "capability_fingerprint": None,
        "initial_sync": False,
        "notify_ts": {},
        "folders": {},
    }


# journal.ndjson is append-only; keep the live file bounded (one rotated copy).
_JOURNAL_MAX_BYTES = 5 * 2 ** 20


class StateStore(object):
    """Reader/writer for the daemon state document, safe across threads.

    update()/folder_update() are read-modify-write of the WHOLE document, and
    the daemon writes it from several threads at once: the main loop (folder
    results, state transitions), the upload workers (throttled progress), the
    progress heartbeat (re-stamps) and the guard watchdog (guard failure info).
    Without a lock, a writer that read the document before another thread's
    write silently overwrote it -- a folder's result, last_sync_ok_at or the
    state itself could revert. Readers need no lock: every write is an atomic
    rename, so get() always sees a complete document.
    """

    def __init__(self, runtime_dir, logger=None):
        # type: (Any, Any) -> None
        self.runtime_dir = runtime_dir
        self.logger = logger
        self._lock = threading.RLock()
        self.state_path = os.path.join(os.fspath(runtime_dir), "state.json")
        self.journal_path = os.path.join(os.fspath(runtime_dir), "journal.ndjson")

    def _warn(self, msg):
        # type: (str) -> None
        if self.logger is not None:
            try:
                self.logger.warning(msg)
            except Exception:
                pass

    def get(self):
        # type: () -> Dict[str, Any]
        """Read the current state fresh from disk.

        A missing or corrupt file yields a fresh default document (and a
        warning when a logger is present).
        """
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return _default_state()
        except (OSError, ValueError):
            self._warn("state.json unreadable or corrupt; using fresh default")
            return _default_state()
        if not isinstance(data, dict):
            self._warn("state.json is not an object; using fresh default")
            return _default_state()
        base = _default_state()
        base.update(data)
        if not isinstance(base.get("folders"), dict):
            base["folders"] = {}
        if not isinstance(base.get("notify_ts"), dict):
            base["notify_ts"] = {}
        return base

    def update(self, **fields):
        # type: (Any) -> Dict[str, Any]
        """Shallow-merge fields into the top level, stamp updated_at, persist."""
        with self._lock:
            data = self.get()
            data.update(fields)
            data["updated_at"] = _utc_now_iso()
            util.atomic_write_json(self.state_path, data)
            return data

    def folder_update(self, name, **fields):
        # type: (str, Any) -> Dict[str, Any]
        """Shallow-merge fields into folders[name], stamp updated_at, persist."""
        with self._lock:
            data = self.get()
            folders = data.get("folders")
            if not isinstance(folders, dict):
                folders = {}
                data["folders"] = folders
            entry = folders.get(name)
            if not isinstance(entry, dict):
                entry = {}
            entry.update(fields)
            folders[name] = entry
            data["updated_at"] = _utc_now_iso()
            util.atomic_write_json(self.state_path, data)
            return data

    def journal(self, record):
        # type: (Dict[str, Any]) -> None
        """Append one JSON line to the journal with an added ISO "ts" field."""
        entry = dict(record)
        entry["ts"] = _utc_now_iso()
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with self._lock:
            try:
                if os.path.getsize(self.journal_path) > _JOURNAL_MAX_BYTES:
                    os.replace(self.journal_path, self.journal_path + ".1")
            except OSError:
                pass
            with open(self.journal_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
