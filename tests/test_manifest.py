import tests._path  # noqa: F401

"""Unit tests for the v2 local manifest database (PLAN section 14, M1).

Pure SQLite + filesystem walk; no rsync, no NAS. The dataless predicate is
injected (tests cannot create real dataless files)."""

import os
import shutil
import tempfile
import unittest

from mycloud import manifest as manifest_mod
from mycloud.manifest import Manifest


def _no_dataless(path, st):
    return False


class ManifestTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mycloud-manifest-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(self.src)
        self.m = Manifest(os.path.join(self.tmp, "manifest.db"))
        self.addCleanup(self.m.close)

    def _mk(self, rel, content="x"):
        path = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as h:
            h.write(content)
        return path

    def _commit_all(self, folder, relpaths):
        rows = []
        for rel in relpaths:
            st = os.lstat(os.path.join(self.src, rel))
            rows.append((rel, st.st_size, st.st_mtime_ns, st.st_ino))
        self.m.commit_uploaded(folder, rows)


class TestPlanBasics(ManifestTestBase):
    def test_initial_all_new_are_uploads(self):
        self._mk("a.txt"); self._mk("sub/b.txt"); self._mk("c.txt")
        plan = self.m.plan_folder("F", self.src, [], _no_dataless)
        self.assertEqual(sorted(plan.uploads), ["a.txt", "c.txt", "sub/b.txt"])
        self.assertEqual(plan.deletions, [])
        self.assertEqual(plan.renames, [])
        self.assertEqual(plan.totals["upload_files"], 3)

    def test_committed_files_are_unchanged(self):
        self._mk("a.txt")
        p1 = self.m.plan_folder("F", self.src, [], _no_dataless)
        self._commit_all("F", p1.uploads)
        p2 = self.m.plan_folder("F", self.src, [], _no_dataless)
        self.assertEqual(p2.uploads, [])
        self.assertEqual(p2.totals["unchanged_files"], 1)

    def test_modify_reuploads(self):
        self._mk("a.txt", "small")
        self._commit_all("F", ["a.txt"])
        self._mk("a.txt", "a much larger different content")
        plan = self.m.plan_folder("F", self.src, [], _no_dataless)
        self.assertEqual(plan.uploads, ["a.txt"])

    def test_delete_is_a_deletion(self):
        self._mk("a.txt"); self._mk("b.txt")
        self._commit_all("F", ["a.txt", "b.txt"])
        os.remove(os.path.join(self.src, "a.txt"))
        plan = self.m.plan_folder("F", self.src, [], _no_dataless)
        self.assertEqual(plan.deletions, ["a.txt"])
        self.assertEqual(plan.uploads, [])

    def test_excludes_prune_dirs_and_files(self):
        self._mk("keep.txt"); self._mk(".git/config"); self._mk("x.tmp")
        plan = self.m.plan_folder("F", self.src, [".git", "*.tmp"], _no_dataless)
        self.assertEqual(plan.uploads, ["keep.txt"])

    def test_new_set_marks_appeared_files_only(self):
        # PLAN 15 P0: new_set drives the --inplace fast path, so it must hold
        # exactly the files with NO manifest row (nothing on the NAS to
        # version) and never a changed or renamed file.
        self._mk("old.txt", "committed content")
        self._commit_all("F", ["old.txt"])
        self._mk("old.txt", "changed content, different size")   # changed
        self._mk("brand-new.txt")                                # appeared
        plan = self.m.plan_folder("F", self.src, [], _no_dataless)
        self.assertEqual(sorted(plan.uploads), ["brand-new.txt", "old.txt"])
        self.assertEqual(plan.new_set, {"brand-new.txt"})

    def test_new_set_excludes_rename_targets(self):
        self._mk("orig.txt", "some content here")
        self._commit_all("F", ["orig.txt"])
        os.rename(os.path.join(self.src, "orig.txt"),
                  os.path.join(self.src, "moved.txt"))
        plan = self.m.plan_folder("F", self.src, [], _no_dataless)
        self.assertEqual(plan.renames, [("orig.txt", "moved.txt")])
        self.assertEqual(plan.new_set, set())


class TestRenames(ManifestTestBase):
    def test_rename_detected_not_upload_delete(self):
        self._mk("orig.txt", "some content here")
        self._commit_all("F", ["orig.txt"])
        os.rename(os.path.join(self.src, "orig.txt"),
                  os.path.join(self.src, "moved.txt"))
        plan = self.m.plan_folder("F", self.src, [], _no_dataless)
        self.assertEqual(plan.renames, [("orig.txt", "moved.txt")])
        self.assertEqual(plan.uploads, [])
        self.assertEqual(plan.deletions, [])

    def test_commit_renamed_stabilizes(self):
        self._mk("orig.txt", "content")
        self._commit_all("F", ["orig.txt"])
        os.rename(os.path.join(self.src, "orig.txt"),
                  os.path.join(self.src, "moved.txt"))
        plan = self.m.plan_folder("F", self.src, [], _no_dataless)
        self.m.commit_renamed("F", plan.renames)
        plan2 = self.m.plan_folder("F", self.src, [], _no_dataless)
        self.assertEqual(plan2.uploads, [])
        self.assertEqual(plan2.renames, [])
        self.assertEqual(plan2.deletions, [])

    def test_ambiguous_hardlink_falls_back_to_upload_delete(self):
        # Two hardlinks share (inode, size, mtime): rename identity is ambiguous,
        # so the vanished row must NOT be paired -- it degrades to upload+delete.
        self._mk("orig.txt", "hardlinked payload")
        self._commit_all("F", ["orig.txt"])
        os.rename(os.path.join(self.src, "orig.txt"),
                  os.path.join(self.src, "r1.txt"))
        os.link(os.path.join(self.src, "r1.txt"),
                os.path.join(self.src, "r2.txt"))
        plan = self.m.plan_folder("F", self.src, [], _no_dataless)
        self.assertEqual(plan.renames, [])
        self.assertEqual(plan.deletions, ["orig.txt"])
        self.assertEqual(sorted(plan.uploads), ["r1.txt", "r2.txt"])

    def test_zero_byte_files_never_rename(self):
        # size>0 is required for rename identity (empty files are too easily
        # confused); a moved empty file is upload+delete, not a rename.
        self._mk("empty", "")
        self._commit_all("F", ["empty"])
        os.rename(os.path.join(self.src, "empty"),
                  os.path.join(self.src, "empty2"))
        plan = self.m.plan_folder("F", self.src, [], _no_dataless)
        self.assertEqual(plan.renames, [])
        self.assertEqual(plan.deletions, ["empty"])
        self.assertEqual(plan.uploads, ["empty2"])


class TestDatalessAndPending(ManifestTestBase):
    def test_dataless_kept_out_of_uploads(self):
        self._mk("normal.txt"); self._mk("cloud.key")

        def _dl(path, st):
            return path.endswith("cloud.key")

        plan = self.m.plan_folder("F", self.src, [], _dl)
        self.assertEqual(plan.uploads, ["normal.txt"])
        self.assertEqual(plan.dataless, ["cloud.key"])
        self.assertEqual(plan.totals["dataless_files"], 1)

    def test_mark_pending_forces_reupload(self):
        self._mk("a.txt")
        self._commit_all("F", ["a.txt"])
        self.assertEqual(self.m.mark_pending("F", ["a.txt"]), 1)
        plan = self.m.plan_folder("F", self.src, [], _no_dataless)
        self.assertEqual(plan.uploads, ["a.txt"])

    def test_mark_pending_ignores_unknown(self):
        self.assertEqual(self.m.mark_pending("F", ["ghost.txt"]), 0)


class TestScopingAndIntrospection(ManifestTestBase):
    def test_subtree_scope_no_false_deletions(self):
        self._mk("sub/b.txt"); self._mk("top.txt")
        self._commit_all("F", ["sub/b.txt", "top.txt"])
        # Scan only sub/: top.txt is out of scope and must not look deleted.
        plan = self.m.plan_folder("F", self.src, [], _no_dataless,
                                  subtrees=["sub"])
        self.assertEqual(plan.deletions, [])
        self.assertEqual(plan.uploads, [])

    def test_counts_and_bytes(self):
        self._mk("a.txt", "abc"); self._mk("b.txt", "de")
        self._commit_all("F", ["a.txt", "b.txt"])
        c = self.m.counts("F")
        self.assertEqual(c["files"], 2)
        self.assertEqual(c["bytes"], 5)
        self.assertEqual(c["pending"], 0)

    def test_forget_folder(self):
        self._mk("a.txt")
        self._commit_all("F", ["a.txt"])
        self.m.forget_folder("F")
        self.assertEqual(self.m.counts("F")["files"], 0)

    def test_commit_deleted_removes_rows(self):
        self._mk("a.txt"); self._mk("b.txt")
        self._commit_all("F", ["a.txt", "b.txt"])
        self.m.commit_deleted("F", ["a.txt"])
        self.assertEqual(self.m.counts("F")["files"], 1)

    def test_meta_roundtrip(self):
        self.assertIsNone(self.m.get_meta("nope"))
        self.m.set_meta("last_deep_verify_at", "2026-07-20T00:00:00+00:00")
        self.assertEqual(self.m.get_meta("last_deep_verify_at"),
                         "2026-07-20T00:00:00+00:00")

    def test_reopen_persists(self):
        self._mk("a.txt")
        self._commit_all("F", ["a.txt"])
        self.m.close()
        m2 = Manifest(os.path.join(self.tmp, "manifest.db"))
        self.addCleanup(m2.close)
        self.assertEqual(m2.counts("F")["files"], 1)


class TestP1WalkErrors(ManifestTestBase):
    """plan_folder must COUNT the OSErrors its walk swallows (PLAN 16.2 T-B1).

    A swallowed scandir error hides a whole subtree, and the diff reads that
    absence as 'vanished' -- a deletion plan for files that are all alive
    (real incident 2026-07-30, iCloud fileproviderd hiccup). The plan stays a
    pure diff; the count is what lets the daemon suppress the propagation.
    """

    def _break_scandir_for(self, target):
        """Make os.scandir raise PermissionError for target only, real elsewhere."""
        real = os.scandir
        wanted = os.path.realpath(target)

        def _fake(path="."):
            if os.path.realpath(os.fspath(path)) == wanted:
                raise PermissionError(13, "Permission denied", str(path))
            return real(path)

        manifest_mod.os.scandir = _fake
        self.addCleanup(setattr, manifest_mod.os, "scandir", real)

    def test_clean_walk_reports_zero_errors(self):
        self._mk("a.txt"); self._mk("sub/b.txt")
        plan = self.m.plan_folder("F", self.src, [], _no_dataless)
        self.assertEqual(plan.walk_errors, 0)

    def test_failed_scandir_counted_and_subtree_still_reads_as_deleted(self):
        self._mk("keep.txt"); self._mk("flaky/one.txt"); self._mk("flaky/two.txt")
        self._commit_all("F", ["keep.txt", "flaky/one.txt", "flaky/two.txt"])
        self._break_scandir_for(os.path.join(self.src, "flaky"))

        plan = self.m.plan_folder("F", self.src, [], _no_dataless)
        self.assertGreaterEqual(plan.walk_errors, 1)
        # Behaviour of the plan itself is unchanged -- the phantom deletions are
        # still listed here, and the daemon is the layer that refuses to act on
        # them. keep.txt (outside the broken subtree) is untouched.
        self.assertEqual(plan.deletions, ["flaky/one.txt", "flaky/two.txt"])
        self.assertEqual(plan.uploads, [])
        self.assertEqual(plan.totals["unchanged_files"], 1)

    def test_missing_scope_root_is_an_error(self):
        self._mk("sub/b.txt")
        self._commit_all("F", ["sub/b.txt"])
        shutil.rmtree(os.path.join(self.src, "sub"))
        plan = self.m.plan_folder("F", self.src, [], _no_dataless,
                                  subtrees=["sub"])
        self.assertEqual(plan.walk_errors, 1)


class TestP1ForgetExcluded(ManifestTestBase):
    """PLAN 16.2 T-B2: newly excluded rows must be FORGOTTEN, not deleted."""

    def _seed(self, relpaths):
        self.m.commit_uploaded(
            "F", [(rel, 10, 1234567, 900 + i)
                  for i, rel in enumerate(relpaths)])

    def test_matches_any_path_component_or_basename(self):
        self._seed([".venv/lib/x.py", "a/__pycache__/m.pyc", "x.pyc",
                    "keep/real.txt"])
        dropped = self.m.forget_excluded(
            "F", ["__pycache__", "*.pyc", ".venv"])
        self.assertEqual(dropped, 3)
        self.assertEqual(sorted(self.m.rows("F")), ["keep/real.txt"])

    def test_trailing_slash_in_pattern_is_stripped(self):
        self._seed([".venv/lib/x.py", "keep.txt"])
        self.assertEqual(self.m.forget_excluded("F", [".venv/"]), 1)
        self.assertEqual(sorted(self.m.rows("F")), ["keep.txt"])

    def test_is_idempotent(self):
        self._seed(["a/__pycache__/m.pyc", "keep.txt"])
        self.assertEqual(self.m.forget_excluded("F", ["__pycache__"]), 1)
        self.assertEqual(self.m.forget_excluded("F", ["__pycache__"]), 0)

    def test_other_folders_are_untouched(self):
        self._seed([".venv/x.py"])
        self.m.commit_uploaded("G", [(".venv/x.py", 10, 1234567, 42)])
        self.assertEqual(self.m.forget_excluded("F", [".venv"]), 1)
        self.assertEqual(self.m.counts("G")["files"], 1)

    def test_empty_patterns_and_empty_table_return_zero(self):
        self.assertEqual(self.m.forget_excluded("F", []), 0)
        self.assertEqual(self.m.forget_excluded("F", None), 0)
        self.assertEqual(self.m.forget_excluded("F", [".venv"]), 0)
        self._seed(["keep.txt"])
        self.assertEqual(self.m.forget_excluded("F", []), 0)
        self.assertEqual(self.m.counts("F")["files"], 1)

    def test_forget_is_a_database_only_operation(self):
        # The rows go; nothing on disk is touched (the NAS copies stay as
        # harmless orphans, and the local files were never ours to remove).
        self._mk("keep/real.txt")
        self._seed(["keep/real.txt", "a/__pycache__/m.pyc"])
        self.assertEqual(self.m.forget_excluded("F", ["__pycache__"]), 1)
        self.assertTrue(os.path.exists(os.path.join(self.src, "keep/real.txt")))


if __name__ == "__main__":
    unittest.main()
