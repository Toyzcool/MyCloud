"""Logging setup, rate-limited macOS notifications, and TCC self-test.

PLAN sections 6.6 (TCC / local-network permissions) and 6.7 (observability).
Python 3.9 stdlib only.
"""

import contextlib
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple

from . import paths
from . import util

_LOG_BYTES = 5 * 2 ** 20
_LOG_BACKUPS = 5
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Exact remediation shown when launchd's python3 lacks Full Disk Access.
FDA_REMEDIATION = (
    "System Settings > Privacy & Security > Full Disk Access > add /usr/bin/python3"
)
_FDA_DEEP_LINK = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"


def setup_logging(logdir, name="mycloud", level=logging.INFO):
    # type: (Any, str, int) -> logging.Logger
    """Configure a rotating file logger plus stderr; idempotent.

    Calling twice must not duplicate handlers, so handlers are tagged and only
    added once per logger.
    """
    os.makedirs(os.fspath(logdir), exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    existing = getattr(logger, "_mycloud_handlers", None)
    if existing:
        return logger

    formatter = logging.Formatter(_LOG_FORMAT)

    file_handler = RotatingFileHandler(
        os.path.join(os.fspath(logdir), "mycloud.log"),
        maxBytes=_LOG_BYTES,
        backupCount=_LOG_BACKUPS,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    import sys
    try:
        interactive = sys.stderr.isatty()
    except (AttributeError, ValueError):
        interactive = False
    if interactive:
        # Under launchd, stderr IS launchd.err.log (StandardErrorPath): mirroring
        # every record there doubled the log into a file nothing ever rotates.
        # That file keeps what only it can hold -- tracebacks and SIGINFO dumps.
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    logger._mycloud_handlers = True  # type: ignore[attr-defined]
    return logger


class Notifier(object):
    """Rate-limited macOS notifications via osascript.

    Timestamps are stored in the state document's notify_ts map keyed by
    category. now_fn / runner are injectable for testing.
    """

    def __init__(self, state_store, now_fn=time.time, runner=util.run, logger=None):
        # type: (Any, Any, Any, Any) -> None
        self.state_store = state_store
        self.now_fn = now_fn
        self.runner = runner
        self.logger = logger

    def _log_warning(self, msg):
        # type: (str) -> None
        if self.logger is not None:
            try:
                self.logger.warning(msg)
            except Exception:
                pass

    def notify(self, cls, title, body, min_hours=6.0, force=False):
        # type: (str, str, str, float, bool) -> bool
        """Send a notification unless one of this category fired recently.

        Returns True if a send was attempted, False if rate-limited away.
        Never raises: an osascript failure logs a warning but the attempt time
        is still recorded so the rate limit still applies.
        """
        now = self.now_fn()
        state = self.state_store.get()
        notify_ts = state.get("notify_ts")
        if not isinstance(notify_ts, dict):
            notify_ts = {}

        if not force:
            last = notify_ts.get(cls)
            if isinstance(last, (int, float)) and (now - last) < min_hours * 3600.0:
                return False

        # Pass title/body as osascript run-arguments instead of interpolating
        # them into the AppleScript source. The previous json.dumps() used its
        # default ensure_ascii=True, escaping CJK as \uXXXX -- which AppleScript
        # cannot parse ("syntax error: expected \" but found unknown token" on
        # EVERY Chinese notification, verified on real hardware 2026-07-25). As
        # `on run argv` values the text needs no escaping and any character (CJK,
        # quotes, corner brackets, newlines) is delivered verbatim.
        cmd = [
            paths.OSASCRIPT,
            "-e",
            "on run argv\n"
            "display notification (item 1 of argv) with title (item 2 of argv)\n"
            "end run",
            body,
            title,
        ]
        # Track whether the toast actually displayed. In the launchd-agent
        # context osascript "display notification" is commonly refused (observed
        # rc=1), so on ANY failure we log the captured stderr AND the full
        # intended title+body at WARNING -- the information then reaches the user
        # via the log tail / GUI even when the toast cannot show. (No new delivery
        # mechanism is added; this is a logging fallback only.)
        reason = None  # type: Optional[str]
        try:
            result = self.runner(cmd, 10)
            if getattr(result, "timed_out", False):
                reason = "timed out"
            elif getattr(result, "rc", 0):
                err = (getattr(result, "err", "") or "").strip().replace("\n", " ")
                reason = "rc=%s" % (result.rc,)
                if err:
                    reason += " stderr=%r" % (err[:300],)
        except Exception as exc:
            reason = "raised: %s" % (exc,)
        if reason is not None:
            self._log_warning(
                "osascript notification failed (%s, cls=%s); could not display "
                "the toast -- logging its content instead: [%s] %s"
                % (reason, cls, title, body))

        merged = dict(notify_ts)
        merged[cls] = now
        try:
            self.state_store.update(notify_ts=merged)
        except Exception as exc:
            self._log_warning("could not record notify timestamp: %s" % exc)
        return True


def tcc_selftest(folders, notifier, logger, settings_opened):
    # type: (List[Any], Notifier, Any, List[bool]) -> Tuple[List[Any], List[Any]]
    """Probe each source folder for Full Disk Access.

    A single os.scandir step per folder is enough to trigger a TCC denial.
    PermissionError means FDA is missing: log remediation, notify, and open the
    settings deep link once per session (guarded by the mutable settings_opened
    flag). FileNotFoundError means the source is simply gone. Returns
    (accessible, denied).
    """
    accessible = []  # type: List[Any]
    denied = []  # type: List[Any]

    for folder in folders:
        src = str(folder.src)
        try:
            with contextlib.closing(os.scandir(src)) as it:
                next(it, None)
        except PermissionError:
            denied.append(folder)
            logger.error(
                "Full Disk Access denied for %s (%s). Fix: %s",
                folder.name, src, FDA_REMEDIATION)
            notifier.notify(
                "tcc",
                "MyCloud needs Full Disk Access",
                "Grant /usr/bin/python3 Full Disk Access to back up "
                + folder.name + ".")
            if not settings_opened:
                settings_opened.append(True)
                try:
                    notifier.runner([paths.OPEN, _FDA_DEEP_LINK], 10)
                except Exception as exc:
                    logger.warning("could not open privacy settings: %s", exc)
        except FileNotFoundError:
            denied.append(folder)
            logger.warning(
                "Source folder missing for %s: %s", folder.name, src)
        except Exception:
            accessible.append(folder)
        else:
            accessible.append(folder)

    return accessible, denied
