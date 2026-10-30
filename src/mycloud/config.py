"""Configuration parsing and validation (PLAN section 7).

The user edits a single config.ini. This module turns it into a validated
Config object, accumulating non-fatal issues into a warnings list and raising
ConfigError (with a human-readable message) for anything that must be fixed.
"""

import configparser
import os
from pathlib import Path
from typing import List, Optional

from . import util

# Built-in rsync excludes always applied, ahead of user patterns.
#
# Adding a pattern here is a MIGRATION, not a config tweak: every manifest row
# already tracked under a newly excluded path vanishes from the local walk and
# would be planned as a deletion. The daemon runs manifest.forget_excluded() on
# start and on reload to absorb that (PLAN 16.2 T-B2) -- do not add patterns
# without it.
BUILTIN_EXCLUDES = [
    ".DS_Store",
    ".Trashes",
    ".TemporaryItems",
    ".localized",
    "._*",
    "*.tmp",
    "~$*",
    "node_modules",
    ".git",
    # Developer build/cache litter: ~1/3 of the real-machine backup volume and
    # the top source of transfer failures (2026-07-30 audit). Deliberately only
    # unambiguous names -- bare "venv", "env", "build" and "target" are common
    # names for REAL user directories, so excluding them would silently drop
    # user data instead of litter.
    "__pycache__",
    "*.pyc",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    ".ruff_cache",
]

_WATCH_MODES = frozenset(("auto", "kqueue", "scan"))
_ICLOUD_POLICIES = frozenset(("skip", "download"))
_TRUE_WORDS = frozenset(("1", "true", "yes", "on"))
_FALSE_WORDS = frozenset(("0", "false", "no", "off"))


class ConfigError(Exception):
    """Raised for configuration problems the user must fix. Message is human-readable."""


class FolderSpec(object):
    """A single backup source: a directory or (Phase 2) a single file."""

    def __init__(self, src, name, is_file=False):
        # type: (Path, str, bool) -> None
        self.src = src
        self.name = name
        self.is_file = is_file

    def __repr__(self):
        # type: () -> str
        return "FolderSpec(name=%r, src=%r, is_file=%r)" % (
            self.name, str(self.src), self.is_file)


class Config(object):
    """Fully-parsed configuration."""

    def __init__(self):
        # type: () -> None
        self.host = ""
        self.share = ""
        self.username = ""
        self.sentinel_uuid = ""
        # NAS connection mode: "user" (Keychain / nsmb.conf credentials for
        # `username`) or "anonymous" (SMB guest session, no credentials).
        self.auth_mode = "user"
        self.dest_rel = "MyCloud"
        self.folders = []  # type: List[FolderSpec]
        self.excludes = []  # type: List[str]
        self.retention_days = 30
        self.trash_max_gb = 50
        self.hot_interval = 30
        self.full_interval = 900
        self.max_delete_files = 200
        self.max_delete_pct = 20
        self.offline_notify_hours = 24
        self.watch_mode = "auto"
        # iCloud dataless-file policy (PLAN section 14, M4).
        self.icloud_policy = "skip"          # skip | download
        self.icloud_evict_after_backup = True
        # Transfer-performance knobs (PLAN section 15, P0).
        self.upload_concurrency = 2          # parallel rsync upload workers
        self.rsync_inplace = True            # --inplace for NEW-file batches
        self.rsync_no_times = True           # drop -t on batch uploads
        self.warnings = []  # type: List[str]

    @property
    def anonymous(self):
        # type: () -> bool
        return self.auth_mode == "anonymous"

    @property
    def mount_username(self):
        # type: () -> str
        """The SMB login for the mount URL: the guest account in anonymous mode."""
        return "guest" if self.anonymous else self.username

    @property
    def effective_excludes(self):
        # type: () -> List[str]
        """Builtin + user exclude patterns -- what every walk must honor.

        The v2 pipeline computes its own file lists (manifest walk, scanner,
        deep-verify NAS walk) instead of passing --exclude-from to rsync, so
        every one of those walks must use THIS list, not cfg.excludes alone;
        using only the user patterns silently backs up .DS_Store/node_modules/
        AppleDouble litter (the bug fixed 2026-07-21).
        """
        return list(BUILTIN_EXCLUDES) + list(self.excludes)


def _parse_multiline(raw):
    # type: (Optional[str]) -> List[str]
    """Split a multiline INI value: one entry per line, skip blanks and comments."""
    if raw is None:
        return []
    result = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        result.append(stripped)
    return result


def _get_positive_int(parser, section, key, default, warnings):
    # type: (configparser.ConfigParser, str, str, int, List[str]) -> int
    raw = parser.get(section, key, fallback=None)
    if raw is None:
        return default
    raw = raw.strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError("Setting '%s' must be an integer, got %r." % (key, raw))
    if value <= 0:
        raise ConfigError("Setting '%s' must be a positive integer, got %d." % (key, value))
    return value


def _get_bool(parser, section, key, default):
    # type: (configparser.ConfigParser, str, str, bool) -> bool
    raw = parser.get(section, key, fallback=None)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value == "":
        return default
    if value in _TRUE_WORDS:
        return True
    if value in _FALSE_WORDS:
        return False
    raise ConfigError(
        "Setting '%s' must be a boolean (true/false), got %r." % (key, raw))


def _validate_dest_rel(raw):
    # type: (Optional[str]) -> str
    """Validate the [backup] dest_rel value and return a normalized relative path.

    Absent key -> default "MyCloud". Rejects empty, ".", absolute paths, "..",
    backslashes; strips leading/trailing slashes; CJK and spaces are allowed.
    """
    if raw is None:
        return "MyCloud"
    value = raw.strip()
    if value == "":
        raise ConfigError("Setting 'dest_rel' must not be empty.")
    if "\\" in value:
        raise ConfigError(
            "Setting 'dest_rel' must use forward slashes, not backslashes: %r" % value)
    if os.path.isabs(value):
        raise ConfigError(
            "Setting 'dest_rel' must be a relative path (no leading '/'), got %r." % value)
    stripped = value.strip("/")
    if stripped == "" or stripped == ".":
        raise ConfigError(
            "Setting 'dest_rel' must name a subdirectory, not %r." % value)
    for part in stripped.split("/"):
        if part == "":
            raise ConfigError(
                "Setting 'dest_rel' must not contain empty path components: %r" % value)
        if part == ".." or part == ".":
            raise ConfigError(
                "Setting 'dest_rel' must not contain '.' or '..' components: %r" % value)
    return stripped


def load_config(path):
    # type: (object) -> Config
    """Parse and validate config.ini at path, returning a Config."""
    cfg = Config()
    # inline_comment_prefixes lets values like "watch_mode = auto ; auto|kqueue|scan"
    # (as shown in the PLAN section 7 example) parse to just "auto".
    # interpolation=None so a '%' in any path/pattern/dest_rel is never treated
    # as an interpolation token (BasicInterpolation would raise
    # InterpolationSyntaxError out of parser.get()). This matches the writer in
    # mycloudctl._new_parser so both readers agree.
    parser = configparser.ConfigParser(
        interpolation=None, inline_comment_prefixes=(";",))
    try:
        read_ok = parser.read(os.fspath(path), encoding="utf-8")
    except (configparser.Error, OSError) as exc:
        raise ConfigError("Could not read config file: %s" % (exc,))
    if not read_ok:
        raise ConfigError("Config file not found or empty: %s" % (path,))

    # [server] is managed by the installer; a missing key is a warning, not an error,
    # so a fresh checkout can still run the test suite.
    if parser.has_section("server"):
        cfg.host = (parser.get("server", "host", fallback="") or "").strip()
        cfg.share = (parser.get("server", "share", fallback="") or "").strip()
        cfg.username = (parser.get("server", "username", fallback="") or "").strip()
        cfg.sentinel_uuid = (parser.get("server", "sentinel_uuid", fallback="") or "").strip()
        raw_mode = (parser.get("server", "auth_mode", fallback="") or "").strip().lower()
        if raw_mode == "":
            raw_mode = "user"
        if raw_mode not in ("user", "anonymous"):
            raise ConfigError(
                "Setting 'auth_mode' must be 'user' or 'anonymous', got %r." % raw_mode)
        cfg.auth_mode = raw_mode
    if not cfg.sentinel_uuid:
        cfg.warnings.append(
            "sentinel_uuid is not set in [server]; the installer normally manages it."
        )

    # [backup] section.
    folders_raw = None
    excludes_raw = None
    if parser.has_section("backup"):
        folders_raw = parser.get("backup", "folders", fallback=None)
        excludes_raw = parser.get("backup", "exclude", fallback=None)
        cfg.dest_rel = _validate_dest_rel(
            parser.get("backup", "dest_rel", fallback=None))
        cfg.retention_days = _get_positive_int(
            parser, "backup", "retention_days", cfg.retention_days, cfg.warnings)
        cfg.trash_max_gb = _get_positive_int(
            parser, "backup", "trash_max_gb", cfg.trash_max_gb, cfg.warnings)
        cfg.hot_interval = _get_positive_int(
            parser, "backup", "hot_interval", cfg.hot_interval, cfg.warnings)
        cfg.full_interval = _get_positive_int(
            parser, "backup", "full_interval", cfg.full_interval, cfg.warnings)
        cfg.max_delete_files = _get_positive_int(
            parser, "backup", "max_delete_files", cfg.max_delete_files, cfg.warnings)
        cfg.offline_notify_hours = _get_positive_int(
            parser, "backup", "offline_notify_hours", cfg.offline_notify_hours, cfg.warnings)

        pct = _get_positive_int(
            parser, "backup", "max_delete_pct", cfg.max_delete_pct, cfg.warnings)
        if pct < 1 or pct > 100:
            raise ConfigError("Setting 'max_delete_pct' must be between 1 and 100, got %d." % pct)
        cfg.max_delete_pct = pct

        watch_mode = parser.get("backup", "watch_mode", fallback=cfg.watch_mode)
        watch_mode = (watch_mode or "").strip() or cfg.watch_mode
        if watch_mode not in _WATCH_MODES:
            raise ConfigError(
                "Setting 'watch_mode' must be one of auto, kqueue, scan; got %r." % watch_mode)
        cfg.watch_mode = watch_mode

        icloud_policy = parser.get("backup", "icloud_policy", fallback=cfg.icloud_policy)
        icloud_policy = (icloud_policy or "").strip() or cfg.icloud_policy
        if icloud_policy not in _ICLOUD_POLICIES:
            raise ConfigError(
                "Setting 'icloud_policy' must be one of skip, download; got %r."
                % icloud_policy)
        cfg.icloud_policy = icloud_policy
        cfg.icloud_evict_after_backup = _get_bool(
            parser, "backup", "icloud_evict_after_backup",
            cfg.icloud_evict_after_backup)

        # Transfer-performance knobs (PLAN section 15, P0).
        concurrency = _get_positive_int(
            parser, "backup", "upload_concurrency", cfg.upload_concurrency,
            cfg.warnings)
        if concurrency > 16:
            raise ConfigError(
                "Setting 'upload_concurrency' must be between 1 and 16, got %d."
                % concurrency)
        cfg.upload_concurrency = concurrency
        cfg.rsync_inplace = _get_bool(
            parser, "backup", "rsync_inplace", cfg.rsync_inplace)
        cfg.rsync_no_times = _get_bool(
            parser, "backup", "rsync_no_times", cfg.rsync_no_times)

    cfg.excludes = _parse_multiline(excludes_raw)

    # Resolve sources. Each entry may be a directory or a single file; a source
    # that does not exist yet is a warning (is_file defaults False) so a fresh
    # checkout still loads and the daemon's source-missing guard handles it.
    resolved = []  # type: List[Path]
    is_files = []  # type: List[bool]
    for entry in _parse_multiline(folders_raw):
        expanded = Path(entry).expanduser()
        try:
            resolved_path = expanded.resolve()
        except (OSError, RuntimeError):
            resolved_path = Path(os.path.abspath(str(expanded)))
        if not resolved_path.exists():
            cfg.warnings.append("Backup source does not exist (skipping until it appears): %s"
                                % (resolved_path,))
            is_file = False
        else:
            is_file = resolved_path.is_file()
        resolved.append(resolved_path)
        is_files.append(is_file)

    _validate_sources(resolved, is_files)

    # Assign names from basenames, suffixing duplicates in listed order.
    used = {}  # type: dict
    for resolved_path, is_file in zip(resolved, is_files):
        base = resolved_path.name or str(resolved_path)
        count = used.get(base, 0) + 1
        used[base] = count
        name = base if count == 1 else "%s-%d" % (base, count)
        cfg.folders.append(FolderSpec(resolved_path, name, is_file))

    _validate_mirror_names(cfg.folders)
    return cfg


def _validate_mirror_names(folders):
    # type: (List[FolderSpec]) -> None
    """Reject sources whose NAS mirror entries would collide (PLAN 12.1).

    A directory source mirrors at data/<name>/ (a directory); a single-file
    source mirrors at data/<basename> (rsync preserves the basename, ignoring
    the suffixed FolderSpec name). Two sources that resolve to the same
    data/<X> entry silently clobber one another every pass: two files ping-pong
    the same mirror (only one is ever backed up) and file-deletion archival for
    the loser is dead; a file whose basename equals a directory source's mirror
    name makes rsync try to replace a directory with a file (a hard Phase-1
    failure that puts the daemon into a recovery loop). Directory mirror names
    are already de-duplicated by suffixing, so only file sources can introduce a
    collision -- detect it up front rather than at rsync time.
    """
    seen = {}  # type: dict
    for spec in folders:
        if spec.is_file:
            key = spec.src.name or str(spec.src)
        else:
            key = spec.name
        prior = seen.get(key)
        if prior is not None:
            raise ConfigError(
                "Backup sources %s and %s would both map to the NAS mirror "
                "'%s'; rename or remove one (single-file sources are mirrored by "
                "their file name)." % (prior, spec.src, key))
        seen[key] = spec.src


def _validate_sources(resolved, is_files):
    # type: (List[Path], List[bool]) -> None
    """Reject overlapping sources (PLAN 12.1).

    - Directory nested inside another directory -> error (unchanged rule).
    - A file lying inside a configured directory (equivalently, a directory that
      is an ancestor of a configured file) -> error. This pair is caught in both
      listing orders because the ancestor check runs over ordered pairs.
    - Two files with an identical path (duplicate) -> error. Files never overlap
      with files otherwise.
    """
    for i, a in enumerate(resolved):
        for j, b in enumerate(resolved):
            if i == j:
                continue
            # Identical path listed twice: a duplicate file is an error; duplicate
            # directories keep the historical suffixing behavior (no error).
            if a == b:
                if i < j and (is_files[i] or is_files[j]):
                    raise ConfigError(
                        "Backup file listed more than once: %s" % (a,))
                continue
            # a strictly contains b.
            if _is_ancestor(a, b):
                if is_files[j]:
                    raise ConfigError(
                        "Backup file %s lies inside backup folder %s; "
                        "remove one of them." % (b, a))
                raise ConfigError(
                    "Backup folder %s is nested inside %s; overlapping folders "
                    "are not allowed." % (b, a))


def _is_ancestor(a, b):
    # type: (Path, Path) -> bool
    """True if a is a strict ancestor of b."""
    if a == b:
        return False
    try:
        b.relative_to(a)
        return True
    except ValueError:
        return False


def write_excludes(cfg, path):
    # type: (Config, object) -> None
    """Write builtin + user exclude patterns, one per line, atomically."""
    lines = list(BUILTIN_EXCLUDES) + list(cfg.excludes)
    text = "\n".join(lines)
    if text:
        text += "\n"
    util.atomic_write_text(path, text)
