"""GUI<->backend contract test (PLAN 12.2 / task G6 step 6).

Pins the mycloudctl JSON output against what the Swift GUI declares it will
decode. The Swift side (gui/src/Models.swift) is the source of truth for which
keys the GUI reads; this test extracts those keys by pragmatic string parsing of
the Swift file, then spawns the real ctl (`config-get --json`, `status --json`,
`doctor --json`) against a temp MYCLOUD_APP with fixture config/state and asserts
that every declared key either appears in the emitted JSON or is a documented
Optional Swift property. This catches contract drift in either direction.

No real NAS is touched: the fixture uses host 127.0.0.1 (so the doctor TCP probe
and any mount path fail fast locally), and MountResult's success shape is
exercised in-process with a stubbed mount factory.
"""

import tests._path  # noqa: F401

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

import mycloudctl

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CTL_PY = os.path.join(_REPO, "src", "mycloudctl.py")
_MODELS_SWIFT = os.path.join(_REPO, "gui", "src", "Models.swift")


# --------------------------------------------------------------------------
# Swift model extraction (pragmatic string parsing, not a real Swift parser).
# --------------------------------------------------------------------------

def _parse_swift_models(text):
    """Return {StructName: {"keys": {prop: json_key}, "optional": set(prop)}}.

    - CodingKeys `case foo = "foo_bar"` -> json key "foo_bar" for property foo.
    - CodingKeys `case a, b` (bare) -> json key == property name for each.
    - A property `var x: T? = ...` is treated as a documented Optional.
    """
    structs = {}
    marks = list(re.finditer(r"\nstruct (\w+)", text))
    for i, m in enumerate(marks):
        name = m.group(1)
        start = m.start()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[start:end]

        optional = set()
        for pm in re.finditer(r"var (\w+)\s*:\s*([^\n=]*)", body):
            prop, typepart = pm.group(1), pm.group(2)
            if "?" in typepart:
                optional.add(prop)

        keys = {}
        ck = re.search(r"enum CodingKeys[^\{]*\{(.*?)\}", body, re.S)
        if ck:
            for line in ck.group(1).splitlines():
                line = line.strip()
                if not line.startswith("case"):
                    continue
                rest = line[len("case"):].strip()
                if "=" in rest:
                    lhs, rhs = rest.split("=", 1)
                    keys[lhs.strip()] = rhs.strip().strip('"')
                else:
                    for part in rest.split(","):
                        p = part.strip()
                        if p:
                            keys[p] = p
        structs[name] = {"keys": keys, "optional": optional}
    return structs


class SwiftParseTests(unittest.TestCase):
    """Guard the parser itself so a false green is impossible."""

    def setUp(self):
        with open(_MODELS_SWIFT, encoding="utf-8") as h:
            self.models = _parse_swift_models(h.read())

    def test_expected_structs_present(self):
        for name in ("Config", "Source", "Status", "Progress",
                     "FolderStatus", "Derived", "DoctorRow", "MountResult"):
            self.assertIn(name, self.models, "missing struct %s" % name)

    def test_renamed_keys_extracted(self):
        cfg = self.models["Config"]["keys"]
        self.assertEqual(cfg.get("destRel"), "dest_rel")
        self.assertEqual(cfg.get("retentionDays"), "retention_days")
        self.assertIn("host", cfg)  # bare case
        self.assertEqual(cfg["host"], "host")

    def test_optionals_detected(self):
        self.assertIn("notice", self.models["Config"]["optional"])
        self.assertIn("totalFiles", self.models["Progress"]["optional"])
        self.assertNotIn("filesDone", self.models["Progress"]["optional"])


# --------------------------------------------------------------------------
# Fixture: a temp MYCLOUD_APP with config.ini + runtime/state.json.
# --------------------------------------------------------------------------

_CONFIG_INI = """\
[server]
host = 127.0.0.1
share = SHARE
username = admin

[backup]
folders =
    {src_dir}
    {src_file}
exclude =
    *.tmp
dest_rel = MyCloud
retention_days = 30
trash_max_gb = 50
hot_interval = 30
full_interval = 900
max_delete_files = 200
max_delete_pct = 20
offline_notify_hours = 24
watch_mode = auto
"""

# state.json exercising progress + held_deletions + failed_files + orphaned.
_STATE = {
    "state": "SYNCING",
    "updated_at": "2026-07-19T10:00:00+00:00",
    "last_sync_ok_at": "2026-07-19T09:00:00+00:00",
    "last_error": None,
    "deletion_disabled": False,
    "watcher_degraded": True,
    "progress": {
        "active": True,
        "folder": "src1",
        "phase": "transferring",
        "files_done": 3,
        "total_files": 10,
        "current": "a/中文.txt",
        "started_at": "2026-07-19T10:00:00+00:00",
    },
    "folders": {
        "src1": {
            "state": "deletionsHeld",
            "last_ok": "2026-07-19T09:00:00+00:00",
            "files_copied": 5,
            "files_trashed": 1,
            "held_deletions": ["gone1.txt", "gone2.txt"],
            "failed_files": ["huge.iso"],
            "orphaned": False,
            # PLAN M5: always present on v2 folders (pending-download when the
            # policy is download; skipped-dataless when the policy is skip).
            "icloud_skipped": {
                "count": 2, "bytes": 4096,
                "samples": ["cloud/a.key", "cloud/b.numbers"],
            },
        },
        "note.txt": {
            "state": "upToDate",
            "last_ok": "2026-07-19T09:00:00+00:00",
            "files_copied": 1,
            "files_trashed": 0,
            "held_deletions": [],
            "failed_files": [],
            "orphaned": True,
            "icloud_skipped": {"count": 0, "bytes": 0, "samples": []},
        },
    },
}


def _make_fixture():
    """Create a temp MYCLOUD_APP dir; return its path (caller removes it)."""
    app = tempfile.mkdtemp(prefix="mycloud-contract-")
    os.makedirs(os.path.join(app, "runtime"))
    os.makedirs(os.path.join(app, "mnt"))
    src_dir = os.path.join(app, "src1")
    src_file = os.path.join(app, "note.txt")
    os.makedirs(src_dir)
    with open(src_file, "w", encoding="utf-8") as h:
        h.write("hi\n")
    with open(os.path.join(app, "config.ini"), "w", encoding="utf-8") as h:
        h.write(_CONFIG_INI.format(src_dir=src_dir, src_file=src_file))
    with open(os.path.join(app, "runtime", "state.json"),
              "w", encoding="utf-8") as h:
        json.dump(_STATE, h)
    return app


def _run_ctl(app, args):
    """Spawn the real ctl against MYCLOUD_APP=app; return parsed stdout JSON."""
    env = dict(os.environ)
    env["MYCLOUD_APP"] = app
    proc = subprocess.run(
        [sys.executable, _CTL_PY] + args,
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=60,
    )
    return json.loads(proc.stdout.decode("utf-8"))


# --------------------------------------------------------------------------
# The contract assertions.
# --------------------------------------------------------------------------

class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_MODELS_SWIFT, encoding="utf-8") as h:
            cls.models = _parse_swift_models(h.read())
        cls.app = _make_fixture()
        cls.config = _run_ctl(cls.app, ["config-get", "--json"])
        cls.status = _run_ctl(cls.app, ["status", "--json"])
        cls.doctor = _run_ctl(cls.app, ["doctor", "--json"])

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.app, ignore_errors=True)

    def _assert_struct(self, struct_name, sample):
        """Every Swift-declared key of struct_name must be present in `sample`
        or be a documented Optional Swift property."""
        model = self.models[struct_name]
        self.assertIsInstance(sample, dict,
                              "%s sample is not an object" % struct_name)
        missing = []
        for prop, json_key in model["keys"].items():
            if json_key in sample:
                continue
            if prop in model["optional"]:
                continue
            missing.append("%s (json '%s')" % (prop, json_key))
        self.assertEqual(
            missing, [],
            "%s: required key(s) absent from emitted JSON and not Optional in "
            "Swift: %s\nemitted keys: %s"
            % (struct_name, missing, sorted(sample.keys())))

    # -- config-get -------------------------------------------------------

    def test_config_struct(self):
        self._assert_struct("Config", self.config)

    def test_source_struct(self):
        sources = self.config.get("sources")
        self.assertTrue(sources, "config-get emitted no sources")
        for src in sources:
            self._assert_struct("Source", src)
        # sanity: both a directory and a file source are represented.
        self.assertEqual(sorted(s["is_file"] for s in sources), [False, True])

    # -- status -----------------------------------------------------------

    def test_status_struct(self):
        self._assert_struct("Status", self.status)

    def test_progress_struct(self):
        prog = self.status.get("progress")
        self.assertIsNotNone(prog, "status emitted no progress block")
        self._assert_struct("Progress", prog)
        self.assertTrue(prog["active"])
        self.assertEqual(prog["total_files"], 10)

    def test_folder_status_struct(self):
        folders = self.status.get("folders")
        self.assertTrue(folders, "status emitted no folders")
        for entry in folders.values():
            self._assert_struct("FolderStatus", entry)
        # held_deletions decodes as a [String] list (Swift heldDeletions).
        self.assertEqual(folders["src1"]["held_deletions"],
                         ["gone1.txt", "gone2.txt"])

    def test_derived_struct(self):
        derived = self.status.get("derived")
        self.assertIsNotNone(derived, "status --json emitted no derived block")
        self._assert_struct("Derived", derived)
        self.assertEqual(derived["total_held_deletions"], 2)

    # -- doctor -----------------------------------------------------------

    def test_doctor_struct(self):
        self.assertIsInstance(self.doctor, list)
        self.assertTrue(self.doctor, "doctor emitted no rows")
        for row in self.doctor:
            self._assert_struct("DoctorRow", row)

    # -- mount (in-process; never touches a real NAS) ---------------------

    def test_mount_result_struct(self):
        cfg_path = os.path.join(self.app, "config.ini")
        out = io.StringIO()

        class _StubMount(object):
            def __init__(self, mnt):
                self._mnt = mnt

            def is_mounted(self):
                return True

        mnt = os.path.join(self.app, "mnt")
        ctl = mycloudctl.Ctl(
            runtime_dir=os.path.join(self.app, "runtime"),
            config_path=cfg_path,
            mnt=mnt,
            out=out,
            mount_factory=lambda cfg: _StubMount(mnt),
        )
        rc = ctl.cmd_mount()
        self.assertEqual(rc, 0)
        result = json.loads(out.getvalue())
        self._assert_struct("MountResult", result)
        self.assertTrue(result["mounted"])
        self.assertEqual(result["mountpoint"], mnt)

    # -- error envelope (CtlError) ----------------------------------------

    def test_ctl_error_envelope(self):
        # config-set with a bad key must emit {"error": ...} (CtlError shape).
        result = _run_ctl(self.app, ["config-set", "not_a_key", "1"])
        self.assertIn("error", result)
        # CtlError declares only `error`; ensure that key is covered.
        self._assert_struct("CtlError", result)
