"""Exercise delivery subprocesses without credentials, network, or a complete adapter."""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = '''import argparse
import subprocess
import sys
from . import __version__

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--owner", action="store_true")
    parser.add_argument("--spawn-owner", action="store_true")
    args = parser.parse_args()
    if args.spawn_owner:
        return subprocess.run([sys.executable, sys.argv[0], "--owner"]).returncode
    return 42 if args.owner else 0
'''


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="poolab-delivery-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "LICENSE").write_text((ROOT / "LICENSE").read_text())
        scripts = self.root / "scripts"
        scripts.mkdir()
        for filename in ("package.py", "release.py"):
            shutil.copyfile(ROOT / "scripts" / filename, scripts / filename)
        self.package = self.root / "poolab_adapter"
        self.package.mkdir()
        (self.package / "__init__.py").write_text('__version__ = "0.1.0"\n')
        (self.package / "cli.py").write_text(CLI)
        self.artifact = self.root / "dist" / "herdr-poolab"

    def execute(self, script, *args):
        # An empty PATH proves dry-run cannot depend on git, gh, make, or poolab.
        environment = {**os.environ, "PATH": "", "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run(
            [sys.executable, str(self.root / "scripts" / script), *map(str, args)],
            cwd=self.root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

    def launch(self, *args):
        return subprocess.run(
            [sys.executable, str(self.artifact), *args],
            cwd=self.root,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

    def test_archive_contains_executable_sources_and_required_license(self):
        (self.package / "secret.json").write_text('{"token": "not-for-distribution"}')
        for directory, filename in (("__pycache__", "old.py"), (".private", "secret.py")):
            target = self.package / directory
            target.mkdir()
            (target / filename).write_text("raise RuntimeError('private')")
        nested = self.package / "transport"
        nested.mkdir()
        (nested / "__init__.py").write_text("")
        (nested / "bridge.py").write_text("VALUE = 1\n")
        result = self.execute("package.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.artifact))
        self.assertTrue(self.artifact.read_bytes().startswith(b"#!/usr/bin/env python3\n"))
        self.assertEqual(stat.S_IMODE(self.artifact.stat().st_mode), 0o755)
        with zipfile.ZipFile(self.artifact) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"LICENSE", "__main__.py", "poolab_adapter/", "poolab_adapter/__init__.py",
                 "poolab_adapter/cli.py", "poolab_adapter/transport/",
                 "poolab_adapter/transport/__init__.py", "poolab_adapter/transport/bridge.py"},
            )
        self.assertEqual(self.launch("--version").stdout.strip(), "0.1.0")
        self.assertEqual(self.launch("--help").returncode, 0)
        self.assertEqual(self.launch("--spawn-owner").returncode, 42)
        self.assertEqual(self.launch("--unknown-option").returncode, 2)

    def test_custom_output_with_spaces(self):
        output = self.root / "output directory" / "custom adapter"
        result = self.execute("package.py", "--output", output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(output))
        self.assertTrue(output.is_file())
        self.assertFalse(self.artifact.exists())

    def test_build_does_not_import_unfinished_cli(self):
        (self.package / "cli.py").write_text("raise RuntimeError('unfinished CLI')\n")
        result = self.execute("package.py")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_build_rejects_symlinked_python(self):
        external = self.root / "private.py"
        external.write_text("SECRET = 'private'\n")
        (self.package / "unsafe.py").symlink_to(external)
        result = self.execute("package.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinked package source", result.stderr)
        self.assertFalse(self.artifact.exists())

    def test_build_rejects_source_overwrite(self):
        result = self.execute("package.py", "--output", self.package / "cli.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.package / "cli.py").read_text(), CLI)

    def test_dry_run_is_offline_without_git_or_provider_tools(self):
        # This fixture is deliberately not a Git checkout; dry-run also works on feature trees.
        result = self.execute("release.py", "--version", "v0.1.0", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing published", result.stdout)
        self.assertEqual(self.launch("--version").stdout.strip(), "0.1.0")

    def test_dry_run_requires_package_version_match(self):
        for version in ("v0.2.0", "0.1.0", "v0.1.0;false"):
            with self.subTest(version=version):
                result = self.execute("release.py", "--version", version, "--dry-run")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must match package version", result.stderr)
        self.assertFalse(self.artifact.exists())

    def test_dry_run_checks_built_executable_version(self):
        (self.package / "cli.py").write_text(CLI.replace("version=__version__", 'version="9.9.9"'))
        result = self.execute("release.py", "--version", "v0.1.0", "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not report package version", result.stderr)

    def test_dry_run_reports_broken_cli(self):
        (self.package / "cli.py").write_text("raise RuntimeError('unfinished CLI')\n")
        result = self.execute("release.py", "--version", "v0.1.0", "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("command failed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_literal_version_is_required_without_evaluation(self):
        (self.package / "__init__.py").write_text('__version__ = str("0.1.0")\n')
        result = self.execute("package.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.artifact.exists())


if __name__ == "__main__":
    unittest.main()
