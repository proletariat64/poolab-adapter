"""Run offline syntax, lint, and regression checks."""

import ast
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXCLUDED = {".git", ".venv", ".worktrees", "__pycache__"}


def main() -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required")
    paths = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
        )
        .decode()
        .split("\0")
    )
    for name in sorted(set(paths)):
        path = ROOT / name
        if path.suffix == ".py" and not EXCLUDED.intersection(Path(name).parts):
            source = path.read_text(encoding="utf-8")
            ast.parse(source, filename=name)
    local_ruff = ROOT / ".venv" / "bin" / "ruff"
    ruff = str(local_ruff) if local_ruff.is_file() else shutil.which("ruff")
    if not ruff:
        raise RuntimeError("Ruff is missing; run make bootstrap first")
    version = subprocess.check_output([ruff, "--version"], text=True).strip()
    if version != "ruff 0.16.1":
        raise RuntimeError("Ruff 0.16.1 is required; run make bootstrap")
    subprocess.run([ruff, "check", "."], cwd=ROOT, check=True)
    subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT,
        check=True,
    )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, SyntaxError, subprocess.CalledProcessError) as error:
        print(f"check: {error}", file=sys.stderr)
        sys.exit(1)
