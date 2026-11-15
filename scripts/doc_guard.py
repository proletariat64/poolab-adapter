"""Check public source hygiene and local Markdown links without network access."""

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
EXCLUDED = {".git", ".venv", ".worktrees", "__pycache__", ".ruff_cache"}
ROOT_DOCS = {"README", "AGENTS", "GLOSSARY", "CHANGELOG", "CONTRIBUTING", "SECURITY"}
PRIVATE = [
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----")),
    (
        "github-token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
    ),
    ("personal-home-path", re.compile(r"/home/[A-Za-z0-9_.-]+/")),
    ("intranet-host", re.compile(r"\b[A-Za-z0-9.-]+\.inc\.[A-Za-z0-9.-]+\b")),
]


def public_files() -> dict[str, Path]:
    tracked = subprocess.check_output(["git", "ls-files", "-z", "--cached"], cwd=ROOT)
    if tracked:
        tracked_ignored = subprocess.run(
            ["git", "check-ignore", "--no-index", "-z", "--stdin"],
            cwd=ROOT, input=tracked, capture_output=True, check=False,
        )
        if tracked_ignored.returncode not in (0, 1):
            raise RuntimeError("Cannot check tracked publication paths")
        if tracked_ignored.stdout:
            paths = [name for name in tracked_ignored.stdout.decode().split("\0") if name]
            raise RuntimeError("tracked-ignored publication paths: " + ", ".join(paths))
    names = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    candidates = sorted(
        {name for name in names if name and not EXCLUDED.intersection(Path(name).parts)}
    )
    # Ignore only untracked private artifacts; tracked ignored files fail above.
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "-z", "--stdin"],
        cwd=ROOT,
        input="\0".join(candidates).encode() + b"\0",
        capture_output=True,
        check=False,
    )
    if ignored.returncode not in (0, 1):
        raise RuntimeError("Cannot determine ignored publication paths")
    excluded = set(ignored.stdout.decode().split("\0"))
    return {name: ROOT / name for name in candidates if name not in excluded}


def markdown_rules(name: str, text: str, available: set[Path]) -> set[str]:
    failures = set()
    path = Path(name)
    if not (
        (len(path.parts) == 1 and path.stem in ROOT_DOCS) or path.parts[0] in {"docs", ".github"}
    ):
        failures.add("markdown-placement")
    fence = None
    body = []
    headings = []
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if match:
            marker = match[1]
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not match[2].strip():
                fence = None
            continue
        if fence:
            continue
        body.append(line)
        if re.match(r"^#{1,6}[^#\s]", line):
            failures.add("heading-spacing")
        heading = re.match(r"^(#{1,6})\s+\S", line)
        if heading:
            level = len(heading[1])
            if headings and level > headings[-1] + 1:
                failures.add("heading-level-jump")
            headings.append(level)
    if fence:
        failures.add("unclosed-code-fence")
    if not headings:
        failures.add("missing-heading")
    prose = "\n".join(body)
    links = re.findall(r"!?\[[^\]]*\]\(\s*(<[^>]*>|[^\s)]+)", prose)
    links += re.findall(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)", prose, re.MULTILINE)
    for link in links:
        parsed = urlsplit(link.strip("<>"))
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        target = (ROOT / name).parent / unquote(parsed.path)
        resolved = target.resolve()
        if not resolved.is_relative_to(ROOT) or resolved not in available:
            failures.add("relative-link-not-public-file")
    return failures


def main() -> int:
    files = public_files()
    available = {path.resolve() for path in files.values() if path.is_file()}
    findings = []
    for name, path in files.items():
        if not path.exists():
            findings.append((name, "missing-public-file"))
            continue
        if path.is_symlink():
            findings.append((name, "publication-symlink"))
            continue
        raw = path.read_bytes()
        # Binary assets are not text documents; still scan their printable bytes.
        failures = {
            rule
            for rule, pattern in PRIVATE
            if pattern.search(raw.decode("utf-8", errors="replace"))
        }
        if b"\0" not in raw:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                failures.add("invalid-utf8")
            else:
                if text.startswith("\ufeff"):
                    failures.add("utf8-bom")
                if text and not text.endswith("\n"):
                    failures.add("missing-final-newline")
                if any(line.rstrip(" \t") != line for line in text.splitlines()):
                    failures.add("trailing-whitespace")
                if path.suffix.lower() == ".md":
                    failures |= markdown_rules(name, text, available)
        findings.extend((name, rule) for rule in sorted(failures))
    for name, rule in findings:
        print(f"{name}: {rule}")
    if findings:
        print(f"doc-guard: {len(findings)} violation(s)", file=sys.stderr)
        return 1
    print(f"doc-guard: {len(files)} public files passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"doc-guard: {error}", file=sys.stderr)
        sys.exit(1)
