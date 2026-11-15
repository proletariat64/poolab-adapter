"""Public-boundary regressions run in disposable repositories without network access."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "scripts" / "doc_guard.py"


class DocGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "scripts").mkdir()
        shutil.copyfile(GUARD, self.root / "scripts" / "doc_guard.py")
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        (self.root / ".gitignore").write_text(".local/\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Example\n", encoding="utf-8")

    def run_guard(self):
        return subprocess.run(
            [sys.executable, str(self.root / "scripts" / "doc_guard.py")],
            capture_output=True, text=True, check=False, timeout=5,
        )

    def test_tracked_ignored_artifact_cannot_bypass_publication_guard(self):
        artifact = self.root / ".local" / "evidence.txt"
        artifact.parent.mkdir()
        artifact.write_text("private runtime evidence\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "-f", ".local/evidence.txt"], check=True)
        result = self.run_guard()
        self.assertNotEqual(result.returncode, 0, "tracked private files would still be published")
        self.assertIn("tracked-ignored", result.stdout + result.stderr)

    def test_secret_diagnostic_does_not_echo_secret(self):
        secret = "ghp_" + "x" * 40
        (self.root / "leak.txt").write_text(secret + "\n", encoding="utf-8")
        result = self.run_guard()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("github-token", result.stdout)
        self.assertNotIn(secret, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
