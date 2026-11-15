"""Smoke safety contracts: no Core, poolab, or network commands are executed here."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import smoke

ROOT = Path(__file__).resolve().parents[1]


class SmokeContractTests(unittest.TestCase):
    def test_isolation_whitelist_cannot_select_the_default_core(self):
        environment = smoke.isolation_env("/var/tmp/pa-example12", "/user", "/usr/bin")
        self.assertEqual(set(environment), {
            "HOME", "PATH", "HC_SESSION", "HC_SOCKET", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
            "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
        })
        self.assertEqual(environment["HC_SESSION"], "pa-example12")
        self.assertEqual(environment["HC_SOCKET"], "/var/tmp/pa-example12/r/c.sock")
        for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
            self.assertTrue(environment[key].startswith("/var/tmp/pa-example12/"))
        prefix = shlex.split(smoke.remote_prefix("/var/tmp/pa-example12"))
        self.assertEqual(prefix[:2], ["env", "-i"])
        self.assertIn("HC_SESSION=pa-example12", prefix)

    def test_marker_cannot_be_satisfied_by_command_echo(self):
        marker = "PA_LITERAL_RESULT_123"
        command = smoke.marker_command(marker)
        self.assertNotIn(marker, command)
        result = subprocess.run(["/bin/sh", "-c", command], capture_output=True, check=True)
        self.assertEqual(result.stdout, marker.encode() + b"\n")

    def test_wrapper_preserves_the_original_remote_line_as_an_argument(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            adapter = directory / "fake adapter"
            adapter.write_text(
                f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n"
            )
            adapter.chmod(0o700)
            wrapper = directory / "via"
            wrapper.write_text(smoke.wrapper_source(str(adapter), "fixture", "/var/tmp/pa-example12"))
            command = "printf 'two words'; printf '$HOME'"
            result = subprocess.run(
                [sys.executable, str(wrapper), command], capture_output=True, check=True, timeout=5,
            )
            arguments = json.loads(result.stdout)
            self.assertEqual(arguments[:3], ["--alias", "fixture", "--"])
            tokens = shlex.split(arguments[3])
            self.assertEqual(tokens[-3:], ["sh", "-c", command])

    def test_scratch_deletion_refuses_unowned_or_symlinked_paths(self):
        with tempfile.TemporaryDirectory(prefix="pa-", dir="/tmp") as temporary:
            root = Path(temporary)
            (root / "owner").write_text("correct")
            with self.assertRaises(ValueError):
                smoke.guarded_local_remove(root, "wrong")
            self.assertTrue(root.exists())
            (root / "owner").write_text("correct")
            root.chmod(0o755)
            with self.assertRaises(ValueError):
                smoke.guarded_local_remove(root, "correct")
            root.chmod(0o700)
            link = root.with_name(root.name + "link")
            link.symlink_to(root, target_is_directory=True)
            try:
                with self.assertRaises(ValueError):
                    smoke.guarded_local_remove(link, "correct")
                self.assertTrue(root.exists())
            finally:
                link.unlink()
        for root in ("/", "/var/tmp", "/var/tmp/foreign", "/var/tmp/pa-x/../foreign"):
            with self.subTest(root=root), self.assertRaises(ValueError):
                smoke.validate_remote_root(root)

    def test_help_and_public_report_rejection_need_no_credentials(self):
        result = subprocess.run(
            [sys.executable, "scripts/smoke.py", "--help"], cwd=ROOT, capture_output=True,
            check=False, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            result = subprocess.run(
                [sys.executable, "scripts/smoke.py", "--output", str(path)], cwd=ROOT,
                env={**os.environ, "PATH": ""}, capture_output=True, check=False, timeout=5,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
