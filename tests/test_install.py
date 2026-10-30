import tests._path  # noqa: F401

"""Guardrails for install/install.sh (parsed as text; never executed).

Since 2026-09-25 install.sh is only the interactive front end of `mycloudctl
install` (the code the GUI installer runs, covered by test_ctl). These tests pin
that shape: the password reaches mycloudctl on stdin only and is read with IFS=
(no trimming); the script no longer re-implements mounting, Keychain writes or
sentinel handling (its drifted copies were the bugs); adopting another machine's
backup needs a typed confirmation; the staging dir is cleaned on every exit.
Running install.sh itself is out of scope (and forbidden).
"""

import os
import re
import subprocess
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_INSTALL = os.path.abspath(os.path.join(_HERE, "..", "install", "install.sh"))


class InstallShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_INSTALL, "r", encoding="utf-8") as handle:
            cls.text = handle.read()
        cls.code = "\n".join(line for line in cls.text.splitlines()
                             if not line.lstrip().startswith("#"))

    def test_bash_syntax(self):
        subprocess.run(["/bin/bash", "-n", _INSTALL], check=True)

    def test_trap_cleans_the_stage_on_exit_int_term(self):
        self.assertIn("trap cleanup EXIT", self.code)
        self.assertRegex(self.code, r"trap 'exit 130' INT TERM")

    def test_delegates_to_mycloudctl_install(self):
        self.assertIn('mycloudctl.py" install', self.code)
        self.assertIn("--from \"$STAGE\"", self.code)

    def test_password_only_travels_on_stdin(self):
        self.assertIn("--password-stdin", self.code)
        self.assertRegex(self.code, r"printf '%s\\n' \"\$SMB_PASS\" \| /usr/bin/python3")
        # never as an argument of any command
        for line in self.code.splitlines():
            if "$SMB_PASS" in line:
                self.assertTrue(line.strip().startswith(("printf '%s\\n' \"$SMB_PASS\" |",
                                                         "IFS= read", "[ -n \"$SMB_PASS\" ]",
                                                         "SMB_PASS=")),
                                line)

    def test_password_read_keeps_surrounding_spaces(self):
        self.assertIn("IFS= read -r -s SMB_PASS", self.code)

    def test_no_reimplemented_mount_keychain_or_sentinel_logic(self):
        for needle in ("mount_smbfs", "add-internet-password", ".mycloud_sentinel",
                       "launchctl bootstrap", "nsmb.conf"):
            self.assertNotIn(needle, self.code, needle)

    def test_adoption_needs_a_typed_confirmation(self):
        self.assertIn('json_get needs_confirm)" = "adopt"', self.code)
        self.assertIn('[ "$ADOPT" = "adopt" ]', self.code)
        self.assertIn("run_install --adopt", self.code)

    def test_cli_link_never_overwrites_a_foreign_file(self):
        self.assertNotRegex(self.code, r"ln -sf \"\$SHIM\" \"\$LINK\"\s*$\n(?!.*readlink)")
        self.assertIn("is not MyCloud's; left untouched", self.code)


if __name__ == "__main__":
    unittest.main()
