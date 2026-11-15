"""Build the dependency-free executable adapter from production Python sources."""

import argparse
import ast
import re
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def package_version(root: Path = ROOT) -> str:
    """Read the literal version without importing the adapter or its CLI."""
    tree = ast.parse((root / "poolab_adapter" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            version = ast.literal_eval(node.value)
            if isinstance(version, str) and re.fullmatch(
                r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version
            ):
                return version
    raise ValueError("poolab_adapter.__version__ must be a literal version string")


def build(output: Path, root: Path = ROOT) -> Path:
    package_version(root)
    source = root / "poolab_adapter"
    if not (source / "cli.py").is_file():
        raise ValueError("poolab_adapter/cli.py is missing")
    output = output.resolve()
    if output == source.resolve() or source.resolve() in output.parents:
        raise ValueError("output must not overwrite package sources")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="herdr-poolab-build-") as temporary:
        staging = Path(temporary) / "app"
        staging.mkdir()
        license_file = root / "LICENSE"
        if not license_file.is_file():
            raise ValueError("LICENSE is required in distributed copies")
        shutil.copyfile(license_file, staging / "LICENSE")
        for path in sorted(source.rglob("*.py")):
            relative = path.relative_to(root)
            if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                continue
            if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root):
                raise ValueError(f"symlinked package source is not allowed: {relative}")
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        (staging / "__main__.py").write_text(
            "from poolab_adapter.cli import main\nraise SystemExit(main())\n", encoding="utf-8"
        )
        # Stage beside the destination, then replace atomically on the same filesystem.
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as handle:
            archive = Path(handle.name)
        try:
            zipapp.create_archive(staging, archive, interpreter="/usr/bin/env python3")
            archive.chmod(0o755)
            archive.replace(output)
        finally:
            archive.unlink(missing_ok=True)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "herdr-poolab")
    args = parser.parse_args()
    try:
        print(build(args.output))
    except (OSError, ValueError, SyntaxError) as error:
        print(f"package: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
