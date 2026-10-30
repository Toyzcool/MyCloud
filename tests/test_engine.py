import tests._path  # noqa: F401

import logging
import os
import shutil
import tempfile
import time
import unittest

from mycloud import engine
from mycloud.engine import SyncEngine, SyncResult, Phase2Result

RSYNC = "/usr/bin/rsync"

# CJK + space filenames exercised throughout (PLAN acceptance test 3).
CJK_A = "文档 测试.txt"        # "文档 测试.txt"
CJK_B = "副本 (1).md"                    # "副本 (1).md"


def _silent_logger():
    logger = logging.getLogger("test_engine")
    logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.CRITICAL)
    return logger


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mycloud_engine_")
        self.src = os.path.join(self.root, "src")
        self.dst = os.path.join(self.root, "dst")
        self.trash = os.path.join(self.root, "trash", "2026-07-18T120000")
        self.excludes = os.path.join(self.root, "excludes.txt")
        os.makedirs(self.src)
        with open(self.excludes, "w", encoding="utf-8") as handle:
            handle.write("")
        self.engine = SyncEngine(RSYNC, self.excludes, _silent_logger())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


class Phase1Tests(EngineTestBase):
    def test_mirror_tree_with_cjk_and_spaces(self):
        _write(os.path.join(self.src, "a.txt"), "hello")
        _write(os.path.join(self.src, "nested", "dir", "b.md"), "world")
        _write(os.path.join(self.src, CJK_A), "cjk-a")
        _write(os.path.join(self.src, "nested", CJK_B), "cjk-b")

        result = self.engine.phase1(self.src, self.dst, self.trash, timeout=60)

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.rc, 0)
        self.assertEqual(_read(os.path.join(self.dst, "a.txt")), "hello")
        self.assertEqual(_read(os.path.join(self.dst, CJK_A)), "cjk-a")
        self.assertEqual(_read(os.path.join(self.dst, "nested", CJK_B)), "cjk-b")
        self.assertEqual(
            _read(os.path.join(self.dst, "nested", "dir", "b.md")), "world"
        )
        # Stats parsed (observability only): 4 files transferred on first run.
        self.assertGreaterEqual(result.files_copied, 4)
        self.assertGreater(result.bytes_copied, 0)

    def test_overwrite_versions_old_copy_at_full_relpath(self):
        rel = os.path.join("nested", CJK_A)
        _write(os.path.join(self.src, rel), "v1-original")
        self.engine.phase1(self.src, self.dst, self.trash, timeout=60)

        _write(os.path.join(self.src, rel), "v2-changed")
        result = self.engine.phase1(self.src, self.dst, self.trash, timeout=60)

        self.assertEqual(result.status, "ok")
        # New content mirrored.
        self.assertEqual(_read(os.path.join(self.dst, rel)), "v2-changed")
        # Old version landed under backup_dir at its full relpath.
        backup_old = os.path.join(self.trash, rel)
        self.assertTrue(os.path.exists(backup_old), backup_old)
        self.assertEqual(_read(backup_old), "v1-original")

    def test_makedirs_creates_missing_dst(self):
        _write(os.path.join(self.src, "x.txt"), "data")
        self.assertFalse(os.path.exists(self.dst))
        result = self.engine.phase1(self.src, self.dst, self.trash, timeout=60)
        self.assertEqual(result.status, "ok")
        self.assertTrue(os.path.isdir(self.dst))

    def test_partial_failure_collects_failed_paths(self):
        _write(os.path.join(self.src, "good.txt"), "ok")
        bad = os.path.join(self.src, "sub", "nope.txt")
        _write(bad, "secret")
        os.chmod(bad, 0o000)
        try:
            result = self.engine.phase1(self.src, self.dst, self.trash, timeout=60)
        finally:
            os.chmod(bad, 0o644)

        self.assertEqual(result.status, "partial")
        self.assertIn(result.rc, (23, 24))
        self.assertTrue(result.failed_paths)
        joined = "\n".join(result.failed_paths)
        self.assertIn("nope.txt", joined)
        # The readable file still made it across.
        self.assertEqual(_read(os.path.join(self.dst, "good.txt")), "ok")

    def test_streaming_mode_without_timeout(self):
        _write(os.path.join(self.src, "s.txt"), "streamed")
        result = self.engine.phase1(self.src, self.dst, self.trash, timeout=None)
        self.assertEqual(result.status, "ok")
        self.assertEqual(_read(os.path.join(self.dst, "s.txt")), "streamed")

    def test_argv_never_contains_delete_flag(self):
        # If a --delete* flag were ever injected into phase1's argv, the assert
        # inside phase1 must fire. We monkeypatch the arg list construction by
        # subclassing is overkill; instead assert the guard directly.
        self.engine.excludes_file = "--delete-during"  # crafted injection
        _write(os.path.join(self.src, "z.txt"), "z")
        with self.assertRaises(AssertionError):
            self.engine.phase1(self.src, self.dst, self.trash, timeout=60)

    def test_proc_registry_populated_and_drained(self):
        registry = set()
        eng = SyncEngine(RSYNC, self.excludes, _silent_logger(), proc_registry=registry)
        _write(os.path.join(self.src, "r.txt"), "reg")
        eng.phase1(self.src, self.dst, self.trash, timeout=60)
        # After completion the process is discarded again.
        self.assertEqual(registry, set())


class SubtreeScopingTests(EngineTestBase):
    def test_subtree_only_syncs_that_subtree(self):
        _write(os.path.join(self.src, "top.txt"), "top")
        _write(os.path.join(self.src, "keep", "k.txt"), "keep")
        _write(os.path.join(self.src, "only", "deep", CJK_A), "deep-cjk")

        result = self.engine.phase1(
            self.src, self.dst, self.trash, subtree="only", timeout=60
        )

        self.assertEqual(result.status, "ok")
        # Only the subtree was mirrored.
        self.assertTrue(os.path.exists(os.path.join(self.dst, "only", "deep", CJK_A)))
        self.assertFalse(os.path.exists(os.path.join(self.dst, "top.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.dst, "keep")))

    def test_subtree_backup_lands_at_full_relpath(self):
        rel = os.path.join("only", "f.txt")
        _write(os.path.join(self.src, rel), "orig")
        self.engine.phase1(self.src, self.dst, self.trash, subtree="only", timeout=60)
        _write(os.path.join(self.src, rel), "new")
        self.engine.phase1(self.src, self.dst, self.trash, subtree="only", timeout=60)
        # effective_backup = backup/only, old version at backup/only/f.txt.
        self.assertEqual(_read(os.path.join(self.trash, "only", "f.txt")), "orig")


class Phase2ComputeTests(EngineTestBase):
    def _mirror(self, subtree=None):
        self.engine.phase1(self.src, self.dst, self.trash, subtree=subtree, timeout=60)

    def test_finds_locally_deleted_files(self):
        _write(os.path.join(self.src, "a.txt"), "a")
        _write(os.path.join(self.src, "sub", "b.txt"), "b")
        _write(os.path.join(self.src, CJK_A), "c")
        self._mirror()

        os.unlink(os.path.join(self.src, "sub", "b.txt"))
        os.unlink(os.path.join(self.src, CJK_A))

        rel = self.engine.phase2_compute(self.src, self.dst, timeout=60)
        self.assertIn(os.path.join("sub", "b.txt"), rel)
        self.assertIn(CJK_A, rel)
        self.assertNotIn("a.txt", rel)

    def test_empty_when_nothing_deleted(self):
        _write(os.path.join(self.src, "a.txt"), "a")
        self._mirror()
        self.assertEqual(self.engine.phase2_compute(self.src, self.dst, timeout=60), [])

    def test_missing_dst_graceful(self):
        _write(os.path.join(self.src, "a.txt"), "a")
        # dst never created.
        rel = self.engine.phase2_compute(self.src, self.dst, timeout=60)
        self.assertEqual(rel, [])

    def test_subtree_prefixes_relpaths(self):
        _write(os.path.join(self.src, "only", "gone.txt"), "g")
        _write(os.path.join(self.src, "only", "stay.txt"), "s")
        self._mirror(subtree="only")
        os.unlink(os.path.join(self.src, "only", "gone.txt"))
        rel = self.engine.phase2_compute(self.src, self.dst, subtree="only", timeout=60)
        # Returned paths are relative to dst_root (prefixed with subtree).
        self.assertIn(os.path.join("only", "gone.txt"), rel)


class Phase2ExecuteTests(EngineTestBase):
    def test_moves_into_trash_preserving_relpaths(self):
        _write(os.path.join(self.dst, "sub", "b.txt"), "b")
        _write(os.path.join(self.dst, CJK_A), "c")
        rels = [os.path.join("sub", "b.txt"), CJK_A]

        result = self.engine.phase2_execute(self.src, self.dst, self.trash, rels)

        self.assertIsInstance(result, Phase2Result)
        self.assertEqual(result.moved, 2)
        self.assertEqual(result.errors, [])
        # Moved out of the mirror.
        self.assertFalse(os.path.exists(os.path.join(self.dst, "sub", "b.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.dst, CJK_A)))
        # Into trash at the same relpath.
        self.assertEqual(_read(os.path.join(self.trash, "sub", "b.txt")), "b")
        self.assertEqual(_read(os.path.join(self.trash, CJK_A)), "c")

    def test_prunes_now_empty_dirs(self):
        _write(os.path.join(self.dst, "a", "b", "c", "leaf.txt"), "x")
        rels = [os.path.join("a", "b", "c", "leaf.txt")]
        self.engine.phase2_execute(self.src, self.dst, self.trash, rels)
        # Emptied ancestor dirs pruned, up to (but not including) dst_root.
        self.assertFalse(os.path.exists(os.path.join(self.dst, "a")))
        self.assertTrue(os.path.isdir(self.dst))

    def test_prune_stops_at_nonempty_dir(self):
        _write(os.path.join(self.dst, "a", "b", "gone.txt"), "x")
        _write(os.path.join(self.dst, "a", "keep.txt"), "y")
        rels = [os.path.join("a", "b", "gone.txt")]
        self.engine.phase2_execute(self.src, self.dst, self.trash, rels)
        # a/b emptied and pruned, but a/ kept because keep.txt remains.
        self.assertFalse(os.path.exists(os.path.join(self.dst, "a", "b")))
        self.assertTrue(os.path.exists(os.path.join(self.dst, "a", "keep.txt")))

    def test_recreation_race_guard(self):
        # File deleted at compute time, then recreated locally before execute.
        _write(os.path.join(self.dst, "recreated.txt"), "mirror-copy")
        _write(os.path.join(self.src, "recreated.txt"), "local-again")
        rels = ["recreated.txt"]

        result = self.engine.phase2_execute(self.src, self.dst, self.trash, rels)

        self.assertEqual(result.skipped_recreated, 1)
        self.assertEqual(result.moved, 0)
        # Mirror copy left untouched (delete not propagated).
        self.assertEqual(_read(os.path.join(self.dst, "recreated.txt")), "mirror-copy")

    def test_realpath_containment_rejects_escape(self):
        # Craft a relpath that would escape dst_root.
        outside = os.path.join(self.root, "outside.txt")
        _write(outside, "must-survive")
        rels = [os.path.join("..", "outside.txt")]

        result = self.engine.phase2_execute(self.src, self.dst, self.trash, rels)

        self.assertEqual(result.skipped_unsafe, 1)
        self.assertEqual(result.moved, 0)
        # The out-of-tree file is untouched.
        self.assertTrue(os.path.exists(outside))
        self.assertEqual(_read(outside), "must-survive")

    def test_missing_mirror_skipped_silently(self):
        rels = ["never_existed.txt"]
        result = self.engine.phase2_execute(self.src, self.dst, self.trash, rels)
        self.assertEqual(result.moved, 0)
        self.assertEqual(result.skipped_unsafe, 0)
        self.assertEqual(result.errors, [])

    def test_deeper_paths_moved_before_parents(self):
        # A dir and a file inside it are both marked for deletion. Processing
        # the deeper file first lets the dir move (or prune) cleanly.
        _write(os.path.join(self.dst, "d", "inner.txt"), "i")
        rels = ["d", os.path.join("d", "inner.txt")]
        result = self.engine.phase2_execute(self.src, self.dst, self.trash, rels)
        self.assertEqual(result.errors, [])
        self.assertFalse(os.path.exists(os.path.join(self.dst, "d")))
        self.assertEqual(_read(os.path.join(self.trash, "d", "inner.txt")), "i")


class EndToEndDeletionTests(EngineTestBase):
    def test_full_two_phase_cycle(self):
        _write(os.path.join(self.src, "keep.txt"), "keep")
        _write(os.path.join(self.src, "docs", CJK_A), "doc")
        _write(os.path.join(self.src, "docs", "remove_me.txt"), "bye")
        self.engine.phase1(self.src, self.dst, self.trash, timeout=60)

        os.unlink(os.path.join(self.src, "docs", "remove_me.txt"))
        rels = self.engine.phase2_compute(self.src, self.dst, timeout=60)
        self.assertIn(os.path.join("docs", "remove_me.txt"), rels)

        result = self.engine.phase2_execute(self.src, self.dst, self.trash, rels)
        self.assertGreaterEqual(result.moved, 1)
        self.assertFalse(os.path.exists(os.path.join(self.dst, "docs", "remove_me.txt")))
        self.assertEqual(
            _read(os.path.join(self.trash, "docs", "remove_me.txt")), "bye"
        )
        # Kept files still mirrored.
        self.assertEqual(_read(os.path.join(self.dst, "keep.txt")), "keep")
        self.assertEqual(_read(os.path.join(self.dst, "docs", CJK_A)), "doc")


class OpenrsyncQuirkRegressionTest(EngineTestBase):
    def test_backup_dir_with_delete_during_must_not_delete(self):
        # QUIRK REGRESSION: on this machine (Apple openrsync) a WRITE-mode run
        # combining "-b --backup-dir" with "--delete-during" silently no-ops
        # every --delete* flag, so a dst-only file MUST survive. This test
        # deliberately runs that forbidden-in-production combination directly
        # against local temp dirs to pin the semantics the whole design rests on.
        import subprocess

        src = os.path.join(self.root, "q_src")
        dst = os.path.join(self.root, "q_dst")
        backup = os.path.join(self.root, "q_backup")
        os.makedirs(src)
        os.makedirs(dst)
        _write(os.path.join(src, "keep.txt"), "x")
        extra = os.path.join(dst, "dst_only.txt")
        _write(extra, "must-survive")

        proc = subprocess.run(
            [
                RSYNC, "-rltD", "-b", "--backup-dir=" + backup,
                "--modify-window=2", "--partial", "--delete-during",
                "--itemize-changes", "--exclude-from=" + self.excludes,
                src + "/", dst + "/",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=60,
        )

        self.assertTrue(
            os.path.exists(extra),
            "CRITICAL: openrsync deleted a dst-only file when '-b --backup-dir' "
            "was combined with '--delete-during'. macOS has CHANGED openrsync "
            "semantics: the two-phase deletion protocol's core safety assumption "
            "is broken. The daemon's capability probe must now fail closed "
            "(disable Phase 2 deletion propagation) and the whole delete path "
            "must be re-verified before this system is trusted again.",
        )


class Phase1FileTests(EngineTestBase):
    def test_mirrors_single_file_into_dst_dir(self):
        src_file = os.path.join(self.src, CJK_A)
        _write(src_file, "file-content")
        result = self.engine.phase1_file(src_file, self.dst, self.trash, timeout=60)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.rc, 0)
        # rsync preserves the basename -> dst/<basename>.
        self.assertEqual(_read(os.path.join(self.dst, CJK_A)), "file-content")
        self.assertGreaterEqual(result.files_copied, 1)

    def test_overwrite_versions_old_copy_into_backup_dir(self):
        src_file = os.path.join(self.src, "note.txt")
        _write(src_file, "v1")
        self.engine.phase1_file(src_file, self.dst, self.trash, timeout=60)
        _write(src_file, "v2-changed")
        result = self.engine.phase1_file(src_file, self.dst, self.trash, timeout=60)
        self.assertEqual(result.status, "ok")
        # New content mirrored; old version parked under the backup dir at basename.
        self.assertEqual(_read(os.path.join(self.dst, "note.txt")), "v2-changed")
        self.assertEqual(_read(os.path.join(self.trash, "note.txt")), "v1")

    def test_creates_missing_dst_dir(self):
        src_file = os.path.join(self.src, "x.txt")
        _write(src_file, "data")
        self.assertFalse(os.path.exists(self.dst))
        result = self.engine.phase1_file(src_file, self.dst, self.trash, timeout=60)
        self.assertEqual(result.status, "ok")
        self.assertTrue(os.path.isdir(self.dst))

    def test_argv_never_contains_delete_flag(self):
        # A crafted --delete* in any argv position must trip the safety assert.
        src_file = os.path.join(self.src, "z.txt")
        _write(src_file, "z")
        with self.assertRaises(AssertionError):
            self.engine.phase1_file(src_file, self.dst, "--delete-during", timeout=60)

    def test_streaming_mode_without_timeout(self):
        src_file = os.path.join(self.src, "s.txt")
        _write(src_file, "streamed")
        result = self.engine.phase1_file(src_file, self.dst, self.trash, timeout=None)
        self.assertEqual(result.status, "ok")
        self.assertEqual(_read(os.path.join(self.dst, "s.txt")), "streamed")


class ProgressCallbackTests(EngineTestBase):
    def test_phase1_reports_transfers_streaming(self):
        for i in range(5):
            _write(os.path.join(self.src, "f%02d.txt" % i), "x" * (i + 1))
        _write(os.path.join(self.src, "sub", CJK_A), "cjk")
        calls = []
        # timeout=None -> live streaming path.
        self.engine.phase1(self.src, self.dst, self.trash, timeout=None,
                           on_progress=lambda n, rel: calls.append((n, rel)))
        self.assertGreaterEqual(len(calls), 6)
        counts = [n for n, _ in calls]
        # files_done is a monotonically increasing within-run counter.
        self.assertEqual(counts, list(range(1, len(counts) + 1)))
        joined = "\n".join(rel for _, rel in calls)
        self.assertIn(CJK_A, joined)

    def test_phase1_reports_transfers_on_timeout_path(self):
        for i in range(3):
            _write(os.path.join(self.src, "g%02d.txt" % i), "y")
        calls = []
        # timeout set -> communicate path replays progress after completion.
        self.engine.phase1(self.src, self.dst, self.trash, timeout=60,
                           on_progress=lambda n, rel: calls.append((n, rel)))
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual([n for n, _ in calls], list(range(1, len(calls) + 1)))

    def test_phase1_file_reports_transfer(self):
        src_file = os.path.join(self.src, "only.txt")
        _write(src_file, "z")
        calls = []
        self.engine.phase1_file(src_file, self.dst, self.trash, timeout=None,
                                on_progress=lambda n, rel: calls.append((n, rel)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 1)
        self.assertIn("only.txt", calls[0][1])

    def test_callback_exception_never_breaks_sync(self):
        _write(os.path.join(self.src, "a.txt"), "a")

        def _boom(n, rel):
            raise RuntimeError("callback blew up")

        # Both paths must complete the sync despite a raising callback.
        result = self.engine.phase1(self.src, self.dst, self.trash, timeout=None,
                                    on_progress=_boom)
        self.assertEqual(result.status, "ok")
        self.assertEqual(_read(os.path.join(self.dst, "a.txt")), "a")


class CapabilityProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mycloud_probe_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_probe_returns_all_four_booleans(self):
        result = SyncEngine.capability_probe(self.tmp, RSYNC)
        self.assertEqual(
            set(result.keys()),
            {
                "backup_moves_overwritten",
                "delete_suppressed_with_backup",
                "deleting_line_format_ok",
                "inplace_updates_ok",
            },
        )
        for value in result.values():
            self.assertIsInstance(value, bool)

    def test_probe_inplace_fingerprint_passes_on_this_machine(self):
        # The PLAN-15 fast path depends on --inplace updating in place; the
        # probe must demonstrate it on the real openrsync.
        result = SyncEngine.capability_probe(self.tmp, RSYNC)
        self.assertTrue(result["inplace_updates_ok"])

    def test_probe_fingerprints_on_this_machine(self):
        result = SyncEngine.capability_probe(self.tmp, RSYNC)
        # On this machine (verified 2026-07-18) the -b flag suppresses --delete.
        self.assertTrue(result["delete_suppressed_with_backup"])
        self.assertTrue(result["backup_moves_overwritten"])
        self.assertTrue(result["deleting_line_format_ok"])


class Phase1BatchTests(EngineTestBase):
    """The v2 batched upload (PLAN 14.3): --from0 --files-from=- on stdin."""

    def test_uploads_only_listed_files(self):
        _write(os.path.join(self.src, "a.txt"), "a")
        _write(os.path.join(self.src, "sub", "b.txt"), "b")
        _write(os.path.join(self.src, "unwanted.txt"), "no")
        res = self.engine.phase1_batch(
            self.src, self.dst, self.trash, ["a.txt", "sub/b.txt"], timeout=60)
        self.assertEqual(res.status, "ok")
        self.assertTrue(os.path.exists(os.path.join(self.dst, "a.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.dst, "sub", "b.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.dst, "unwanted.txt")))

    def test_cjk_and_space_and_newline_safe_names(self):
        names = [CJK_A, "sub/" + CJK_B, "with\nnewline.txt"]
        for rel in names:
            _write(os.path.join(self.src, rel), "x")
        res = self.engine.phase1_batch(self.src, self.dst, self.trash, names,
                                       timeout=60)
        self.assertIn(res.status, ("ok", "partial"))
        for rel in names:
            self.assertTrue(os.path.exists(os.path.join(self.dst, rel)),
                            "missing on mirror: %r" % rel)

    def test_overwrite_is_versioned_into_backup_dir(self):
        _write(os.path.join(self.src, "f.txt"), "v1")
        self.engine.phase1_batch(self.src, self.dst, self.trash, ["f.txt"],
                                 timeout=60)
        _write(os.path.join(self.src, "f.txt"), "v2 longer content")
        self.engine.phase1_batch(self.src, self.dst, self.trash, ["f.txt"],
                                 timeout=60)
        self.assertEqual(_read(os.path.join(self.dst, "f.txt")),
                         "v2 longer content")
        self.assertTrue(os.path.exists(os.path.join(self.trash, "f.txt")))

    def test_streaming_progress_callback(self):
        for i in range(3):
            _write(os.path.join(self.src, "f%d.txt" % i), "c")
        seen = []
        # timeout=None takes the streaming path (live progress per transfer line).
        self.engine.phase1_batch(
            self.src, self.dst, self.trash, ["f0.txt", "f1.txt", "f2.txt"],
            timeout=None, on_progress=lambda done, cur: seen.append((done, cur)))
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], 3)

    def test_batch_argv_never_carries_delete(self):
        # Static guard mirrors the invariants test at the call level.
        _write(os.path.join(self.src, "a.txt"), "a")
        captured = {}
        orig = self.engine._run

        def _spy(argv, timeout, **kw):
            captured["argv"] = list(argv)
            return orig(argv, timeout, **kw)

        self.engine._run = _spy
        self.engine.phase1_batch(self.src, self.dst, self.trash, ["a.txt"],
                                 timeout=60)
        self.assertTrue(all("--delete" not in a for a in captured["argv"]))
        self.assertIn("--from0", captured["argv"])
        self.assertIn("--files-from=-", captured["argv"])

    def _captured_batch_argv(self, **kw):
        _write(os.path.join(self.src, "a.txt"), "a")
        captured = {}
        orig = self.engine._run

        def _spy(argv, timeout, **kw):
            captured["argv"] = list(argv)
            return orig(argv, timeout, **kw)

        self.engine._run = _spy
        res = self.engine.phase1_batch(
            self.src, self.dst, self.trash, ["a.txt"], timeout=60, **kw)
        self.assertEqual(res.status, "ok")
        return captured["argv"]

    def test_batch_default_argv_keeps_backup_and_times(self):
        argv = self._captured_batch_argv()
        self.assertIn("-rltD", argv)
        self.assertIn("-b", argv)
        self.assertTrue(any(a.startswith("--backup-dir") for a in argv))
        self.assertNotIn("--inplace", argv)

    def test_batch_inplace_argv_drops_backup_entirely(self):
        # PLAN 15: --inplace + -b is DESTRUCTIVE on this openrsync (verified
        # 2026-07-21: the destination file vanishes and the new content lands
        # in the backup dir), so an inplace argv must carry no -b/--backup-dir.
        argv = self._captured_batch_argv(inplace=True)
        self.assertIn("--inplace", argv)
        self.assertNotIn("-b", argv)
        self.assertFalse(any(a.startswith("--backup-dir") for a in argv))

    def test_batch_no_times_argv_drops_t_flag(self):
        argv = self._captured_batch_argv(no_times=True)
        self.assertIn("-rlD", argv)
        self.assertNotIn("-rltD", argv)

    def test_batch_inplace_writes_and_updates_in_place(self):
        # End-to-end against the real openrsync: a new file lands, an update
        # rewrites the same inode (true in-place, not silently ignored).
        _write(os.path.join(self.src, "f.txt"), "v1")
        res = self.engine.phase1_batch(self.src, self.dst, self.trash,
                                       ["f.txt"], timeout=60, inplace=True)
        self.assertEqual(res.status, "ok")
        mirror = os.path.join(self.dst, "f.txt")
        self.assertEqual(_read(mirror), "v1")
        inode = os.stat(mirror).st_ino
        _write(os.path.join(self.src, "f.txt"), "v2 much longer content")
        res = self.engine.phase1_batch(self.src, self.dst, self.trash,
                                       ["f.txt"], timeout=60, inplace=True)
        self.assertEqual(res.status, "ok")
        self.assertEqual(_read(mirror), "v2 much longer content")
        self.assertEqual(os.stat(mirror).st_ino, inode)
        # Nothing was versioned: inplace batches are new-file batches by
        # contract, so no backup entry may appear.
        self.assertFalse(os.path.exists(os.path.join(self.trash, "f.txt")))

    def test_batch_no_times_leaves_mirror_mtime_fresh(self):
        # Without -t the mirror mtime is the write time, not the source mtime.
        path = os.path.join(self.src, "old.txt")
        _write(path, "x")
        old = time.time() - 86400.0
        os.utime(path, (old, old))
        res = self.engine.phase1_batch(self.src, self.dst, self.trash,
                                       ["old.txt"], timeout=60, no_times=True)
        self.assertEqual(res.status, "ok")
        mirror_mtime = os.stat(os.path.join(self.dst, "old.txt")).st_mtime
        self.assertGreater(mirror_mtime, old + 3600.0)


if __name__ == "__main__":
    unittest.main()


class FieldReviewEngineTests(EngineTestBase):
    """Field review 2026-09-25 ("守护进程时常失败导致无法同步")."""

    def test_names_starting_with_hash_or_semicolon_are_not_dropped(self):
        # --files-from treats "#..." and ";..." lines as comments and silently
        # drops them with rc 0 -- recorded as uploaded, never on the NAS
        for rel in ("#notes#", ";semi", "sub/#deep#", "plain.txt"):
            _write(os.path.join(self.src, rel), rel)
        res = self.engine.phase1_batch(
            self.src, self.dst, self.trash,
            ["#notes#", ";semi", "sub/#deep#", "plain.txt"], timeout=60)
        self.assertEqual(res.status, "ok")
        for rel in ("#notes#", ";semi", "sub/#deep#", "plain.txt"):
            self.assertTrue(os.path.exists(os.path.join(self.dst, rel)), rel)

    def test_failure_kinds(self):
        fk = engine._failure_kind
        self.assertEqual(fk(30, "rsync: io timeout after 180 seconds -- exiting", False),
                         "io_timeout")                       # rsync 2.6.9
        self.assertEqual(fk(20, "rsync(123): error: poll: timeout", False),
                         "io_timeout")                       # openrsync
        self.assertEqual(fk(20, "", False), "killed")        # SIGTERM (watchdog)
        self.assertEqual(fk(20, "rsync error: received SIGINT, SIGTERM, or SIGHUP (code 20)",
                            False), "killed")
        self.assertEqual(fk(-9, "", False), "killed")
        self.assertEqual(fk(None, "", True), "killed")       # wedged / engine timeout
        self.assertEqual(fk(12, "rsync: connection unexpectedly closed", False), "error")

    def test_failed_paths_understand_rsync_269(self):
        err = ('rsync: send_files failed to open "/src/a b/c.txt": Permission denied (13)\n'
               'rsync: recv_generator: mkdir "/dst/x" failed: File exists (17)\n'
               "rsync error: some files could not be transferred (code 23) at main.c(977) "
               "[sender=2.6.9]\n")
        self.assertEqual(self.engine._parse_failed_paths(err),
                         ["/src/a b/c.txt", "/dst/x"])

    def test_unusable_destination_fails_without_running_rsync(self):
        blocker = os.path.join(self.dst, "file-not-dir")
        _write(blocker, "x")
        calls = []
        orig = self.engine._run
        self.engine._run = lambda *a, **k: calls.append(a) or orig(*a, **k)
        res = self.engine.phase1_batch(
            self.src, os.path.join(blocker, "sub"), self.trash, ["a"], timeout=60)
        self.assertEqual((res.status, res.kind), ("failure", "error"))
        self.assertEqual(calls, [])

    def test_batch_argv_has_progress_and_no_partial_or_contimeout(self):
        _write(os.path.join(self.src, "a.txt"), "a")
        captured = {}
        orig = self.engine._run

        def spy(argv, timeout, **kw):
            captured["argv"] = list(argv)
            return orig(argv, timeout, **kw)
        self.engine._run = spy
        self.engine.phase1_batch(self.src, self.dst, self.trash, ["a.txt"], timeout=60)
        argv = captured["argv"]
        self.assertIn("--progress", argv)
        self.assertNotIn("--partial", argv)
        self.assertFalse(any(a.startswith("--contimeout") for a in argv))


class StreamWedgeAndLivenessTests(unittest.TestCase):
    """_stream must not hang when the rsync client has exited but a child it
    forked still holds stderr (uninterruptible SMB I/O), and must report
    liveness from --progress redraws while one large file transfers."""

    def setUp(self):
        self.engine = SyncEngine(RSYNC, "/dev/null", logging.getLogger("t"),
                                 proc_registry=set())
        for name, value in (("_DRAIN_GRACE", 0.5), ("_REAP_GRACE", 0.5)):
            old = getattr(engine, name)
            setattr(engine, name, value)
            self.addCleanup(setattr, engine, name, old)

    SCRIPT = "(exec 1>/dev/null; sleep 30) & echo '>f+++++++++ a.txt'; exit 30"

    def test_straggler_holding_stderr_is_killed_and_the_run_completes(self):
        # the client prints and exits 30; a forked child keeps fd 2 open
        start = time.monotonic()
        rc, out, err, timed_out = self.engine._run_raw(["/bin/sh", "-c", self.SCRIPT], None)
        self.assertLess(time.monotonic() - start, 10.0)   # bounded, not 30s
        self.assertEqual(rc, 30)                           # the client's own verdict
        self.assertFalse(timed_out)                        # killable -> not wedged
        self.assertIn(b"a.txt", out)

    def test_unkillable_straggler_is_given_up_on_and_reported_wedged(self):
        # uninterruptible I/O ignores SIGKILL; model that by making the kill a no-op
        orig = engine._killpg
        engine._killpg = lambda pgid, sig: True
        self.addCleanup(setattr, engine, "_killpg", orig)

        class _Recording(set):
            pids = []

            def add(self, proc):
                self.pids.append(proc.pid)
                set.add(self, proc)
        self.engine.proc_registry = _Recording()
        start = time.monotonic()
        rc, out, err, timed_out = self.engine._run_raw(["/bin/sh", "-c", self.SCRIPT], None)
        self.assertLess(time.monotonic() - start, 10.0)
        self.assertTrue(timed_out)                         # -> SyncResult kind "killed"
        # clean up exactly the straggler this test left behind (its group)
        for pgid in _Recording.pids:
            orig(pgid, 9)

    def test_progress_redraws_count_as_liveness_and_lines_still_parse(self):
        script = ("printf '>f+++++++++ big.bin\\n'; "
                  "for i in 1 2 3; do printf '\\r   %d0%%' $i; sleep 1.1; done; "
                  "printf '\\r 100%% (xfer#1)\\n'")
        alive = []
        seen = []
        rc, out, err, timed_out = self.engine._run_raw(
            ["/bin/sh", "-c", script], None,
            on_progress=lambda done, rel: seen.append(rel),
            on_alive=lambda: alive.append(time.monotonic()))
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["big.bin"])
        self.assertGreaterEqual(len(alive), 3)  # heard from during the file
        self.assertNotIn(b"\r", out)            # only the last frame is kept
