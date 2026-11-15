"""Create or reuse an owned development worktree, then prepare its environment."""

import argparse
import shlex
import subprocess
import sys
from pathlib import Path


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def main() -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", default="feat/standalone-via")
    parser.add_argument("--worktree", default=".worktrees/standalone-via")
    args = parser.parse_args()
    root = Path(git(Path(__file__).resolve().parent, "rev-parse", "--show-toplevel"))
    try:
        git(root, "rev-parse", "--verify", "HEAD")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Create the baseline commit before running bootstrap") from exc
    git(root, "check-ref-format", "--branch", args.branch)
    records = []
    for block in git(root, "worktree", "list", "--porcelain").split("\n\n"):
        records.append(dict(line.split(" ", 1) for line in block.splitlines() if " " in line))
    main_root = Path(records[0]["worktree"])
    requested = Path(args.worktree).expanduser()
    target = (requested if requested.is_absolute() else main_root / requested).resolve()
    current_branch = git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if current_branch == args.branch:
        target = root
    else:
        matching = [item for item in records if Path(item["worktree"]).resolve() == target]
        if matching:
            if matching[0].get("branch") != f"refs/heads/{args.branch}":
                raise RuntimeError("Requested worktree path belongs to another branch")
            if not target.is_dir():
                raise RuntimeError("Registered worktree is missing; repair it manually")
            common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
            if git(target, "rev-parse", "--path-format=absolute", "--git-common-dir") != common:
                raise RuntimeError("Existing worktree belongs to another repository")
            if git(target, "symbolic-ref", "--quiet", "--short", "HEAD") != args.branch:
                raise RuntimeError("Existing worktree branch does not match")
        else:
            if target.exists() or target.is_symlink():
                raise RuntimeError("Requested path already exists; no files were changed")
            if any(item.get("branch") == f"refs/heads/{args.branch}" for item in records):
                raise RuntimeError("Requested branch is checked out at another path")
            exists = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{args.branch}",
                ],
                check=False,
            ).returncode
            if exists not in (0, 1):
                raise RuntimeError("Cannot determine whether requested branch exists")
            command = ["git", "-C", str(root), "worktree", "add"]
            if exists:
                command += ["-b", args.branch]
            subprocess.run(
                [*command, str(target), args.branch if not exists else "HEAD"], check=True
            )
    environment = target / ".venv"
    python = environment / "bin" / "python"
    if environment.is_symlink():
        raise RuntimeError("Refusing to use a symlink as the worktree virtual environment")
    if environment.exists():
        if not (environment / "pyvenv.cfg").is_file() or not python.is_file():
            raise RuntimeError("Existing .venv is not a usable virtual environment")
    else:
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    subprocess.run(
        [str(python), "-m", "pip", "install", "-r", str(target / "requirements-dev.txt")],
        check=True,
    )
    print(f"cd {shlex.quote(str(target))}")
    print(". .venv/bin/activate")
    print("make check doc-guard")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"bootstrap: {error}", file=sys.stderr)
        sys.exit(1)
