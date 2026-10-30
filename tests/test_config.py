import tests._path  # noqa: F401

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mycloud import config
from mycloud import paths
from mycloud.config import ConfigError, load_config, write_excludes


def write_ini(directory, text):
    path = os.path.join(directory, "config.ini")
    with open(path, "w", encoding="utf-8") as h:
        h.write(text)
    return path


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        # Two real folders to reference.
        self.docs = os.path.join(self.dir, "Documents")
        self.desk = os.path.join(self.dir, "Desktop")
        os.makedirs(self.docs)
        os.makedirs(self.desk)

    def test_happy_path_and_defaults(self):
        ini = write_ini(self.dir, (
            "[server]\n"
            "host = 192.168.5.1\n"
            "share = SHARE\n"
            "username = admin\n"
            "sentinel_uuid = abc-123\n"
            "\n"
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "    %s\n"
        ) % (self.docs, self.desk))
        cfg = load_config(ini)
        self.assertEqual(cfg.host, "192.168.5.1")
        self.assertEqual(cfg.share, "SHARE")
        self.assertEqual(cfg.username, "admin")
        self.assertEqual(cfg.sentinel_uuid, "abc-123")
        self.assertEqual(len(cfg.folders), 2)
        self.assertEqual({f.name for f in cfg.folders}, {"Documents", "Desktop"})
        # Defaults.
        self.assertEqual(cfg.retention_days, 30)
        self.assertEqual(cfg.trash_max_gb, 50)
        self.assertEqual(cfg.hot_interval, 30)
        self.assertEqual(cfg.full_interval, 900)
        self.assertEqual(cfg.max_delete_files, 200)
        self.assertEqual(cfg.max_delete_pct, 20)
        self.assertEqual(cfg.offline_notify_hours, 24)
        self.assertEqual(cfg.watch_mode, "auto")
        # sentinel present -> no sentinel warning.
        self.assertFalse(any("sentinel_uuid" in w for w in cfg.warnings))

    def test_multiline_folders_skip_blank_and_comments(self):
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "\n"
            "    # a comment\n"
            "    ; another comment\n"
            "    %s\n"
        ) % (self.docs, self.desk))
        cfg = load_config(ini)
        self.assertEqual(len(cfg.folders), 2)

    def test_nested_folder_rejected(self):
        child = os.path.join(self.docs, "child")
        os.makedirs(child)
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "    %s\n"
        ) % (self.docs, child))
        with self.assertRaises(ConfigError) as ctx:
            load_config(ini)
        self.assertIn("nested", str(ctx.exception).lower())

    def test_duplicate_basename_suffixing(self):
        # Two different absolute paths with the same basename "Documents".
        other_parent = tempfile.mkdtemp()
        other_docs = os.path.join(other_parent, "Documents")
        os.makedirs(other_docs)
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "    %s\n"
        ) % (self.docs, other_docs))
        cfg = load_config(ini)
        names = [f.name for f in cfg.folders]
        self.assertEqual(names, ["Documents", "Documents-2"])

    def test_triple_duplicate_suffixing(self):
        p2 = os.path.join(tempfile.mkdtemp(), "Data")
        p3 = os.path.join(tempfile.mkdtemp(), "Data")
        p1 = os.path.join(self.dir, "Data")
        for p in (p1, p2, p3):
            os.makedirs(p)
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "    %s\n"
            "    %s\n"
        ) % (p1, p2, p3))
        cfg = load_config(ini)
        self.assertEqual([f.name for f in cfg.folders], ["Data", "Data-2", "Data-3"])

    def test_bad_watch_mode(self):
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "watch_mode = turbo\n"
        ) % self.docs)
        with self.assertRaises(ConfigError) as ctx:
            load_config(ini)
        self.assertIn("watch_mode", str(ctx.exception))

    def test_inline_comment_watch_mode(self):
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "watch_mode = auto        ; auto | kqueue | scan\n"
        ) % self.docs)
        cfg = load_config(ini)
        self.assertEqual(cfg.watch_mode, "auto")

    def test_icloud_defaults(self):
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
        ) % self.docs)
        cfg = load_config(ini)
        self.assertEqual(cfg.icloud_policy, "skip")
        self.assertTrue(cfg.icloud_evict_after_backup)

    def test_icloud_policy_download(self):
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "icloud_policy = download\n"
            "icloud_evict_after_backup = false\n"
        ) % self.docs)
        cfg = load_config(ini)
        self.assertEqual(cfg.icloud_policy, "download")
        self.assertFalse(cfg.icloud_evict_after_backup)

    def test_bad_icloud_policy_rejected(self):
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "icloud_policy = purge\n"
        ) % self.docs)
        with self.assertRaises(ConfigError) as ctx:
            load_config(ini)
        self.assertIn("icloud_policy", str(ctx.exception))

    def test_bad_icloud_evict_bool_rejected(self):
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "icloud_evict_after_backup = maybe\n"
        ) % self.docs)
        with self.assertRaises(ConfigError) as ctx:
            load_config(ini)
        self.assertIn("icloud_evict_after_backup", str(ctx.exception))

    def test_icloud_evict_bool_words(self):
        for word, expected in (("yes", True), ("no", False), ("1", True),
                               ("0", False), ("on", True), ("off", False)):
            ini = write_ini(self.dir, (
                "[backup]\n"
                "folders =\n"
                "    %s\n"
                "icloud_evict_after_backup = %s\n"
            ) % (self.docs, word))
            cfg = load_config(ini)
            self.assertEqual(cfg.icloud_evict_after_backup, expected, word)

    def test_transfer_performance_defaults(self):
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
        ) % self.docs)
        cfg = load_config(ini)
        self.assertEqual(cfg.upload_concurrency, 2)
        self.assertTrue(cfg.rsync_inplace)
        self.assertTrue(cfg.rsync_no_times)

    def test_transfer_performance_explicit(self):
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "upload_concurrency = 8\n"
            "rsync_inplace = false\n"
            "rsync_no_times = false\n"
        ) % self.docs)
        cfg = load_config(ini)
        self.assertEqual(cfg.upload_concurrency, 8)
        self.assertFalse(cfg.rsync_inplace)
        self.assertFalse(cfg.rsync_no_times)

    def test_upload_concurrency_range_rejected(self):
        for bad in ("0", "17", "-1"):
            ini = write_ini(self.dir, (
                "[backup]\n"
                "folders =\n"
                "    %s\n"
                "upload_concurrency = %s\n"
            ) % (self.docs, bad))
            with self.assertRaises(ConfigError, msg=bad) as ctx:
                load_config(ini)
            self.assertIn("upload_concurrency", str(ctx.exception))

    def test_effective_excludes_prepends_builtins(self):
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "exclude =\n"
            "    *.photoslibrary\n"
        ) % self.docs)
        cfg = load_config(ini)
        effective = cfg.effective_excludes
        self.assertEqual(
            effective,
            list(config.BUILTIN_EXCLUDES) + ["*.photoslibrary"])
        # AppleDouble sidecars are excluded out of the box (PLAN 15 P0).
        self.assertIn("._*", config.BUILTIN_EXCLUDES)
        self.assertIn(".DS_Store", config.BUILTIN_EXCLUDES)

    def test_bad_max_delete_pct(self):
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "max_delete_pct = 150\n"
        ) % self.docs)
        with self.assertRaises(ConfigError):
            load_config(ini)

    def test_non_positive_int_rejected(self):
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "retention_days = 0\n"
        ) % self.docs)
        with self.assertRaises(ConfigError):
            load_config(ini)

    def test_missing_sentinel_warning(self):
        ini = write_ini(self.dir, (
            "[server]\n"
            "host = 192.168.5.1\n"
            "share = SHARE\n"
            "username = admin\n"
            "\n"
            "[backup]\n"
            "folders =\n"
            "    %s\n"
        ) % self.docs)
        cfg = load_config(ini)
        self.assertTrue(any("sentinel_uuid" in w for w in cfg.warnings))
        # Missing sentinel is not an error.
        self.assertEqual(cfg.sentinel_uuid, "")

    def test_nonexistent_folder_is_warning_not_error(self):
        ghost = os.path.join(self.dir, "Ghost")
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
        ) % ghost)
        cfg = load_config(ini)
        self.assertEqual(len(cfg.folders), 1)
        self.assertTrue(any("does not exist" in w for w in cfg.warnings))

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config(os.path.join(self.dir, "nope.ini"))

    def test_write_excludes_content(self):
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "exclude =\n"
            "    *.photoslibrary\n"
            "    build/\n"
        ) % self.docs)
        cfg = load_config(ini)
        out = os.path.join(self.dir, "excludes.txt")
        write_excludes(cfg, out)
        with open(out, encoding="utf-8") as h:
            lines = h.read().splitlines()
        # Builtins first, then user patterns, in order.
        self.assertEqual(lines[:len(config.BUILTIN_EXCLUDES)], config.BUILTIN_EXCLUDES)
        self.assertEqual(lines[-2:], ["*.photoslibrary", "build/"])
        self.assertEqual(cfg.excludes, ["*.photoslibrary", "build/"])


class FileSourceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.docs = os.path.join(self.dir, "Documents")
        os.makedirs(self.docs)
        self.notes = os.path.join(self.dir, "notes.txt")
        with open(self.notes, "w", encoding="utf-8") as h:
            h.write("hello\n")

    def _by_name(self, cfg):
        return {f.name: f for f in cfg.folders}

    def test_file_source_marked_is_file(self):
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "    %s\n"
        ) % (self.docs, self.notes))
        cfg = load_config(ini)
        specs = self._by_name(cfg)
        self.assertFalse(specs["Documents"].is_file)
        self.assertTrue(specs["notes.txt"].is_file)

    def test_nonexistent_source_is_not_file(self):
        ghost = os.path.join(self.dir, "ghost.txt")
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
        ) % ghost)
        cfg = load_config(ini)
        self.assertEqual(len(cfg.folders), 1)
        self.assertFalse(cfg.folders[0].is_file)
        self.assertTrue(any("does not exist" in w for w in cfg.warnings))

    def test_file_inside_configured_directory_rejected(self):
        inner = os.path.join(self.docs, "inner.txt")
        with open(inner, "w", encoding="utf-8") as h:
            h.write("x\n")
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "    %s\n"
        ) % (self.docs, inner))
        with self.assertRaises(ConfigError) as ctx:
            load_config(ini)
        self.assertIn("inside", str(ctx.exception).lower())

    def test_file_inside_configured_directory_rejected_reverse_order(self):
        inner = os.path.join(self.docs, "inner.txt")
        with open(inner, "w", encoding="utf-8") as h:
            h.write("x\n")
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "    %s\n"
        ) % (inner, self.docs))
        with self.assertRaises(ConfigError):
            load_config(ini)

    def test_duplicate_file_path_rejected(self):
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "    %s\n"
        ) % (self.notes, self.notes))
        with self.assertRaises(ConfigError) as ctx:
            load_config(ini)
        self.assertIn("more than once", str(ctx.exception).lower())

    def test_same_basename_file_sources_rejected(self):
        # Two DIFFERENT files that share a basename would both mirror to
        # data/notes.txt and clobber each other (regression: daemon ping-pong).
        other = tempfile.mkdtemp()
        other_notes = os.path.join(other, "notes.txt")
        with open(other_notes, "w", encoding="utf-8") as h:
            h.write("world\n")
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "    %s\n"
        ) % (self.notes, other_notes))
        with self.assertRaises(ConfigError) as ctx:
            load_config(ini)
        self.assertIn("mirror", str(ctx.exception).lower())

    def test_file_basename_colliding_with_dir_name_rejected(self):
        # A file named "Documents" would mirror to data/Documents, colliding
        # with the directory source's data/Documents/ mirror.
        other = tempfile.mkdtemp()
        clash = os.path.join(other, "Documents")
        with open(clash, "w", encoding="utf-8") as h:
            h.write("x\n")
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
            "    %s\n"
        ) % (self.docs, clash))
        with self.assertRaises(ConfigError) as ctx:
            load_config(ini)
        self.assertIn("mirror", str(ctx.exception).lower())

    def test_percent_in_source_path_does_not_crash(self):
        # A '%' in a path must not raise InterpolationSyntaxError (regression:
        # load_config used a BasicInterpolation parser).
        pct = os.path.join(self.dir, "100% done")
        os.makedirs(pct)
        ini = write_ini(self.dir, (
            "[backup]\n"
            "folders =\n"
            "    %s\n"
        ) % pct)
        cfg = load_config(ini)
        self.assertEqual(len(cfg.folders), 1)
        self.assertEqual(cfg.folders[0].name, "100% done")


class DestRelTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.docs = os.path.join(self.dir, "Documents")
        os.makedirs(self.docs)

    def _load(self, dest_line):
        ini = write_ini(self.dir, (
            "[backup]\n"
            "%s"
            "folders =\n"
            "    %s\n"
        ) % (dest_line, self.docs))
        return load_config(ini)

    def test_default_when_absent(self):
        cfg = self._load("")
        self.assertEqual(cfg.dest_rel, "MyCloud")

    def test_default_when_no_backup_section(self):
        ini = write_ini(self.dir, "[server]\nhost = h\n")
        cfg = load_config(ini)
        self.assertEqual(cfg.dest_rel, "MyCloud")

    def test_explicit_value(self):
        cfg = self._load("dest_rel = Backup/MyCloud\n")
        self.assertEqual(cfg.dest_rel, "Backup/MyCloud")

    def test_trailing_slash_stripped(self):
        cfg = self._load("dest_rel = MyCloud/\n")
        self.assertEqual(cfg.dest_rel, "MyCloud")

    def test_cjk_and_spaces_allowed(self):
        cfg = self._load("dest_rel = 备份 数据/云\n")
        self.assertEqual(cfg.dest_rel, "备份 数据/云")

    def test_absolute_rejected(self):
        with self.assertRaises(ConfigError):
            self._load("dest_rel = /Volumes/SHARE/MyCloud\n")

    def test_empty_rejected(self):
        with self.assertRaises(ConfigError):
            self._load("dest_rel =    \n")

    def test_dotdot_rejected(self):
        with self.assertRaises(ConfigError):
            self._load("dest_rel = foo/../bar\n")

    def test_dot_rejected(self):
        with self.assertRaises(ConfigError):
            self._load("dest_rel = .\n")

    def test_backslash_rejected(self):
        with self.assertRaises(ConfigError):
            self._load("dest_rel = foo\\bar\n")

    def test_derived_helpers(self):
        self.assertEqual(paths.data_rel("MyCloud"), "MyCloud/data")
        self.assertEqual(paths.trash_rel("MyCloud"), "MyCloud/.trash")
        self.assertEqual(paths.heartbeat_rel("Backup/X"), "Backup/X/last_sync.json")


class PathsOverrideTests(unittest.TestCase):
    def test_mycloud_app_env_override(self):
        src_dir = str(Path(__file__).resolve().parents[1] / "src")
        override = tempfile.mkdtemp()
        env = dict(os.environ)
        env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
        env["MYCLOUD_APP"] = override
        code = (
            "from mycloud import paths\n"
            "print(str(paths.APP))\n"
            "print(str(paths.RUNTIME))\n"
            "print(str(paths.CONFIG))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], override)
        self.assertEqual(lines[1], os.path.join(override, "runtime"))
        self.assertEqual(lines[2], os.path.join(override, "config.ini"))


class BuiltinDeveloperLitterTests(unittest.TestCase):
    """PLAN 16.2 T-B2: builtin excludes cover developer litter, conservatively."""

    def test_developer_litter_patterns_are_builtin(self):
        for pattern in ("__pycache__", "*.pyc", ".venv", ".pytest_cache",
                        ".mypy_cache", ".tox", ".ruff_cache"):
            self.assertIn(pattern, config.BUILTIN_EXCLUDES)

    def test_ambiguous_bare_names_are_not_excluded(self):
        # 'venv'/'env'/'build'/'target' are common names for REAL user
        # directories; excluding them would silently drop user data, so the
        # builtin list deliberately stops at the unambiguous dot-names.
        for pattern in ("venv", "env", "build", "target", "dist"):
            self.assertNotIn(pattern, config.BUILTIN_EXCLUDES)

    def test_effective_excludes_still_prepends_builtins(self):
        cfg = config.Config()
        cfg.excludes = ["*.photoslibrary"]
        self.assertEqual(cfg.effective_excludes,
                         list(config.BUILTIN_EXCLUDES) + ["*.photoslibrary"])


if __name__ == "__main__":
    unittest.main()


class AuthModeTests(unittest.TestCase):
    """[server] auth_mode: 'user' (default, Keychain/nsmb.conf credentials) or
    'anonymous' (SMB guest session). Drives MountManager's login + fallback."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.docs = os.path.join(self.dir, "Documents")
        os.makedirs(self.docs)

    def _load(self, server_extra=""):
        ini = write_ini(self.dir, (
            "[server]\nhost = 192.168.5.1\nshare = SHARE\nusername = admin\n"
            "sentinel_uuid = abc-123\n%s\n[backup]\nfolders =\n    %s\n"
        ) % (server_extra, self.docs))
        return load_config(ini)

    def test_default_is_user_mode(self):
        cfg = self._load()
        self.assertEqual(cfg.auth_mode, "user")
        self.assertFalse(cfg.anonymous)
        self.assertEqual(cfg.mount_username, "admin")

    def test_anonymous_mode_mounts_as_guest(self):
        cfg = self._load("auth_mode = anonymous")
        self.assertTrue(cfg.anonymous)
        self.assertEqual(cfg.mount_username, "guest")

    def test_mode_is_case_insensitive(self):
        self.assertEqual(self._load("auth_mode = Anonymous").auth_mode, "anonymous")

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ConfigError):
            self._load("auth_mode = bogus")
