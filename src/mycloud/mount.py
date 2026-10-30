"""SMB mount manager and the four-layer health guard (PLAN section 6.2).

The private mount point (<APP>/mnt) is never /Volumes; the bare directory is
kept at chmod 0500 while unmounted so any stray write hits EACCES instead of
silently filling the boot disk. Guard checks that touch the mounted filesystem
run as timed child processes: a wedged SMB vnode hangs the child, never the
daemon.
"""

import logging
import os
import re
import socket
import urllib.parse
from pathlib import Path
from typing import Any, List, Optional, Tuple

from mycloud import paths, util


class MountError(Exception):
    """Programming/environment error in mount handling (e.g. caller bug)."""


class CriticalLocalWriteDetected(Exception):
    """Data was written into the dead mount point on the local disk.

    This means local files landed inside the bare directory while the share
    was not mounted. We must never delete them: notify and halt.
    """


def read_nsmb_password(host, conf_path=None, username=None):
    # type: (str, Optional[Path], Optional[str]) -> Optional[str]
    """Return the password for *host* from nsmb.conf, or None.

    On this macOS `mount_smbfs -N` does not deliver this file's password to the
    SMB auth exchange, so mount() falls back to embedding the password in the
    mount URL; this parser recovers it. Section matching is case-insensitive: an
    exact ``[<host>]`` wins, then any ``[<host>:USER]`` form, then ``[default]``
    as the weakest match. ``username=`` lines, blank lines, and lines whose
    first non-space char is ``;`` or ``#`` are ignored. A missing file, a
    missing matching section, or a missing/empty password key all yield None.

    A file that exists with permissions looser than 0600 is still read, but logs
    one warning (the warning names only the path and mode, never the password).
    """
    path = conf_path if conf_path is not None else paths.NSMB_CONF
    try:
        with open(path, "r", encoding="utf-8") as handle:
            try:
                mode = os.fstat(handle.fileno()).st_mode & 0o777
            except OSError:
                mode = -1
            if mode >= 0 and (mode & 0o177):
                logging.getLogger(__name__).warning(
                    "nsmb.conf %s has loose permissions %04o (expected 0600); "
                    "reading anyway", path, mode)
            text = handle.read()
    except OSError:
        return None

    # Map lowercased section name -> its (stripped) password= value. Only
    # sections that actually carry a password land here.
    passwords = {}  # type: dict
    current = None  # type: Optional[str]
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in ";#":
            continue
        if line[0] == "[" and line.endswith("]"):
            current = line[1:-1].strip().lower()
            continue
        if current is None or "=" not in line:
            continue
        key, _sep, value = line.partition("=")
        if key.strip().lower() == "password":
            passwords[current] = value.strip()

    host_l = host.lower()
    exact = passwords.get(host_l)
    prefix = None
    # A [host:USER] section for the configured user wins over any other
    # [host:*] section; without a username the first such section is used.
    if username:
        prefix = passwords.get(host_l + ":" + username.lower())
    if prefix is None:
        for name in passwords:
            if name.startswith(host_l + ":"):
                prefix = passwords[name]
                break
    default = passwords.get("default")
    # Empty strings are falsy, so an explicit ``password=`` (no value) falls
    # through to the next-weakest match, and "nothing usable" collapses to None.
    return exact or prefix or default or None


# mount_smbfs prints its URL argument verbatim in "mount error: <url>: <why>"
# on macOS 26/27 (verified by disassembly, 2026-09-25). For the embedded-
# credential fallback that URL carries the password, so every stderr that may
# reach a log goes through this first.
_URL_USERINFO = re.compile(r"(//[^/@\s:]*):[^@\s]*@")


_UNKNOWN_ERR_RE = re.compile(r"Unknown error: (-\d+)")


def _ntstatus(err):
    # type: (str) -> Optional[str]
    """"Unknown error: -1073741275" -> "0xC0000225" (a raw NTSTATUS)."""
    m = _UNKNOWN_ERR_RE.search(err or "")
    if not m:
        return None
    try:
        return "0x%08X" % (int(m.group(1)) & 0xFFFFFFFF)
    except ValueError:
        return None


def _mount_error_kind(rc, err):
    # type: (int, str) -> str
    """Classify a failed mount_smbfs by its stderr, not its exit code alone.

    eexist: "File exists" -- the share is already mounted in this session.
    auth: rc 77 / "Authentication error" -- the login was rejected.
    refused: rc 64 "mount error:" with a SERVER-side cause -- a raw NTSTATUS
        ("Unknown error: -N"), "No such file or directory" or "Permission
        denied": the login worked and the TREE CONNECT to the share was
        refused (share missing, disk not mounted on the router, permissions).
        Every rc 64 used to go to the EEXIST handler, which found nothing and
        gave up.
    other: anything else, including rc 64 with a local or network errno
        (mountpoint not ours, Resource busy, Socket is not connected right
        after a wake): mount_smbfs prints every mount-stage errno as
        "mount error: %s", and the raw text is the accurate message there.
    """
    text = err or ""
    if "File exists" in text:
        return "eexist"
    if rc == 77 or "Authentication error" in text:
        return "auth"
    if rc == 64 and "mount error" in text:
        if (_ntstatus(text) or "No such file or directory" in text
                or "Permission denied" in text):
            return "refused"
        return "other"
    if rc == 64:
        return "eexist"
    return "other"


def _redact(text):
    # type: (Optional[str]) -> str
    return _URL_USERINFO.sub(r"\1:***@", text or "")


_LIBC = None


def _c_realpath(path):
    # type: (str) -> Optional[str]
    """libc realpath(3). Unlike os.path.realpath it also canonicalises the CASE
    of every component on a case-insensitive volume -- which is how the kernel
    records a mountpoint in the mount table. None if unavailable."""
    global _LIBC
    try:
        if _LIBC is None:
            import ctypes
            import ctypes.util
            lib = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            lib.realpath.restype = ctypes.c_char_p
            lib.realpath.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
            _LIBC = (lib, ctypes)
        lib, ctypes = _LIBC
        buf = ctypes.create_string_buffer(4096)
        resolved = lib.realpath(os.fsencode(path), buf)
        return os.fsdecode(resolved) if resolved else None
    except Exception:
        return None


def _mountpoint_keys(mountpoint):
    # type: (Any) -> frozenset
    """Every spelling under which the mount table may list our mountpoint.

    The kernel records the CANONICAL path (symlinks resolved, case normalised,
    /tmp -> /private/tmp; verified with hdiutil on macOS 26). Only the parent
    is resolved -- the mountpoint itself may be the root of a wedged SMB mount,
    and resolving it would touch that. Compared case-insensitively (APFS
    default)."""
    raw = os.path.abspath(os.fspath(mountpoint))
    parent, leaf = os.path.split(raw)
    canon_parent = _c_realpath(parent) or os.path.realpath(parent)
    return frozenset(p.lower() for p in (raw, os.path.join(canon_parent, leaf)))


# "<from> on <mountpoint> (<fstype>, <options...>)" -- the from-name of autofs
# maps contains spaces and is simply not matched (never at our mountpoint).
_TABLE_LINE = re.compile(r"^(?P<frm>\S+) on (?P<mp>.+) \((?P<fs>[^,)]+)")
_SMB_FROM = re.compile(r"^//(?:(?P<user>[^@/]*)@)?(?P<host>[^/]+)/(?P<share>.+)$")


class MountManager(object):
    def __init__(self, host, share, username, mountpoint, sentinel_uuid, logger,
                 guest=False, password=None):
        # type: (str, str, str, Any, str, Any, bool, Optional[str]) -> None
        self.host = host
        # CANDIDATE password for a verification mount (server-set). Never
        # logged, never stored anywhere else; when set, mount() uses the
        # embedded-credential URL directly so the outcome reflects THIS password
        # and not a Keychain entry or a reused session the -N handshake rides on.
        self._password = password
        self.share = share
        self.username = username
        # Anonymous (SMB guest) session: `username` is the guest account and no
        # credential exists anywhere, so an auth rejection must not try the
        # nsmb.conf embedded-password fallback.
        self.guest = bool(guest)
        self.mountpoint = mountpoint
        self.sentinel_uuid = sentinel_uuid
        self.logger = logger
        # Why the most recent guard_ok() failed; doctor surfaces this.
        self.last_failure = ""
        # Mechanism of that failure (rc + stderr snippet / mismatch values).
        # 2026-08-01 gateway incident: the sentinel layer failed for 80 minutes
        # because launchd's python3 lacked Full Disk Access for network volumes
        # -- cat's "Operation not permitted" was discarded, so the log said
        # only "failing at layer 'sentinel'" and the TCC cause stayed invisible.
        self.last_failure_detail = ""
        # Why the most recent mount() attempt failed (redacted), for the GUI.
        self.last_mount_error = ""
        # "already_mounted" when mount() found our share already in place and
        # did not run mount_smbfs; callers use it to decide whether a failing
        # guard means "drop this session and remount".
        self.last_mount_note = ""
        self._mnt_keys = _mountpoint_keys(mountpoint)
        # Set once the -N handshake was rejected while the nsmb.conf credential
        # worked: later mounts in this process go straight to that credential.
        # On macOS 12 mount_smbfs never reads the Keychain at all, so -N fails
        # (rc=77, synthesized locally) on every attempt and only added noise.
        self._prefer_embedded = False

    def probe_tcp445(self, timeout=3.0):
        # type: (float) -> bool
        """TCP 445 reachability via a DIRECT socket. Never ping: the router
        blocks ICMP.

        On macOS 26 this direct connect() can be refused (EHOSTUNREACH, i.e.
        "No route to host") by the "Local Network" privacy gate when the calling
        process -- a launchd-started /usr/bin/python3 -- lacks a Local Network
        grant, EVEN THOUGH the SMB subsystem (mount_smbfs/smbutil, brokered by a
        system service) reaches the same host fine. So a False here does NOT
        prove the NAS is offline; use probe_reachable(), which adds the brokered
        fallback, for any "is the NAS there?" decision.
        """
        try:
            conn = socket.create_connection((self.host, 445), timeout=timeout)
        except OSError:
            return False
        try:
            conn.close()
        except OSError:
            pass
        return True

    def probe_smb_view(self, timeout=8.0):
        # type: (float) -> bool
        """Brokered reachability probe: `smbutil view -N //<user>@<host>`.

        Runs through the SMB subsystem (a system service), so it is NOT subject
        to the per-process "Local Network" block that can defeat probe_tcp445 on
        a launchd python3 (verified on real hardware 2026-07-24: smbutil listed
        the share 5/5 while direct python3/nc sockets got EHOSTUNREACH). -N means
        no interactive password prompt; merely reaching the server and getting a
        share listing proves reachability, which is all layer 1 needs -- mount
        identity and auth are validated by the mount + sentinel + statshares
        layers. The password never appears on argv; util.run bounds it with an
        unwedgeable kill path.
        """
        target = "//" + self.username + "@" + self.host
        result = util.run([paths.SMBUTIL, "view", "-N", target], timeout)
        if result.timed_out:
            return False
        if result.rc == 0:
            return True
        # An authentication rejection is an ANSWER from the server: it is
        # reachable. Treating it as "unreachable" gated the mount attempt off
        # (retried only every 5th backoff, ~18 min) exactly when launchd could
        # not read the Keychain (rc=77) -- although the nsmb.conf fallback would
        # have mounted fine.
        err = result.err or ""
        return (result.rc == 77 or "Authentication error" in err
                or "rejected the connection" in err)

    def probe_reachable(self, timeout=3.0):
        # type: (float) -> bool
        """Return True iff the NAS is reachable, tolerating the macOS 26 "Local
        Network" block on direct sockets.

        Fast path: the direct TCP-445 socket probe (instant when it works, the
        normal case on machines with the grant). Fallback: the brokered
        smbutil-view probe, tried only when the socket path fails, so a machine
        whose launchd python3 cannot obtain a Local Network grant still detects
        the NAS via the SMB subsystem that actually carries the backup. This is
        what lets the daemon come online without depending on a Local Network
        permission that a bare system python3 under launchd often cannot get.
        """
        if self.probe_tcp445(timeout=timeout):
            return True
        return self.probe_smb_view(timeout=max(timeout, 8.0))

    def _read_table(self, timeout=10):
        # type: (float) -> Optional[List[Tuple[str, str, str]]]
        """[(from, mountpoint, fstype)] for every mount, or None if unreadable.

        /sbin/mount lists with getmntinfo(MNT_NOWAIT) (verified by disassembly),
        so a wedged SMB server can never block this read.
        """
        try:
            result = util.run([paths.MOUNT], timeout)
        except OSError:
            return None
        if result.timed_out or result.rc != 0:
            return None
        rows = []
        for line in result.out.splitlines():
            m = _TABLE_LINE.match(line)
            if m:
                rows.append((m.group("frm"), m.group("mp"), m.group("fs").strip()))
        return rows

    def _is_our_share(self, frm):
        # type: (str) -> bool
        """Compare a smbfs from-name with our identity, field by field.

        The kernel rewrites the guest login as `//GUEST:@host/share` and
        percent-encodes non-ASCII / reserved characters, so each field is
        decoded, the trailing ':' of the empty password dropped, and the
        comparison made case-insensitively (SMB treats all three so).
        """
        m = _SMB_FROM.match(frm)
        if not m:
            return False
        uq = urllib.parse.unquote
        user = uq(m.group("user") or "").rstrip(":")
        return (user.lower() == self.username.lower()
                and uq(m.group("host")).lower() == self.host.lower()
                and uq(m.group("share")).lower() == self.share.lower())

    def mount_state(self, timeout=10):
        # type: (float) -> Optional[bool]
        """True if our share is mounted at our mountpoint, False if not, None
        if the mount table could not be read (unknown -- NOT "gone")."""
        rows = self._read_table(timeout)
        if rows is None:
            return None
        return any(fs == "smbfs" and mp.lower() in self._mnt_keys
                   and self._is_our_share(frm)
                   for frm, mp, fs in rows)

    def is_mounted(self, timeout=10):
        # type: (float) -> bool
        """True iff the mount table lists exactly our share at our mountpoint.

        Reads the kernel mount table (a local, load-independent operation), so
        the default 10s timeout is ample; the parameter lets the guard watchdog
        raise it while a sync is saturating the SMB link (PLAN 6.2 / A1).
        """
        return self.mount_state(timeout) is True

    def mounts_at_mountpoint(self, timeout=10):
        # type: (float) -> Optional[List[Tuple[str, str]]]
        """Every mount at our mountpoint -- any share, any filesystem -- as
        [(from, fstype)], top layer last; None if the table is unreadable."""
        rows = self._read_table(timeout)
        if rows is None:
            return None
        return [(frm, fs) for frm, mp, fs in rows if mp.lower() in self._mnt_keys]

    def same_share_elsewhere(self, timeout=10):
        # type: (float) -> List[str]
        """Mountpoints OTHER than ours where this machine has our share mounted.
        macOS refuses a second mount of a share that is already mounted in the
        same session ("File exists"), so these explain an EEXIST."""
        return [mp for frm, mp, fs in (self._read_table(timeout) or [])
                if fs == "smbfs" and self._is_our_share(frm)
                and mp.lower() not in self._mnt_keys]

    def sentinel_ok(self, timeout=10):
        # type: (float) -> bool
        """Read the sentinel file via a timed child process and match the UUID.

        cat runs as a child so a hung SMB vnode kills only the child. The timeout
        is a parameter (default 10s) so the guard watchdog can raise it to 30s
        while a full-throttle sync is starving the router's SMB stack, avoiding a
        false "mount dead" verdict under load (A1).
        """
        sentinel = self.mountpoint / paths.SENTINEL_REL
        result = util.run([paths.CAT, str(sentinel)], timeout)
        if result.timed_out:
            self.last_failure_detail = "cat timed out after %ss" % timeout
            return False
        if result.rc != 0:
            self.last_failure_detail = "cat rc=%s: %s" % (
                result.rc, (result.err or "").strip()[:120])
            return False
        if result.out.strip() != self.sentinel_uuid:
            self.last_failure_detail = (
                "uuid mismatch: share has %r, config expects %r"
                % (result.out.strip()[:60], self.sentinel_uuid))
            return False
        self.last_failure_detail = ""
        return True

    def session_ok(self, timeout=10):
        # type: (float) -> bool
        """Verify a live SMB session exists for the mountpoint.

        smbutil statshares runs as a timed child; the timeout is parameterized
        (default 10s) so the watchdog can raise it to 30s under sync load (A1).
        """
        result = util.run([paths.SMBUTIL, "statshares", "-m", str(self.mountpoint)], timeout)
        if result.timed_out:
            self.last_failure_detail = "statshares timed out after %ss" % timeout
            return False
        if result.rc != 0:
            self.last_failure_detail = "statshares rc=%s: %s" % (
                result.rc, (result.err or "").strip()[:120])
            return False
        # The share name heads its rows (padded with spaces); a bare substring
        # test matched a one-letter share against any output at all.
        want = self.share.lower()
        listed = any(
            line.strip().lower().startswith(want)
            and line.strip()[len(want):len(want) + 1] in ("", " ", "\t")
            for line in result.out.splitlines())
        if not listed:
            self.last_failure_detail = "statshares lists no session for share %r" % self.share
            return False
        self.last_failure_detail = ""
        return True

    def guard_ok(self, timeout=10):
        # type: (float) -> bool
        """Four-layer guard, short-circuiting on the first failed layer.

        timeout is threaded to the three child-process layers (is_mounted /
        sentinel / statshares) so a caller under sync load can widen it; the
        reachability probe keeps its own short (load-independent) timeout.

        Layer 1 is probe_reachable (socket, then a brokered smbutil fallback) so
        a machine whose launchd python3 is blocked from direct LAN sockets by
        macOS 26 "Local Network" privacy is not falsely judged offline while the
        SMB subsystem reaches the NAS fine. The failure label stays "tcp445" so
        the daemon's Local-Network soft-pass / hint logic keys off it unchanged.
        """
        if not self.probe_reachable():
            self.last_failure = "tcp445"
            return False
        if not self.is_mounted(timeout):
            self.last_failure = "is_mounted"
            return False
        if not self.sentinel_ok(timeout):
            self.last_failure = "sentinel"
            return False
        if not self.session_ok(timeout):
            self.last_failure = "session"
            return False
        self.last_failure = ""
        return True

    def _target(self, password=None):
        # type: (Optional[str]) -> str
        """The mount URL. User and share are percent-encoded (non-ASCII and
        reserved characters broke URL parsing); ';' stays literal so a
        DOMAIN;user login keeps working. With `password` the URL carries the
        credential -- hand it to util.run and nothing else."""
        user = urllib.parse.quote(self.username, safe=";")
        if password is not None:
            user += ":" + urllib.parse.quote(password, safe="")
        return ("//" + user + "@" + self.host + "/"
                + urllib.parse.quote(self.share, safe=""))

    def _argv(self, target, handshake_n):
        # type: (str, bool) -> List[str]
        argv = [paths.MOUNT_SMBFS]
        if handshake_n:
            argv.append("-N")
        argv += ["-o", "soft,nobrowse,nodev,nosuid", "-d", "0700", "-f", "0600",
                 target, str(self.mountpoint)]
        return argv

    def mount(self, _retry=False):
        # type: (bool) -> bool
        """Make sure our share is mounted at our mountpoint.

        IDEMPOTENT: if the mount table already shows our share there, returns
        True without running mount_smbfs (last_mount_note="already_mounted").
        Re-running it on every offline iteration used to log an ERROR each time
        ("File exists ... treating as success") while never replacing a session
        that had actually died. Anything else occupying the mountpoint is
        unmounted first. Then `mount_smbfs -N` (no password on argv); on an
        authentication rejection, ONE retry with the nsmb.conf password embedded
        in the URL (see _mount_embedded_fallback).
        """
        self.last_mount_note = ""
        self.last_mount_error = ""
        if self.mount_state() is True:
            self.last_mount_note = "already_mounted"
            return True
        occupied = self.mounts_at_mountpoint()
        if occupied and not self.force_unmount():
            return self._mount_failed(
                "mountpoint %s is occupied (%s) and will not release"
                % (self.mountpoint, ", ".join(frm for frm, _fs in occupied)))
        if self._password is not None:
            # Verification with a CANDIDATE password: embedded-credential URL
            # only (see __init__), never the -N handshake.
            return self._mount_embedded_fallback(_retry)
        if self._prefer_embedded and not self.guest:
            return self._mount_embedded_fallback(_retry)
        result = util.run(self._argv(self._target(), handshake_n=True), 60)
        if result.timed_out:
            return self._mount_failed("mount_smbfs timed out")
        if result.rc == 0:
            return True
        err = _redact((result.err or "").strip())
        kind = _mount_error_kind(result.rc, err)
        self.last_mount_error = "rc=%s: %s" % (result.rc, err[:160])
        if kind == "auth" and not self.guest:
            # Expected under launchd (see _prefer_embedded): not an ERROR.
            self.logger.info("mount_smbfs -N rejected (rc=%s); trying the "
                             "nsmb.conf credential", result.rc)
            ok = self._mount_embedded_fallback(_retry)
            if ok:
                self._prefer_embedded = True
            return ok
        # Capture the stderr (first 200 chars) at ERROR: the bare
        # "mount_smbfs failed rc=64" with no stderr made a real install failure
        # undiagnosable (PLAN 6.2).
        self.logger.error("mount_smbfs failed rc=%s: %s", result.rc, err[:200])
        if kind == "eexist":
            return self._resolve_eexist(_retry)
        if kind == "refused":
            return self._share_refused(err)
        if kind == "auth":
            return self._mount_embedded_fallback(_retry)
        return False

    def _share_refused(self, err):
        # type: (str) -> bool
        """The server took the login but refused the share itself. On macOS 12
        that surfaced as "mount error: <mnt>: Unknown error: -1073741275"
        (0xC0000225, STATUS_NOT_FOUND leaking from the DFS-referral fallback
        that runs after the TREE CONNECT failed) -- which read like a client
        bug. Nothing on this Mac can fix it; say what to check."""
        status = _ntstatus(err)
        detail = (err or "").strip()
        if len(detail) > 160:
            detail = "..." + detail[-160:]
        return self._mount_failed(
            "路由器接受了登录，但拒绝连接共享「%s」%s：通常是路由器上的 USB 硬盘"
            "没有挂载（例如断电重启后）、共享名已不存在或该共享的权限变了。请在路由器"
            "管理页检查 USB 存储；恢复后会自动重连。（mount_smbfs：%s）"
            % (self.share, " (NTSTATUS %s)" % status if status else "", detail))

    def _mount_failed(self, message):
        # type: (str) -> bool
        self.last_mount_error = message
        self.logger.error("%s", message)
        return False

    def _resolve_eexist(self, retried):
        # type: (bool) -> bool
        """mount_smbfs said "File exists": macOS refuses a second mount of a
        share already mounted in this session.

        - Our share at our mountpoint after all (a race) -> success.
        - Our own leftover verification mounts (mycloud_verify.*, e.g. a GUI
          timeout killed server-set before its cleanup) -> unmount, retry once.
        - Mounted elsewhere by someone else -> name it (log + last_mount_error)
          instead of failing forever with nothing to go on.
        """
        if self.is_mounted():
            self.logger.info(
                "mount_smbfs reported File exists, but our share is mounted at "
                "%s; treating as success", self.mountpoint)
            self.last_mount_note = "already_mounted"
            return True
        elsewhere = self.same_share_elsewhere()
        ours = [mp for mp in elsewhere
                if os.path.basename(mp).startswith("mycloud_verify.")]
        for mp in ours:
            self.logger.warning("unmounting leftover verification mount %s", mp)
            r = util.run([paths.UMOUNT, mp], 30)
            if r.timed_out or r.rc != 0:
                util.run([paths.DISKUTIL, "unmount", "force", mp], 30)
        if ours and not retried:
            return self.mount(_retry=True)
        others = [mp for mp in elsewhere if mp not in ours]
        if others:
            return self._mount_failed(
                "the share is already mounted at %s in this login session, and "
                "macOS refuses a second mount of it; unmount that copy (e.g. "
                "eject it in Finder) to let MyCloud mount its own"
                % ", ".join(others))
        return False

    def _mount_embedded_fallback(self, retried=False):
        # type: (bool) -> bool
        """Retry the mount with the nsmb.conf password embedded in the URL.

        REDACTION INVARIANT: the mount URL (and the argv that carries it) holds
        the plaintext password, percent-encoded. It is handed to util.run and to
        nothing else -- never logged, never stored on self, never placed in an
        exception. mount_smbfs DOES echo that URL in its "mount error:" message
        (macOS 26/27), so its stderr is redacted before it is logged.
        """
        if self.guest:
            return self._mount_failed(
                "guest (anonymous) session rejected by %s: the share does not "
                "allow anonymous access -- switch to 注册用户模式 or enable guest "
                "access on the NAS" % self.host)
        password = (self._password if self._password is not None
                    else read_nsmb_password(self.host, username=self.username))
        if password is None:
            return self._mount_failed(
                "authentication rejected and no nsmb.conf password found for "
                "%s; cannot attempt embedded-credential fallback" % self.host)
        result = util.run(self._argv(self._target(password), handshake_n=False), 60)
        del password
        if result.timed_out:
            return self._mount_failed(
                "mount_smbfs embedded-credential fallback timed out")
        if result.rc != 0:
            err = _redact((result.err or "").strip())
            self.logger.error(
                "mount_smbfs embedded-credential fallback failed rc=%s: %s",
                result.rc, err[:200])
            self.last_mount_error = "rc=%s: %s" % (result.rc, err[:160])
            kind = _mount_error_kind(result.rc, err)
            if kind == "eexist":
                return self._resolve_eexist(retried)
            if kind == "refused":
                return self._share_refused(err)
            if kind == "auth":
                self._prefer_embedded = False
            return False
        self.logger.info(
            "mounted via embedded-credential fallback (-N path rejected by server)")
        return True

    def force_unmount(self):
        # type: () -> bool
        """Unmount EVERYTHING at our mountpoint; True once nothing is left.

        Mounts can stack on one mountpoint and umount pops only the top layer
        (verified with hdiutil), and the old "escalate only if OUR share is
        still listed" test never escalated for a foreign or unrecognised layer.
        So: per layer, umount -> umount -f -> diskutil unmount force, judged by
        the mount table alone.
        """
        mp = str(self.mountpoint)
        for _layer in range(4):
            at = self.mounts_at_mountpoint()
            if at == []:
                return True
            r = util.run([paths.UMOUNT, mp], 30)
            if r.timed_out or r.rc != 0:
                r = util.run([paths.UMOUNT, "-f", mp], 30)
                if r.timed_out or r.rc != 0:
                    util.run([paths.DISKUTIL, "unmount", "force", mp], 30)
        return self.mounts_at_mountpoint() == []

    def ensure_bare_dir_safe(self):
        # type: () -> None
        """Validate and lock down the UNMOUNTED bare mount point.

        - Anything still mounted there (per the mount table), or the table
          unreadable -> MountError (caller invoked this at the wrong time).
        - Any entry inside -> CriticalLocalWriteDetected (local data was
          written into the dead mountpoint; never delete it).
        - Otherwise reset the bare directory to 0500 so a future stray write
          hits EACCES.

        Never asks the filesystem whether the path is a mountpoint: a stat of a
        dead SMB root blocks the calling (main) thread in the kernel, or fails
        with ENOTCONN and made os.path.ismount() answer "not a mountpoint" --
        after which the share's cached listing looked like stray local data.
        The directory is listed only once the table says nothing is mounted
        there (then it is plain local APFS), and re-checked afterwards.
        """
        at = self.mounts_at_mountpoint()
        if at is None:
            raise MountError("mount table unreadable; not judging %s" % self.mountpoint)
        if at:
            raise MountError("mountpoint is still mounted: " + str(self.mountpoint))
        try:
            entries = os.listdir(str(self.mountpoint))
        except OSError as exc:
            raise MountError("bare mountpoint unreadable: %s" % exc)
        if entries:
            if self.mounts_at_mountpoint() != []:
                raise MountError("mountpoint became mounted: " + str(self.mountpoint))
            raise CriticalLocalWriteDetected(
                "local data found in unmounted mountpoint: " + str(self.mountpoint))
        os.chmod(str(self.mountpoint), 0o500)
