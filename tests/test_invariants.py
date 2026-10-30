import tests._path  # noqa: F401

"""Static safety-net invariants over src/mycloud/ (PLAN sections 2, 6.1, 6.4).

These tests parse the source (never import it) and assert the three properties
that keep MyCloud from ever destroying data by accident:

(a) shutil.rmtree is called only in trash.py (pruning is the single code path
    that truly destroys NAS data).
(b) No write-mode rsync argv ever carries a --delete* flag. On this machine
    openrsync silently no-ops the whole run when -b is combined with --delete*,
    so deletion must stay a dry-run computation + Python rename. The one
    legitimate write-mode --delete lives in engine.capability_probe, which
    deliberately fingerprints that suppression behavior in a throwaway sandbox.
(c) Every subprocess.run/Popen call site passes a timeout, except the two
    deliberately long-lived, watchdog-covered processes (the rsync Popen and the
    caffeinate Popen).

Robust, not clever: an argv is recognized as "rsync-like" when it is a list
literal containing a flag string, and "write-mode" when it does not contain the
dry-run flag "-n". A --delete* element in such a list outside the whitelisted
function is a violation.
"""

import ast
import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_PKG = os.path.abspath(os.path.join(_HERE, "..", "src", "mycloud"))
_SRC_TOP = os.path.abspath(os.path.join(_HERE, "..", "src"))


def _src_files():
    # type: () -> list
    """Every .py under src/ -- the mycloud package AND the top-level entry points
    (mycloudctl.py, mycloudd.py). Phase 2 put new write paths into
    src/mycloudctl.py, so the safety net must cover it too, not just the package.
    """
    files = []
    for name in sorted(os.listdir(_SRC_PKG)):
        if name.endswith(".py"):
            files.append((name, os.path.join(_SRC_PKG, name)))
    for name in sorted(os.listdir(_SRC_TOP)):
        if name.endswith(".py"):
            files.append((name, os.path.join(_SRC_TOP, name)))
    return files


def _parse(path):
    # type: (str) -> ast.AST
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    return ast.parse(source, filename=path)


def _annotate_parents(tree):
    # type: (ast.AST) -> None
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._mc_parent = node  # type: ignore[attr-defined]


def _enclosing_func(node):
    # type: (ast.AST) -> object
    cur = getattr(node, "_mc_parent", None)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = getattr(cur, "_mc_parent", None)
    return None


def _str_const(node):
    # type: (ast.AST) -> object
    """Return the str value of a string-literal node, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _list_string_elems(list_node):
    # type: (ast.List) -> list
    values = []
    for elt in list_node.elts:
        val = _str_const(elt)
        if val is not None:
            values.append(val)
    return values


class TestNoRmtreeOutsideTrash(unittest.TestCase):
    """(a) shutil.rmtree may be *called* only from trash.py."""

    def test_rmtree_calls_only_in_trash(self):
        offenders = []
        for name, path in _src_files():
            tree = _parse(path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_rmtree = (
                    (isinstance(func, ast.Attribute) and func.attr == "rmtree")
                    or (isinstance(func, ast.Name) and func.id == "rmtree")
                )
                if is_rmtree and name != "trash.py":
                    offenders.append("%s:%d" % (name, getattr(node, "lineno", 0)))
        self.assertEqual(
            offenders, [],
            "shutil.rmtree may only be called in trash.py; found: " + repr(offenders))


class TestNoWriteModeDelete(unittest.TestCase):
    """(b) No write-mode rsync argv carries a --delete* flag."""

    # The only function permitted to construct a write-mode -b + --delete argv:
    # it fingerprints openrsync's delete-suppression behavior in a sandbox.
    _WRITE_DELETE_WHITELIST = frozenset(["capability_probe"])

    def test_delete_literals_confined_to_engine(self):
        offenders = []
        for name, path in _src_files():
            if name == "engine.py":
                continue
            tree = _parse(path)
            for node in ast.walk(tree):
                val = _str_const(node)
                if val is not None and "--delete" in val:
                    offenders.append(
                        "%s:%d %r" % (name, getattr(node, "lineno", 0), val))
        self.assertEqual(
            offenders, [],
            "a --delete string literal appears outside engine.py: " + repr(offenders))

    def test_no_write_mode_delete_argv(self):
        offenders = []
        for name, path in _src_files():
            tree = _parse(path)
            _annotate_parents(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.List):
                    continue
                elems = _list_string_elems(node)
                if not elems:
                    continue
                # Only consider flag-bearing lists (rsync-like argv).
                if not any(e.startswith("-") for e in elems):
                    continue
                has_delete = any(e.startswith("--delete") for e in elems)
                if not has_delete:
                    continue
                is_dry_run = "-n" in elems
                if is_dry_run:
                    continue  # dry-run --delete is the safe deletion-compute path
                func = _enclosing_func(node)
                if func not in self._WRITE_DELETE_WHITELIST:
                    offenders.append(
                        "%s:%d in %s -> %r"
                        % (name, getattr(node, "lineno", 0), func, elems))
        self.assertEqual(
            offenders, [],
            "write-mode rsync argv contains a --delete* flag: " + repr(offenders))


class TestInplaceNeverCombinesWithBackup(unittest.TestCase):
    """--inplace + -b/--backup-dir is DESTRUCTIVE on this openrsync (verified
    2026-07-21): the destination file vanishes and the NEW content lands in
    the backup dir, losing the old version outright (PLAN section 15 P0).

    Statically: (i) '--inplace' string literals may appear only in engine.py
    (the sole argv builder); (ii) no list literal anywhere may combine
    '--inplace' with '-b'/'--backup-dir'. Dynamically composed argvs are
    covered by SyncEngine._assert_inplace_backup_exclusive at call time.
    """

    def test_inplace_literal_confined_to_engine(self):
        # Match only flag-shaped literals (an argv element starts with the
        # flag); prose mentioning --inplace in docstrings/log lines is fine.
        offenders = []
        for name, path in _src_files():
            if name == "engine.py":
                continue
            tree = _parse(path)
            for node in ast.walk(tree):
                val = _str_const(node)
                if val is not None and val.startswith("--inplace"):
                    offenders.append(
                        "%s:%d %r" % (name, getattr(node, "lineno", 0), val))
        self.assertEqual(
            offenders, [],
            "an --inplace flag literal appears outside engine.py: "
            + repr(offenders))

    def test_no_list_literal_combines_inplace_with_backup(self):
        offenders = []
        for name, path in _src_files():
            tree = _parse(path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.List):
                    continue
                elems = _list_string_elems(node)
                if "--inplace" not in elems:
                    continue
                if any(e == "-b" or e.startswith("--backup-dir")
                       for e in elems):
                    offenders.append(
                        "%s:%d -> %r"
                        % (name, getattr(node, "lineno", 0), elems))
        self.assertEqual(
            offenders, [],
            "an argv list combines --inplace with -b/--backup-dir "
            "(destroys the destination file on this openrsync): "
            + repr(offenders))


class TestSubprocessTimeouts(unittest.TestCase):
    """(c) Every subprocess.run/Popen call passes a timeout (with a whitelist).

    Whitelisted long-lived processes have watchdog coverage instead of a
    subprocess timeout: the rsync Popen (killed via the guard watchdog's process
    group SIGTERM/SIGKILL) and the caffeinate Popen (terminated on shutdown).
    util.run's Popen is whitelisted too: the timeout lives on the following
    communicate(timeout=...) and is enforced by an UNWEDGEABLE kill path
    (killpg SIGKILL + bounded reap + abandon), a stronger guarantee than a plain
    Popen timeout kwarg -- it is the very mechanism that makes every OTHER
    call site's timeout unhangable.
    """

    # (basename, enclosing function name) pairs allowed to omit timeout.
    _WHITELIST = frozenset([
        ("engine.py", "_run_raw"),        # rsync Popen; watchdog kills the group
        ("daemon.py", "_start_caffeinate"),  # caffeinate Popen; terminated on exit
        ("daemon.py", "_hold_awake"),        # caffeinate -w <pid> for one pass;
                                             # terminated in the pass's finally
        ("util.py", "run"),               # timeout on communicate() + killpg reap
    ])

    @staticmethod
    def _is_subprocess_call(func):
        # type: (ast.AST) -> bool
        if not isinstance(func, ast.Attribute):
            return False
        if func.attr not in ("run", "Popen"):
            return False
        return isinstance(func.value, ast.Name) and func.value.id == "subprocess"

    def test_all_calls_have_timeout(self):
        offenders = []
        for name, path in _src_files():
            tree = _parse(path)
            _annotate_parents(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not self._is_subprocess_call(node.func):
                    continue
                has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
                if has_timeout:
                    continue
                func = _enclosing_func(node)
                if (name, func) in self._WHITELIST:
                    continue
                offenders.append(
                    "%s:%d in %s" % (name, getattr(node, "lineno", 0), func))
        self.assertEqual(
            offenders, [],
            "subprocess call without a timeout (and not whitelisted): "
            + repr(offenders))


class TestV2ModulesCovered(unittest.TestCase):
    """The v2 modules must be inside the static safety-net scan (regression:
    a new write path added to a module the scan misses would go unchecked)."""

    def test_manifest_and_icloud_scanned(self):
        names = set(name for name, _p in _src_files())
        self.assertIn("manifest.py", names)
        self.assertIn("icloud.py", names)


class TestDeepVerifyNeverDrivesSourceRsync(unittest.TestCase):
    """Data-safety invariant discovered empirically 2026-07-20: an `rsync -n`
    dry-run over the SOURCE tree MATERIALIZES iCloud dataless files (downloads
    them). So deep verify must reconcile the manifest against the NAS by
    os.walk/lstat only and NEVER invoke the rsync engine over the source. This
    asserts the deep_verify / _verify_folder bodies contain no `self.engine`
    access (their only deletion path, self._apply_deletions, uses Python renames,
    not rsync)."""

    def _func_nodes(self, tree, names):
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name in names:
                found[node.name] = node
        return found

    def test_verify_functions_do_not_touch_engine(self):
        path = os.path.join(_SRC_PKG, "daemon.py")
        tree = _parse(path)
        funcs = self._func_nodes(tree, {"deep_verify", "_verify_folder"})
        self.assertEqual(set(funcs), {"deep_verify", "_verify_folder"},
                         "deep_verify/_verify_folder not found in daemon.py")
        offenders = []
        for fname, fnode in funcs.items():
            for node in ast.walk(fnode):
                if (isinstance(node, ast.Attribute) and node.attr == "engine"
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "self"):
                    offenders.append("%s:%d" % (fname, getattr(node, "lineno", 0)))
        self.assertEqual(
            offenders, [],
            "deep verify must not drive the rsync engine over the source "
            "(it would materialize iCloud dataless files): " + repr(offenders))


if __name__ == "__main__":
    unittest.main()


class TestVersionsAgree(unittest.TestCase):
    """The app bundle version and the backend package version must match.

    The GUI decides whether the INSTALLED backend is stale by comparing its
    reported version with the app's own. If the two sources of truth drift,
    every install would look stale (or a stale one would look current), and the
    one-click 更新后端 button becomes noise (2026-09-12)."""

    def test_build_script_version_matches_package_version(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = open(os.path.join(root, "gui", "build_app.sh"), encoding="utf-8").read()
        match = re.search(r'^VERSION="([^"]+)"', script, re.M)
        self.assertIsNotNone(match, "build_app.sh must define VERSION")
        init = open(os.path.join(root, "src", "mycloud", "__init__.py"),
                    encoding="utf-8").read()
        pkg = re.search(r'__version__ = "([^"]+)"', init)
        self.assertIsNotNone(pkg, "mycloud/__init__.py must define __version__")
        self.assertEqual(match.group(1), pkg.group(1),
                         "gui/build_app.sh VERSION and mycloud.__version__ must agree")
